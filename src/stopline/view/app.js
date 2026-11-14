'use strict';
const $ = (id) => document.getElementById(id);
const make = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
let current = null;
let loading = false;
let refreshAgain = false;
let busy = false;
let pending = null;
let latestKey = '';
let targetLoading = false;
let targetSignature = '';
const names = {'person:operator': 'Operator', 'agent:repair': 'Repair agent', 'agent:worker-1': 'Worker one', 'agent:worker-2': 'Worker two'};
const person = (name) => names[name] || name || 'Unknown';
const clock = (value) => value ? new Date(value).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}) : '—';
function title(row) {
  const target = (row.target || '').split('/').pop();
  if (row.tool === 'rollback') return `Roll back ${target}`;
  if (row.tool === 'restart') return `Restart ${target}`;
  if (row.tool === 'shift_traffic') return `Shift ${row.args?.percent ?? '?'}% to region B`;
  return `${row.tool || 'Action'} ${target}`.trim();
}
function chainText(rows) { return rows?.length ? rows.map(row => person(row.holder)).join(' → ') : 'No handed-down permission'; }
function refusalText(row) {
  const detail = row.detail || {};
  if (detail.revoked_by) return `Stopped by ${person(detail.revoked_by)}${detail.revoked_at ? ' at ' + clock(detail.revoked_at) : ''}`;
  const reasons = {revoked:'Permission stopped', expired:'Permission expired', limit_exceeded:'Permission limit reached', target_not_allowed:'Target outside permission', tool_not_allowed:'Action outside permission', cancelled_by_stop:'Blocked by the stop'};
  return reasons[row.reason] || 'Permission refused';
}
function actionButton(row, receipt = false) {
  const node = make('button', null, receipt ? 'receipt-row' : 'trace-row');
  node.dataset.actionId = row.action_id || '';
  const label = make('span', null, 'row-title');
  if (!receipt) label.append(make('span', person(row.principal), 'who'));
  label.append(document.createTextNode(title(row)));
  node.append(label);
  const outcome = make('span', row.outcome, `row-outcome ${row.outcome.replaceAll(' ', '-')}`);
  if (row.undo) outcome.append(make('span', {no_undo:'no undo', undone:'undone', undo_unknown:'undo unknown'}[row.undo] || row.undo, 'undo-mark'));
  node.append(outcome);
  const notes = [];
  if (receipt) notes.push(chainText(row.chain));
  else {
    if (row.outcome === 'unknown') notes.push('Sent · no answer');
    if (row.kind === 'refusal') notes.push(refusalText(row));
    if (row.held) notes.push('held by test schedule');
  }
  if (notes.length) node.append(make('span', notes.join(' · '), 'row-detail'));
  node.addEventListener('click', () => showChain(row.action_id));
  return node;
}
function stopLine(stop) {
  const line = make('div', null, 'stop-line');
  line.append(make('span', `STOP · ${person(stop.by)} · ${clock(stop.at)}`));
  return line;
}
function render(state) {
  current = state;
  const run = state.run;
  $('mode').textContent = ({flag:'STOP FLAG', stopline:'STOPLINE', fixed:'FIXED RULES'})[run?.mode] || 'READY';
  $('run-label').textContent = run ? (run.mode === 'stopline' ? 'Permission checked before every action' : 'Permission checked before the stop') : 'Press 1 to begin';
  $('context').textContent = `${run?.cluster === 'mock' ? 'Mock cluster · ' : ''}timing controlled · router built for this test`;
  $('permission-note').textContent = run?.mode === 'stopline' ? 'Permission: Operator → Repair agent → two workers' : 'A repair agent. Two workers. One stop.';
  $('workers').replaceChildren(...state.workers.map(worker => make('span', `${person(worker.principal)}: ${{killed:'crashed', held:'held by test schedule', stopped:'stopped', done:'completed', running:'running'}[worker.status] || worker.status}`, `worker-${worker.status}`)));
  const rows = [];
  let stopped = false;
  // The receipt has the full accounting; keep the live trace readable beside it.
  const visibleRows = state.receipt ? state.rows.slice(-5) : state.rows;
  for (const row of visibleRows) {
    if (state.stop && row.after_stop && !stopped) { rows.push(stopLine(state.stop)); stopped = true; }
    rows.push(actionButton(row));
  }
  if (state.stop && !stopped) rows.push(stopLine(state.stop));
  if (!rows.length) rows.push(make('p', 'Start the repair. Watch what happens when permission stops.', 'empty'));
  $('trace').replaceChildren(...rows);
  const omitted = state.omitted_rows + state.rows.length - visibleRows.length;
  $('omitted').textContent = omitted ? `${omitted} earlier rows · showing the latest ${visibleRows.length}` : '';
  const receipt = state.receipt;
  $('receipt').hidden = !receipt;
  if (receipt) {
    const heading = make('div', null, 'receipt-heading');
    const name = make('h2', 'The receipt'); name.id = 'receipt-title';
    heading.append(name, make('p', `Stopped by ${person(receipt.stopped_by)} · ${clock(receipt.stopped_at)}`), make('span', receipt.status.toUpperCase(), `receipt-state ${receipt.status === 'open' ? 'unknown' : 'completed'}`));
    $('receipt').replaceChildren(heading, ...receipt.rows.map(row => actionButton(row, true)));
  }
  confirmPending(state);
}
async function refresh() {
  if (loading) { refreshAgain = true; return; }
  loading = true;
  try {
    const response = await fetch('/api/state');
    const state = await response.json();
    if (!response.ok || state.pending) throw new Error('pending');
    render(state);
    $('connection').textContent = 'Live';
  } catch (_) {
    $('connection').textContent = 'Atlas pending · keeping confirmed state';
  } finally {
    loading = false;
    if (refreshAgain) { refreshAgain = false; setTimeout(refresh, 100); }
  }
}
function definition(list, label, value) { list.append(make('dt', label), make('dd', value ?? '—')); }
async function pollTargets() {
  if (targetLoading) return;
  targetLoading = true;
  try {
    const response = await fetch('/api/targets');
    if (!response.ok) throw new Error('pending');
    const states = await response.json();
    const signature = JSON.stringify(states);
    if (signature === targetSignature) return;
    targetSignature = signature;
    const sections = [];
    for (const name of ['cluster','router']) {
      const state = states[name] || {reachable:false};
      const section = make('section', null, `system ${state.reachable ? '' : 'unreachable'}`);
      const heading = make('h3', name === 'cluster' ? 'Cluster' : 'Router', 'system-heading');
      heading.append(make('span', state.reachable ? 'reachable' : 'unreachable'));
      section.append(heading);
      if (!state.reachable) section.append(make('p', 'Current state unknown. Waiting for the system.', 'quiet'));
      else if (name === 'cluster') {
        for (const [deployment, data] of Object.entries(state.deployments || {})) {
          const block = make('div', null, 'deployment'); block.append(make('h3', deployment));
          const list = make('dl');
          definition(list, 'Image', data.image);
          definition(list, 'Revision', `${data.revision ?? '—'}${data.ready === false ? ' · not ready' : ''}`);
          definition(list, 'Last restart', data.restarted_at || 'None');
          definition(list, 'Action id', data.action_id || 'None');
          block.append(list); section.append(block);
        }
      } else {
        section.append(make('p', Object.entries(state.weights || {}).map(([region, percent]) => `${region.replace('region-', 'Region ').toUpperCase()} ${percent}%`).join(' / '), 'weights'));
        const list = make('dl'); definition(list, 'Action id', state.last_action_id || 'None'); section.append(list);
      }
      sections.push(section);
    }
    $('targets').replaceChildren(...sections);
  } catch (_) {
    $('targets').replaceChildren(make('p', 'Systems pending. Current state unknown.', 'quiet'));
    targetSignature = '';
  } finally { targetLoading = false; }
}
function status(message, isPending = false) { $('key-status').textContent = `${latestKey ? latestKey + ' · ' : ''}${message}`; $('key-status').classList.toggle('pending', isPending); }
function confirmPending(state) {
  if (!pending || busy) return;
  const run = state.run;
  let confirmed = false;
  if (pending.key === '1' || pending.key === '2') confirmed = !!run && (pending.run_id ? run._id === pending.run_id : run._id !== pending.beforeRun);
  if (pending.key === 'S') confirmed = !!state.stop;
  if (pending.key === 'K') confirmed = state.workers.some(worker => worker.status === 'killed');
  if (pending.key === 'W') confirmed = state.workers.some(worker => worker.principal === 'agent:worker-2' && !['killed','held'].includes(worker.status));
  if (pending.key === 'R') confirmed = state.receipt?.status === 'closed';
  if (pending.key === 'U') confirmed = state.receipt?.rows.some(row => row.undo);
  if (confirmed) { status('Confirmed.'); pending = null; }
}
async function keypress(key) {
  if (busy) return;
  if (key === 'M' && $('numbers-dialog').open) { $('numbers-dialog').close(); return; }
  busy = true; latestKey = key; status('pending', true);
  const beforeRun = current?.run?._id;
  try {
    const response = await fetch('/api/key/' + key, {method:'POST'});
    const result = await response.json();
    if (key === 'M') {
      $('numbers-lines').replaceChildren(...(result.lines?.length ? result.lines : [result.message]).map(line => make('p', line)));
      if (!$('numbers-dialog').open) $('numbers-dialog').showModal();
    }
    pending = result.pending ? {...result, key, beforeRun} : null;
    status(result.pending ? `pending · ${result.message}` : result.message || 'Request failed.', !!result.pending);
  } catch (_) { status('Connection lost · request unconfirmed.'); }
  finally { busy = false; await refresh(); await pollTargets(); }
}
async function showChain(actionId) {
  if (!actionId) return;
  $('chain-content').replaceChildren(make('p', 'pending', 'unknown'));
  if (!$('chain-dialog').open) $('chain-dialog').showModal();
  try {
    const response = await fetch('/api/chain/' + encodeURIComponent(actionId));
    if (!response.ok) throw new Error('missing');
    const data = await response.json();
    const nodes = [make('p', actionId, 'quiet')];
    const list = make('ol');
    for (const permission of data.permissions) list.append(make('li', `${person(permission.grantor)} granted permission to ${person(permission.holder)} · ${permission.status === 'revoked' ? 'stopped' : 'active'}`));
    nodes.push(list, make('h3', 'Policy'), make('p', data.policy ? `Version ${data.policy._id} · ${data.policy.text}` : 'No policy recorded for this action.'));
    nodes.push(make('h3', 'The decision'));
    const decisions = data.audit.filter(entry => ['allow', 'deny', 'claim', 'outcome', 'undo'].includes(entry.kind));
    if (!decisions.length) nodes.push(make('p', data.effect ? `Recorded as ${data.effect.state === 'applied' ? 'completed' : (data.effect.state === 'sent' ? 'unknown' : data.effect.state === 'cancelled' ? 'blocked' : 'waiting')}.` : 'No decision recorded.'));
    for (const entry of decisions) nodes.push(make('p', `${clock(entry.at)} · ${{allow:'Permission granted', deny:'Refused', claim:'Sent', outcome:'Outcome recorded', undo:'Undo requested'}[entry.kind]}${entry.kind === 'deny' ? ' · ' + refusalText(entry) : ''}`));
    nodes.push(make('h3', 'The target’s evidence'), make('pre', data.effect?.evidence ? JSON.stringify(data.effect.evidence, null, 2) : 'Unknown — no target evidence recorded.'));
    $('chain-content').replaceChildren(...nodes);
  } catch (_) { $('chain-content').replaceChildren(make('p', 'Evidence pending. Close and try again.', 'unknown')); }
}
document.addEventListener('keydown', event => {
  if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
  const key = event.key.toUpperCase();
  if (!'12SKWRUVM'.includes(key) || key.length !== 1 || $('chain-dialog').open) return;
  if ($('numbers-dialog').open && key !== 'M') $('numbers-dialog').close();
  event.preventDefault(); keypress(key);
});
document.querySelectorAll('[data-key]').forEach(button => button.addEventListener('click', () => keypress(button.dataset.key)));
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
const events = new EventSource('/api/events');
events.addEventListener('rebuild', refresh);
events.addEventListener('change', () => { refresh(); pollTargets(); });
events.onerror = () => { $('connection').textContent = 'Reconnecting · keeping confirmed state'; };
refresh(); pollTargets();
setInterval(pollTargets, 1500);
setInterval(() => { if (pending || $('connection').textContent !== 'Live') refresh(); }, 2000);
window.addEventListener('pagehide', () => events.close());
