# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Check the X-T30 III service console, or take a fixed UI-state snapshot.

Default prints a plan. --execute verifies fixed firmware samples and console
state using the proven temporary config[f7] read access, restores f7, then
sends exactly hello and collects its reply. No keys, menus, mode settings,
general RAM writes, persistence requests or firmware commands are allowed.
--ui-state instead reads fixed UI mode/dispatcher locations and sends no
console command. Both profiles restore the temporary debug flag.
--patch-state reads fixed code/cache-coordination prerequisites only. It
does not install a patch, invoke cache routines, or send console commands.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import time

from ff80_identify import usb1, fftlib, ffjlib
from inspect_network_menu import Image

ROOT = Path(__file__).resolve().parent
FLAG = struct.pack('<4I', 1, 0, 0xF7, 1)
SAMPLES = (
    ('console_request_dispatch', 0x0307C59C, 36),
    ('console_ingress', 0x03273234, 108),
    ('console_hello_handler', 0x03273E78, 32),
    ('console_hello_table_entry', 0x038604B8, 24),
    ('console_hello_string', 0x038605C0, 15),
)
STATES = (('input_ring', 0x045F6B80, 40),
          ('console_flags', 0x045F6FA8, 16),
          ('output_ring', 0x045F6FB8, 40))
READS = {(a, n) for _, a, n in (*SAMPLES, *STATES)}
UI_SAMPLES = (
    ('root_dispatch', 0x02DDC7B4, 68),
    ('jig_handler', 0x02E19EA0, 104),
    ('jig_local_dispatch', 0x02DE6534, 88),
    ('root_dispatch_width', 0x036C1DA8, 2),
    ('root_jig_fallback_cell', 0x03643504, 4),
)
UI_STATES = (
    ('ui_mode', 0x02A305F8, 4),
    ('candidate_ui_mode', 0x02A30604, 4),
    ('requested_ui_action', 0x02A3060C, 4),
    ('root_dispatch_state', 0x04695C48, 2),
    ('root_dispatch_row', 0x046F3A30, 2),
    ('jig_dispatch_state', 0x04695CDE, 2),
    ('connection_mode', 0x0297FF08, 1),
    ('communication_selection', 0x029804EC, 1),
)
UI_READS = {(a, n) for _, a, n in (*UI_SAMPLES, *UI_STATES)}
PATCH_SAMPLES = SAMPLES + (
    ('jig_completion', 0x02E19EA0, 104),
    ('rec_request_wrapper', 0x02E174D4, 28),
    ('jig_key_action_table', 0x036C0E40, 48),
    ('normal_mode_completion', 0x02E18D3C, 172),
    ('ram_write_helper', 0x01450874, 108),
    ('cache_clean_all_entry', 0x015E80D0, 44),
    ('cache_broadcast', 0x015E7FF4, 220),
    ('cache_clean_worker', 0x015E8534, 36),
    ('cache_completion_wait', 0x015E84B0, 96),
    ('kernel_instruction_cache_all', 0x0009C8B0, 16),
    ('kernel_cache_range_dispatch', 0x0009C898, 72),
    ('kernel_cache_all_dispatch', 0x0009C99C, 180),
)
PATCH_STATES = STATES + UI_STATES + (
    ('cache_coordinator_initialized', 0x01E0E150, 4),
    ('cache_cpu_mask', 0x01E0E154, 4),
    ('cache_clean_ipi_id', 0x024B44C8, 4),
    ('cache_clean_invalidate_ipi_id', 0x024B44CC, 4),
    ('cache_clean_completion_flags', 0x024C1198, 16),
    ('cache_clean_invalidate_completion_flags', 0x024C11A8, 16),
)
PATCH_READS = {(a, n) for _, a, n in (*PATCH_SAMPLES, *PATCH_STATES)}
KERNEL_SHA256 = 'f6fd863952976e96a6ffa4cb90663164e41a79bbe00806a5fbf9fee32c9e0ef5'
HELLO = struct.pack('<6I', 0x200001, 0, 0, 0, 6, 6) + b'hello\n' + bytes(6)
POLL = struct.pack('<6I', 0x200001, 0, 0, 0, 7, 256) + bytes(12)
REPORT_KIND = 'ff80_console_probe_v1'


def validate_request(request, reads=READS, console=True):
    request = bytes(request)
    if len(request) < 20 or request[4:16] != bytes(12):
        raise RuntimeError('Unexpected service header')
    operation, _, _, _, function = struct.unpack('<5I', request[:20])
    body = request[20:]
    allowed = (
        (operation == 0x150000 and function == 4 and body == bytes(12))
        or (operation == 0x1A0002 and function == 1 and body == struct.pack('<4I', 1, 0, 0, 0))
        or (operation == 0x1A0002 and function == 2 and body == FLAG)
        or (operation == 0x1A0002 and function == 3 and body in (FLAG+b'\0', FLAG+b'\1'))
        or (operation == 0x200001 and function == 2 and len(body) == 16
            and body[:8] == bytes(8) and 1 <= struct.unpack('<I', body[12:])[0] <= 256
            and struct.unpack('<II', body[8:]) in reads)
        or (console and request in (HELLO, POLL)))
    if not allowed:
        raise RuntimeError('Request outside fixed console diagnostic allowlist')


