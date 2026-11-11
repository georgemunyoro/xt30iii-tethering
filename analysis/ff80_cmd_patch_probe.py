# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Fixed, staged RAM experiment through the writable legacy CMD callback.

Default is plan only. Each live phase requires the preceding successful
private journal, fresh original-code checks and live MMU permission checks.
callback: original no-op through CMD, restore NULL. cache: original cache
helpers, restore NULL. marker: hello return byte 0 -> 7 -> 0 with cache
synchronization and execution/readback checks. No mode/key/save/flash API.
An uncertain USB transfer stops ALL follow-up traffic, including cleanup.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time

import ff80_console_probe as p
from ff80_hello_recovery_snapshot import ROOT_POINTER, POOL_START, POOL_END, walk

KIND = 'ff80_cmd_patch_probe_v1'
CMD_SLOT, NOOP = 0x0223A7E8, 0x014CF220
CLEAN, ICACHE, FLAGS = 0x015E80D0, 0x0009C8B0, 0x024C1198
HELLO, MARKER, RO_POINTER = 0x03273E78, 0x03273E8C, 0x038604C8
PAYLOAD = b'CMD CACHE\n'
CMD = struct.pack('<9I', 0x200001, 0, 0, 0, 5, 0, 0, 0, len(PAYLOAD)) + PAYLOAD
PHASES = ('callback', 'cache', 'marker')
EXTRA_SAMPLES = (
    ('memory_dispatch', 0x0307C2C8, 0x100),
    ('cmd_dispatch', 0x0307C3C8, 0x100),
    ('memory_write_dispatch', 0x0307C4C8, 0xD4),
    ('response_serializer', 0x0307B26C, 32),
    ('console_worker', 0x03272F1C, 0x50),
    ('cmd_parser', 0x014508E0, 0x100),
    ('cmd_parser_tail', 0x014509E0, 0x2C),
    ('cmd_string_copy', 0x015E888C, 0x44),
    ('no_op', NOOP, 8),
    ('cmd_token', 0x018031B0, 4),
)
READS = p.PATCH_READS | {(a, n) for _, a, n in EXTRA_SAMPLES} | {
    (CMD_SLOT, 8), (CMD_SLOT, 16), (MARKER, 1), (RO_POINTER, 8), (ROOT_POINTER, 8)}
TARGETS = (('cmd_callback', CMD_SLOT, True, False),
           ('hello_code', HELLO, True, True), ('hello_table', RO_POINTER, False, False),
           ('no_op', NOOP, None, True), ('data_clean', CLEAN, None, True),
           ('icache', ICACHE, None, True))


def write_request(address, data):
    return struct.pack('<9I', 0x200001, 0, 0, 0, 3, 0, 0, address, len(data)) + data


def allowed_writes(phase):
    targets = (0, NOOP) if phase == 'callback' else (0, NOOP, CLEAN, ICACHE)
    writes = {(CMD_SLOT, struct.pack('<Q', value)) for value in targets}
    if phase == 'marker': writes |= {(MARKER, b'\0'), (MARKER, b'\xe0')}
    return writes


def validate(request, reads, phase):
    request = bytes(request)
    if request == CMD: return 'cmd'
    if len(request) >= 36 and request[:28] == struct.pack('<7I', 0x200001, 0, 0, 0, 3, 0, 0):
        address, length = struct.unpack('<II', request[28:36])
        if length == len(request)-36 and (address, request[36:]) in allowed_writes(phase):
            return 'ram'
        raise RuntimeError('Write outside fixed phase allowlist')
    p.validate_request(request, reads, console=True)
    return 'console' if request in (p.HELLO, p.POLL) else 'preflight'


