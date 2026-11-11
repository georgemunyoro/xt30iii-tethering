# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Fixed temporary menu-clone proof/arm experiment; default is offline plan.

Proof allocates 1 KB through the native nonblocking pool API, copies three
linked menu descriptors, calls the conditional router without publishing
it to UI tasks, releases the allocation and restores all code. Arm repeats that
verified setup and the proven physical PLAY -> REC transition. No saved
mode/profile setters, flash requests or memory-permission changes are used.
Uncertain USB fences ALL subsequent traffic, including cleanup.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct

import ff80_rec_patch_trial as rec
import ff80_rec_recovery_profile as recovery
import service_tether_menu as m
import usb_host

q, p, s = rec.q, rec.p, rec.s
KIND = 'ff80_menu_clone_trial_v1'
OBLIGATIONS = ('restore_required', 'ram_restore_required', 'code_restore_required',
               'profile_restore_required', 'allocation_restore_required', 'recovery_required')


def extra_samples():
    result = m.samples()
    kernel = (p.ROOT / 'threadx-kernel.bin').read_bytes()
    if hashlib.sha256(kernel).hexdigest() != p.KERNEL_SHA256:
        raise RuntimeError('Unexpected kernel image')
    for name, address, length in (('pool_allocate', m.ALLOCATE, 0xB8),
                                  ('pool_allocate_inner', 0x94CB4, 0x214),
                                  ('pool_release', m.RELEASE, 0x2A8)):
        for offset in range(0, length, 256):
            size = min(256, length-offset)
            result[(f'{name}_{offset}', address+offset, size)] = kernel[
                address+offset-m.KERNEL_BASE:address+offset-m.KERNEL_BASE+size]
    return result


def recipes(b):
    original, words, writes = rec.recipes('arm', b)
    for address, size in ((m.HELPER, m.HELPER_SIZE), (m.ROUTER, m.ROUTER_SIZE)):
        original[address] = b.data[address-b.base:address-b.base+size]
        writes.add((address, original[address]))
    original[m.LOOKUP_RET] = struct.pack('<I', 0xD65F03C0)
    words[m.LOOKUP_RET] = {0xD65F03C0, s.branch(m.LOOKUP_RET, m.ROUTER)}
    writes |= {(m.HELPER, s.atomic_store(m.LOOKUP_RET, word)) for word in words[m.LOOKUP_RET]}
    return original, words, writes


class Transport(rec.Transport):
    command_limit = 160
    table_limit = 48

    def __init__(self, device, phase, report, checkpoint, writes, samples):
        super().__init__(device, phase, report, checkpoint, writes)
        self.reads |= {(address, size) for _, address, size in samples}
        self.reads |= {(m.HELPER, m.HELPER_SIZE), (m.ROUTER, m.ROUTER_SIZE),
                       (m.LOOKUP_RET, 4), (m.POOL_ID_ADDRESS, 4)}
        self.reads |= {(address, size) for _, address, size in recovery.STATES} | set(recovery.PROFILE_READS)


