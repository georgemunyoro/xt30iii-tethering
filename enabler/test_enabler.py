import copy
import http.client
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import utility as u
import enable_flow as f
from local_state import atomic_json


def device(address=7, product=0x0317):
    return dict(product=product, bus=2, address=address, node=f'/dev/bus/usb/002/{address:03}')


def snapshot(mode=5, selection=3):
    return dict(complete=True, session='closed', properties={
        '0xd16e': {'value': {'decoded': mode}}, '0xd041': {'value': {'decoded': selection}}},
        observations={'advertises_initiate_capture': mode == 5})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = u.Enabler(self.temp.name)

    def detect(self, dev=None, report=None, action=None):
        with patch.object(u.camera, 'devices', return_value=[dev or device()]), \
             patch.object(u, 'inspect_camera', return_value=(Path('check.json'), report or snapshot())) as inspect:
            self.app.detect(action)
            return inspect

    def test_already_enabled_never_prepares_and_closes_check(self):
        with patch.object(f, 'prepare', side_effect=AssertionError('unexpected patch')):
            self.detect()
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')
        self.assertNotIn('enable', self.app.snapshot()['actions'])
        self.assertEqual(self.app.workflow['enabled_node'], device()['node'])

    def test_reconnect_without_user_restart_confirmation_cannot_claim_persistence(self):
        self.detect()
        self.detect(device(8))
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')
        self.assertNotIn('restart_check', self.app.workflow)
        self.detect(device(8), action='restart')
        self.assertEqual(self.app.snapshot()['stage'], 'verified')

    def test_same_attachment_restart_button_does_not_read_or_claim_restart(self):
        self.detect()
        inspect = self.detect(action='restart')
        inspect.assert_not_called()
        self.assertEqual(self.app.snapshot()['stage'], 'enabled')

    def test_stock_mode_requires_physical_service_startup(self):
        self.detect(report=snapshot(0, 1))
        self.assertEqual(self.app.snapshot()['stage'], 'normal')
        with self.assertRaises(ValueError): self.app.action('enable')

    def test_auto_does_not_count_as_fixed(self):
        self.detect(report=snapshot(5, 2))
        self.assertEqual(self.app.snapshot()['stage'], 'select_fixed')

    def test_armed_service_is_only_a_handoff_no_commands(self):
        with patch.object(f, 'armed_menu', return_value=Path('arm.json')):
            inspect = self.detect(device(product=0xFF80))
        inspect.assert_not_called()
        self.assertEqual(self.app.snapshot()['stage'], 'menu')
        with self.assertRaises(ValueError): self.app.action('enable')

    def test_transport_timeout_is_not_retried_on_check_or_simple_reconnect(self):
        failed = dict(complete=False, session='uncertain_after_transport_error', error='Timeout')
        self.detect(report=failed)
        self.assertEqual(self.app.snapshot()['stage'], 'restart_required')
        self.detect(action='check').assert_not_called()
        self.detect(device(8)).assert_not_called()
        self.detect(device(8), action='restarted').assert_called_once()
        self.assertIsNone(self.app.workflow['fault'])

    def test_wrong_identity_and_busy_session_cannot_enable(self):
        for session in ('not_opened', 'not_acquired'):
            self.detect(report=dict(complete=False, session=session, error='Wrong model or busy'), action='check')
            self.assertEqual(self.app.snapshot()['stage'], 'error')
            self.assertNotIn('enable', self.app.snapshot()['actions'])

    def test_setup_failure_keeps_journal_fault_and_requires_new_attachment(self):
        service = device(product=0xFF80)
        with patch.object(u.camera, 'devices', return_value=[service]), \
             patch.object(f, 'missing_references', return_value=[]), \
             patch.object(f, 'armed_menu', return_value=None), \
             patch.object(f, 'prepare', side_effect=RuntimeError('lost acknowledgement')) as prepare:
            self.app.enable()
            self.assertEqual(self.app.snapshot()['stage'], 'setup_restart')
            with self.assertRaisesRegex(RuntimeError, 'physical restart'): self.app.enable()
            self.assertEqual(prepare.call_count, 1)
        self.detect(device(8)).assert_not_called()

    def test_killed_setup_fault_survives_relaunch(self):
        self.app.workflow.update(preparing=True, failed_node=device()['node']); self.app.save()
        relaunched = u.Enabler(self.temp.name)
        self.assertEqual(relaunched.workflow['fault'], 'setup')

    def test_interrupted_read_is_detected_on_launch(self):
        checks = Path(self.temp.name)/'checks'; checks.mkdir()
        atomic_json(checks/'old.json', dict(session='owned'))
        relaunched = u.Enabler(self.temp.name)
        self.assertEqual(relaunched.workflow['fault'], 'usb')

    def test_demo_never_uses_usb(self):
        with patch.object(u.camera, 'devices', side_effect=AssertionError('USB used')):
            demo = u.Enabler(self.temp.name, demo=True); demo.start()
            with self.assertRaises(ValueError): demo.action('enable')
        self.assertTrue(demo.snapshot()['demo'])

    def test_missing_firmware_blocks_setup_before_usb_access(self):
        with patch.object(f,'missing_references',return_value=['threadx-kernel.bin']), \
             patch.object(u.camera,'devices',side_effect=AssertionError('USB used')):
            self.app.enable()
        self.assertEqual(self.app.snapshot()['stage'],'firmware')
        self.assertNotIn('enable',self.app.snapshot()['actions'])


