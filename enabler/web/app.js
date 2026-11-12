const $ = id => document.getElementById(id);
const names = {check: 'Check connection', enable: 'Enable menu', restart: 'Verify restart', restarted: 'Restarted — check camera', release_macos: 'Release Mac camera and retry'};
const steps = {disconnected: 1, checking: 1, normal: 2, service: 2, preparing: 2, menu: 3, select_fixed: 3, enabled: 4, verified: 5};
let token = '', busy = false, previous = '', actionError = '';
async function refresh() {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('The utility is unavailable.');
    const state = await response.json();
    token = state.token;
    const signature = JSON.stringify(state);
    if (signature === previous) return;
    previous = signature;
    $('title').textContent = state.title;
    $('message').textContent = state.message;
    $('status-label').textContent = ({enabled: 'READY TO TETHER', verified: 'RESTART VERIFIED', preparing: 'SETUP IN PROGRESS', error: 'NEEDS ATTENTION', mac_usb_busy: 'NEEDS ATTENTION'})[state.stage] || 'CAMERA CONNECTION';
    $('indicator').className = 'indicator ' + (['enabled', 'verified'].includes(state.stage) ? 'ready' : ['checking', 'preparing'].includes(state.stage) ? 'working' : '');
    $('demo').hidden = !state.demo;
    $('details').hidden = !state.detail;
    $('detail').textContent = state.detail || '';
    if (state.detail && ['error', 'mac_usb_busy', 'restart_required', 'setup_restart'].includes(state.stage)) $('details').open = true;
    $('actions').replaceChildren();
    for (const action of state.actions) {
      const button = document.createElement('button');
      button.textContent = names[action]; button.disabled = busy;
      button.addEventListener('click', () => act(action));
      $('actions').append(button);
    }
    const step = steps[state.stage] || 1;
    document.querySelectorAll('[data-step]').forEach(item => {
      const n = Number(item.dataset.step);
      item.classList.toggle('current', n === step);
      item.classList.toggle('done', n < step);
    });
    $('error').textContent = actionError;
    $('error').hidden = !actionError;
  } catch (error) {
    previous = '';
    $('error').textContent = 'Connection to the utility was lost. Keep its terminal open or launch it again.';
    $('error').hidden = false;
  }
}
async function act(action) {
  if (busy) return;
  busy = true; actionError = '';
  document.querySelectorAll('#actions button').forEach(button => button.disabled = true);
  try {
    const response = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Enabler-Token': token}, body: JSON.stringify({action})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'This action is unavailable.');
  } catch (error) {
    actionError = error.message;
    $('error').textContent = actionError; $('error').hidden = false;
  } finally { busy = false; previous = ''; await refresh(); }
}
refresh(); setInterval(refresh, 1000);
