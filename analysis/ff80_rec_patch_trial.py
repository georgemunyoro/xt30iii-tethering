# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Fixed RAM-only atomic-store proof, then an armed physical PLAY -> REC trial.

--phase atomic executes/restores hello status 7 using one aligned STR W.
--phase arm requires that successful journal, installs a conditional JIG
completion trampoline and redirects only JIG PLAY press to the original REC
request wrapper. It restores the temporary store helper and debug flag,
closes USB, and leaves the two branches/trampoline armed for physical PLAY.
No host mode/key/capture/save/flash command is sent. ARM intentionally leaves
a recorded RAM restoration obligation until a verified restart or disarm.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct

import ff80_cmd_patch_probe as q
import service_rec_patch as s
from ff80_rec_prerequisites_snapshot import STATES, SAMPLES

p = q.p
KIND = 'ff80_rec_patch_trial_v1'
CODE = ((s.ATOMIC, 32), (s.HELLO, 32), (s.JIG_BRANCH, 4), (s.PLAY_BRANCH, 4), (q.MARKER, 4))
READS = q.READS | {(a, n) for a, n in CODE} | {(a, n) for _, a, n in STATES+SAMPLES}


def recipes(phase, image):
    original = {a: image.data[a-image.base:a-image.base+n] for a, n in CODE}
    words = {a: {struct.unpack('<I', original[a])[0], word} for a, word in
             (s.PATCH_WORDS.items() if phase == 'arm' else ((q.MARKER, 0x528000E0),))}
    writes = {(q.CMD_SLOT, struct.pack('<Q', target)) for target in (0, q.NOOP, q.CLEAN, q.ICACHE, s.ATOMIC)}
    writes |= {(s.ATOMIC, original[s.ATOMIC]), (s.HELLO, original[s.HELLO])}
    writes |= {(s.ATOMIC, s.atomic_store(a, w)) for a, values in words.items() for w in values}
    if phase == 'arm': writes.add((s.HELLO, s.trampoline()))
    return original, words, writes


class Transport(q.Transport):
    command_limit = 64
    table_limit = 27
    def __init__(self, device, phase, report, checkpoint, writes):
        super().__init__(device, phase, report, checkpoint)
        self.reads = set(READS)
        self.writes = writes

    def validate(self, request):
        if len(request) >= 36 and request[:28] == struct.pack('<7I', 0x200001, 0, 0, 0, 3, 0, 0):
            a, n = struct.unpack('<II', request[28:36])
            if n == len(request)-36 and (a, request[36:]) in self.writes: return 'ram'
            raise RuntimeError('Write outside fixed trial recipes; active branches require atomic store')
        return q.validate(request, self.reads, 'callback')