class Trial(rec.Trial):
    def __init__(self, *args, samples, baseline, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples, self.baseline = samples, baseline
        self.helper_current = self.original[m.HELPER]
        self.allocation = None

    def memory(self, address, data):
        if address == m.HELPER:
            actual = p.read_fixed(self.t, address, m.HELPER_SIZE)
            if actual != self.helper_current:
                raise RuntimeError('Helper changed outside the recorded callback result')
            self.t.writes.add((address, actual[:len(data)]))
        super().memory(address, data)
        if address == m.HELPER:
            self.helper_current = data + self.helper_current[len(data):]

    def profile(self):
        return b''.join(p.read_fixed(self.t, address, size) for address, size in recovery.PROFILE_READS)

    def check_saved_state(self):
        states = {name: int.from_bytes(p.read_fixed(self.t, address, size), 'little')
                  for name, address, size in recovery.STATES}
        if states != self.baseline['states'] or self.profile().hex() != self.baseline['profile_hex']:
            raise RuntimeError('Service/profile/settings state changed from the fresh recovery snapshot')
        self.r['saved_state_unchanged'] = True
        self.save()

    def check_trial_state(self):
        super().check_trial_state()
        for (name, address, size), expected in self.samples.items():
            if p.read_fixed(self.t, address, size) != expected:
                raise RuntimeError(f'Menu/pool original code or data differs: {name}')
        self.check_saved_state()
        self.pool_id = int.from_bytes(p.read_fixed(self.t, m.POOL_ID_ADDRESS, 4), 'little')
        if not 1 <= self.pool_id <= 0x7FFE:
            raise RuntimeError('Native pool not initialized')
        self.r['pool_id'] = self.pool_id
        root = int.from_bytes(p.read_fixed(self.t, q.ROOT_POINTER, 8), 'little')
        for name, address, executable in (('menu_router', m.ROUTER, True),
                                          ('menu_lookup_return', m.LOOKUP_RET, True),
                                          ('native_pool_id', m.POOL_ID_ADDRESS, False)):
            mapping = q.walk(root, address, self.t.table_entry)
            self.r['mappings'][name] = mapping
            if not (mapping.get('mapped') and mapping.get('access_flag') and mapping.get('el1_writable')
                    and mapping.get('physical_address') == hex(address)
                    and mapping.get('privileged_execute_never') != executable):
                raise RuntimeError(f'Mapping differs: {name}')
        self.save()

    def callback(self, action, *, kind=0):
        pointer = self.allocation if action == 'release' else 0
        payload = m.callback(action, pointer=pointer, kind=kind)
        self.t.writes.add((m.HELPER, payload))
        self.memory(m.HELPER, payload)
        self.synchronize()
        record = dict(action=action, kind=kind if action.startswith('lookup') else None,
                      attempted=True, returned=False)
        self.r.setdefault('native_calls', []).append(record)
        if action == 'allocate':
            self.r.update(allocation_restore_required=True, allocation_result_unknown=True)
        self.save()
        self.cmd(m.HELPER)
        data = p.read_fixed(self.t, m.HELPER, m.HELPER_SIZE)
        if data[:80] != payload[:80] or data[92:] != bytes(4):
            raise RuntimeError('Native callback changed executable bytes or result padding')
        pointer, status = struct.unpack_from('<Qi', data, 80)
        self.helper_current = data
        self.t.writes.add((m.HELPER, data))
        record.update(returned=True, pointer=hex(pointer), status=status)
        self.save()
        if action == 'allocate':
            if status == 0 and m.owned_pointer(pointer):
                self.allocation = pointer
                self.r.update(allocation_address=hex(pointer), allocation_result_unknown=False)
            elif status != 0 and pointer == 0:
                self.r.update(allocation_restore_required=False, allocation_result_unknown=False)
            else:
                raise RuntimeError('Native allocation result outside verified pool bounds')
            self.save()
        elif action == 'release':
            if pointer != self.allocation:
                raise RuntimeError('Release changed the recorded allocation pointer')
            if status == 0:
                self.allocation = None
                self.r.update(allocation_restore_required=False, allocation_released=True)
                self.save()
        if status != 0:
            raise RuntimeError(f'Native {action} failed: {status}')
        return pointer

    def allocate_clone(self):
        pointer = self.callback('allocate')
        root = int.from_bytes(p.read_fixed(self.t, q.ROOT_POINTER, 8), 'little')
        for name, address in (('allocation_first', pointer), ('allocation_last', pointer + m.SIZE - 1)):
            mapping = q.walk(root, address, self.t.table_entry)
            self.r['mappings'][name] = mapping
            if not (mapping.get('mapped') and mapping.get('access_flag') and mapping.get('el1_writable')
                    and mapping.get('physical_address') == hex(address)):
                raise RuntimeError('Allocation is not identity-mapped writable RAM')
        payload = m.clone(pointer)
        for offset in range(0, m.SIZE, 256):
            address = pointer + offset
            self.t.reads.add((address, 256))
            before = p.read_fixed(self.t, address, 256)
            wanted = payload[offset:offset+256]
            self.t.writes |= {(address, before), (address, wanted)}
            self.memory(address, wanted)
        actual = b''.join(p.read_fixed(self.t, pointer+i, 256) for i in range(0, m.SIZE, 256))
        if actual != payload:
            raise RuntimeError('Menu clone readback differs')
        self.r.update(clone_verified=True, clone_sha256=hashlib.sha256(payload).hexdigest())
        self.save()

    def lookup_proof(self, routed, *, staged=False):
        a = p.Image('MainA')
        for kind in (0, 0x8B, 1):
            original = next(a.unpack('<Q', address + 8)[0] for address in range(0x0195CAC8, 0x0195F000, 16)
                            if a.unpack('<I', address)[0] == kind)
            expected = ({m.ROOT: self.allocation, m.NETWORK: self.allocation + m.NETWORK_OFFSET}
                        .get(original, original) if routed else original)
            result = self.callback('lookup_routed' if staged else 'lookup', kind=kind)
            if result != expected:
                raise RuntimeError(f'Native menu lookup returned an unexpected pointer for {kind:#x}')
        self.r['routed_lookup_verified' if routed else 'original_lookup_verified'] = True
        self.save()

    def perform_action(self):
        self.allocate_clone()
        payload = m.router(self.allocation)
        self.t.writes.add((m.ROUTER, payload))
        self.memory(m.ROUTER, payload)
        self.synchronize()
        if self.t.phase == 'arm':
            self.r['lookup_ever_published'] = True
            self.save()
            self.atomic(m.LOOKUP_RET, s.branch(m.LOOKUP_RET, m.ROUTER))
            self.r['menu_hook_armed'] = True
            self.save()
        self.lookup_proof(True, staged=self.t.phase == 'proof')
        self.check_saved_state()
        if self.t.phase == 'proof':
            self.lookup_proof(False)
            self.restore()
            if self.r.get('allocation_restore_required') or self.r.get('ram_restore_required'):
                raise RuntimeError('Proof cleanup incomplete')
            self.r['menu_clone_proof_complete'] = True
        else:
            self.memory(m.HELPER, self.original[m.HELPER])
            self.synchronize()
            super().perform_action()
            self.check_saved_state()
            self.r['menu_clone_armed'] = True
        self.save()

    def restore(self):
        self.idle()
        self.memory(q.CMD_SLOT, bytes(8))
        if self.r.get('cache_failure'):
            raise RuntimeError('Cache failure prevents verified restoration')
        # Remove every incoming hook before restoring handlers or freeing data.
        for address in reversed(tuple(self.words)):
            self.atomic(address, struct.unpack('<I', self.original[address][:4])[0])
        self.r['menu_hook_armed'] = False
        self.memory(m.ROUTER, self.original[m.ROUTER])
        self.synchronize()
        if self.allocation is not None and not self.r.get('lookup_ever_published'):
            if int.from_bytes(p.read_fixed(self.t, m.POOL_ID_ADDRESS, 4), 'little') != self.pool_id:
                raise RuntimeError('Pool identity changed; refusing release')
            self.callback('release')
        # Once UI tasks could have observed the clone, never free its storage
        # in this session: an unobserved cached descriptor must remain valid.
        # A failed arm needs a verified power restart to close this obligation.
        self.memory(m.HELPER, self.original[m.HELPER])
        self.memory(s.HELLO, self.original[s.HELLO])
        self.synchronize()
        self.hello(0)
        for address, data in self.original.items():
            if p.read_fixed(self.t, address, len(data)) != data:
                raise RuntimeError('Original code did not restore')
        if p.read_fixed(self.t, q.CMD_SLOT, 16) != bytes(16):
            raise RuntimeError('Callbacks not restored')
        self.check_saved_state()
        allocation_pending = bool(self.r.get('allocation_restore_required'))
        self.r.update(ram_restore_required=allocation_pending, code_restore_required=False, code_restore_verified=True,
                      ram_restore_verified=not allocation_pending, patch_armed=False, menu_clone_armed=False)
        self.save()


def load_baseline(path, hashes):
    raw = path.read_bytes()
    report = json.loads(raw)
    if (report.get('kind') != recovery.KIND or report.get('image_hashes') != hashes
        or not report.get('recovery_verified') or not report.get('snapshot_complete')
        or report.get('session') != 'closed' or report.get('restored_f7') != '00' or report.get('error')
        or report.get('profile_fields') != dict(slot=1, function=1, option=0, transport=2)
        or report.get('states', {}).get('connection_mode') != 0):
        raise RuntimeError('Verified Card Reader recovery snapshot required')
    return report, dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--phase', choices=('proof', 'arm'), default='proof')
    parser.add_argument('--recovery', type=Path, default=p.ROOT/'ff80-rec-recovery-menu-exposure-2.json')
    parser.add_argument('--proof', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: native 1 KB allocation, linked menu clone, conditional lookup proof and full cleanup.')
        print('Arm requires that proof and leaves recorded RAM hooks for physical PLAY. No mode/save/flash/MMU changes.')
        return 0
    expected, extra, hashes = q.samples()
    baseline, reference = load_baseline(args.recovery, hashes)
    atomic_reference = rec.prior_ok('arm', p.ROOT/'ff80-rec-atomic-2.json', hashes)
    for path in (*p.ROOT.glob('ff80-rec-*.json'), *p.ROOT.glob('ff80-tether-profile-*.json'),
                 *p.ROOT.glob('ff80-menu-clone-*.json')):
        old = json.loads(path.read_text())
        if old.get('kind') in (rec.KIND, 'ff80_tether_profile_trial_v1', KIND) and any(old.get(k) for k in OBLIGATIONS):
            raise RuntimeError(f'Unresolved preceding experiment: {path.name}')
    proof = None
    if args.phase == 'arm':
        if not args.proof:
            raise RuntimeError('Arm requires an explicit live proof journal')
        raw = args.proof.read_bytes(); old = json.loads(raw)
        if (old.get('kind') != KIND or old.get('phase') != 'proof' or old.get('image_hashes') != hashes
            or not old.get('menu_clone_proof_complete') or not old.get('allocation_released')
            or not old.get('code_restore_verified') or old.get('session') != 'closed' or old.get('error')
            or old.get('restored_f7') != '00' or any(old.get(k) for k in OBLIGATIONS)):
            raise RuntimeError('Completed allocation/lookup/restoration proof required')
        proof = dict(path=str(args.proof), sha256=hashlib.sha256(raw).hexdigest())
    b = p.Image('MainB')
    original, words, writes = recipes(b)
    samples = extra_samples()
    samples_rec = {address: b.data[address-b.base:address-b.base+size] for _, address, size in rec.SAMPLES}
    output = args.output or p.ROOT/f'ff80-menu-clone-{args.phase}.json'
    report = dict(kind=KIND, phase=args.phase, image_hashes=hashes, recovery_snapshot=reference,
                  atomic_proof=atomic_reference, menu_proof=proof, session='not_opened',
                  started_at_utc=datetime.now(timezone.utc).isoformat(), original_code={hex(a): v.hex() for a, v in original.items()},
                  original_profile_hex=baseline['profile_hex'], original_states=baseline['states'],
                  trial_complete=False, **{key: False for key in OBLIGATIONS})
    q.save_private(output, report, create=True)
    def checkpoint(): q.save_private(output, report)
    t = None
    try:
        with usb_host.service_context(p.usb1) as context:
            devices = [d for d in context.getDeviceList() if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1:
                raise RuntimeError('Expected exactly one service camera')
            device = devices[0]
            report['device_node'] = usb_host.attachment_node(device.getBusNumber(), device.getDeviceAddress())
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    t = Transport(handle, args.phase, report, checkpoint, writes, samples)
                    try:
                        t.open_session(); report['session'] = 'open'; checkpoint()
                        jig = p.ffjlib.jig(t); report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Identity/config mismatch')
                        Trial(jig, t, report, checkpoint, original, words, samples=samples, baseline=baseline).run(expected, extra, samples_rec)
                    finally:
                        if t.sid is not None and t.healthy and not t.awaiting and not t.pending:
                            t.close_session(); report['session'] = 'closed'; checkpoint()
                        elif t.sid is not None:
                            report['session'] = 'unconfirmed_after_error'
            finally:
                handle.close()
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['recovery_required'] = bool(any(report.get(k) for k in OBLIGATIONS if k != 'recovery_required')
                                            or t and (not t.healthy or t.pending))
        report['ready_for_physical_play'] = bool(args.phase == 'arm' and report['trial_complete']
            and report.get('menu_clone_armed') and report.get('patch_armed') and report.get('restore_verified')
            and report['session'] == 'closed' and not report.get('error'))
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); checkpoint()
    print(json.dumps({key: report.get(key) for key in ('phase', 'trial_complete', 'menu_clone_proof_complete',
        'routed_lookup_verified', 'original_lookup_verified', 'allocation_address', 'allocation_released',
        'menu_clone_armed', 'ready_for_physical_play', 'saved_state_unchanged', 'code_restore_verified',
        'session', 'restored_f7', 'recovery_required', 'error')}, indent=2))
    return 0 if report['trial_complete'] and report['session'] == 'closed' and not report.get('error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