class Transport(fftlib.ftl):
    """Fence console execution until verification/restoration have succeeded."""
    reads = READS
    console_allowed = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.healthy = True
        self.console_permitted = False
        self.hello_attempted = False
        self.polls = 0

    def write_cmd(self, request):
        validate_request(request, self.reads, self.console_allowed)
        if not self.healthy:
            raise RuntimeError('USB stream uncertain; no follow-up commands')
        if request in (HELLO, POLL):
            if not self.console_permitted:
                raise RuntimeError('Console preflight/restoration not complete')
            if request == HELLO:
                if self.hello_attempted:
                    raise RuntimeError('Hello must never be retried automatically')
                self.hello_attempted = True
            else:
                if not self.hello_attempted or self.polls >= 40:
                    raise RuntimeError('Console poll outside bounded hello transaction')
                self.polls += 1
        try:
            return super().write_cmd(request)
        except BaseException:
            self.healthy = False
            raise

    def read_cmd(self, size=256):
        if not self.healthy:
            raise RuntimeError('USB stream uncertain')
        try:
            return super().read_cmd(size)
        except BaseException:
            self.healthy = False
            raise


class UITransport(Transport):
    reads = UI_READS
    console_allowed = False


class PatchStateTransport(Transport):
    reads = PATCH_READS
    console_allowed = False


def patch_expected_samples():
    """Pin all three extracted images before USB is opened."""
    a, b = Image('MainA'), Image('MainB')
    kernel = (ROOT/'threadx-kernel.bin').read_bytes()
    if hashlib.sha256(kernel).hexdigest() != KERNEL_SHA256:
        raise RuntimeError('ThreadX kernel hash mismatch')
    expected = {}
    for _, address, size in PATCH_SAMPLES:
        if 0x81000 <= address < 0x81000+len(kernel):
            base, data = 0x81000, kernel
        elif a.base <= address < b.base:
            base, data = a.base, a.data
        else:
            base, data = b.base, b.data
        sample = data[address-base:address-base+size]
        assert len(sample) == size
        expected[address] = sample
    return expected, dict(MainA=a.sha256, MainB=b.sha256, ThreadX=KERNEL_SHA256)


def read_fixed(transport, address, size):
    params = struct.pack('<4I', 0, 0, address, size)
    request = struct.pack('<5I', 0x200001, 0, 0, 0, 2) + params
    transport.write_cmd(request)
    response = bytes(transport.read_cmd(36+size))
    if len(response) != 36+size or response[:20] != struct.pack('<5I', 0x200001, 0, 0, 0, 2) or response[20:36] != params:
        raise RuntimeError('Unexpected fixed memory-read response')
    return response[36:]


def verify_idle(states):
    busy, initialized, previous, first_poll = struct.unpack('<4I', states['console_flags'])
    if (busy, initialized) != (0, 1):
        raise RuntimeError('Console not initialized and idle; no hello sent')
    for key, expected_pointer in (('input_ring', 0x045F6BA8), ('output_ring', 0x045F6FE0)):
        pointer, capacity, read, write, empty = struct.unpack('<4QI4x', states[key])
        if pointer != expected_pointer or capacity != 1024 or not 0 <= read == write < capacity or empty != 1:
            raise RuntimeError(f'{key} is not the expected empty ring; no hello sent')
    return dict(initialized=True, idle=True, input_empty=True, output_empty=True,
                previous_exit_status=previous, previous_first_poll=first_poll)


