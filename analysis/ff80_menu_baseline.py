# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Read a fresh Card Reader baseline for native-menu setup; no RAM code writes.

Uses the existing fixed service reads and temporary cfg[f7] access. Checks
every original menu-hook byte as well as the original menu data. This never
closes another experiment's journal or substitutes for its recovery check.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import ff80_menu_clone_trial as menu
import ff80_rec_recovery_profile as r
import usb_host


def verify_card_reader(report):
    if (report.get('profile_fields') != dict(slot=1, function=1, option=0, transport=2)
            or report.get('states', {}).get('connection_mode') != 0):
        raise RuntimeError('Select USB CARD READER before restarting in service mode')
    if not report.get('menu_code_restore_verified') or not report.get('menu_snapshot_complete'):
        raise RuntimeError('Original menu code and data were not verified')


def collect(jig, transport, expected, extra, report, save, originals):
    r.collect(jig, transport, expected, extra, report, save,
              menu_snapshot=True, extra_checks=originals)
    verify_card_reader(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, default=r.p.ROOT/'ff80-menu-baseline.json')
    args = parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: verify original menu/code, idle callbacks and Card Reader; read a fresh baseline.')
        return 0
    expected, extra, hashes = r.samples(menu_snapshot=True)
    originals = menu.recipes(r.p.Image('MainB'))[0]
    report = dict(kind=r.KIND, source_kind='fresh_menu_baseline', image_hashes=hashes,
                  started_at_utc=datetime.now(timezone.utc).isoformat(), session='not_opened',
                  restore_required=False, snapshot_complete=False, recovery_verified=False)
    r.q.save_private(args.output, report, create=True)
    def save(): r.q.save_private(args.output, report)
    try:
        with usb_host.service_context(r.p.usb1) as context:
            devices = [d for d in context.getDeviceList()
                       if (d.getVendorID(), d.getProductID()) == (0x04CB, 0xFF80)]
            if len(devices) != 1: raise RuntimeError('Expected exactly one service camera')
            device = devices[0]
            report['device_node'] = usb_host.attachment_node(device.getBusNumber(), device.getDeviceAddress())
            handle = device.open()
            try:
                with handle.claimInterface(0):
                    transport = r.Transport(handle, report, save, menu_snapshot=True)
                    transport.reads.update((a, len(data)) for a, data in originals.items())
                    try:
                        transport.open_session(); report['session'] = 'open'; save()
                        jig = r.p.ffjlib.jig(transport)
                        report['model'] = jig.get_device_info(4)
                        if report['model'] != 'X-T30 III' or jig.get_config_data_size(mb=1) != (0x960000, 0x100):
                            raise RuntimeError('Camera identity/config mismatch')
                        collect(jig, transport, expected, extra, report, save, originals)
                    finally:
                        if transport.sid is not None and transport.healthy and not transport.awaiting:
                            transport.close_session(); report['session'] = 'closed'
                        elif transport.sid is not None:
                            report['session'] = 'unconfirmed_after_error'
            finally:
                handle.close()
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['recovery_verified'] = bool(not report.get('error') and r.recovery_verified(report)
            and report.get('menu_code_restore_verified') and report.get('menu_snapshot_complete'))
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); save()
    print(json.dumps({k: report.get(k) for k in
        ('recovery_verified', 'states', 'profile_fields', 'session', 'restored_f7', 'error')}, indent=2))
    return 0 if report['recovery_verified'] else 1


if __name__ == '__main__': raise SystemExit(main())
