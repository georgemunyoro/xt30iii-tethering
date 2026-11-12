"""Explicit, one-time release of the current user's Apple camera helpers."""
import os
import signal
import sys
import time

import usb.core
import usb.util
from usb_diagnostics import CAMERA_HELPERS, camera_helpers


def claim_after_release(dev, interface, report, save):
    if sys.platform != 'darwin' or report.get('session') != 'not_opened':
        raise RuntimeError('Mac camera release requires a fresh macOS USB check')
    uid = os.getuid()
    if uid == 0: raise RuntimeError('Run the utility as your normal Mac login, without sudo')
    helpers = camera_helpers()
    attempt = report['mac_camera_release'] = dict(helpers_before=helpers, signals=[], claim_attempts=0)
    save()
    # Stop only helpers seen at the start, never new processes launched by macOS.
    # Check the UID and system executable again immediately before each signal.
    for helper in helpers:
        executable = helper.get('executable', '')
        if (helper['name'] not in CAMERA_HELPERS or helper['uid'] != uid or helper['pid'] <= 1
                or helper['pid'] == os.getpid()
                or not executable.startswith(('/usr/libexec/', '/System/Library/'))
                or executable.rsplit('/', 1)[-1] != helper['name']):
            continue
        if helper not in camera_helpers(): continue
        row = dict(pid=helper['pid'], name=helper['name'], signal='SIGKILL', result='requested')
        attempt['signals'].append(row)
        save()
        try:
            os.kill(helper['pid'], signal.SIGKILL)
            row['result'] = 'sent'
        except ProcessLookupError:
            row['result'] = 'already_exited'
        except OSError as exc:
            row['result'] = str(exc)
            save()
            raise
    save()
    # SIGKILL is asynchronous. Allow up to half a second for interface release,
    # retrying only the host claim; no PTP command is sent before a claim succeeds.
    for index in range(10):
        attempt['claim_attempts'] = index + 1
        try:
            usb.util.claim_interface(dev, interface)
            return  # The caller records ownership and persists the report next.
        except usb.core.USBError as exc:
            if exc.backend_error_code not in (-3, -6) or index == 9: raise
            time.sleep(0.05)