def preflight(jig, transport, expected, report, checkpoint, *, ui_state=False, patch_state=False):
    if ui_state and patch_state:
        raise ValueError('Choose one fixed snapshot profile')
    samples = PATCH_SAMPLES if patch_state else UI_SAMPLES if ui_state else SAMPLES
    state_reads = PATCH_STATES if patch_state else UI_STATES if ui_state else STATES
    original = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
    report['original_f7'] = original.hex()
    checkpoint()
    if original != b'\0':
        raise RuntimeError('Expected debug flag zero; no changes requested')
    report['restore_required'] = True
    checkpoint()
    try:
        jig.write_config_data(0, 0xF7, b'\1', mb=1)
        if bytes(jig.read_config_data(0, 0xF7, 1, mb=1)) != b'\1':
            raise RuntimeError('Debug read-access enable did not verify')
        report['static_samples'] = []
        for name, address, size in samples:
            data = read_fixed(transport, address, size)
            matches = data == expected[address]
            report['static_samples'].append(dict(name=name, address=hex(address), matches=matches,
                                                 data_hex=data.hex()))
            checkpoint()
            if not matches:
                raise RuntimeError('Firmware sample mismatch; no hello sent')
        states = {}
        for name, address, size in state_reads:
            states[name] = read_fixed(transport, address, size)
        report['state_samples'] = {key: value.hex() for key, value in states.items()}
        if patch_state:
            report['console_preflight'] = verify_idle(states)
            report['patch_state'] = {
                name: (list(struct.unpack('<4I', value)) if name.endswith('_completion_flags')
                       else int.from_bytes(value, 'little'))
                for name, value in states.items() if name not in {s[0] for s in STATES}}
            report['activation_ready'] = False
            report['interpretation'] = 'Read-only prerequisite snapshot; cache behavior, code writes and service exit remain untested.'
        elif ui_state:
            report['ui_state'] = {key: int.from_bytes(value, 'little') for key, value in states.items()}
        else:
            report['console_preflight'] = verify_idle(states)
        checkpoint()
    finally:
        try:
            if not transport.healthy:
                raise RuntimeError('Transport uncertain; flag restoration still required')
            jig.write_config_data(0, 0xF7, original, mb=1)
            restored = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
            report['restored_f7'] = restored.hex()
            if restored != original:
                raise RuntimeError('Debug flag restoration did not verify')
            report['restore_required'] = False
            report['restore_verified'] = True
        except BaseException as exc:
            report['restore_verified'] = False
            report['restore_error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            checkpoint()
    transport.console_permitted = not (ui_state or patch_state)


def exchange(transport, request, function):
    transport.write_cmd(request)
    response = bytes(transport.read_cmd(0x120))
    if len(response) != 0x120:
        raise RuntimeError('Unexpected console response length')
    op, error1, error2, reserved, actual_function = struct.unpack('<5I', response[:20])
    if (op, reserved, actual_function) != (0x200001, 0, function):
        raise RuntimeError('Unexpected console response header')
    if error1 or error2:
        raise RuntimeError(f'Console rejected request: {error1:#x}/{error2:#x}')
    return response


def hello(transport, report, checkpoint, sleep=time.sleep):
    report['hello_attempted'] = True
    checkpoint()
    submission = exchange(transport, HELLO, 6)
    accepted, = struct.unpack('<I', submission[20:24])
    report['accepted_bytes'] = accepted
    checkpoint()
    if accepted != 6:
        raise RuntimeError('Partial/unexpected command acceptance; not retrying')
    output = bytearray()
    report['polls'] = []
    for index in range(40):
        response = exchange(transport, POLL, 7)
        busy, status, length = struct.unpack('<3I', response[20:32])
        if busy not in (0, 1) or length > 255 or response[32+length] != 0:
            raise RuntimeError('Malformed console reply fields')
        output.extend(response[32:32+length])
        report['polls'].append(dict(busy=busy, exit_status=status, output_bytes=length))
        report['output_hex'] = output.hex()
        checkpoint()
        if len(output) > 512:
            raise RuntimeError('Unexpectedly large hello output')
        if not busy:
            if status != 0 or output != b'Hello, world!\n':
                raise RuntimeError('Console completed without the expected hello reply')
            report['console_confirmed'] = True
            report['output_text'] = output.decode('ascii')
            checkpoint()
            return
        sleep(0.1)
    raise RuntimeError('Console did not finish within 40 polls; no command retry')


def restore_flag(jig, report, checkpoint):
    """Recover a recorded preflight obligation on a freshly opened transport."""
    if report.get('kind') != REPORT_KIND or report.get('original_f7') != '00' or report.get('hello_attempted'):
        raise RuntimeError('Report is not a valid preflight restoration obligation')
    if not report.get('restore_required'):
        raise RuntimeError('No pending debug flag restoration in this report')
    current = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
    report['recovery_flag_before'] = current.hex()
    checkpoint()
    if current not in (b'\0', b'\1'):
        raise RuntimeError('Unexpected debug flag; restoration stopped without overwriting it')
    if current == b'\1':
        jig.write_config_data(0, 0xF7, b'\0', mb=1)
    verified = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
    report['restored_f7'] = verified.hex()
    if verified != b'\0':
        raise RuntimeError('Recovery readback did not verify zero')
    report['restore_required'] = False
    report['restore_verified'] = True
    checkpoint()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--execute', action='store_true')
    actions.add_argument('--restore', action='store_true', help='Restore only a pending flag obligation in the existing report')
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument('--ui-state', action='store_true', help='Fixed UI-state snapshot only; no console command')
    profiles.add_argument('--patch-state', action='store_true', help='Read fixed patch/cache prerequisites only; no patch or console command')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.output is None:
        args.output = ROOT / ('ff80-patch-state-snapshot.json' if args.patch_state else
                             'ff80-ui-state-snapshot.json' if args.ui_state else 'ff80-console-probe.json')
    if not args.execute and not args.restore:
        if args.patch_state:
            print('OFFLINE PLAN: identify service X-T30 III; verify 17 fixed samples from MainA/MainB/ThreadX.')
            print('Temporarily enable config[f7], read fixed console/UI/cache-coordination state, restore and verify zero.')
            print('No patch, cache operation, console command, key, mode setting, save, upload or flash.')
            return 0
        if args.ui_state:
            print('OFFLINE PLAN: verify service X-T30 III and five fixed firmware samples.')
            print('Temporarily enable config[f7], read eight fixed UI state locations, restore and verify zero.')
            print('No console commands, keys, mode writes, saves, uploads or flashes.')
            return 0
        print('OFFLINE PLAN: verify X-T30 III in service mode and fixed firmware samples.')
        print('Temporarily enable config[f7] only for fixed reads; require empty, idle console.')
        print('Restore f7=0 and verify BEFORE sending exactly hello followed by bounded reply polls.')
        print('No keys, mode/menu settings, arbitrary memory writes, saves, uploads or flashes.')
        return 0
    if args.restore:
        report = json.loads(args.output.read_text())
        if (report.get('kind') != REPORT_KIND or report.get('original_f7') != '00'
                or report.get('hello_attempted') or report.get('model') != 'X-T30 III'):
            raise RuntimeError('Existing report is not a valid preflight recovery journal')
        if not report.get('restore_required'):
            if report.get('restore_verified'):
                print('Debug flag restoration already verified; no USB access.')
                return 0
            raise RuntimeError('No pending restoration in report')
        recovery = dict(started_at_utc=datetime.now(timezone.utc).isoformat(), complete=False)
        report.setdefault('recoveries', []).append(recovery)
        report['session'] = 'not_opened'
    else:
        image = Image('MainB')
        if args.patch_state:
            expected, image_hashes = patch_expected_samples()
        else:
            expected = {address: image.data[address-image.base:address-image.base+size]
                        for _, address, size in (UI_SAMPLES if args.ui_state else SAMPLES)}
        report = dict(kind=REPORT_KIND, started_at_utc=datetime.now(timezone.utc).isoformat(),
                      main_b_sha256=image.sha256, restore_required=False,
                      restore_verified=None, hello_attempted=False, console_confirmed=False,
                      session='not_opened')
        report['purpose'] = 'fixed_patch_prerequisites' if args.patch_state else 'fixed_ui_state' if args.ui_state else 'console_hello'
        if args.patch_state:
            report['image_hashes'] = image_hashes
        with open(args.output, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    def checkpoint():
        temp = args.output.with_suffix(args.output.suffix+'.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
        temp.replace(args.output)
    try:
        with usb1.USBContext() as context:
            devices = [d for d in context.getDeviceList()
                       if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1:
                raise RuntimeError('Expected one service-mode camera (04cb:ff80)')
            device = devices[0]
            report['device_node'] = f'/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}'
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    transport_class = PatchStateTransport if args.patch_state else UITransport if args.ui_state else Transport
                    transport = transport_class(handle, EP_IN=0x81, EP_OUT=0x01)
                    transport.ms = 3000
                    try:
                        transport.open_session()
                        report['session'] = 'open'
                        checkpoint()
                        jig = ffjlib.jig(transport)
                        report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III':
                            raise RuntimeError('Camera model mismatch')
                        if jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Configuration bounds mismatch')
                        if args.restore:
                            restore_flag(jig, report, checkpoint)
                            recovery['complete'] = True
                        else:
                            preflight(jig, transport, expected, report, checkpoint,
                                      ui_state=args.ui_state, patch_state=args.patch_state)
                            if args.ui_state or args.patch_state:
                                report['snapshot_complete'] = True
                            else:
                                hello(transport, report, checkpoint)
                    finally:
                        if transport.sid is not None and transport.healthy:
                            transport.close_session()
                            report['session'] = 'closed'
                        elif transport.sid is not None:
                            report['session'] = 'unconfirmed_after_transport_error'
            finally:
                handle.close()
    except BaseException as exc:
        (recovery if args.restore else report)['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        (recovery if args.restore else report)['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    print(json.dumps(report, indent=2))
    if args.restore:
        return 0 if recovery['complete'] and report['session'] == 'closed' and 'error' not in recovery else 1
    success = report.get('snapshot_complete') if args.ui_state or args.patch_state else report.get('console_confirmed')
    return 0 if success and report['session'] == 'closed' and 'error' not in report else 1


if __name__ == '__main__':
    raise SystemExit(main())
