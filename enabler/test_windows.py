"""Host and reconnect regressions, including native Windows execution in CI."""
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

import utility as u
import usb_host as host
import usb.backend.libusb1
import usb.core
from local_state import atomic_json
from process_lock import exclusive_lock
from test_enabler import device, snapshot

ROOT = Path(__file__).resolve().parents[1]


class HostFilesTests(unittest.TestCase):
    def test_lock_excludes_other_process_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'utility.lock'
            code = ('from process_lock import exclusive_lock; import sys; '
                    'exec("with exclusive_lock(sys.argv[1]): print(\'acquired\')")')
            command = [sys.executable, '-c', code, str(path)]
            def run():
                return subprocess.run(command, cwd=ROOT/'enabler', capture_output=True, text=True, timeout=15)
            with exclusive_lock(path):
                blocked = run()
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('already running', blocked.stderr)
            acquired = run()
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
            self.assertIn('acquired', acquired.stdout)

    def test_both_journal_writers_replace_and_clean_up_on_failure(self):
        import ff80_cmd_patch_probe as service
        with tempfile.TemporaryDirectory() as directory:
            for writer in (atomic_json, service.save_private):
                with self.subTest(writer=writer.__name__):
                    path = Path(directory)/'journal.json'
                    writer(path, {'generation': 1})
                    writer(path, {'generation': 2})
                    self.assertEqual(json.loads(path.read_text()), {'generation': 2})
                    with patch('os.replace', side_effect=PermissionError('injected write failure')):
                        with self.assertRaises(PermissionError): writer(path, {'generation': 3})
                    self.assertEqual(json.loads(path.read_text()), {'generation': 2})
                    self.assertEqual(list(Path(directory).iterdir()), [path])


class WindowsAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = u.Enabler(self.temp.name)
        self.platform = patch.object(u.sys, 'platform', 'win32')
        self.platform.start(); self.addCleanup(self.platform.stop)

    def detect(self, devices=None, report=None, action=None):
        with patch.object(u.camera, 'devices', return_value=[device()] if devices is None else devices), \
             patch.object(u, 'inspect_camera', return_value=(Path('check.json'), report or snapshot())) as inspect:
            self.app.detect(action)
        return inspect

    def test_reused_address_needs_observed_gap_and_user_confirmation(self):
        self.detect()
        original = self.app.workflow['enabled_node']
        self.detect(action='restart').assert_not_called()
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')
        self.detect([])
        self.detect()
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')
        self.assertNotEqual(self.app.device['node'], original)
        self.detect(action='restart').assert_called_once()
        self.assertEqual(self.app.snapshot()['stage'], 'verified')

    def test_relaunch_is_not_a_reconnection(self):
        self.detect()
        node = self.app.workflow['enabled_node']
        self.app = u.Enabler(self.temp.name)
        self.detect(action='restart').assert_not_called()
        self.assertEqual(self.app.device['node'], node)
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')

    def test_fault_needs_reconnection_and_explicit_restart_confirmation(self):
        self.detect(report=dict(complete=False, session='uncertain_after_transport_error', error='Timeout'))
        self.detect(action='restarted').assert_not_called()
        self.detect([])
        self.detect().assert_not_called()
        self.detect(action='restarted').assert_called_once()
        self.assertIsNone(self.app.workflow['fault'])

    def test_observed_gap_survives_relaunch(self):
        self.detect()
        self.detect([])
        self.app = u.Enabler(self.temp.name)
        self.detect(action='restart')
        self.assertEqual(self.app.snapshot()['stage'], 'verified')

    def test_menu_journal_and_service_process_share_attachment_identity(self):
        with patch.object(u.camera, 'devices', return_value=[device(product=0xFF80)]), \
             patch.object(u.flow, 'missing_references', return_value=[]), \
             patch.object(u.flow, 'armed_menu', return_value=None), \
             patch.object(u.flow, 'prepare', return_value=Path('arm.json')) as prepare:
            self.app.enable()
        token = self.app.workflow['windows_attachment']['token']
        self.assertEqual(prepare.call_args.kwargs['attachment_token'], token)
        with patch.dict(os.environ, {'FUJI_USB_ATTACHMENT_TOKEN': token}):
            self.assertEqual(host.attachment_node(2, 7), 'usb:002/007#' + token)


