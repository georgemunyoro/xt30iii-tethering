#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyusb==1.3.1"]
# ///
"""Read X-T30 III 1.10 USB capabilities and two mode properties.

Default: print the plan without opening USB. --execute writes a new report.
Only GetDeviceInfo, OpenSession, GetDevicePropDesc/GetDevicePropValue for
D041/D16E, and CloseSession are permitted. No property/data OUT, capture,
storage, service, reset, kernel-driver detach or firmware-update commands.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import time

import usb.core
import usb.util
import usb_host

from ptp_device_info import Reader, parse_info

PROPERTIES = (0xD16E, 0xD041)
OK = 0x2001
LIMIT = 1024 * 1024
DEFAULT_REPORT = Path(__file__).with_name('raw-usb-snapshot.json')


def validate_command(op, params):
    valid = ((op in (0x1001, 0x1003) and params == ()) or
             (op == 0x1002 and params == (1,)) or
             (op in (0x1014, 0x1015) and len(params) == 1 and params[0] in PROPERTIES))
    if not valid:
        raise ValueError('Command is outside the read-only snapshot allowlist')


class PTP:
    data_operations = frozenset((0x1001, 0x1014, 0x1015))
    transfer_limit, byte_limit, deadline_seconds = 32, LIMIT, 12

    def __init__(self, dev, out_endpoint, in_endpoint):
        self.dev, self.out_endpoint, self.in_endpoint = dev, out_endpoint, in_endpoint
        self.healthy = True
        self.requests = []

    def validate(self, op, params):
        validate_command(op, params)

    def read_timeout(self, op):
        return 3000

    def validate_data(self, op, params, data):
        if data is not None: raise RuntimeError('Data OUT is not permitted')

    def exchange(self, op, params, transaction, *, out_data=None):
        self.validate(op, params)
        self.validate_data(op, params, out_data)
        if not self.healthy:
            raise RuntimeError('PTP stream is uncertain; no further commands permitted')
        request = dict(operation=f'0x{op:04x}', parameters=list(params), transaction=transaction)
        if out_data is not None: request['out_data_hex'] = out_data.hex()
        self.requests.append(request)
        command = struct.pack('<IHHI', 12 + len(params)*4, 1, op, transaction)
        command += b''.join(struct.pack('<I', p) for p in params)
        try:
            if self.dev.write(self.out_endpoint, command, timeout=3000) != len(command):
                raise RuntimeError('Incomplete PTP command transfer')
            if out_data is not None:
                outgoing = struct.pack('<IHHI', 12+len(out_data), 2, op, transaction)+out_data
                if self.dev.write(self.out_endpoint, outgoing, timeout=3000) != len(outgoing):
                    raise RuntimeError('Incomplete PTP data transfer')
            pending, payload, total = bytearray(), None, 0
            deadline = time.monotonic() + self.deadline_seconds
            for _ in range(self.transfer_limit):
                if time.monotonic() >= deadline:
                    raise RuntimeError('PTP transaction exceeded time bound')
                chunk = bytes(self.dev.read(self.in_endpoint, 65536, timeout=self.read_timeout(op)))
                total += len(chunk)
                if total > self.byte_limit:
                    raise RuntimeError('PTP transaction exceeded size bound')
                pending.extend(chunk)
                while len(pending) >= 12:
                    size, kind, code, tx = struct.unpack_from('<IHHI', pending)
                    if not 12 <= size <= self.byte_limit or tx != transaction:
                        raise RuntimeError('Unexpected PTP container header')
                    if len(pending) < size:
                        break
                    data = bytes(pending[12:size])
                    del pending[:size]
                    if kind == 2 and code == op and payload is None and op in self.data_operations:
                        payload = data
                    elif kind == 3:
                        # Preserve bounded diagnostics before rejecting a
                        # response. Vendor-defined errors are valid PTP
                        # responses too, and end a matched transaction.
                        request['response_container'] = dict(size=size, code=hex(code), transaction=tx,
                            parameters_hex=data[:20].hex(), pending_length=len(pending),
                            pending_prefix_hex=bytes(pending[:64]).hex())
                        if pending or len(data) > 20 or len(data) % 4 or not (
                                0x2000 <= code <= 0x2FFF or 0xA000 <= code <= 0xAFFF):
                            raise RuntimeError('Unexpected PTP response layout')
                        if code == OK and op in self.data_operations and payload is None:
                            raise RuntimeError('Successful read returned no data container')
                        if code == OK and data:
                            raise RuntimeError('Unexpected successful response parameters')
                        request['response'] = f'0x{code:04x}'
                        if payload is not None:
                            request['data_length'] = len(payload)
                        # A matched, fully consumed error response ends the
                        # transaction even if the device first sent data. Do
                        # not decode that data as a successful property read.
                        if code != OK and payload is not None:
                            request['discarded_error_data'] = True
                            payload = None
                        return code, payload
                    else:
                        raise RuntimeError('Unexpected PTP container type or operation')
            raise RuntimeError('PTP transaction exceeded transfer bound')
        except BaseException:
            self.healthy = False
            raise


def parse_u16_descriptor(data, prop):
    r = Reader(data)
    if r.number('H') != prop or r.number('H') != 4:
        raise ValueError('Unexpected property identity or data type')
    result = dict(type='uint16', access=r.number('B'), default=r.number('H'),
                  current=r.number('H'), form=r.number('B'))
    if result['form'] == 1:
        result['range'] = [r.number('H') for _ in range(3)]
    elif result['form'] == 2:
        count = r.number('H')
        if count > 256:
            raise ValueError('Property enumeration exceeds bound')
        result['values'] = [r.number('H') for _ in range(count)]
    elif result['form'] != 0:
        raise ValueError('Unexpected property form')
    if r.offset != len(data):
        raise ValueError('Unexpected trailing property bytes')
    return result


def snapshot(ptp, report, save):
    """Read identity first; own a fresh session or stop without taking one over."""
    report['commands'] = ptp.requests
    report['complete'] = False
    code, data = ptp.exchange(0x1001, (), 0)
    if code != OK:
        raise RuntimeError(f'GetDeviceInfo failed: {code:#06x}')
    info = parse_info(data)
    report['device_info'] = info  # parse_info deliberately discards the serial.
    save()
    if (info['manufacturer'], info['model'], info['firmware_version']) != ('FUJIFILM', 'X-T30 III', '1.10'):
        raise RuntimeError('Camera identity/version does not match X-T30 III 1.10')
    ops = set(info['operations'])
    if not {'0x1002', '0x1003', '0x1014', '0x1015'} <= ops:
        raise RuntimeError('Required session/read operations are not advertised')
    owned, transaction = False, 0
    report['session'] = 'opening'
    save()
    try:
        code, _ = ptp.exchange(0x1002, (1,), 0)
        if code != OK:
            report['session'] = 'not_acquired'
            raise RuntimeError(f'OpenSession failed: {code:#06x}; existing sessions are left alone')
        owned = True
        report['session'] = 'owned'
        report['properties'] = {}
        save()
        for prop in PROPERTIES:
            key = f'0x{prop:04x}'
            if key not in info['properties']:
                report['properties'][key] = dict(skipped='not advertised')
                save()
                continue
            row = report['properties'][key] = {}
            for op, kind in ((0x1014, 'descriptor'), (0x1015, 'value')):
                transaction += 1
                code, data = ptp.exchange(op, (prop,), transaction)
                row[kind] = dict(response=f'0x{code:04x}')
                save()
                if code == OK:
                    if kind == 'descriptor':
                        row[kind]['decoded'] = parse_u16_descriptor(data, prop)
                    else:
                        if len(data) != 2:
                            raise ValueError('Unexpected U16 property value length')
                        row[kind]['decoded'] = struct.unpack('<H', data)[0]
                elif code not in (0x2002, 0x2005, 0x200A):
                    raise RuntimeError(f'Unexpected property-read response: {code:#06x}')
                save()
        usb_mode = report['properties'].get('0xd16e', {}).get('value', {}).get('decoded')
        report['observations'] = dict(
            usb_mode_value=usb_mode, raw_mode_confirmed=usb_mode == 6,
            advertises_initiate_capture='0x100e' in ops,
            operation_count=len(info['operations']), property_count=len(info['properties']),
            tethering_tested=False)
        report['complete'] = True
    finally:
        if owned and ptp.healthy:
            report['session'] = 'closing'
            try:
                code, _ = ptp.exchange(0x1003, (), transaction + 1)
                if code != OK:
                    raise RuntimeError(f'CloseSession failed: {code:#06x}')
                report['session'] = 'closed'
            except BaseException:
                report['session'] = 'close_unconfirmed'
                report['complete'] = False
                raise
            finally:
                save()
        elif report.get('session') in ('opening', 'owned'):
            report['session'] = 'uncertain_after_transport_error'
            report['complete'] = False
            save()


def connect_and_snapshot(report, save, *, snapshot_fn=snapshot, ptp_class=PTP):
    matches = list(usb.core.find(find_all=True, idVendor=0x04CB, backend=usb_host.pyusb_backend()))
    if len(matches) != 1 or matches[0].idProduct == 0xFF80:
        raise RuntimeError('Expected one Fujifilm camera in normal PTP mode, not service mode')
    dev, claimed, interface = matches[0], False, None
    protocol_started = False
    try:
        cfg = dev.get_active_configuration()
        interfaces = list(cfg)
        report['usb'] = dict(id=f'{dev.idVendor:04x}:{dev.idProduct:04x}',
            bus=dev.bus, address=dev.address, bcd_device=f'0x{dev.bcdDevice:04x}',
            configuration=cfg.bConfigurationValue,
            interfaces=[dict(number=i.bInterfaceNumber, alternate=i.bAlternateSetting,
                class_code=i.bInterfaceClass, subclass=i.bInterfaceSubClass,
                protocol=i.bInterfaceProtocol,
                endpoints=[dict(address=f'0x{ep.bEndpointAddress:02x}', attributes=ep.bmAttributes,
                                max_packet=ep.wMaxPacketSize) for ep in i]) for i in interfaces])
        save()
        candidates = [i for i in interfaces if i.bAlternateSetting == 0 and
                      (i.bInterfaceClass, i.bInterfaceSubClass, i.bInterfaceProtocol) == (6, 1, 1)]
        if len(candidates) != 1:
            raise RuntimeError('Expected exactly one PTP interface at alternate setting zero')
        intf = candidates[0]
        interface = intf.bInterfaceNumber
        usb_host.check_kernel_driver(dev, interface)
        bulk = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == 2]
        incoming = [e.bEndpointAddress for e in bulk if e.bEndpointAddress & 0x80]
        outgoing = [e.bEndpointAddress for e in bulk if not e.bEndpointAddress & 0x80]
        if len(incoming) != 1 or len(outgoing) != 1:
            raise RuntimeError('Expected one bulk endpoint in each direction')
        usb.util.claim_interface(dev, interface)
        claimed = True
        protocol_started = True
        snapshot_fn(ptp_class(dev, outgoing[0], incoming[0]), report, save)
    except usb.core.USBError as exc:
        if not protocol_started: raise usb_host.access_error(exc) from exc
        raise
    finally:
        try:
            if claimed:
                usb.util.release_interface(dev, interface)
        finally:
            usb.util.dispose_resources(dev)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if not args.execute:
        print('Plan only; no USB access.')
        print('Camera: X-T30 III 1.10, normally started in USB RAW CONV./BACKUP RESTORE.')
        print('Read USB descriptors and DeviceInfo; open a fresh session; read advertised')
        print('D16E/D041 descriptors and values; close the session. No settings writes.')
        print(f'Execute with --execute; report must not already exist: {args.output}')
        return
    # Reserve the output before touching USB; never overwrite an existing run.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    report = dict(started_at_utc=datetime.now(timezone.utc).isoformat(),
                  scope='Read-only capabilities/mode snapshot; no tether activation', complete=False)
    def save():
        temporary = args.output.with_name(args.output.name + '.tmp')
        with open(temporary, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    save()
    try:
        connect_and_snapshot(report, save)
    except BaseException as error:
        report['complete'] = False
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps(dict(report=str(args.output), complete=report['complete'],
                          session=report.get('session'), observations=report.get('observations')), indent=2))


if __name__ == '__main__':
    main()
