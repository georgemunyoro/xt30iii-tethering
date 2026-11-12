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
import usb.core
import usb.util
from test_enabler import device


class Interface(list):
    bInterfaceNumber, bAlternateSetting = 0, 0
    bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol = 6, 1, 1


class Configuration(list):
    bConfigurationValue = 1


class USBAccessTests(unittest.TestCase):
    def connection(self, platform='darwin', claim_error=None):
        endpoints = [SimpleNamespace(bEndpointAddress=a, bmAttributes=2, wMaxPacketSize=512)
                     for a in (0x02, 0x81)]
        dev = Mock(idVendor=0x04CB, idProduct=0x0317, bus=2, address=7, bcdDevice=0x110)
        dev.get_active_configuration.return_value = Configuration([Interface(endpoints)])
        dev.is_kernel_driver_active.return_value = True
        with ExitStack() as stack:
            stack.enter_context(patch.object(host.sys, 'platform', platform))
            stack.enter_context(patch.object(host, 'pyusb_backend', return_value=object()))
            stack.enter_context(patch.object(usb.core, 'find', return_value=[dev]))
            claim = stack.enter_context(patch.object(usb.util, 'claim_interface', side_effect=claim_error))
            release = stack.enter_context(patch.object(usb.util, 'release_interface'))
            dispose = stack.enter_context(patch.object(usb.util, 'dispose_resources'))
            read = Mock()
            report = {'session': 'not_opened'}
            if claim_error is not None or platform == 'linux':
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
            return dev, claim

    def test_macos_ordinary_claim_decides_ownership_despite_driver_indication(self):
        dev, claim = self.connection()
        dev.is_kernel_driver_active.assert_not_called()
        claim.assert_called_once_with(dev, 0)

    def test_macos_busy_or_denied_claim_never_starts_camera_protocol(self):
        for code in (-6, -3):
            with self.subTest(code=code):
                dev, claim = self.connection(claim_error=usb.core.USBError('blocked', error_code=code))
                claim.assert_called_once_with(dev, 0)

    def test_linux_active_driver_still_blocks_before_claim(self):
        dev, claim = self.connection(platform='linux')
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


if __name__ == '__main__': unittest.main()
