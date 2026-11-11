# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Query only identification strings through the camera's ff80 interface.

Uses reviewed fffw transport and identity methods at commit
ccee949884a6dad10095231441e4d608c10e59f0 (MIT):
https://github.com/tiredboffin/fffw

Opens/closes a diagnostic transport session and requests six text tags.
No debug enable, RAM/configuration writes, firmware commands, resets,
status-reset tags, or calibration functions are invoked.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import usb1

UPSTREAM = Path(__file__).resolve().parent / "tools" / "fffw" / "ff80"
sys.path.insert(0, str(UPSTREAM))
import fftlib
import ffjlib


def identify():
    with usb1.USBContext() as context:
        devices = [d for d in context.getDeviceList()
                   if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
        if len(devices) != 1:
            raise RuntimeError(f"Expected one 04cb:ff80 device; found {len(devices)}")
        device = devices[0]
        node = f"/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}"
        try:
            handle = device.open()
        except usb1.USBErrorAccess:
            raise RuntimeError(f"Linux USB access denied for {node}; no service commands sent") from None
        try:
            with handle.claimInterface(0):
                transport = fftlib.ftl(handle, EP_IN=0x81, EP_OUT=0x01)
                transport.ms = 3000
                jig = ffjlib.jig(transport)
                fields = {}
                try:
                    transport.open_session()
                    for tag, name in ((1, "vendor"), (2, "manufacturer"),
                                      (4, "product"), (8, "version_tag_08"),
                                      (0x10, "device_type"), (0x40, "framework")):
                        fields[name] = jig.get_device_info(tag)
                finally:
                    if transport.sid is not None:
                        transport.close_session()
                return {
                    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                    "usb_id": "04cb:ff80",
                    "device_node": node,
                    "source_commit": "ccee949884a6dad10095231441e4d608c10e59f0",
                    "query": "0x150000 identification tags 1, 2, 4, 8, 0x10, 0x40",
                    "identification": fields,
                }
        finally:
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = identify()
    except (RuntimeError, usb1.USBError, ffjlib.jig_exception) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