class TracedUSB:
    """Journal the exact transfer boundary, including failures inside ftl."""
    def __init__(self, device, transport): self.device, self.t = device, transport

    def transfer(self, method, *args, **kwargs):
        if not self.t.healthy: raise RuntimeError('USB fenced')
        record = dict(phase=method, complete=False)
        if method == 'controlWrite': record['request_code'] = hex(args[-2][0])
        self.t.r['last_io'] = record
        self.t.save()
        try:
            result = getattr(self.device, method)(*args, **kwargs)
            record.update(complete=True, transferred=result if isinstance(result, int) else len(result))
            self.t.save()
            return result
        except BaseException as exc:
            self.t.healthy = False
            record['error'] = f'{type(exc).__name__}: {exc}'
            self.t.save()
            raise

    def controlWrite(self, *a, **kw): return self.transfer('controlWrite', *a, **kw)
    def bulkWrite(self, *a, **kw): return self.transfer('bulkWrite', *a, **kw)
    def bulkRead(self, *a, **kw): return self.transfer('bulkRead', *a, **kw)


class Transport(p.fftlib.ftl):
    command_limit = 12
    table_limit = 18
    def __init__(self, device, phase, report, checkpoint):
        self.r, self.save, self.phase = report, checkpoint, phase
        self.healthy, self.armed, self.pending, self.awaiting = True, False, False, False
        self.polls = self.submissions = self.commands = self.table_reads = 0
        self.reads = set(READS)
        self.last_request = None
        super().__init__(TracedUSB(device, self), EP_IN=0x81, EP_OUT=1)
        self.ms = 3000

    def validate(self, request):
        return validate(request, self.reads, self.phase)

    def write_cmd(self, request):
        request = bytes(request)
        kind = self.validate(request)
        if not self.healthy or self.awaiting: raise RuntimeError('USB uncertain or response outstanding')
        if kind in ('cmd', 'ram', 'console') and not self.armed:
            raise RuntimeError('Live prerequisites not complete')
        if kind in ('cmd', 'ram') and self.pending:
            raise RuntimeError('Console worker still outstanding')
        if request == CMD:
            if self.commands >= self.command_limit: raise RuntimeError('CMD submission bound reached')
            self.commands += 1
        if request == p.HELLO:
            if self.pending or self.submissions >= 4: raise RuntimeError('Unexpected hello resubmission')
            self.pending, self.polls = True, 0
            self.submissions += 1
        elif request == p.POLL:
            if not self.pending or self.polls >= 40: raise RuntimeError('Poll outside bounded worker transaction')
            self.polls += 1
        self.last_request, self.awaiting = request, True
        self.r['last_request_hex'] = request.hex()
        self.save()
        try: return super().write_cmd(request)
        except BaseException:
            self.healthy = False
            raise

    def read_cmd(self, size=256):
        if not self.healthy or not self.awaiting: raise RuntimeError('No safe response pending')
        try:
            response = bytes(super().read_cmd(size))
            request = self.last_request
            if len(response) != size: raise RuntimeError('Short service response')
            if request[:4] in (struct.pack('<I', 0x200001), struct.pack('<I', 0x1A0002)):
                if response[:4] != request[:4] or response[12:20] != request[12:20]:
                    raise RuntimeError('Malformed service response header')
                function = struct.unpack('<I', request[16:20])[0]
                if response[4:12] == bytes(8):
                    if function in (2, 3) and response[20:36] != request[20:36]:
                        raise RuntimeError('Memory response address/length mismatch')
                    if request == CMD and response[32:36] != struct.pack('<I', len(PAYLOAD)):
                        raise RuntimeError('CMD response length mismatch')
            self.awaiting = False
            return response
        except BaseException:
            self.healthy = False
            raise

    def table_entry(self, address):
        if address % 8 or not POOL_START <= address <= POOL_END-8 or self.table_reads >= self.table_limit:
            raise RuntimeError('Page-table read outside fixed pool/count bound')
        self.table_reads += 1
        self.reads.add((address, 8))
        return struct.unpack('<Q', p.read_fixed(self, address, 8))[0]


