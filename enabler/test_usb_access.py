"""Exercise macOS interface ownership without talking to a physical camera."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import utility as u
import usb_host as host
import usb_diagnostics as diagnostics
import usb.core
import usb.util
from test_enabler import device


class Interface(list):
    bInterfaceNumber, bAlternateSetting = 0, 0
    bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol = 6, 1, 1


class Configuration(list):
    bConfigurationValue = 1


def camera_device():
    endpoints = [SimpleNamespace(bEndpointAddress=a, bmAttributes=2, wMaxPacketSize=512)
                 for a in (0x02, 0x81)]
    dev = Mock(idVendor=0x04CB, idProduct=0x0317, bus=2, address=7, bcdDevice=0x110)
    dev.get_active_configuration.return_value = Configuration([Interface(endpoints)])
    dev.is_kernel_driver_active.return_value = True
    return dev


class USBAccessTests(unittest.TestCase):
    def connection(self, platform='darwin', claim_error=None, configuration_error=None):
        dev = camera_device()
        dev.get_active_configuration.side_effect = configuration_error
        with ExitStack() as stack:
            stack.enter_context(patch.object(host.sys, 'platform', platform))
            stack.enter_context(patch.object(host, 'pyusb_backend', return_value=object()))
            stack.enter_context(patch.object(usb.core, 'find', return_value=[dev]))
            claim = stack.enter_context(patch.object(usb.util, 'claim_interface', side_effect=claim_error))
            release = stack.enter_context(patch.object(usb.util, 'release_interface'))
            dispose = stack.enter_context(patch.object(usb.util, 'dispose_resources'))
            read = Mock()
            report = {'session': 'not_opened'}
            if claim_error is not None or configuration_error is not None or platform == 'linux':
                with self.assertRaises(host.USBHostError):
                    u.protocol.connect_and_snapshot(report, lambda: None, snapshot_fn=read)
                read.assert_not_called()
                release.assert_not_called()
                self.assertEqual(report['session'], 'not_opened')
            else:
                u.protocol.connect_and_snapshot(report, lambda: None, snapshot_fn=read)
                read.assert_called_once()
                release.assert_called_once_with(dev, 0)
            dispose.assert_called_once_with(dev)
            dev.detach_kernel_driver.assert_not_called()
            dev.attach_kernel_driver.assert_not_called()
            dev.reset.assert_not_called()
            dev.set_configuration.assert_not_called()
            dev.write.assert_not_called()  # Snapshot is stubbed; no protocol commands in these tests.
            return dev, claim, report

    def test_macos_ordinary_claim_decides_ownership_despite_driver_indication(self):
        dev, claim, _ = self.connection()
        dev.is_kernel_driver_active.assert_not_called()
        claim.assert_called_once_with(dev, 0)

    def test_macos_busy_or_denied_claim_never_starts_camera_protocol(self):
        for code in (-6, -3):
            with self.subTest(code=code):
                dev, claim, report = self.connection(claim_error=usb.core.USBError('blocked', error_code=code, errno=13))
                claim.assert_called_once_with(dev, 0)
                self.assertEqual(report['usb_access']['stage'], 'claim_interface')
                self.assertEqual(report['usb_access']['errno'], 13)
                self.assertEqual(report['usb_access']['backend_error_code'], code)

    def test_configuration_failure_is_distinguished_from_claim_failure(self):
        _, claim, report = self.connection(configuration_error=usb.core.USBError('blocked', error_code=-3))
        claim.assert_not_called()
        self.assertEqual(report['usb_access']['stage'], 'read_configuration')
        self.assertEqual(report['usb']['id'], '04cb:0317')

    def test_linux_active_driver_still_blocks_before_claim(self):
        dev, claim, _ = self.connection(platform='linux')
        dev.is_kernel_driver_active.assert_called_once_with(0)
        claim.assert_not_called()

    def test_host_failure_is_saved_and_shown_without_firmware_or_restart_advice(self):
        with tempfile.TemporaryDirectory() as directory:
            error = host.USBHostError('macOS could not give the utility USB access. Details: Resource busy')
            with patch.object(u.protocol, 'connect_and_snapshot', side_effect=error):
                path, report = u.inspect_camera(directory)
            self.assertEqual(json.loads(path.read_text())['error_type'], 'USBHostError')
            self.assertEqual(report['session'], 'not_opened')
            app = u.Enabler(directory)
            with patch.object(u.camera, 'devices', return_value=[device()]), \
                 patch.object(u, 'inspect_camera', return_value=(path, report)):
                app.detect()
            state = app.snapshot()
            self.assertEqual(state['title'], 'Check USB access on this computer')
            self.assertIn('Details: Resource busy', state['message'])
            self.assertNotIn('firmware', state['message'].lower())
            self.assertEqual(state['actions'], ['check'])
            self.assertFalse(app.workflow.get('fault'))


class DiagnosticTests(unittest.TestCase):
    def test_helpers_retain_only_known_names_and_numeric_ids(self):
        output = ('84572 501 /usr/libexec/ptpcamerad\n'
                  '100 501 /System/Library/Image Capture/Devices/PTPCamera.app/Contents/MacOS/PTPCamera\n'
                  '200 501 /private/unrelated-file\n')
        with patch.object(host.sys, 'platform', 'darwin'), \
             patch.object(diagnostics.subprocess, 'run', return_value=SimpleNamespace(stdout=output)) as run:
            self.assertEqual(diagnostics.camera_helpers(), [
                dict(name='ptpcamerad', pid=84572, uid=501, executable='/usr/libexec/ptpcamerad'),
                dict(name='PTPCamera', pid=100, uid=501,
                     executable='/System/Library/Image Capture/Devices/PTPCamera.app/Contents/MacOS/PTPCamera')])
            self.assertEqual(run.call_args.args[0], ['/bin/ps', '-axo', 'pid=,uid=,comm='])

    def test_probe_claims_and_releases_without_ptp_commands_and_records_helper_restart(self):
        dev = camera_device()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict(diagnostics.os.environ))
            stack.enter_context(patch.object(host, 'pyusb_backend', return_value=None))
            stack.enter_context(patch.object(host, 'check_kernel_driver'))
            stack.enter_context(patch.object(usb.core, 'find', return_value=[dev]))
            claim = stack.enter_context(patch.object(usb.util, 'claim_interface'))
            release = stack.enter_context(patch.object(usb.util, 'release_interface'))
            stack.enter_context(patch.object(usb.util, 'dispose_resources'))
            stack.enter_context(patch.object(diagnostics, 'camera_helpers', side_effect=[
                [dict(name='ptpcamerad', pid=100)], [dict(name='ptpcamerad', pid=101)]]))
            path, report = diagnostics.diagnose(directory)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertTrue(report['complete'])
            self.assertTrue(report['interface_released'])
            self.assertEqual(report['session'], 'not_opened')
            self.assertEqual(report['commands'], [])
            self.assertEqual(report['camera_helpers_before'][0]['pid'], 100)
            self.assertEqual(report['camera_helpers_after'][0]['pid'], 101)
            claim.assert_called_once_with(dev, 0)
            release.assert_called_once_with(dev, 0)
            dev.write.assert_not_called()
            dev.read.assert_not_called()
            dev.detach_kernel_driver.assert_not_called()
            dev.reset.assert_not_called()

    def test_probe_failure_is_saved_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(diagnostics.os.environ), \
             patch.object(host, 'pyusb_backend', return_value=None), \
             patch.object(diagnostics, 'camera_helpers', return_value=[]), \
             patch.object(diagnostics.protocol, 'connect_and_snapshot', side_effect=host.USBHostError('blocked')):
            path, report = diagnostics.diagnose(directory)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertFalse(report['complete'])
            self.assertNotIn('interface_released', report)
            self.assertEqual(report['commands'], [])
            self.assertEqual(report['error'], 'USBHostError: blocked')


if __name__ == '__main__': unittest.main()