class SetupTests(unittest.TestCase):
    def run_setup(self, fail=None, active=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); analysis = root/'analysis'; analysis.mkdir()
            data = root/'logs'; data.mkdir()
            if active:
                atomic_json(analysis/'ff80-menu-clone-arm-old.json',
                    dict(kind='ff80_menu_clone_trial_v1', phase='arm', recovery_required=True))
            calls = []
            def run(command, **kwargs):
                name = Path(command[1]).name
                phase = command[command.index('--phase')+1] if '--phase' in command else None
                calls.append((name, phase))
                row = dict(session='closed', recovery_verified=True, menu_clone_proof_complete=True,
                           ready_for_physical_play=True, saved_state_unchanged=True)
                if fail == (name, phase): row['error'] = 'Injected failure'
                output = Path(command[command.index('--output')+1]); atomic_json(output, row)
                return SimpleNamespace(returncode=int('error' in row))
            with patch.object(f, 'ROOT', root), patch.object(f, 'ANALYSIS', analysis), \
                 patch.object(f.subprocess, 'run', side_effect=run):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'Injected failure'): f.prepare(data, lambda *args: None)
                else: self.assertTrue(f.prepare(data, lambda *args: None).exists())
            state = json.loads(next((data/'setup').glob('*.json')).read_text())
            return calls, state

    def test_fresh_baseline_proof_then_arm(self):
        calls, state = self.run_setup()
        self.assertEqual(calls, [('ff80_menu_baseline.py', None),
            ('ff80_menu_clone_trial.py', 'proof'), ('ff80_menu_clone_trial.py', 'arm')])
        self.assertTrue(state['complete'])

    def test_previous_menu_recovery_precedes_every_new_mutation(self):
        calls, _ = self.run_setup(active=True)
        self.assertEqual(calls[0], ('ff80_rec_recovery_profile.py', None))

    def test_each_failed_stage_prevents_all_later_stages(self):
        order = [('ff80_rec_recovery_profile.py', None), ('ff80_menu_baseline.py', None),
                 ('ff80_menu_clone_trial.py', 'proof'), ('ff80_menu_clone_trial.py', 'arm')]
        for i, stage in enumerate(order):
            with self.subTest(stage=stage):
                calls, state = self.run_setup(fail=stage, active=True)
                self.assertEqual(calls, order[:i+1]); self.assertFalse(state['complete'])

    def test_multiple_unresolved_trials_stop_before_camera_access(self):
        with tempfile.TemporaryDirectory() as directory:
            analysis = Path(directory)/'analysis'; analysis.mkdir()
            for i in range(2):
                atomic_json(analysis/f'ff80-menu-clone-arm-{i}.json',
                            dict(kind='ff80_menu_clone_trial_v1', recovery_required=True))
            with patch.object(f, 'ANALYSIS', analysis), patch.object(f.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Multiple unresolved'): f.prepare(Path(directory), lambda *a: None)
                run.assert_not_called()


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ff80_menu_baseline as b
        from service_fixture import Device
        cls.b, cls.Device = b, Device
        try: cls.expected, cls.extra, _ = b.r.samples(menu_snapshot=True)
        except FileNotFoundError: raise unittest.SkipTest('Import local firmware to run original-byte baseline tests')
        cls.originals = b.menu.recipes(b.r.p.Image('MainB'))[0]
        from service_fixture import baseline
        cls.reference = baseline(b.r)

    def case(self, changed=None):
        b = self.b; d = self.Device(self.expected, self.extra)
        for a, data in self.originals.items(): d.put(a, data)
        for name, a, n in b.r.STATES: d.put(a, self.reference['states'][name].to_bytes(n, 'little'))
        d.put(b.r.PROFILE, bytes.fromhex(self.reference['profile_hex']))
        for _, a in b.r.TARGETS + b.r.menu.MAPPING_TARGETS:
            d.put(0x10B000+8*(a>>21), struct.pack('<Q', (a & ~0x1FFFFF) | 0x609))
        if changed: d.put(*changed)
        report = {}; t = b.r.Transport(d, report, lambda: None, menu_snapshot=True); t.sid = 1
        t.reads.update((a,len(data)) for a,data in self.originals.items())
        return d, report, t

    def test_fresh_baseline_checks_native_menu_originals_and_leaves_flag_zero(self):
        b=self.b; d,report,t=self.case()
        b.collect(b.r.p.ffjlib.jig(t),t,self.expected,self.extra,report,lambda:None,self.originals)
        self.assertEqual(d.flag,b'\0'); self.assertTrue(report['menu_code_restore_verified'])
        self.assertEqual(report['states'],self.reference['states'])
        for request in d.requests:
            op,_,_,_,fn=struct.unpack('<5I',request[:20])
            self.assertTrue(op != 0x200001 or fn == 2)

    def test_active_hook_or_non_card_setting_cannot_be_a_baseline(self):
        b=self.b
        for changed in ((b.menu.m.LOOKUP_RET,b'\0'*4),(0x0297FF08,b'\4')):
            d,report,t=self.case(changed)
            with self.assertRaises(RuntimeError):
                b.collect(b.r.p.ffjlib.jig(t),t,self.expected,self.extra,report,lambda:None,self.originals)
            self.assertEqual(d.flag,b'\0')


class HTTPTests(unittest.TestCase):
    def test_tokens_origins_routes_and_demo_block_camera_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            app=u.Enabler(directory,demo=True); app.start()
            server=u.ThreadingHTTPServer(('127.0.0.1',0),u.Handler); server.app=app
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            def request(method,path,headers=None,body=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
                connection.request(method,path,body=body,headers=headers or {})
                response=connection.getresponse(); data=response.read(); connection.close()
                return response.status,data
            try:
                self.assertEqual(request('GET','/')[0],200)
                self.assertEqual(request('GET','/../../FWUP0037.DAT')[0],404)
                self.assertEqual(request('GET','/api/state',headers={'Host':'evil.test'})[0],403)
                self.assertEqual(request('POST','/api/action',body='{"action":"enable"}')[0],403)
                headers={'X-Enabler-Token':app.token,'Origin':'https://evil.test'}
                self.assertEqual(request('POST','/api/action',headers=headers,body='{"action":"enable"}')[0],403)
                self.assertEqual(request('POST','/api/action',headers={'X-Enabler-Token':app.token},body='{"action":"enable"}')[0],409)
            finally: server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
