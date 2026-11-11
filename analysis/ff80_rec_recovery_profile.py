# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Verify REC-trial code restoration and read the current connection profile.

Default is plan only. Live execution permits fixed reads and temporary
cfg[f7] access only; no console execution, general RAM writes or mode changes.
The exact armed journal is closed only after original code, NULL callbacks,
idle console, restored f7=0 and clean session closure have all been verified.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import usb_host

import ff80_cmd_patch_probe as q
import ff80_rec_patch_trial as trial
import tether_menu_candidate as menu

p = q.p
KIND = 'ff80_rec_recovery_profile_v1'
PROFILE, PROFILE_SIZE = 0x02AFF910, 0x474
PROFILE_READS = tuple((PROFILE+i, min(256, PROFILE_SIZE-i)) for i in range(0, PROFILE_SIZE, 256))
EXTRA_SAMPLES = (
    ('key_handler_scratch', 'MainB', trial.s.ATOMIC, 40),
    ('play_branch', 'MainB', trial.s.PLAY_BRANCH, 4),
    ('slot3_function_getter', 'MainA', 0x013D73A4, 40),
    ('profile_initializer', 'MainA', 0x01420AE8, 0x8C),
) + tuple((f'profile_builder_{i}', 'MainA', 0x0142065C+i, min(256, 0x48C-i))
          for i in range(0, 0x48C, 256))
STATES = (
    ('cmd_and_key_slots', q.CMD_SLOT, 16),
    ('connection_mode', 0x0297FF08, 1),
    ('communication_selection', 0x029804EC, 1),
    ('ui_communication_selection', 0x02A8E3C0, 1),
    ('slot3_function', 0x02980514, 1),
    ('profile_category', 0x02B001FC, 4),
    ('handover_state', 0x02A86994, 4),
    ('ui_mode', 0x02A305F8, 4),
    ('backend', 0x0488A72C, 4),
)
TARGETS = (('profile', PROFILE), ('profile_name_scratch', 0x029E98C8),
           ('wrapper_scratch', trial.s.ATOMIC), ('profile_builder', 0x0142065C))
READS = p.PATCH_READS | {(a, n) for _, _, a, n in EXTRA_SAMPLES} | {
    (a, n) for _, a, n in STATES} | set(PROFILE_READS) | {(q.ROOT_POINTER, 8)}
MENU_SAMPLES = tuple((name, 'MainA', a, n) for name, a, n in menu.STATIC_SAMPLES)


class Transport(q.Transport):
    table_limit = 12

    def __init__(self, device, report, checkpoint, *, menu_snapshot=False):
        super().__init__(device, 'callback', report, checkpoint)
        self.reads = set(READS)
        if menu_snapshot:
            self.reads.update((a, n) for _, _, a, n in MENU_SAMPLES)
            self.table_limit = 3 * (len(TARGETS) + len(menu.MAPPING_TARGETS))

    def validate(self, request):
        p.validate_request(request, self.reads, console=False)
        return 'preflight'


def samples(*, menu_snapshot=False):
    expected, hashes = p.patch_expected_samples()
    images = {name: p.Image(name) for name in ('MainA', 'MainB')}
    extra = {}
    for _, name, address, size in EXTRA_SAMPLES + (MENU_SAMPLES if menu_snapshot else ()):
        image = images[name]
        offset = address-image.base
        if not 0 <= offset <= len(image.data)-size: raise RuntimeError('Sample outside pinned image')
        extra[address] = image.data[offset:offset+size]
    return expected, extra, hashes


