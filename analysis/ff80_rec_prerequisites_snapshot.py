# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Fixed read-only service-to-REC prerequisites; restore temporary cfg[f7]."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import ff80_cmd_patch_probe as q

p = q.p
SAMPLES = (
    ('jig_play_dispatch', 0x02DE65D4, 0x4C),
    ('jig_root', 0x02E19EA0, 0x68),
    ('normal_rec_completion', 0x02E18DAC, 0x3C),
    ('mode_request_wrapper', 0x02E174D4, 0x1C),
)
STATES = (
    ('ui_event', 0x02A140A8, 16), ('ui_mode_history_candidate_action', 0x02A305F8, 24),
    ('mode_gate_af4', 0x02A30B9C, 4), ('mode_gate_ca8', 0x02A30D50, 4),
    ('ui_dispatch_states_0_to_79', 0x04695C48, 160),
    ('task24_dispatch_states', 0x0488BBF0, 32), ('task24_dispatch_rows', 0x04662760, 32),
    ('task52_dispatch_states', 0x048880B0, 32), ('usb_hardware_state', 0x0495D008, 2),
    ('event_routing_flag', 0x02411578, 4), ('backend', 0x0488A72C, 4),
    ('cmd_and_key_slots', q.CMD_SLOT, 16),
)


class Transport(q.Transport):
    def __init__(self, device, report, checkpoint):
        super().__init__(device, 'callback', report, checkpoint)
        self.reads |= {(a, n) for _, a, n in SAMPLES+STATES}

    def write_cmd(self, request):
        p.validate_request(request, self.reads, console=False)
        return super().write_cmd(request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, default=p.ROOT/'ff80-rec-prerequisites.json')
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: fixed original code/UI/task states and page permissions; temporary debug flag restored.')
        print('No general RAM writes, console command, keys, mode changes, saves or flash.')
        return 0
    expected, hashes = p.patch_expected_samples()
    b = p.Image('MainB')
    expected_extra = {a: b.data[a-b.base:a-b.base+n] for _, a, n in SAMPLES}
    report = dict(kind='ff80_rec_prerequisites_v1', image_hashes=hashes, restore_required=False,
                  session='not_opened', snapshot_complete=False, started_at_utc=datetime.now(timezone.utc).isoformat())
    q.save_private(args.output, report, create=True)
    def checkpoint(): q.save_private(args.output, report)
    try:
        with p.usb1.USBContext() as context:
            devices = [d for d in context.getDeviceList() if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1: raise RuntimeError('Expected one service camera')
            device = devices[0]
            report['device_node'] = f'/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}'
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    t = Transport(handle, report, checkpoint)
                    try:
                        t.open_session(); report['session'] = 'open'; checkpoint()
                        jig = p.ffjlib.jig(t)
                        report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Identity/config mismatch')
                        p.preflight(jig, t, expected, report, checkpoint, patch_state=True)
                        report.update(restore_required=True, restore_verified=False); checkpoint()
                        try:
                            jig.write_config_data(0, 0xF7, b'\1', mb=1)
                            if bytes(jig.read_config_data(0, 0xF7, 1, mb=1)) != b'\1': raise RuntimeError('Enable unverified')
                            for name, a, n in SAMPLES:
                                if p.read_fixed(t, a, n) != expected_extra[a]: raise RuntimeError(f'Code differs: {name}')
                            report['extra_state'] = {name: p.read_fixed(t, a, n).hex() for name, a, n in STATES}
                            root = struct.unpack('<Q', p.read_fixed(t, q.ROOT_POINTER, 8))[0]
                            report['mappings'] = {name: q.walk(root, a, t.table_entry) for name, a in
                                (('jig_play_branch', 0x02DE6614), ('jig_completion_branch', 0x02E19EE0), ('hello_scratch', q.HELLO))}
                            report['snapshot_complete'] = True; checkpoint()
                        finally:
                            if t.healthy:
                                jig.write_config_data(0, 0xF7, b'\0', mb=1)
                                flag = bytes(jig.read_config_data(0, 0xF7, 1, mb=1))
                                report.update(restored_f7=flag.hex(), restore_verified=flag == b'\0', restore_required=flag != b'\0'); checkpoint()
                    finally:
                        if t.sid is not None and t.healthy:
                            t.close_session(); report['session'] = 'closed'
                        elif t.sid is not None: report['session'] = 'unconfirmed_after_error'
            finally: handle.close()
    except BaseException as exc: report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); checkpoint()
    print(json.dumps({k: report.get(k) for k in ('snapshot_complete', 'extra_state', 'mappings', 'restored_f7', 'session', 'error')}, indent=2))
    return 0 if report['snapshot_complete'] and report.get('restore_verified') and report['session'] == 'closed' and 'error' not in report else 1


if __name__ == '__main__': raise SystemExit(main())
