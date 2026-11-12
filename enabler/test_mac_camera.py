"""Exercise explicit Mac helper release without signaling real processes."""
from contextlib import ExitStack
import copy
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

import utility as u
import mac_camera as mac
import usb.core
from test_enabler import device, snapshot
from test_usb_access import camera_device


def helper(pid=100, uid=501, name='ptpcamerad', executable='/usr/libexec/ptpcamerad'):
    return dict(pid=pid, uid=uid, name=name, executable=executable)


def denied():
    return dict(complete=False, session='not_opened', error_type='USBHostError',
                error='USBHostError: Access denied',
                usb_access=dict(stage='claim_interface', backend_error_code=-3))


class HelperReleaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(mac.sys, 'platform', 'darwin'))
        self.stack.enter_context(patch.object(mac.os, 'getuid', return_value=501, create=True))
        self.stack.enter_context(patch.object(mac.os, 'getpid', return_value=999))
        self.stack.enter_context(patch.object(mac.signal, 'SIGKILL', 9, create=True))
        self.kill = self.stack.enter_context(patch.object(mac.os, 'kill'))
        self.sleep = self.stack.enter_context(patch.object(mac.time, 'sleep'))
        self.claim = self.stack.enter_context(patch.object(mac.usb.util, 'claim_interface'))
        self.report = dict(session='not_opened')
        self.dev = Mock()

    def test_kills_only_unchanged_system_helpers_owned_by_current_user_once(self):
        rows = [helper(), helper(200, name='mscamerad-xpc', executable='/System/Library/XPCServices/mscamerad-xpc'),
                helper(300, uid=502), helper(400, executable='/tmp/ptpcamerad'), helper(500)]
        # PID 500 disappears before signaling. A replacement with PID 600 must
        # never be added to the original targets or killed in a background loop.
        current = rows[:4] + [helper(600)]
        self.claim.side_effect = [usb.core.USBError('exclusive', error_code=-3), None]
        with patch.object(mac, 'camera_helpers', side_effect=[rows, current, current, current]):
            mac.claim_after_release(self.dev, 0, self.report, lambda: None)
        self.assertEqual([c.args for c in self.kill.call_args_list], [(100, signal.SIGKILL), (200, signal.SIGKILL)])
        self.assertEqual(self.claim.call_count, 2)
        self.assertEqual(self.report['mac_camera_release']['claim_attempts'], 2)
        self.dev.write.assert_not_called()
        self.dev.reset.assert_not_called()
        self.dev.detach_kernel_driver.assert_not_called()

    def test_repeated_denial_is_bounded_without_repeated_kills(self):
        self.claim.side_effect = usb.core.USBError('exclusive', error_code=-3)
        with patch.object(mac, 'camera_helpers', return_value=[helper()]):
            with self.assertRaises(usb.core.USBError):
                mac.claim_after_release(self.dev, 0, self.report, lambda: None)
        self.kill.assert_called_once_with(100, signal.SIGKILL)
        self.assertEqual(self.claim.call_count, 10)
        self.assertEqual(self.sleep.call_count, 9)

    def test_unrelated_usb_errors_are_not_retried(self):
        self.claim.side_effect = usb.core.USBError('removed', error_code=-4)
        with patch.object(mac, 'camera_helpers', return_value=[]):
            with self.assertRaises(usb.core.USBError):
                mac.claim_after_release(self.dev, 0, self.report, lambda: None)
        self.claim.assert_called_once()
        self.sleep.assert_not_called()

    def test_wrong_platform_existing_session_and_root_never_signal_or_claim(self):
        for platform, session, uid in [('linux', 'not_opened', 501), ('darwin', 'owned', 501), ('darwin', 'not_opened', 0)]:
            with self.subTest(platform=platform, session=session, uid=uid), \
                 patch.object(mac.sys, 'platform', platform), patch.object(mac.os, 'getuid', return_value=uid):
                with self.assertRaises(RuntimeError):
                    mac.claim_after_release(self.dev, 0, dict(session=session), lambda: None)
        self.kill.assert_not_called()
        self.claim.assert_not_called()

    def test_release_check_sends_no_protocol_before_claim_and_releases_owned_interface(self):
        for blocked in (True, False):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                dev = camera_device()
                self.claim.side_effect = usb.core.USBError('exclusive', error_code=-3) if blocked else None
                stack.enter_context(patch.object(u.usb_host, 'pyusb_backend', return_value=None))
                stack.enter_context(patch.object(usb.core, 'find', return_value=[dev]))
                stack.enter_context(patch.object(mac, 'camera_helpers', return_value=[]))
                stack.enter_context(patch('usb_diagnostics.camera_helpers', return_value=[]))
                release = stack.enter_context(patch.object(usb.util, 'release_interface'))
                dispose = stack.enter_context(patch.object(usb.util, 'dispose_resources'))
                read = stack.enter_context(patch.object(u.protocol, 'snapshot',
                    side_effect=lambda ptp, report, save: report.update(session='closed', complete=True)))
                _, report = u.inspect_camera(directory, release_macos=True)
                self.assertEqual(report['complete'], not blocked)
                if blocked:
                    read.assert_not_called()
                    release.assert_not_called()
                    self.assertEqual(report['session'], 'not_opened')
                    self.assertEqual(report['error_type'], 'USBHostError')
                else:
                    read.assert_called_once()
                    release.assert_called_once_with(dev, 0)
                dispose.assert_called_once_with(dev)
                dev.write.assert_not_called()
                self.assertEqual(report['mac_camera_release']['helpers_after'], [])


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = u.Enabler(self.temp.name)
        self.platform = patch.object(u.sys, 'platform', 'darwin')
        self.platform.start(); self.addCleanup(self.platform.stop)

    def detect(self, report=None, action=None, dev=None):
        with patch.object(u.camera, 'devices', return_value=[dev or device()]), \
             patch.object(u, 'inspect_camera', return_value=(Path('check.json'), report or denied())) as inspect:
            self.app.detect(action)
        return inspect

    def test_release_requires_click_and_is_not_inherited_by_later_checks(self):
        self.detect().assert_called_once_with(self.app.root)
        self.assertEqual(self.app.snapshot()['stage'], 'mac_usb_busy')
        self.assertIn('release_macos', self.app.snapshot()['actions'])
        self.detect().assert_not_called()  # Polling must not stop any helper.
        self.app.action('release_macos')
        action = self.app.commands.get_nowait()
        self.detect(report=snapshot(), action=action).assert_called_once_with(self.app.root, release_macos=True)
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')
        with self.assertRaises(ValueError): self.app.action('release_macos')
        self.detect(action='check').assert_called_once_with(self.app.root)

    def test_stale_attachment_or_fault_blocks_release_before_inspection(self):
        self.detect()
        with patch.object(u.camera, 'devices', return_value=[device(8)]), patch.object(u, 'inspect_camera') as inspect:
            with self.assertRaisesRegex(RuntimeError, 'connection changed'): self.app.detect('release_macos')
            inspect.assert_not_called()
        self.detect(action='check')
        self.app.workflow['fault'] = 'usb'
        with patch.object(u.camera, 'devices', return_value=[device()]), patch.object(u, 'inspect_camera') as inspect:
            with self.assertRaises(RuntimeError): self.app.detect('release_macos')
            inspect.assert_not_called()

    def test_no_release_for_configuration_denial_ptp_failure_or_other_platform(self):
        config = denied(); config['usb_access']['stage'] = 'read_configuration'
        stream = denied(); stream['session'] = 'uncertain_after_transport_error'
        for platform, report in [('darwin', config), ('linux', denied()), ('darwin', stream)]:
            with self.subTest(platform=platform, report=report), patch.object(u.sys, 'platform', platform):
                self.app.workflow.pop('fault', None)
                self.detect(report=copy.deepcopy(report), action='check')
                self.assertNotIn('release_macos', self.app.snapshot()['actions'])


if __name__ == '__main__': unittest.main()