def validate_journal(journal, hashes):
    original, _, _ = trial.recipes('arm', p.Image('MainB'))
    stores = [dict(address=hex(a), before=hex(struct.unpack('<I', original[a])[0]),
                   after=hex(w), attempted=True, verified=True) for a, w in trial.s.PATCH_WORDS.items()]
    if (journal.get('kind') != trial.KIND or journal.get('phase') != 'arm'
        or journal.get('model') != 'X-T30 III' or journal.get('image_hashes') != hashes
        or journal.get('original_code') != {hex(a): v.hex() for a, v in original.items()}
        or journal.get('atomic_stores') != stores or journal.get('trial_complete') is not True
        or journal.get('original_f7') != '00' or journal.get('restored_f7') != '00'
        or journal.get('session') != 'closed' or journal.get('error')):
        raise RuntimeError('Source is not the exact completed REC arm trial')


def evidence_path(reference, root):
    """Relocate copied evidence only when its recorded content hash matches."""
    original = Path(reference.get('path', ''))
    path = original if original.is_absolute() else root.parent / original
    if path.is_file(): return path
    relocated = root / original.name
    if (relocated.is_file() and reference.get('sha256')
            and hashlib.sha256(relocated.read_bytes()).hexdigest() == reference['sha256']):
        return relocated
    raise RuntimeError(f'Original-profile evidence missing or changed: {original.name}')


def validate_source(journal, hashes):
    if journal.get('kind') == 'ff80_menu_clone_trial_v1':
        import ff80_menu_clone_trial as menu_trial
        original, _, _ = menu_trial.recipes(p.Image('MainB'))
        if (journal.get('phase') not in ('proof', 'arm') or journal.get('model') != 'X-T30 III'
            or journal.get('image_hashes') != hashes or journal.get('original_f7') != '00'
            or journal.get('original_code') != {hex(a): data.hex() for a, data in original.items()}):
            raise RuntimeError('Source is not the fixed menu-clone experiment')
        reference = journal.get('recovery_snapshot', {})
        path = evidence_path(reference, p.ROOT)
        baseline, verified_reference = menu_trial.load_baseline(path, hashes)
        if (verified_reference['sha256'] != reference.get('sha256')
            or journal.get('original_profile_hex') != baseline['profile_hex']
            or journal.get('original_states') != baseline['states']):
            raise RuntimeError('Menu experiment baseline differs')
        return 'menu_clone_trial'
    if journal.get('kind') != 'ff80_tether_profile_trial_v1':
        validate_journal(journal, hashes)
        return 'completed_rec_arm'
    if journal.get('phase') == 'arm':
        # The profile arm uses the exact REC recipes plus a native profile
        # builder. Validate the shared code journal and the additional data.
        validate_journal({**journal, 'kind': trial.KIND}, hashes)
        original = bytes.fromhex(journal.get('original_profile_hex', ''))
        helper = bytes.fromhex(journal.get('original_helper_hex', ''))
        b = p.Image('MainB'); off = trial.s.ATOMIC-b.base
        wanted_states = dict(cmd_and_key_slots=0, connection_mode=0, communication_selection=1,
            ui_communication_selection=1, slot1_function=1, slot3_function=2, profile_category=10,
            handover_state=0, ui_mode=13, backend=3)
        if len(original) != PROFILE_SIZE or helper != b.data[off:off+36] or journal.get('profile_states') != wanted_states:
            raise RuntimeError('Tether arm baseline differs')
        fields = {name: struct.unpack_from('<I', original, offset)[0] for name, offset in
                  (('slot', 0), ('function', 0x20), ('option', 0x24), ('transport', 0x3EC))}
        calls = journal.get('profile_calls', [])
        if (fields != dict(slot=1, function=1, option=0, transport=2) or len(calls) != 1
            or calls[0].get('slot') != 3 or not all(calls[0].get(k) is True for k in ('attempted', 'returned', 'verified'))
            or calls[0].get('fields') != dict(slot=3, function=2, option=1, transport=2)
            or len(bytes.fromhex(calls[0].get('profile_hex', ''))) != PROFILE_SIZE):
            raise RuntimeError('Tether arm profile transition differs')
        # Cross-check against the independently recorded pre-arm recovery.
        baseline = journal.get('recovery_snapshot', {})
        path = evidence_path(baseline, p.ROOT)
        raw = path.read_bytes(); recovered = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != baseline.get('sha256')
            or recovered.get('kind') != KIND or recovered.get('image_hashes') != hashes
            or not recovered.get('recovery_verified') or not recovered.get('snapshot_complete')
            or recovered.get('profile_hex') != original.hex() or recovered.get('error')):
            raise RuntimeError('Independent original-profile evidence differs')
        return 'completed_tether_profile_arm'
    # A completed short USB response to the exact oversized *read*, before
    # any callback/code/profile mutation. Original dispatcher rejects 288
    # bytes before the memory helper, with a 284-byte error response. Allow
    # a new bounded read-only session to verify state and restore cfg[f7].
    request = struct.pack('<9I', 0x200001, 0, 0, 0, 2, 0, 0, 0x01421490, 0x120).hex()
    if (journal.get('phase') != 'proof' or journal.get('model') != 'X-T30 III'
        or journal.get('image_hashes') != hashes or journal.get('last_request_hex') != request
        or journal.get('error') != 'RuntimeError: Short service response'
        or journal.get('last_io') != dict(phase='bulkRead', complete=True, transferred=284)
        or journal.get('original_f7') != '00' or journal.get('writes') or journal.get('commands')
        or journal.get('hellos') or journal.get('profile_calls') or journal.get('atomic_stores')
        or any(journal.get(k) for k in ('ram_restore_required', 'code_restore_required', 'profile_restore_required'))):
        raise RuntimeError('Source is not the exact completed oversized-read rejection')
    return 'completed_oversized_read_rejection'


