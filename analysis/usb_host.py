"""Host USB compatibility. No camera commands, resets, or driver detachment."""
import ctypes
import ctypes.util
from functools import lru_cache
import os
from pathlib import Path
import platform
import sys


class USBHostError(RuntimeError):
    """Host-side access failed before a camera protocol session started."""


def library_candidates():
    override = os.environ.get('FUJI_LIBUSB_PATH')
    if override:
        return [override]
    prefixes = ['/opt/homebrew', '/usr/local']
    if platform.machine() == 'x86_64': prefixes.reverse()
    paths = [f'{prefix}/opt/libusb/lib/libusb-1.0.dylib' for prefix in prefixes]
    paths += [f'{prefix}/lib/libusb-1.0.dylib' for prefix in prefixes]
    paths += ['/opt/local/lib/libusb-1.0.dylib']
    found = ctypes.util.find_library('usb-1.0')
    if found: paths.append(found)
    return list(dict.fromkeys(paths))


def missing_library():
    return USBHostError('USB library unavailable. On the Mac, run brew install libusb, then restart the companion. '
                        'Use a native Python/uv and Homebrew installation for your Mac architecture. '
                        'A custom library can be selected with FUJI_LIBUSB_PATH.')


@lru_cache(maxsize=1)
def pyusb_backend():
    if sys.platform != 'darwin': return None  # Preserve PyUSB's normal Linux discovery.
    import usb.backend.libusb1
    for path in library_candidates():
        backend = usb.backend.libusb1.get_backend(find_library=lambda name, path=path: path)
        if backend is not None: return backend
    raise missing_library()


def service_context(usb1):
    if sys.platform == 'darwin':
        for path in library_candidates():
            try:
                library = ctypes.CDLL(path, use_errno=True, use_last_error=True)
                usb1.loadLibrary(library)
                break
            except OSError:
                continue
        else: raise missing_library()
    return usb1.USBContext()


def attachment_node(bus, address):
    if sys.platform == 'darwin': return f'usb:{bus:03d}/{address:03d}'
    return f'/dev/bus/usb/{bus:03d}/{address:03d}'


def access_error(exc):
    if sys.platform == 'darwin':
        return USBHostError('macOS could not give the companion USB access. Unlock the Mac and allow the USB accessory '
                            'if prompted. Quit Photos, Image Capture and other camera apps, then use Check connection. '
                            f'Details: {exc}')
    return USBHostError(f'USB access unavailable. Close other camera apps and check device permissions. Details: {exc}')


def check_kernel_driver(dev, interface):
    import usb.core
    try:
        active = dev.is_kernel_driver_active(interface)
    except NotImplementedError:
        if sys.platform != 'darwin': raise
        return  # Older macOS backends cannot query this; exclusive claim still must succeed.
    except usb.core.USBError as exc:
        if sys.platform != 'darwin' or exc.backend_error_code != -12: raise
        return
    if active:
        raise access_error('Interface has an active kernel driver; it was left attached')