class Trial(q.Experiment):
    def __init__(self, jig, transport, report, checkpoint, original, words, sleep=q.time.sleep):
        super().__init__(jig, transport, report, checkpoint, sleep)
        self.original, self.words = original, words

    def allowed_writes(self): return self.t.writes

    def memory(self, address, data):
        if (address, data) not in self.t.writes: raise RuntimeError('Unreviewed RAM payload')
        if address != q.CMD_SLOT:
            self.r['code_restore_required'] = True
            self.save()
        return super().memory(address, data)

    def atomic(self, address, word):
        if address not in self.words or word not in self.words[address]: raise RuntimeError('Unreviewed atomic store')
        current = struct.unpack('<I', p.read_fixed(self.t, address, 4))[0]
        if current not in self.words[address]: raise RuntimeError('Unknown instruction; refusing to overwrite')
        if current == word: return
        self.memory(s.ATOMIC, s.atomic_store(address, word))
        self.synchronize()
        record = dict(address=hex(address), before=hex(current), after=hex(word), attempted=True, verified=False)
        self.r.setdefault('atomic_stores', []).append(record)
        self.r.update(ram_restore_required=True, code_restore_required=True)
        self.save()
        self.cmd(s.ATOMIC)
        self.synchronize()
        if p.read_fixed(self.t, address, 4) != struct.pack('<I', word): raise RuntimeError('Atomic store readback differs')
        record['verified'] = True
        self.save()

    def check_trial_state(self):
        values = {name: p.read_fixed(self.t, a, n) for name, a, n in STATES}
        self.r['trial_state'] = {k: v.hex() for k, v in values.items()}
        mode, previous, third, candidate, older, action = struct.unpack('<6I', values['ui_mode_history_candidate_action'])
        state24 = struct.unpack('<16H', values['task24_dispatch_states'])
        state52 = struct.unpack('<16H', values['task52_dispatch_states'])
        if ((mode, candidate, action) != (13, 0, 19) or state24[:2] != (3, 0) or state24[7] != 1
            or state52[:2] != (3, 6) or values['usb_hardware_state'] != b'\x0a\0'
            or values['backend'] != struct.pack('<I', 3) or values['mode_gate_af4'] != bytes(4)
            or values['mode_gate_ca8'] != bytes(4) or values['cmd_and_key_slots'] != bytes(16)):
            raise RuntimeError('Live service/teardown state differs from investigated path')
        self.check_mappings()
        root = struct.unpack('<Q', p.read_fixed(self.t, q.ROOT_POINTER, 8))[0]
        for name, address in (('atomic_scratch', s.ATOMIC), ('jig_branch', s.JIG_BRANCH), ('play_branch', s.PLAY_BRANCH)):
            mapping = q.walk(root, address, self.t.table_entry)
            self.r['mappings'][name] = mapping
            if not (mapping['mapped'] and mapping['access_flag'] and mapping['el1_writable']
                    and not mapping['privileged_execute_never'] and mapping['physical_address'] == hex(address)):
                raise RuntimeError('Trial target mapping differs')
        self.save()

    def restore(self):
        self.idle()
        self.memory(q.CMD_SLOT, bytes(8))
        if self.r.get('cache_failure'): raise RuntimeError('Cache failure prevents verified code restoration')
        for address in reversed(tuple(self.words)):
            self.atomic(address, struct.unpack('<I', self.original[address])[0])
        self.memory(s.ATOMIC, self.original[s.ATOMIC])
        self.memory(s.HELLO, self.original[s.HELLO])
        self.synchronize()
        self.hello(0)
        for address, size in CODE:
            if p.read_fixed(self.t, address, size) != self.original[address]: raise RuntimeError('Original code restoration differs')
        if p.read_fixed(self.t, q.CMD_SLOT, 16) != bytes(16): raise RuntimeError('Callback restoration differs')
        self.r.update(ram_restore_required=False, code_restore_required=False, code_restore_verified=True,
                      ram_restore_verified=True, patch_armed=False)
        self.save()

    def perform_action(self):
        if self.t.phase == 'atomic':
            self.atomic(q.MARKER, 0x528000E0)
            self.hello(7)
            self.r['atomic_marker_executed'] = True
        else:
            self.memory(s.HELLO, s.trampoline())
            self.synchronize()
            for address, word in s.PATCH_WORDS.items(): self.atomic(address, word)
            self.memory(s.ATOMIC, self.original[s.ATOMIC])
            self.synchronize()
            if p.read_fixed(self.t, s.HELLO, 32) != s.trampoline(): raise RuntimeError('Trampoline readback differs')
            if p.read_fixed(self.t, s.ATOMIC, 32) != self.original[s.ATOMIC][:32]: raise RuntimeError('Store helper not restored')
            if p.read_fixed(self.t, q.CMD_SLOT, 16) != bytes(16): raise RuntimeError('Callbacks not restored')
            self.r['patch_armed'] = True

    def run(self, expected, extra, samples_extra):
        p.preflight(self.jig, self.t, expected, self.r, self.save, patch_state=True)
        state = self.r['patch_state']
        if any(state[k] != v for k, v in dict(ui_mode=13, candidate_ui_mode=0, root_dispatch_state=15,
            cache_coordinator_initialized=1, cache_cpu_mask=15, cache_clean_ipi_id=0xA0001).items()):
            raise RuntimeError('Unexpected UI/cache state')
        if state['cache_clean_completion_flags'] != [1]*4 or state['cache_clean_invalidate_completion_flags'] != [1]*4:
            raise RuntimeError('Cache coordinator not idle')
        self.r.update(restore_required=True, restore_verified=False); self.save()
        success = False
        try:
            self.jig.write_config_data(0, 0xF7, b'\1', mb=1)
            if bytes(self.jig.read_config_data(0, 0xF7, 1, mb=1)) != b'\1': raise RuntimeError('Debug enable unverified')
            for _, a, n in q.EXTRA_SAMPLES:
                if p.read_fixed(self.t, a, n) != extra[a]: raise RuntimeError('CMD/cache code differs')
            for _, a, n in SAMPLES:
                if p.read_fixed(self.t, a, n) != samples_extra[a]: raise RuntimeError('REC path code differs')
            for a, n in CODE:
                if p.read_fixed(self.t, a, n) != self.original[a][:n]: raise RuntimeError('Trial code already changed')
            self.check_trial_state()
            self.t.armed = True
            self.hello(0)
            self.perform_action()
            success = True
            self.save()
        finally:
            if self.t.healthy and not self.t.awaiting and not self.t.pending:
                try:
                    if self.r.get('ram_restore_required') and (self.t.phase == 'atomic' or not success): self.restore()
                except BaseException as exc:
                    self.r['ram_restore_error'] = f'{type(exc).__name__}: {exc}'; self.save()
                finally:
                    if self.t.healthy:
                        self.jig.write_config_data(0, 0xF7, b'\0', mb=1)
                        flag = bytes(self.jig.read_config_data(0, 0xF7, 1, mb=1))
                        self.r.update(restored_f7=flag.hex(), restore_required=flag != b'\0', restore_verified=flag == b'\0')
                        self.save()
        if not success or self.r.get('restore_required') or (self.t.phase == 'atomic' and self.r.get('ram_restore_required')):
            raise RuntimeError('Trial/restoration incomplete')
        self.r['trial_complete'] = True
        self.save()