def collect(jig, t, expected, extra, report, checkpoint, *, menu_snapshot=False, extra_checks=None):
    flag = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
    report['flag_before'] = flag.hex()
    if flag not in (b'\0', b'\1'): raise RuntimeError('Unexpected debug flag; no write requested')
    report.update(restore_required=True, restore_verified=False)
    checkpoint()
    try:
        if flag == b'\0': jig.write_config_data(0, 0xF7, b'\1', mb=1)
        if bytes(jig.read_config_data(0, 0xF7, 1, mb=1)) != b'\1':
            raise RuntimeError('Read-access flag did not verify')
        report['samples'] = []
        checks = [(name, a, n, expected[a]) for name, a, n in p.PATCH_SAMPLES]
        checks += [(name, a, n, extra[a]) for name, _, a, n in
                   EXTRA_SAMPLES + (MENU_SAMPLES if menu_snapshot else ())]
        checks += [(f'menu_trial_original_{a:x}', a, len(data), data)
                   for a, data in (extra_checks or {}).items()]
        for name, a, n, original in checks:
            value = p.read_fixed(t, a, n)
            report['samples'].append(dict(name=name, address=hex(a), data_hex=value.hex(), matches=value == original))
            checkpoint()
            if value != original: raise RuntimeError(f'Original code differs: {name}; no code restoration attempted')
        if extra_checks:
            report['menu_code_restore_verified'] = True
        callbacks = p.read_fixed(t, q.CMD_SLOT, 16)
        report['callbacks_null'] = callbacks == bytes(16)
        if not report['callbacks_null']: raise RuntimeError('CMD/KEY callback is not NULL')
        report['console_idle'] = p.verify_idle({name: p.read_fixed(t, a, n) for name, a, n in p.STATES})
        report['original_code_and_callbacks_verified'] = True
        checkpoint()
        report['states'] = {name: int.from_bytes(p.read_fixed(t, a, n), 'little') for name, a, n in STATES}
        profile = b''.join(p.read_fixed(t, a, n) for a, n in PROFILE_READS)
        report['profile_hex'] = profile.hex()
        report['profile_fields'] = {name: struct.unpack_from('<I', profile, offset)[0] for name, offset in
                                    (('slot', 0), ('function', 0x20), ('option', 0x24), ('transport', 0x3EC))}
        if 'expected_profile_hex' in report:
            report['profile_restore_verified'] = profile.hex() == report['expected_profile_hex']
            report['saved_settings_unchanged'] = all(report['states'][name] == value
                for name, value in report['expected_saved_states'].items())
            checkpoint()
            if not report['profile_restore_verified'] or not report['saved_settings_unchanged']:
                raise RuntimeError('Original runtime profile or saved settings differ; no restoration attempted')
        root = struct.unpack('<Q', p.read_fixed(t, q.ROOT_POINTER, 8))[0]
        report['mappings'] = {}
        for name, address in TARGETS + (menu.MAPPING_TARGETS if menu_snapshot else ()):
            report['mappings'][name] = q.walk(root, address, t.table_entry)
            checkpoint()
        if menu_snapshot:
            report['menu_snapshot_complete'] = True
            report['menu_data_is_writable'] = all(
                report['mappings'][name].get('mapped') is True
                and report['mappings'][name].get('physical_address') == hex(address)
                and report['mappings'][name].get('el1_writable') is True
                and report['mappings'][name].get('access_flag') is True
                for name, address in menu.MAPPING_TARGETS[:2])
        report['snapshot_complete'] = True
    finally:
        if t.healthy and not t.awaiting:
            current = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
            if current not in (b'\0', b'\1'): raise RuntimeError('Unexpected flag during restoration')
            if current == b'\1': jig.write_config_data(0, 0xF7, b'\0', mb=1)
            restored = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
            report.update(restored_f7=restored.hex(), restore_verified=restored == b'\0', restore_required=restored != b'\0')
            checkpoint()