class Experiment:
    def __init__(self, jig, transport, report, checkpoint, sleep=time.sleep):
        self.jig, self.t, self.r, self.save, self.sleep = jig, transport, report, checkpoint, sleep

    def idle(self):
        return p.verify_idle({name: p.read_fixed(self.t, a, n) for name, a, n in p.STATES})

    def allowed_writes(self):
        return allowed_writes(self.t.phase)

    def memory(self, address, data):
        if (address, data) not in self.allowed_writes(): raise RuntimeError('Write outside phase')
        before = p.read_fixed(self.t, address, len(data))
        if (address, before) not in self.allowed_writes():
            raise RuntimeError('Unexpected memory value; refusing overwrite')
        if before == data: return
        record = dict(address=hex(address), before=before.hex(), after=data.hex(), verified=False)
        self.r.setdefault('writes', []).append(record)
        self.r['ram_restore_required'] = True
        if address == MARKER:
            self.r.update(code_write_attempted=True, code_restore_verified=False)
        self.save()  # Durable restoration obligation BEFORE the mutation.
        self.t.write_cmd(write_request(address, data))
        reply = self.t.read_cmd(36)
        if reply[4:12] != bytes(8): raise RuntimeError('RAM write rejected')
        if p.read_fixed(self.t, address, len(data)) != data: raise RuntimeError('RAM write readback mismatch')
        record['verified'] = True
        self.save()

    def cmd(self, target):
        self.idle()
        if p.read_fixed(self.t, CMD_SLOT, 16) != bytes(16): raise RuntimeError('CMD/KEY callback is not NULL')
        self.memory(CMD_SLOT, struct.pack('<Q', target))
        record = dict(target=hex(target), attempted=True, returned=False)
        self.r.setdefault('commands', []).append(record)
        self.save()
        try:
            self.t.write_cmd(CMD)
            reply = self.t.read_cmd(36)
            record['response_hex'] = reply.hex()
            record['returned'] = True
            self.save()
            if reply[4:12] != bytes(8): raise RuntimeError('CMD callback rejected')
        finally:
            if self.t.healthy and not self.t.awaiting:
                self.memory(CMD_SLOT, bytes(8))
        if target == CLEAN:
            flags = list(struct.unpack('<4I', p.read_fixed(self.t, FLAGS, 16)))
            record['completion_flags'] = flags
            self.save()
            if flags != [1]*4:
                self.r['cache_failure'] = True
                self.save()
                raise RuntimeError('Protocol OK does not establish cache success: incomplete CPU flags')

    def synchronize(self):
        for target in (CLEAN, ICACHE, CLEAN): self.cmd(target)

    def hello(self, expected_status):
        self.idle()
        record = dict(attempted=True, complete=False, expected_status=expected_status, polls=[])
        self.r.setdefault('hellos', []).append(record)
        self.save()
        reply = p.exchange(self.t, p.HELLO, 6)
        if struct.unpack('<I', reply[20:24])[0] != 6: raise RuntimeError('Partial hello; never resubmit')
        output = bytearray()
        for _ in range(40):
            reply = p.exchange(self.t, p.POLL, 7)
            busy, status, length = struct.unpack('<III', reply[20:32])
            if busy not in (0, 1) or length > 255 or reply[32+length] != 0:
                self.t.healthy = False
                raise RuntimeError('Malformed console poll; USB fenced')
            output.extend(reply[32:32+length])
            record['polls'].append(dict(busy=busy, status=status, length=length))
            record['output_hex'] = output.hex()
            self.save()
            if len(output) > 256: raise RuntimeError('Excessive console output')
            if not busy:
                self.t.pending = False
                record.update(complete=True, status=status)
                self.save()
                self.idle()
                if (status, bytes(output)) != (expected_status, b'Hello, world!\n'):
                    raise RuntimeError('Hello execution result mismatch')
                return
            self.sleep(0.1)
        raise RuntimeError('Hello worker did not finish; no further execution')

    def check_mappings(self):
        root = struct.unpack('<Q', p.read_fixed(self.t, ROOT_POINTER, 8))[0]
        self.r['mappings'] = {}
        for name, address, writable, executable in TARGETS:
            result = walk(root, address, self.t.table_entry)
            self.r['mappings'][name] = result
            self.save()
            if (not result['mapped'] or not result['access_flag'] or result['physical_address'] != hex(address)
                or (writable is not None and result['el1_writable'] != writable)
                or result['privileged_execute_never'] == executable):
                raise RuntimeError(f'Unexpected live mapping: {name}')

    def restore_ram(self, expected):
        self.idle()
        self.memory(CMD_SLOT, bytes(8))
        if self.r.get('code_write_attempted'):
            self.memory(MARKER, b'\0')
            if self.r.get('cache_failure'): raise RuntimeError('Cache failed; code execution restoration unverified')
            self.synchronize()
            self.hello(0)
            self.r['code_restore_verified'] = True
        if p.read_fixed(self.t, HELLO, 32) != expected[HELLO]: raise RuntimeError('Original code differs')
        if p.read_fixed(self.t, RO_POINTER, 8) != struct.pack('<Q', HELLO): raise RuntimeError('Original RO table differs')
        if p.read_fixed(self.t, CMD_SLOT, 16) != bytes(16): raise RuntimeError('Callbacks not restored to NULL')
        self.r.update(ram_restore_required=False, ram_restore_verified=True)
        self.save()

    def run(self, expected, extra):
        p.preflight(self.jig, self.t, expected, self.r, self.save, patch_state=True)
        state = self.r['patch_state']
        required = dict(ui_mode=13, candidate_ui_mode=0, root_dispatch_state=15,
                        cache_coordinator_initialized=1, cache_cpu_mask=15, cache_clean_ipi_id=0xA0001)
        if any(state[k] != v for k, v in required.items()): raise RuntimeError('Unexpected service/cache state')
        if any(state[k] != [1]*4 for k in ('cache_clean_completion_flags', 'cache_clean_invalidate_completion_flags')):
            raise RuntimeError('Cache coordinator not idle')
        self.r.update(restore_required=True, restore_verified=False)
        self.save()
        try:
            self.jig.write_config_data(0, 0xF7, b'\1', mb=1)
            if bytes(self.jig.read_config_data(0, 0xF7, 1, mb=1)) != b'\1': raise RuntimeError('Debug enable unverified')
            for name, address, size in EXTRA_SAMPLES:
                if p.read_fixed(self.t, address, size) != extra[address]: raise RuntimeError(f'Firmware mismatch: {name}')
            if p.read_fixed(self.t, CMD_SLOT, 16) != bytes(16): raise RuntimeError('Unexpected pre-existing callback')
            self.check_mappings()
            self.t.armed = True
            self.hello(0)
            self.cmd(NOOP)
            self.r['noop_completed'] = True
            if self.t.phase in ('cache', 'marker'):
                self.synchronize()
                self.r['cache_helpers_completed'] = True
            if self.t.phase == 'marker':
                self.memory(MARKER, b'\xe0')
                self.synchronize()
                self.hello(7)
                self.r['marker_executed'] = True
            self.r['phase_action_complete'] = True
            self.save()
        finally:
            if self.t.healthy and not self.t.awaiting:
                try:
                    if self.r.get('ram_restore_required') and not self.t.pending:
                        self.restore_ram(expected)
                except BaseException as exc:
                    self.r['ram_restore_error'] = f'{type(exc).__name__}: {exc}'
                    self.save()
                finally:
                    if self.t.healthy and not self.t.awaiting:
                        self.jig.write_config_data(0, 0xF7, b'\0', mb=1)
                        value = bytes(self.jig.read_config_data(0, 0xF7, 1, mb=1))
                        self.r.update(restored_f7=value.hex(), restore_required=value != b'\0', restore_verified=value == b'\0')
                        self.save()
        if self.r.get('ram_restore_required') or self.r.get('restore_required') or self.t.pending:
            raise RuntimeError('Restoration incomplete')
        self.r['experiment_complete'] = True
        self.save()


