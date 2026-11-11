"""Enumerate Fujifilm attachments without claiming a USB interface."""
import usb.core
import usb_host


def devices():
    return [dict(product=d.idProduct, bus=d.bus, address=d.address,
                 node=usb_host.attachment_node(d.bus, d.address))
            for d in usb.core.find(find_all=True, idVendor=0x04CB, backend=usb_host.pyusb_backend())]