def recovery_verified(report):
    if report.get('source_kind') == 'menu_clone_trial' and not (
        report.get('menu_code_restore_verified') and report.get('snapshot_complete')
        and report.get('profile_restore_verified') and report.get('saved_settings_unchanged')):
        return False
    if report.get('source_kind') == 'completed_tether_profile_arm' and not (
        report.get('snapshot_complete') and report.get('profile_restore_verified')
        and report.get('saved_settings_unchanged')):
        return False
    return bool(report.get('original_code_and_callbacks_verified') and report.get('callbacks_null')
                and report.get('console_idle') and report.get('restore_verified')
                and report.get('restored_f7') == '00' and report.get('session') == 'closed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--menu-snapshot', action='store_true',
                        help='Also compare the native connection menu and read its memory permissions; no menu writes')
    parser.add_argument('--journal', type=Path, default=p.ROOT/'ff80-rec-arm.json')
    parser.add_argument('--output', type=Path, default=p.ROOT/'ff80-rec-recovery-profile.json')
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: verify original REC patch bytes, idle console and NULL callbacks after restart.')
        print('Read current profile, saved slot-3 function and relevant mappings; restore cfg[f7]=0 and close USB.')
        print('No console, general RAM write, mode change, capture, setting save or flash.')
        if args.menu_snapshot:
            print('Also verify the connection-menu descriptor, three rows, label records and five live memory mappings.')
        return 0
    expected, extra, hashes = samples(menu_snapshot=args.menu_snapshot)
    raw = args.journal.read_bytes(); journal = json.loads(raw); source_kind = validate_source(journal, hashes)
    if journal.get('recovery_required') is False:
        print('Source trial restoration already verified; no USB access.')
        return 0
    if args.output.resolve() == args.journal.resolve(): raise RuntimeError('Evidence must use a new file')
    report = dict(kind=KIND, source_journal=str(args.journal), source_sha256=hashlib.sha256(raw).hexdigest(),
                  source_kind=source_kind,
                  image_hashes=hashes, started_at_utc=datetime.now(timezone.utc).isoformat(),
                  session='not_opened', restore_required=False, snapshot_complete=False)
    if args.menu_snapshot:
        report['menu_candidate'] = menu.inspect()
    extra_checks = None
    if source_kind == 'menu_clone_trial':
        extra_checks = {int(a, 16): bytes.fromhex(data) for a, data in journal['original_code'].items()}
        report['expected_profile_hex'] = journal['original_profile_hex']
        report['expected_saved_states'] = {name: journal['original_states'][name] for name in
            ('connection_mode', 'communication_selection', 'ui_communication_selection', 'slot3_function')}
    elif source_kind == 'completed_tether_profile_arm':
        report['expected_profile_hex'] = journal['original_profile_hex']
        report['expected_saved_states'] = {name: journal['profile_states'][name] for name in
            ('connection_mode', 'communication_selection', 'ui_communication_selection', 'slot3_function')}
    q.save_private(args.output, report, create=True)
    def checkpoint(): q.save_private(args.output, report)
    try:
        with usb_host.service_context(p.usb1) as context:
            devices = [d for d in context.getDeviceList() if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1: raise RuntimeError('Expected exactly one service camera')
            device = devices[0]
            report['device_node'] = usb_host.attachment_node(device.getBusNumber(), device.getDeviceAddress())
            if (source_kind == 'menu_clone_trial' and journal.get('allocation_restore_required')
                and report['device_node'] == journal.get('device_node')):
                raise RuntimeError('Menu allocation recovery requires a fresh power restart and USB enumeration')
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    t = Transport(handle, report, checkpoint, menu_snapshot=args.menu_snapshot)
                    t.reads.update((a, len(data)) for a, data in (extra_checks or {}).items())
                    try:
                        t.open_session(); report['session'] = 'open'; checkpoint()
                        jig = p.ffjlib.jig(t); report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Camera identity/config bounds mismatch')
                        collect(jig, t, expected, extra, report, checkpoint,
                                menu_snapshot=args.menu_snapshot, extra_checks=extra_checks)
                    finally:
                        if t.sid is not None and t.healthy and not t.awaiting:
                            t.close_session(); report['session'] = 'closed'
                        elif t.sid is not None: report['session'] = 'unconfirmed_after_error'
            finally: handle.close()
    except BaseException as exc: report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['recovery_verified'] = recovery_verified(report)
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); checkpoint()
    if report['recovery_verified']:
        current = args.journal.read_bytes()
        if current != raw: raise RuntimeError('Source journal changed; recovery evidence saved without altering source')
        journal.setdefault('recoveries', []).append(dict(report=args.output.name,
            verified_at_utc=report['finished_at_utc'], previous_obligations={key: journal.get(key) for key in
            ('restore_required', 'ram_restore_required', 'code_restore_required', 'profile_restore_required', 'recovery_required')},
            original_code_and_callbacks_verified=True, debug_flag_verified='00', session='closed'))
        journal.update(restore_required=False, restore_verified=True, ram_restore_required=False, code_restore_required=False,
                       recovery_required=False, code_restore_verified=True, patch_armed=False, ready_for_physical_play=False)
        if source_kind == 'completed_tether_profile_arm':
            journal.update(profile_restore_required=False, profile_restore_verified=True, tether_profile_armed=False)
        elif source_kind == 'menu_clone_trial':
            journal.update(allocation_restore_required=False, allocation_result_unknown=False,
                           menu_hook_armed=False, menu_clone_armed=False, menu_code_restore_verified=True,
                           allocation_obligation_closed_by_restart=True)
        q.save_private(args.journal, journal)
    print(json.dumps({k: report.get(k) for k in ('recovery_verified', 'snapshot_complete', 'states',
        'profile_fields', 'mappings', 'menu_snapshot_complete', 'menu_data_is_writable',
        'restored_f7', 'session', 'error')}, indent=2))
    return 0 if report['recovery_verified'] and report['snapshot_complete'] and 'error' not in report else 1


if __name__ == '__main__': raise SystemExit(main())