class USBHostTests(unittest.TestCase):
    def tearDown(self):
        host.pyusb_backend.cache_clear()

    def test_usbdk_selection_precedes_use_and_failure_does_not_fall_back(self):
        backend = SimpleNamespace(lib=SimpleNamespace(libusb_set_option=Mock(return_value=0)), ctx=object())
        with patch.object(host.sys, 'platform', 'win32'), \
             patch.object(host, 'windows_library', return_value=SimpleNamespace(_name='pinned.dll')), \
             patch.object(usb.backend.libusb1, 'get_backend', return_value=backend):
            self.assertIs(host.pyusb_backend(), backend)
            backend.lib.libusb_set_option.assert_called_once_with(backend.ctx, 1)
            host.pyusb_backend.cache_clear()
            backend.lib.libusb_set_option.return_value = -5
            with self.assertRaisesRegex(host.USBHostError, 'UsbDk'): host.pyusb_backend()

    def test_service_context_uses_same_dll_and_usbdk(self):
        binding = SimpleNamespace(loadLibrary=Mock(return_value=True), USBContext=Mock())
        library = object()
        with patch.object(host.sys, 'platform', 'win32'), patch.object(host, 'windows_library', return_value=library):
            host.service_context(binding)
            binding.loadLibrary.assert_called_once_with(library)
            binding.USBContext.assert_called_once_with(use_usbdk=True)
            binding.loadLibrary.return_value = False
            with self.assertRaisesRegex(host.USBHostError, 'already loaded'): host.service_context(binding)
            self.assertEqual(binding.USBContext.call_count, 1)

    def test_unsupported_driver_query_is_allowed_but_access_errors_are_not(self):
        dev = Mock()
        with patch.object(host.sys, 'platform', 'win32'):
            for exc in (NotImplementedError(), usb.core.USBError('unsupported', error_code=-12)):
                dev.is_kernel_driver_active.side_effect = exc
                host.check_kernel_driver(dev, 0)
            dev.is_kernel_driver_active.side_effect = usb.core.USBError('access denied', error_code=-3)
            with self.assertRaises(usb.core.USBError): host.check_kernel_driver(dev, 0)
            dev.detach_kernel_driver.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows DLL test')
    def test_installed_windows_dll_loads_for_both_transports(self):
        import usb1
        library = host.windows_library()
        self.assertTrue(Path(library._name).is_file())
        # A hosted runner has no UsbDk system driver. Verify the real DLL gives
        # actionable failure, or initializes correctly if the runtime is present.
        try:
            host.pyusb_backend()
        except host.USBHostError as exc:
            self.assertIn('UsbDk', str(exc))
            with self.assertRaises(usb1.USBError):
                with host.service_context(usb1): pass
        else:
            with host.service_context(usb1) as context:
                self.assertIsNotNone(context)


class LaunchTests(unittest.TestCase):
    def run_demo(self, command, batch=False):
        with tempfile.TemporaryDirectory(prefix='tether test ') as directory:
            log_path = Path(directory)/'output.txt'
            with log_path.open('wb') as log:
                arguments = command + ['--demo', '--no-browser', '--port', '0',
                    '--data-dir', str(Path(directory)/'state')]
                process = subprocess.Popen(subprocess.list2cmdline(arguments) if batch else arguments,
                    shell=batch, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        output = log_path.read_text(errors='replace')
                        match = re.search(r'http://127\.0\.0\.1:\d+/', output)
                        if match: break
                        if process.poll() is not None: self.fail(output)
                        time.sleep(.1)
                    else: self.fail('Demo startup timed out: ' + output)
                    with urlopen(match[0]+'api/state', timeout=5) as response:
                        state = json.load(response)
                    self.assertTrue(state['demo'])
                    self.assertEqual(state['actions'], [])
                    with urlopen(match[0]+'app.js', timeout=5) as response:
                        self.assertEqual(response.headers.get_content_type(), 'text/javascript')
                finally:
                    # Demo never accesses a camera; termination is safe here.
                    if sys.platform == 'win32':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                    else: process.send_signal(signal.SIGINT)
                    process.wait(timeout=15)

    def test_native_python_demo_starts_without_camera_access(self):
        self.run_demo([sys.executable, '-u', str(ROOT/'enabler'/'utility.py')])

    @unittest.skipUnless(sys.platform == 'win32' and shutil.which('uv'), 'Windows launcher needs uv')
    def test_windows_cmd_launcher_resolves_dependencies_and_starts_demo(self):
        self.run_demo([str(ROOT/'enabler'/'Start Tether Enabler.cmd')], batch=True)

    @unittest.skipUnless(sys.platform == 'win32' and shutil.which('uv'), 'Windows launcher needs uv')
    def test_windows_import_launcher_handles_path_with_spaces_and_rejects_wrong_firmware(self):
        with tempfile.TemporaryDirectory(prefix='firmware import ') as directory:
            path = Path(directory)/'wrong firmware.DAT'; path.write_bytes(b'wrong')
            args = [str(ROOT/'enabler'/'Import Firmware.cmd'), str(path)]
            process = subprocess.run(subprocess.list2cmdline(args), shell=True, input='\n',
                                     capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
            self.assertIn('size differs', process.stderr)


if __name__ == '__main__': unittest.main()
