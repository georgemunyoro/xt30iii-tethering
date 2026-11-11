"""Journaled orchestration of the existing native-menu proof and arm tools."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'analysis'))
from local_state import atomic_json, now
import usb_host
from import_firmware import OUTPUTS

ANALYSIS = ROOT/'analysis'
FLAGS = ('restore_required', 'ram_restore_required', 'code_restore_required',
         'profile_restore_required', 'allocation_restore_required', 'recovery_required')
KINDS = ('ff80_rec_patch_trial_v1', 'ff80_tether_profile_trial_v1', 'ff80_menu_clone_trial_v1')


def missing_references():
    return [name for name in OUTPUTS if not (ANALYSIS/name).is_file()]


def active_trials():
    result = []
    paths = {*ANALYSIS.glob('ff80-rec-*.json'), *ANALYSIS.glob('ff80-tether-profile-*.json'),
             *ANALYSIS.glob('ff80-menu-clone-*.json')}
    for path in sorted(paths):
        row = json.loads(path.read_text())
        if row.get('kind') in KINDS and any(row.get(k) for k in FLAGS):
            result.append((path, row))
    return result


def armed_menu(device):
    trials = active_trials()
    if len(trials) != 1: return None
    path, row = trials[0]
    if (row.get('kind') == 'ff80_menu_clone_trial_v1' and row.get('ready_for_physical_play')
            and row.get('device_node') == device['node'] and row.get('session') == 'closed'
            and not row.get('error')):
        return path
    return None


def prepare(data_root, on_status):
    token = uuid.uuid4().hex[:12]
    work = Path(data_root)/'setup'; work.mkdir(exist_ok=True, parents=True, mode=0o700)
    operation = work/f'{token}.json'
    state = dict(kind='native_menu_setup_v1', started=now(), complete=False, commands=[])
    def save(): atomic_json(operation, state)
    save()
    def run(script, arguments, output):
        command = [sys.executable, str(ANALYSIS/script), '--execute', *arguments, '--output', str(output)]
        log = work/f'{token}-{len(state["commands"])+1}-{script}.log'
        item = dict(script=script, output=str(output), log=str(log), started=now())
        state['commands'].append(item); save()
        with log.open('xb') as stream:
            process = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
        item.update(returncode=process.returncode, finished=now()); save()
        if not output.exists(): raise RuntimeError(f'{script} stopped before its journal. See {log.name}.')
        row = json.loads(output.read_text())
        if process.returncode or row.get('error'):
            error = row.get('error') or f'{script} failed; see {log.name}'
            if row.get('session') == 'not_opened' and any(s in error for s in ('USBErrorBusy', 'USBErrorAccess', 'USBHostError')):
                raise usb_host.access_error(error)
            raise RuntimeError(error)
        if row.get('session') != 'closed': raise RuntimeError('Camera session did not close cleanly')
        return row
    try:
        active = active_trials()
        if len(active) > 1: raise RuntimeError('Multiple unresolved experiments; inspect their recovery journals before setup')
        if active:
            on_status('preparing', 'Checking the previous setup', 'Keep power and USB connected while the restart is verified.')
            recovery = ANALYSIS/f'ff80-menu-clone-recovery-enabler-{token}.json'
            row = run('ff80_rec_recovery_profile.py', ['--menu-snapshot', '--journal', str(active[0][0])], recovery)
            if not row.get('recovery_verified'): raise RuntimeError('Previous setup recovery was not verified')
        on_status('preparing', 'Checking your camera', 'Reading the original menu and USB Card Reader settings.')
        baseline = ANALYSIS/f'ff80-menu-baseline-enabler-{token}.json'
        row = run('ff80_menu_baseline.py', [], baseline)
        if not row.get('recovery_verified'): raise RuntimeError('Fresh Card Reader baseline was not verified')
        on_status('preparing', 'Testing menu access', 'Keep the camera connected. This check restores its temporary changes before continuing.')
        proof = ANALYSIS/f'ff80-menu-clone-proof-enabler-{token}.json'
        row = run('ff80_menu_clone_trial.py', ['--phase', 'proof', '--recovery', str(baseline)], proof)
        if not row.get('menu_clone_proof_complete') or any(row.get(k) for k in FLAGS):
            raise RuntimeError('Menu proof or its restoration was incomplete')
        on_status('preparing', 'Exposing USB tethering', 'Keep power and USB connected until the PLAY instructions appear.')
        arm = ANALYSIS/f'ff80-menu-clone-arm-enabler-{token}.json'
        row = run('ff80_menu_clone_trial.py', ['--phase', 'arm', '--recovery', str(baseline), '--proof', str(proof)], arm)
        if not row.get('ready_for_physical_play') or not row.get('saved_state_unchanged'):
            raise RuntimeError('Menu handoff was not verified')
        state.update(complete=True, arm=str(arm), finished=now()); save()
        return arm
    except BaseException as exc:
        state.update(error=f'{type(exc).__name__}: {exc}', finished=now()); save()
        raise
