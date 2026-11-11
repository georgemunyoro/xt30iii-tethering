# /// script
# requires-python = ">=3.11"
# dependencies = ["pyusb==1.3.1"]
# ///
"""Read this camera's standard PTP DeviceInfo, without opening a session.

The only bulk OUT command is GetDeviceInfo (0x1001). No configuration,
property, firmware, storage, memory, or service-mode writes are issued.
USB strings and protocol layout follow USB/PTP and libgphoto2's ptp.h and
ptp-pack.c. The camera's serial number is parsed but omitted from output.
Run: uv run analysis/ptp_device_info.py --output analysis/usb-device-info.json
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

import usb.core
import usb.util


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError("Truncated DeviceInfo")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def number(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def string(self):
        return self.take(self.number("B") * 2).decode("utf-16-le").rstrip("\0")

    def codes(self):
        count = self.number("I")
        if count > (len(self.data) - self.offset) // 2:
            raise ValueError("Invalid DeviceInfo array length")
        return [f"0x{self.number('H'):04x}" for _ in range(count)]


def parse_info(data):
    r = Reader(data)
    result = {
        "standard_version": r.number("H"),
        "vendor_extension_id": r.number("I"),
        "vendor_extension_version": r.number("H"),
        "vendor_extension_description": r.string(),
        "functional_mode": r.number("H"),
    }
    for key in ("operations", "events", "properties", "capture_formats", "image_formats"):
        result[key] = r.codes()
    for key in ("manufacturer", "model", "firmware_version", "serial_number"):
        value = r.string()
        if key != "serial_number":
            result[key] = value
    if r.offset != len(data):
        raise ValueError("Unexpected trailing DeviceInfo bytes")
    return result


def inspect():
    matches = list(usb.core.find(find_all=True, idVendor=0x04CB, idProduct=0x0317))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one 04cb:0317 camera; found {len(matches)}")
    dev = matches[0]
    claimed = False
    try:
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) != (6, 1, 1):
            raise RuntimeError("Interface 0 is not standard USB PTP")
        if dev.is_kernel_driver_active(0):
            raise RuntimeError("Interface has a kernel driver; leaving it attached")
        endpoints = {ep.bEndpointAddress: ep for ep in intf}
        for address in (0x01, 0x81):
            if address not in endpoints or usb.util.endpoint_type(endpoints[address].bmAttributes) != 2:
                raise RuntimeError("Expected PTP bulk endpoints are missing")
        usb.util.claim_interface(dev, 0)
        claimed = True
        # PTP/USB: length=12, container=command, GetDeviceInfo, transaction=0.
        command = struct.pack("<IHHI", 12, 1, 0x1001, 0)
        if dev.write(0x01, command, timeout=3000) != len(command):
            raise RuntimeError("Incomplete GetDeviceInfo command transfer")
        pending = bytearray()
        data = None
        total = 0
        for _ in range(32):
            chunk = bytes(dev.read(0x81, 65536, timeout=3000))
            total += len(chunk)
            if total > 1024 * 1024:
                raise RuntimeError("DeviceInfo response exceeds bound")
            pending.extend(chunk)
            while len(pending) >= 12:
                size, kind, code, transaction = struct.unpack_from("<IHHI", pending)
                if not 12 <= size <= 1024 * 1024 or transaction != 0:
                    raise RuntimeError("Unexpected PTP container header")
                if len(pending) < size:
                    break
                payload = bytes(pending[12:size])
                del pending[:size]
                if kind == 2 and code == 0x1001 and data is None:
                    data = payload
                elif kind == 3:
                    if code != 0x2001 or data is None or payload or pending:
                        raise RuntimeError(f"Unexpected PTP response 0x{code:04x}")
                    return {
                        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                        "usb_id": "04cb:0317",
                        "usb_bus": dev.bus,
                        "usb_address": dev.address,
                        "usb_bcd_device": f"0x{dev.bcdDevice:04x}",
                        "interface": "PTP (06/01/01)",
                        "endpoints": [f"0x{x:02x}" for x in endpoints],
                        "query": "GetDeviceInfo (0x1001), outside a session",
                        "response": "OK (0x2001)",
                        "device_info": parse_info(data),
                    }
                else:
                    raise RuntimeError("Unexpected PTP data or container type")
        raise RuntimeError("GetDeviceInfo did not finish within transfer bound")
    finally:
        try:
            if claimed:
                usb.util.release_interface(dev, 0)
        finally:
            usb.util.dispose_resources(dev)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(inspect(), indent=2) + "\n"
    if args.output:
        args.output.write_text(result)
    print(result, end="")
