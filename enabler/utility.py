# /// script
# requires-python = ">=3.11"
# dependencies = ["pyusb==1.3.1", "libusb1==3.3.1", "libusb-package==1.0.30.1; sys_platform == 'win32'"]
# ///
"""Guided native USB Fixed enablement for Fujifilm X-T30 III firmware 1.10."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import sys
import threading
from urllib.parse import urlparse
import uuid
import webbrowser

import enable_flow as flow
import usb_devices as camera
import ptp_raw_snapshot as protocol
import usb_host
from local_state import atomic_json, now
from process_lock import exclusive_lock

STATIC = Path(__file__).resolve().parent/'web'
ACTIONS = {
    'disconnected': ['check'], 'normal': ['check'], 'service': ['enable'],
    'menu': ['check'], 'select_fixed': ['check'], 'enabled': ['restart'],
    'verified': ['check'], 'checking': [], 'preparing': [],
    'error': ['check'], 'restart_required': ['restarted'], 'setup_restart': ['check'], 'firmware': ['check'],
}


def inspect_camera(root):
    folder = Path(root)/'checks'; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder/f'{uuid.uuid4().hex}.json'
    report = dict(kind='enabler_read_only_check_v1', started=now(), complete=False, session='not_opened')
    def save(): atomic_json(path, report)
    save()
    def snapshot(ptp, row, checkpoint):
        try: protocol.snapshot(ptp, row, checkpoint)
        except BaseException:
            if not ptp.healthy: row['session'] = 'uncertain_after_transport_error'
            checkpoint(); raise
    try:
        protocol.connect_and_snapshot(report, save, snapshot_fn=snapshot)
    except BaseException as exc:
        report.update(error=f'{type(exc).__name__}: {exc}', error_type=type(exc).__name__, complete=False)
    finally:
        report['finished'] = now(); save()
    return path, report


class Enabler:
    def __init__(self, data, demo=False):
        self.root = Path(data).resolve(); self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'workflow.json'
        self.workflow = json.loads(self.path.read_text()) if self.path.exists() and not demo else {}
        self.demo = demo
        self.lock = threading.RLock()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.fingerprint = None
        self.device = None
        self.token = secrets.token_urlsafe(32)
        self.state = dict(stage='disconnected', title='Connect your camera',
                          message='Connect your X-T30 III and switch it on normally. Close other camera apps first.',
                          detail='', actions=['check'], demo=demo, updated=now())
        if not demo:
            # A killed process must not silently reuse a possibly incomplete USB stream.
            for path in sorted((self.root/'checks').glob('*.json')):
                row = json.loads(path.read_text())
                if row.get('restart_checked_by'): continue
                if row.get('session') in ('opening', 'owned', 'closing', 'uncertain_after_transport_error', 'close_unconfirmed'):
                    self.workflow.update(fault='usb', failed_node=self.workflow.get('last_node'))
            if self.workflow.get('preparing'):
                self.workflow.update(fault='setup', preparing=False)
            self.save()

    def save(self):
        if not self.demo: atomic_json(self.path, self.workflow)

    def status(self, stage, title, message, detail=''):
        with self.lock:
            self.state.update(stage=stage, title=title, message=message, detail=detail,
                              actions=ACTIONS[stage], updated=now())

    def snapshot(self):
        with self.lock: return {**self.state, 'token': self.token}

    def action(self, action):
        if self.demo: raise ValueError('Demo does not access a camera')
        with self.lock:
            if action not in self.state['actions']: raise ValueError('That action is not available now')
            self.state['actions'] = []
        self.commands.put(action)

    def attached_devices(self):
        devices = camera.devices()
        if sys.platform != 'win32': return devices
        # Windows can reuse a port's address across a power cycle. Persist an
        # attachment token, changing it only on observed absence/re-enumeration,
        # never just because the utility restarted. Service child processes use
        # the same token, so recovery journals still fence an unchanged session.
        previous = self.workflow.get('windows_attachment', {})
        if not devices:
            if previous.get('present'):
                previous['present'] = False; self.save()
            return devices
        if len(devices) != 1: return devices
        device = devices[0]
        fingerprint = [device['product'], device['bus'], device['address']]
        if not previous.get('present') or previous.get('fingerprint') != fingerprint:
            previous = dict(present=True, fingerprint=fingerprint, token=uuid.uuid4().hex)
            self.workflow['windows_attachment'] = previous; self.save()
        return [{**device, 'node': device['node'] + '#' + previous['token']}]

    def start(self):
        if self.demo:
            self.status('enabled', 'USB Fixed is enabled',
                        'Preview only. This is where you will verify a normal restart. No camera is accessed.')
            with self.lock: self.state['actions'] = []
            return
        self.worker = threading.Thread(target=self.work, daemon=True, name='tether-enabler')
        self.worker.start()

    def menu_instructions(self):
        self.status('menu', 'Select USB Fixed on the camera',
            'Press and release PLAY. When live view appears, unplug USB and keep the camera powered on. '
            'Open NETWORK/USB SETTING → CONNECTION MODE, select USB FIXED, exit the menu, then reconnect USB. '
            'Leave the camera on until this utility confirms the setting.')

    def work(self):
        while not self.stop.is_set():
            try: action = self.commands.get(timeout=1)
            except queue.Empty: action = None
            try:
                if action == 'enable': self.enable()
                else: self.detect(action)
            except usb_host.USBHostError as exc:
                self.status('error', 'Check USB access', str(exc))
            except Exception as exc:
                self.status('error', 'Check the connection', 'The utility stopped this check. See the details below.', str(exc))

    def enable(self):
        if flow.missing_references():
            self.firmware_instructions(); return
        # Re-enumerate immediately before mutation; never trust a stale UI device.
        devices = self.attached_devices()
        if len(devices) != 1 or devices[0]['product'] != 0xFF80:
            raise RuntimeError('Restart in service mode before enabling the menu')
        device = devices[0]
        if self.workflow.get('fault') == 'setup' and device['node'] == self.workflow.get('failed_node'):
            raise RuntimeError('The previous setup needs a physical restart before another attempt')
        if flow.armed_menu(device):
            self.menu_instructions(); return
        self.workflow.update(preparing=True, failed_node=device['node']); self.save()
        try:
            token = self.workflow.get('windows_attachment', {}).get('token') if sys.platform == 'win32' else None
            arm = flow.prepare(self.root, self.status, attachment_token=token)
        except Exception as exc:
            self.workflow.update(preparing=False, fault='setup'); self.save()
            self.status('setup_restart', 'Restart before trying setup again',
                'Unplug USB for at least five seconds and switch off. Reconnect USB, then switch on '
                'holding Shutter + joystick Up to enter service mode. '
                'If shutdown hangs, unplug USB and remove/reinsert the battery first. '
                'If USB Card Reader was not selected, start normally and select it before entering service mode. '
                'The next attempt verifies the original state before making changes.', str(exc))
            return
        self.workflow.update(preparing=False, fault=None, arm=str(arm), phase='menu_armed')
        self.save(); self.menu_instructions()

    def firmware_instructions(self):
        command = ('Drag FWUP0037.DAT onto enabler/Import Firmware.cmd. ' if sys.platform == 'win32' else
                   'From the project folder, run: uv run enabler/import_firmware.py /path/to/FWUP0037.DAT ')
        self.status('firmware', 'Import your firmware references first',
            'Menu setup needs reference images from your unmodified X-T30 III 1.10 FWUP0037.DAT. '
            + command + 'Then click Check connection. Importing files does not access the camera.')

    def detect(self, action=None):
        devices = self.attached_devices()
        fingerprint = tuple((d['product'], d['bus'], d['address'], d['node']) for d in devices)
        if fingerprint == self.fingerprint and action is None: return
        self.fingerprint = fingerprint
        if not devices:
            self.device = None
            if self.workflow.get('phase') == 'menu_armed': self.menu_instructions()
            else: self.status('disconnected', 'Connect your camera',
                             'Connect USB and switch on normally. Close the Fuji app and other camera apps first.')
            return
        if len(devices) != 1: raise RuntimeError('Connect only one Fujifilm camera')
        self.device = device = devices[0]
        if device['product'] == 0xFF80:
            if flow.armed_menu(device) and not self.workflow.get('fault'):
                self.workflow['phase'] = 'menu_armed'; self.save(); self.menu_instructions()
            elif self.workflow.get('fault') == 'setup' and device['node'] == self.workflow.get('failed_node'):
                self.status('setup_restart', 'Restart the camera in service mode',
                            'Unplug USB for at least five seconds and switch off. Reconnect USB, then switch on '
                            'holding Shutter + joystick Up. Release both after startup.')
            else:
                if flow.missing_references():
                    self.firmware_instructions(); return
                self.status('service', 'Ready to expose USB tethering',
                    'Release the shutter and joystick. USB CARD READER must have been selected before startup. '
                    'Enable menu checks the camera, tests the temporary patch, and exposes USB AUTO and USB FIXED. '
                    'Keep USB and power connected during setup.')
            return
        if self.workflow.get('fault') == 'setup':
            self.status('setup_restart', 'Verify the interrupted setup first',
                        'Unplug USB for at least five seconds and switch off. Reconnect USB, then switch on '
                        'holding Shutter + joystick Up to enter service mode. '
                        'Release both after startup. The previous setup journal must be checked before proceeding.')
            return
        if self.workflow.get('fault') == 'usb':
            if action != 'restarted' or device['node'] == self.workflow.get('failed_node'):
                self.status('restart_required', 'Restart the camera normally',
                    'Unplug USB for at least five seconds, switch off and back on with no buttons held, then reconnect. '
                    'If shutdown hangs, remove/reinsert the battery while USB is unplugged. Click Restarted afterward.')
                return
        if action == 'restart' and device['node'] == self.workflow.get('enabled_node'):
            self.status('enabled', 'Restart to verify the saved setting',
                'Leave USB Fixed selected. Unplug USB for at least five seconds, switch off and back on normally with no buttons held, '
                'then reconnect without opening the connection-mode menu. Click Verify restart afterward.')
            return
        self.workflow['last_node'] = device['node']; self.save()
        self.status('checking', 'Checking USB tethering', 'Reading camera identity and mode. No settings are changed.')
        path, report = inspect_camera(self.root)
        self.workflow['last_check'] = str(path); self.save()
        if not report.get('complete') or report.get('session') != 'closed' or report.get('error'):
            if report.get('session') not in ('not_opened', 'not_acquired', 'closed', None):
                self.workflow.update(fault='usb', failed_node=device['node']); self.save()
                self.status('restart_required', 'Restart the camera normally',
                            'The USB reply was incomplete. Unplug USB, switch off/on normally, reconnect, and click Restarted.',
                            report.get('error', 'USB session did not close cleanly'))
            elif (report.get('error_type') == 'USBHostError'
                  or report.get('error', '').startswith('USBHostError:')):
                self.status('error', 'Check USB access on this computer',
                            report['error'].removeprefix('USBHostError: ').strip(),
                            f"Connection report: {path}")
            else:
                self.status('error', 'Camera check did not finish',
                            'The camera check stopped. The error below explains which check failed.',
                            report.get('error', 'Camera session was unavailable'))
            return
        if self.workflow.get('fault') == 'usb':
            for old in (self.root/'checks').glob('*.json'):
                row = json.loads(old.read_text())
                if row.get('session') not in ('closed', 'not_opened', 'not_acquired', None):
                    row['restart_checked_by'] = str(path); atomic_json(old, row)
            self.workflow['fault'] = None; self.save()
        props = report.get('properties', {})
        mode = props.get('0xd16e', {}).get('value', {}).get('decoded')
        selection = props.get('0xd041', {}).get('value', {}).get('decoded')
        fixed = mode == 5 and selection == 3 and report.get('observations', {}).get('advertises_initiate_capture')
        if fixed:
            if action == 'restart' and self.workflow.get('enabled_node') and device['node'] != self.workflow['enabled_node']:
                self.workflow.update(phase='restart_verified', restart_check=str(path), restart_confirmed_at=now())
                self.save()
            if self.workflow.get('phase') == 'restart_verified':
                self.status('verified', 'Tethering survived the restart',
                    'USB Fixed is active after your confirmed normal restart. Close this utility and open '
                    'the Fuji app or Tether Companion. Keep USB Fixed selected; the menu patch itself is temporary.')
            else:
                self.workflow.setdefault('enabled_node', device['node'])
                self.workflow['phase'] = 'enabled'; self.save()
                self.status('enabled', 'USB Fixed is already enabled',
                    'No setup is needed to use tethering now. Close this utility and open the Fuji app or Tether Companion. '
                    'To verify persistence: unplug USB for at least five seconds, switch off/on normally with no buttons held, reconnect, '
                    'then click Verify restart without changing the connection-mode menu.')
        elif self.workflow.get('phase') == 'menu_armed' or mode == 5:
            self.status('select_fixed', 'Choose USB Fixed',
                'Tethering is not reporting USB Fixed yet. Unplug USB without switching off, select USB FIXED '
                'in the connection-mode menu, then reconnect. If the option is absent, select USB CARD READER '
                'and restart in service mode to expose it.')
        else:
            self.workflow.pop('enabled_node', None)
            self.workflow['phase'] = 'needs_setup'; self.save()
            self.status('normal', 'One-time menu setup',
                'On the camera select USB CARD READER and AUTO POWER OFF → OFF. With USB connected, '
                'switch off, then switch on holding Shutter + joystick Up. Release both after startup. '
                'This utility will offer Enable menu when service mode appears.')

    def close(self):
        self.stop.set()
        if self.worker: self.worker.join()  # Do not interrupt a bounded patch subprocess.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def send(self, code, data, kind='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', kind); self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if not self.valid_host(): self.send(403, b'{}'); return
        path = urlparse(self.path).path
        if path == '/api/state': self.send(200, json.dumps(self.server.app.snapshot()).encode()); return
        try:
            # Windows registry associations can label .js as text/plain, which
            # browsers reject with nosniff. Serve our three known types directly.
            name, kind = {'/':('index.html', 'text/html; charset=utf-8'),
                          '/app.js':('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css':('style.css', 'text/css; charset=utf-8')}[path]
            self.send(200, (STATIC/name).read_bytes(), kind)
        except (KeyError, OSError): self.send(404, b'{}')

    def do_POST(self):
        origin = self.headers.get('Origin')
        if (not self.valid_host() or self.headers.get('X-Enabler-Token') != self.server.app.token
            or origin not in (None, f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')):
            self.send(403, b'{}'); return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if self.path != '/api/action' or not 0 < length <= 1024: raise ValueError('Invalid request')
            self.server.app.action(json.loads(self.rfile.read(length))['action'])
            self.send(200, b'{"ok":true}')
        except (ValueError, KeyError, TypeError) as exc:
            self.send(409, json.dumps({'error':str(exc)}).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--data-dir', type=Path, default=flow.ROOT/'enabler-data')
    parser.add_argument('--no-browser', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true')
    modes.add_argument('--check-usb', action='store_true', help='Check Windows USB libraries/runtime without opening a camera')
    modes.add_argument('--diagnose-usb', action='store_true', help='Report host USB access by claiming/releasing the camera without PTP commands')
    args = parser.parse_args()
    os.umask(0o077)
    lock_path = flow.ROOT/('enabler-demo.lock' if args.demo else 'companion.lock')
    with exclusive_lock(lock_path):
        if args.diagnose_usb:
            from usb_diagnostics import diagnose
            path, report = diagnose(args.data_dir)
            print(json.dumps(dict(report_path=str(path), **report), indent=2), flush=True)
            raise SystemExit(0 if report['complete'] else 1)
        if args.check_usb:
            if sys.platform != 'win32': parser.error('--check-usb is for the Windows UsbDk runtime')
            import usb1
            try:
                usb_host.pyusb_backend()
                with usb_host.service_context(usb1): pass
            except (usb_host.USBHostError, usb1.USBError) as exc:
                parser.exit(1, f'{usb_host.WINDOWS_HELP}\nDetails: {exc}\n')
            print('Windows USB ready: both PTP and service libraries initialized with UsbDk. No camera opened.')
            return
        app = Enabler(args.data_dir, args.demo)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler); server.app = app
        def shutdown(*_):
            app.stop.set(); threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGINT, shutdown); signal.signal(signal.SIGTERM, shutdown)
        url = f'http://127.0.0.1:{server.server_port}/'
        app.start()
        print(f'Fuji Tether Enabler: {url}\nLogs: {app.root}\nClose this utility before opening another camera app.', flush=True)
        if not args.no_browser: webbrowser.open(url)
        try: server.serve_forever(poll_interval=0.2)
        finally: app.close(); server.server_close()


if __name__ == '__main__': main()