def samples():
    expected, hashes = p.patch_expected_samples()
    a, b = p.Image('MainA'), p.Image('MainB')
    extra = {}
    for _, address, size in EXTRA_SAMPLES:
        image = a if address < b.base else b
        extra[address] = image.data[address-image.base:address-image.base+size]
        if len(extra[address]) != size: raise RuntimeError('Truncated local image sample')
    return expected, extra, hashes


def prerequisite(phase, path, hashes):
    data = path.read_bytes()
    report = json.loads(data)
    if report.get('image_hashes') != hashes or report.get('session') != 'closed' or report.get('restore_required') or report.get('error'):
        raise RuntimeError('Preceding journal does not establish a clean matching firmware session')
    if phase == 'callback':
        good = (report.get('kind') == 'ff80_hello_recovery_snapshot_v1' and report.get('recovery_verified')
                and report.get('snapshot_complete') and report.get('cmd_and_key_callbacks_hex') == bytes(16).hex())
    else:
        good = (report.get('kind') == KIND and report.get('phase') == PHASES[PHASES.index(phase)-1]
                and report.get('experiment_complete') and report.get('ram_restore_verified')
                and not report.get('recovery_required') and not report.get('ram_restore_required'))
    if not good or report.get('restored_f7') != '00': raise RuntimeError('Required prior live phase is not verified')
    return dict(path=str(path), sha256=hashlib.sha256(data).hexdigest())


