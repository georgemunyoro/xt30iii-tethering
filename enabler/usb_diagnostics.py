"""USB ownership probe: claim/release only; no PTP commands or camera settings."""
from importlib.metadata import version
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

import enable_flow  # Set up the shared analysis imports.
from local_state import atomic_json, now
import ptp_raw_snapshot as protocol
import usb_host


CAMERA_HELPERS = frozenset(('PTPCamera', 'ptpcamerad', 'mscamerad', 'mscamerad-xpc'))


def camera_helpers():
    if sys.platform != 'darwin': return []
    # Only retain known camera helper identities, not command arguments
    # or details of unrelated applications. Presence alone does not prove ownership.
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,uid=,comm='],
                            capture_output=True, text=True, timeout=5, check=True)
    helpers = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3: continue
        pid, uid, executable = fields
        name = executable.rsplit('/', 1)[-1]
        if name in CAMERA_HELPERS and pid.isdecimal() and uid.isdecimal():
            helpers.append(dict(name=name, pid=int(pid), uid=int(uid), executable=executable))
    return helpers


def diagnose(root):
    folder = Path(root)/'diagnostics'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder/f'usb-{uuid.uuid4().hex}.json'
    report = dict(kind='usb_ownership_diagnostic_v1', started=now(), complete=False,
                  session='not_opened', commands=[],
                  host=dict(platform=sys.platform, os_version=platform.mac_ver()[0]
                            if sys.platform == 'darwin' else platform.release(),
                            architecture=platform.machine(), python=platform.python_version(),
                            pyusb=version('pyusb')))
    def save(): atomic_json(path, report)
    def helpers(key):
        try: report[key] = camera_helpers()
        except (OSError, subprocess.SubprocessError) as exc:
            report[key] = dict(error=str(exc))
        save()
    def claimed(_ptp, row, checkpoint):
        row['complete'] = True
        checkpoint()
    save()
    helpers('camera_helpers_before')
    # Set before backend initialization. Native USBInterfaceOpen errors go to
    # the launching terminal; the JSON preserves our stage and libusb error code.
    os.environ.setdefault('LIBUSB_DEBUG', '3')
    try:
        backend = usb_host.pyusb_backend()
        library = getattr(getattr(backend, 'lib', None), '_name', None)
        if library:
            report['host']['libusb_library'] = str(library)
            report['host']['libusb_resolved_path'] = str(Path(library).resolve())
        save()
        protocol.connect_and_snapshot(report, save, snapshot_fn=claimed)
        report['interface_released'] = True
    except Exception as exc:
        report.update(complete=False, error=f'{type(exc).__name__}: {exc}')
    finally:
        helpers('camera_helpers_after')
        report['finished'] = now()
        save()
    return path, report