def prior_ok(phase, path, hashes):
    raw = path.read_bytes(); report = json.loads(raw)
    if report.get('image_hashes') != hashes or report.get('session') != 'closed' or report.get('error') or report.get('restored_f7') != '00':
        raise RuntimeError('Preceding live journal not verified')
    good = ((report.get('kind') == q.KIND and report.get('phase') == 'marker' and report.get('marker_executed')
             and report.get('experiment_complete')) if phase == 'atomic' else
            (report.get('kind') == KIND and report.get('phase') == 'atomic' and report.get('atomic_marker_executed')
             and report.get('trial_complete')))
    if not good or not report.get('code_restore_verified') or any(report.get(k) for k in ('restore_required', 'ram_restore_required', 'recovery_required')):
        raise RuntimeError('Required preceding execution/restoration proof missing')
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true'); parser.add_argument('--phase', choices=('atomic', 'arm'), default='atomic')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prior', type=Path, help='Exact successful preceding live journal')
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: atomic marker proof/restoration; then arm two conditional JIG branches for physical PLAY.')
        print('Arm leaves only recorded RAM patches, with CMD slots/debug flag restored and USB session closed.')
        return 0
    expected, extra, hashes = q.samples(); b = p.Image('MainB')
    original, words, writes = recipes(args.phase, b)
    samples_extra = {a: b.data[a-b.base:a-b.base+n] for _, a, n in SAMPLES}
    prior_path = args.prior or p.ROOT/('ff80-cmd-marker.json' if args.phase == 'atomic' else 'ff80-rec-atomic.json')
    prior = prior_ok(args.phase, prior_path, hashes)
    for path in p.ROOT.glob('ff80-rec-*.json'):
        old = json.loads(path.read_text())
        if old.get('kind') == KIND and (old.get('ram_restore_required') or (old.get('writes') and not old.get('trial_complete'))):
            raise RuntimeError(f'Existing trial must be resolved before another: {path.name}')
    args.output = args.output or p.ROOT/f'ff80-rec-{args.phase}.json'
    report = dict(kind=KIND, phase=args.phase, prerequisite=prior, image_hashes=hashes, session='not_opened',
                  started_at_utc=datetime.now(timezone.utc).isoformat(), restore_required=False, ram_restore_required=False,
                  code_restore_required=False, patch_armed=False, trial_complete=False,
                  original_code={hex(a): data.hex() for a, data in original.items()})
    q.save_private(args.output, report, create=True)
    def checkpoint(): q.save_private(args.output, report)
    t = None
    try:
        with p.usb1.USBContext() as context:
            devices = [d for d in context.getDeviceList() if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1: raise RuntimeError('Expected one service camera')
            device = devices[0]; report['device_node'] = f'/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}'
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    t = Transport(handle, args.phase, report, checkpoint, writes)
                    try:
                        t.open_session(); report['session'] = 'open'; checkpoint()
                        jig = p.ffjlib.jig(t); report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Camera identity/config mismatch')
                        Trial(jig, t, report, checkpoint, original, words).run(expected, extra, samples_extra)
                    finally:
                        if t.sid is not None and t.healthy and not t.awaiting and not t.pending:
                            t.close_session(); report['session'] = 'closed'; checkpoint()
                        elif t.sid is not None: report['session'] = 'unconfirmed_after_error'
            finally: handle.close()
    except BaseException as exc: report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['recovery_required'] = bool(report['restore_required'] or report['ram_restore_required'] or (t and (not t.healthy or t.pending)))
        report['ready_for_physical_play'] = bool(args.phase == 'arm' and report['trial_complete'] and report['patch_armed']
            and report.get('restore_verified') and report['session'] == 'closed' and 'error' not in report)
        checkpoint()
    print(json.dumps({k: report.get(k) for k in ('phase', 'atomic_marker_executed', 'code_restore_verified', 'patch_armed',
        'ready_for_physical_play', 'restored_f7', 'session', 'trial_complete', 'recovery_required', 'error', 'last_io')}, indent=2))
    return 0 if report['trial_complete'] and report['session'] == 'closed' and not report['restore_required'] and 'error' not in report else 1


if __name__ == '__main__': raise SystemExit(main())