def save_private(path, report, *, create=False):
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temp = None
    else:
        fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
        temp = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        if temp: temp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temp and temp.exists(): temp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--phase', choices=PHASES, default='callback')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prior', type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: callback (original no-op), cache (original clean/IC/clean), then marker (hello 0 -> 7 -> 0).')
        print('Each phase requires the previous clean live journal and fresh firmware/MMU/idle checks.')
        print('Only writable CMD callback and one hello instruction byte; restore NULL/code/debug flag.')
        print('USB uncertainty stops all traffic. No mode, key, capture, settings save or flash request.')
        return 0
    args.output = args.output or p.ROOT/f'ff80-cmd-{args.phase}.json'
    args.prior = args.prior or p.ROOT/('ff80-hello-recovery-snapshot-2.json' if args.phase == 'callback'
                                      else f'ff80-cmd-{PHASES[PHASES.index(args.phase)-1]}.json')
    expected, extra, hashes = samples()
    prior = prerequisite(args.phase, args.prior, hashes)
    # A failed live phase is archived, even if the caller selects a new name.
    for path in p.ROOT.glob('ff80-cmd-*.json'):
        previous = json.loads(path.read_text())
        if previous.get('kind') == KIND and not previous.get('experiment_complete') and previous.get('writes'):
            raise RuntimeError(f'Prior failed live experiment requires investigation: {path.name}')
    report = dict(kind=KIND, phase=args.phase, started_at_utc=datetime.now(timezone.utc).isoformat(),
                  image_hashes=hashes, prerequisite=prior, restore_required=False, ram_restore_required=False,
                  code_write_attempted=False, marker_executed=False, experiment_complete=False, session='not_opened')
    save_private(args.output, report, create=True)
    def checkpoint(): save_private(args.output, report)
    transport = None
    try:
        with p.usb1.USBContext() as context:
            devices = [d for d in context.getDeviceList() if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1: raise RuntimeError('Expected exactly one service camera')
            device = devices[0]
            report['device_node'] = f'/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}'
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    transport = Transport(handle, args.phase, report, checkpoint)
                    try:
                        report['session'] = 'opening'; checkpoint()
                        transport.open_session(); report['session'] = 'open'; checkpoint()
                        jig = p.ffjlib.jig(transport)
                        report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Camera model/config bounds mismatch')
                        Experiment(jig, transport, report, checkpoint).run(expected, extra)
                    finally:
                        if transport.sid is not None and transport.healthy and not transport.awaiting and not transport.pending:
                            transport.close_session(); report['session'] = 'closed'; checkpoint()
                        elif report['session'] != 'not_opened':
                            report['session'] = 'unconfirmed_after_error'
            finally: handle.close()
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['recovery_required'] = bool(report.get('restore_required') or report.get('ram_restore_required')
                                            or (transport and (not transport.healthy or transport.pending)))
        checkpoint()
    print(json.dumps({k: report.get(k) for k in ('phase', 'device_node', 'noop_completed', 'cache_helpers_completed',
        'marker_executed', 'code_restore_verified', 'ram_restore_verified', 'restored_f7', 'session',
        'experiment_complete', 'recovery_required', 'error', 'last_io')}, indent=2))
    return 0 if report['experiment_complete'] and report['session'] == 'closed' and not report['recovery_required'] and 'error' not in report else 1


if __name__ == '__main__': raise SystemExit(main())
