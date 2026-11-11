"""Host USB compatibility; Windows uses temporary UsbDk redirection.

No camera commands, resets, permanent driver replacement, or driver installs.
"""
import ctypes
import ctypes.util
from functools import lru_cache
import os
from pathlib import Path
import platform
import re
import sys


class USBHostError(RuntimeError):
    """Host-side access failed before a camera protocol session started."""


WINDOWS_HELP = ('Windows USB access needs UsbDk (x64). Install the official UsbDk runtime '
                'linked in the Windows setup guide, then restart this utility. '
                'Close FUJIFILM TETHER APP, Photos, and other camera apps. '
                'Keep the camera\'s existing driver; do not replace it with Zadig.')


@lru_cache(maxsize=1)
def windows_library():
    """Load one pinned libusb DLL for both the PTP and service transports."""
    try:
        import libusb_package
        path = libusb_package.get_library_path()
        if not path: raise OSError('libusb-package did not supply a DLL')
        return ctypes.CDLL(str(path), use_errno=True, use_last_error=True)
    except (ImportError, OSError) as exc:
        raise USBHostError('Windows USB library unavailable. Start with Start Tether Enabler.cmd '
                           f'to install the Python dependencies. Details: {exc}') from exc


def use_usbdk(backend):
    """Select UsbDk before any enumeration; never fall back to another driver."""
    option = backend.lib.libusb_set_option
    option.argtypes = [ctypes.c_void_p, ctypes.c_int]
    option.restype = ctypes.c_int
    result = option(backend.ctx, 1)  # LIBUSB_OPTION_USE_USBDK
    if result:
        raise USBHostError(f'{WINDOWS_HELP} libusb returned {result}.')


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
    import usb.backend.libusb1
    if sys.platform == 'win32':
        library = windows_library()
        backend = usb.backend.libusb1.get_backend(find_library=lambda _: library._name)
        if backend is None: raise USBHostError(WINDOWS_HELP)
        use_usbdk(backend)
        return backend
    if sys.platform != 'darwin': return None  # Preserve PyUSB's normal Linux discovery.
    for path in library_candidates():
        backend = usb.backend.libusb1.get_backend(find_library=lambda name, path=path: path)
        if backend is not None: return backend
    raise missing_library()


def service_context(usb1):
    if sys.platform == 'win32':
        if not usb1.loadLibrary(windows_library()):
            raise USBHostError('Another USB library was already loaded. Restart the utility.')
        return usb1.USBContext(use_usbdk=True)
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
    if sys.platform == 'win32':
        node = f'usb:{bus:03d}/{address:03d}'
        token = os.environ.get('FUJI_USB_ATTACHMENT_TOKEN')
        if token:
            if not re.fullmatch('[0-9a-f]{32}', token): raise USBHostError('Invalid USB attachment token')
            node += '#' + token
        return node
    if sys.platform == 'darwin': return f'usb:{bus:03d}/{address:03d}'
    return f'/dev/bus/usb/{bus:03d}/{address:03d}'


def access_error(exc):
    if sys.platform == 'win32':
        return USBHostError(f'{WINDOWS_HELP} Details: {exc}')
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
        if sys.platform not in ('darwin', 'win32'): raise
        return  # macOS/Windows may not support this query; the exclusive claim must still succeed.
    except usb.core.USBError as exc:
        if sys.platform not in ('darwin', 'win32') or exc.backend_error_code != -12: raise
        return
    if active:
        raise access_error('Interface has an active kernel driver; it was left attached')
