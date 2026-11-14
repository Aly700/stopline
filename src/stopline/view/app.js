'use strict';
const $ = (id) => document.getElementById(id);
const make = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
let current = null;
let liveState = null;
let selectedRun = null;
let targetStates = null;
let runsLoading = false;
let runList = [];
let navigationVersion = 0;
let runSignature = '';
let loading = false;
let refreshAgain = false;
let busy = false;
let pending = null;
let latestKey = '';
let targetLoading = false;
let targetSignature = '';
let operationLoading = false;
let validationTimer;
let searchRequest = null;
let searchFocus = null;
const targetValues = new Map();
const targetHighlights = new Map();
const names = {'person:operator': 'Operator', 'agent:repair': 'Repair agent', 'agent:worker-1': 'Worker one', 'agent:worker-2': 'Worker two'};
const person = (name) => names[name] || name || 'Unknown';
const clock = (value) => value ? new Date(value).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}) : '—';
function title(row) {
  const target = (row.target || '').split('/').pop();
  if (row.reverses && row.tool === 'shift_traffic') return `Undo: shift region B back to ${row.args?.percent ?? '?'}%`;
  if (row.reverses && row.tool === 'rollback') return `Undo rollback of ${target}`;
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
  const outcome = make('span', row.outcome, `row-outcome ${row.outcome.toLowerCase().replaceAll(' ', '-')}`);
  if (row.undo) outcome.append(make('span', {no_undo:'no undo', undone:'undone', undo_unknown:'undo unknown'}[row.undo] || row.undo, 'undo-mark'));
  node.append(outcome);
  const notes = [];
  if (receipt) notes.push(chainText(row.chain));
  else {
    if (row.ran_after_stop) notes.push(`started after the stop · ${row.target_at_epoch != null ? clock(row.target_at_epoch * 1000) + ' target clock' : 'target clock pending'}`);
    if (row.outcome === 'unknown') notes.push('Sent · no answer');
    if (row.kind === 'refusal') notes.push(refusalText(row));
    if (row.kind === 'action' && row.outcome === 'blocked') notes.push(row.refusal ? `Cancelled before it was sent · ${refusalText(row.refusal)}` : 'Cancelled before it was sent');
    if (row.held) notes.push('held by test schedule');
  }
  if (row.outcome === 'STOPPED IN TIME') notes.push('claimed, then fenced at the target before it arrived');
  if (row.reconciled) notes.push('asked the router by action id · applied');
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
  const pressed = selectedRun ? null : state.last_key;
  if (pressed && pressed.n !== seenKey) {
    // A key pressed anywhere (this page, another page, a script) shows on every open page.
    const first = seenKey === undefined;
    seenKey = pressed.n;
    if (first) {
      latestKey = pressed.key;
      pending = pressed.pending ? {key:pressed.key} : null;
      status(pressed.message, pressed.pending);
    }
    if (!first) {
      if (!busy) {
        latestKey = pressed.key;
        pending = pressed.pending ? {key:pressed.key} : null;
        status(pressed.message, pressed.pending);
      }
      if (pressed.key === 'M' && pressed.lines?.length) {
        $('numbers-lines').replaceChildren(...pressed.lines.map(line => make('p', line)));
        $('numbers-note').hidden = false;
        if (!$('numbers-dialog').open) $('numbers-dialog').showModal();
      } else if (pressed.key !== 'M' && $('numbers-dialog').open) $('numbers-dialog').close();
    }
  } else if (!pressed && seenKey === undefined) seenKey = 0;
  const run = state.run;
  const liveAgent = String(run?.planner || '').startsWith('deep agent');
  $('mode').textContent = liveAgent ? 'RUN 3 · LIVE AGENT' : ({flag:'RUN 1 · STOP FLAG', stopline:'RUN 2 · STOPLINE', fixed:'FIXED RULES'})[run?.mode] || 'READY';
  $('run-label').textContent = liveAgent ? `${run.planner} · every tool call checked in MongoDB Atlas` : ({flag:'Workers check a stop flag before each step', fixed:'Rules read once when the run starts', stopline:'Every action checked in MongoDB Atlas, in one transaction'})[run?.mode] || 'Press 1 to begin';
  $('context').textContent = `${run?.cluster === 'mock' ? 'Mock cluster · ' : ''}timing controlled · router built for this test`;
  const repair = state.permissions.find(permission => permission.holder === 'agent:repair');
  const workers = state.permissions.filter(permission => ['agent:worker-1', 'agent:worker-2'].includes(permission.holder));
  $('permission-note').textContent = run?.mode === 'stopline'
    ? (repair ? `Permission ${repair.status === 'revoked' ? 'stopped' : 'granted'}: Operator → Repair agent → ${workers.length} workers` : 'Permission pending')
    : 'A repair agent. Two workers. One stop.';
  $('workers').replaceChildren(...state.workers.map(worker => make('span', `${person(worker.principal)}: ${{killed:'crashed', held:'held by test schedule', stopped:'stopped', done:'completed', running:'running'}[worker.status] || worker.status}`, `worker-${worker.status}`)));
  const ready = selectedRun ? null : state.ready_key;
  $('readiness').textContent = ready ? `Ready for ${ready}` : '';
  $('ready-key').textContent = ready || '';
  $('ready-button').hidden = !ready;
  $('action-count').textContent = `${state.rows.length + state.omitted_rows} recorded`;
  document.querySelectorAll('.commands [data-key]').forEach(button => button.classList.toggle('next-command', button.dataset.key === ready));
  updateEnvironment();
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
  const baseline = ['flag', 'fixed'].includes(run?.mode);
  $('receipt').hidden = !receipt && !baseline;
  $('receipt').classList.toggle('no-receipt', baseline && !receipt);
  if (baseline && !receipt) {
    const note = make('p', 'No receipt. A stop flag keeps no record of what was already started.');
    note.id = 'receipt-title';
    $('receipt').replaceChildren(note);
  }
  if (receipt) {
    const heading = make('div', null, 'receipt-heading');
    const name = make('h2', 'The receipt'); name.id = 'receipt-title';
    heading.append(name, make('p', `Stopped by ${person(receipt.stopped_by)} · ${clock(receipt.stopped_at)}`), make('span', receipt.status.toUpperCase(), `receipt-state ${receipt.status === 'open' ? 'unknown' : 'completed'}`));
    $('receipt').replaceChildren(heading, ...receipt.rows.map(row => actionButton(row, true)));
  }
  if (pending && !selectedRun) pollOperation();
}
async function refresh() {
  if (loading) { refreshAgain = true; return; }
  loading = true;
  try {
    const selected = selectedRun;
    const response = await fetch('/api/state' + (selected ? '?run_id=' + encodeURIComponent(selected) : ''));
    const state = await response.json();
    if (!response.ok || state.pending) throw new Error('pending');
    if (selected !== selectedRun) return;
    if (!selected) liveState = state;
    render(state);
    connection('Live');
  } catch (_) {
    connection('Atlas pending · keeping confirmed state');
  } finally {
    loading = false;
    if (refreshAgain) { refreshAgain = false; setTimeout(refresh, 100); }
  }
}
function targetValue(tag, key, value) {
  const text = String(value ?? '—');
  const now = Date.now();
  if (targetValues.has(key) && targetValues.get(key) !== text) targetHighlights.set(key, now + 1000);
  targetValues.set(key, text);
  const node = make(tag, text);
  node.dataset.targetValue = key;
  const remaining = (targetHighlights.get(key) || 0) - now;
  if (remaining > 0) {
    node.classList.add('target-changed');
    setTimeout(() => node.classList.remove('target-changed'), remaining);
  }
  return node;
}
function definition(list, label, value, key) {
  list.append(make('dt', label), targetValue('dd', key, value));
}
async function pollTargets() {
  if (targetLoading) return;
  targetLoading = true;
  try {
    const response = await fetch('/api/targets');
    if (!response.ok) throw new Error('pending');
    const states = await response.json();
    targetStates = states;
    updateEnvironment();
    if (selectedRun) return;
    const signature = JSON.stringify(states);
    if (signature === targetSignature) return;
    targetSignature = signature;
    renderTargets(states);
  } catch (_) {
    targetStates = null;
    updateEnvironment();
    if (!selectedRun) $('targets').replaceChildren(make('p', 'Systems pending. Current state unknown.', 'quiet'));
    targetSignature = '';
  } finally { targetLoading = false; }
}
function renderTargets(states, saved = false) {
  const sections = [];
    for (const name of ['cluster','router']) {
      const state = states[name] || {reachable:false};
      const section = make('section', null, `system ${saved || state.reachable ? '' : 'unreachable'}`);
      const heading = make('h3', name === 'cluster' ? 'Cluster' : 'Router', 'system-heading');
      heading.append(targetValue('span', name + '/reachable', saved ? 'stored evidence' : state.reachable ? 'reachable' : 'unreachable'));
      section.append(heading);
      if (saved && !states[name]) section.append(make('p', 'No target evidence in the stored actions.', 'quiet'));
      else if (!saved && !state.reachable) section.append(make('p', 'Current state unknown. Waiting for the system.', 'quiet'));
      else if (name === 'cluster') {
        for (const [deployment, data] of Object.entries(state.deployments || {})) {
          const block = make('div', null, 'deployment'); block.append(make('h3', deployment));
          const list = make('dl');
          const prefix = `cluster/${deployment}/`;
          definition(list, 'Image', data.image, prefix + 'image');
          definition(list, 'Version / rev.', `${data.version ?? '—'} / ${data.revision ?? '—'}${data.ready === false ? ' · not ready' : ''}`, prefix + 'revision');
          definition(list, 'Last restart', data.restarted_at ? clock(data.restarted_at) : 'never', prefix + 'restart');
          definition(list, 'Action id', data.action_id || 'none yet', prefix + 'action');
          block.append(list); section.append(block);
        }
      } else {
        const weights = make('p', null, 'weights');
        for (const [region, percent] of Object.entries(state.weights || {})) {
          const weight = make('span', null, 'weight');
          const value = targetValue('span', 'router/' + region, `${percent}%`);
          value.classList.add('weight-value');
          weight.append(make('span', region.replace('region-', 'Region ').toUpperCase()), value);
          weights.append(weight);
        }
        section.append(weights);
        const list = make('dl'); definition(list, 'Action id', state.last_action_id || 'none yet', 'router/action'); section.append(list);
      }
      sections.push(section);
    }
    $('targets').replaceChildren(...sections);
}
function renderHistoricalTargets(state) {
  const saved = {};
  const rows = [...(state.receipt?.rows || []), ...state.rows].sort((a,b) => (a.evidence?.at_epoch || 0) - (b.evidence?.at_epoch || 0));
  for (const row of rows) {
    const evidence = row.evidence;
    if (evidence?.system === 'cluster' && evidence.after && evidence.deployment) {
      saved.cluster ||= {deployments:{}};
      saved.cluster.deployments[evidence.deployment] = evidence.after;
    }
    if (evidence?.system === 'router' && evidence.weights) saved.router = {weights:evidence.weights, last_action_id:evidence.action_id};
  }
  renderTargets(saved, true);
}
let seenKey;
function status(message, isPending = false) { $('key-status').textContent = `${latestKey ? latestKey + ' · ' : ''}${message}`; $('key-status').classList.toggle('pending', isPending); }
function showValidation(result) {
  const detail = result.detail || {};
  const refusal = detail.refusal || detail.error || detail.message || result.message;
  $('validation-text').textContent = typeof refusal === 'string' ? refusal : JSON.stringify(refusal, null, 2);
  $('validation-panel').hidden = false;
  clearTimeout(validationTimer);
  validationTimer = setTimeout(() => { $('validation-panel').hidden = true; }, 5000);
}
async function pollOperation() {
  if (!pending || busy || operationLoading || selectedRun) return;
  operationLoading = true;
  const request = pending;
  try {
    const response = await fetch('/api/operation/' + request.key);
    const result = await response.json();
    if (response.ok && !result.pending && pending === request) {
      pending = null;
      status(result.message || 'Request finished.');
      if (request.key === 'V') showValidation(result);
    }
  } catch (_) { /* Keep pending until the stage can confirm its own result. */ }
  finally { operationLoading = false; }
}
async function keypress(key) {
  if (key === 'F') { toggleStage(); return; }
  if (selectedRun && !['/', 'P', 'T', 'M'].includes(key)) {
    latestKey = key; status('Earlier run is read-only. Back to live to use commands.'); return;
  }
  if (key === '/') { openAsk(); return; }
  if (busy) return;
  if (!selectedRun) pending = null;
  if (key === 'P') {
    latestKey = key;
    if (!current?.run) { status('Start a run to open its report.'); return; }
    window.open('/report/' + encodeURIComponent(current.run._id), '_blank', 'noopener');
    status('Opened the run report.');
    return;
  }
  if (key === 'T') {
    latestKey = key;
    if (!current?.run?.trace_url) { status('This run has no model trace.'); return; }
    window.open(current.run.trace_url, '_blank', 'noopener');
    status('Opened the model trace in LangSmith.');
    return;
  }
  if (key === 'M' && $('numbers-dialog').open) { $('numbers-dialog').close(); return; }
  busy = true; latestKey = key; status('pending', true);
  const beforeRun = current?.run?._id;
  try {
    const response = await fetch('/api/key/' + key, {method:'POST'});
    const result = await response.json();
    if (key === 'M') {
      $('numbers-lines').replaceChildren(...(result.lines?.length ? result.lines : [result.message]).map(line => make('p', line)));
      $('numbers-note').hidden = !result.lines?.length;
      if (!$('numbers-dialog').open) $('numbers-dialog').showModal();
    }
    pending = result.pending ? {...result, key, beforeRun} : null;
    if (key === 'V' && !result.pending) showValidation(result);
    status(result.pending ? `pending · ${result.message}` : result.message || 'Request failed.', !!result.pending);
  } catch (_) { status('Connection lost · request unconfirmed.'); }
  finally { busy = false; await refresh(); await pollTargets(); if (pending) pollOperation(); }
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
    if (!decisions.length) nodes.push(make('p', data.effect ? `Recorded as ${data.effect.state === 'applied' ? 'completed' : (data.effect.state === 'sent' ? 'unknown' : data.effect.state === 'cancelled' ? 'blocked' : data.effect.state === 'rejected' ? 'no effect' : 'waiting')}.` : 'No decision recorded.'));
    for (const entry of decisions) nodes.push(make('p', `${clock(entry.at)} · ${{allow:'Permission granted', deny:'Refused', claim:'Sent', outcome:'Outcome recorded', undo:'Undo requested'}[entry.kind]}${entry.kind === 'deny' ? ' · ' + refusalText(entry) : ''}`));
    nodes.push(make('h3', 'The target’s evidence'), make('pre', data.effect?.evidence ? JSON.stringify(data.effect.evidence, null, 2) : (data.effect?.state === 'cancelled' || !data.effect ? 'Blocked before sending. No target evidence is needed.' : data.effect?.state === 'admitted' ? 'Waiting. Nothing sent yet.' : 'Unknown — no target evidence recorded.')));
    $('chain-content').replaceChildren(...nodes);
  } catch (_) { $('chain-content').replaceChildren(make('p', 'Evidence pending. Close and try again.', 'unknown')); }
}
function openAsk() {
  if ($('chain-dialog').open) return;
  if ($('numbers-dialog').open) $('numbers-dialog').close();
  if ($('ask-panel').hidden) searchFocus = document.activeElement;
  $('ask-panel').hidden = false;
  $('ask-input').focus();
}
function closeAsk() {
  searchRequest?.abort();
  searchRequest = null;
  $('ask-panel').hidden = true;
  $('ask-results').hidden = true;
  searchFocus?.focus();
}
async function askAudit(event) {
  event.preventDefault();
  const question = $('ask-input').value.trim();
  if (!question) return;
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  $('ask-question').textContent = question;
  $('ask-engine').textContent = '';
  $('ask-rows').replaceChildren();
  $('ask-message').textContent = 'searching';
  $('ask-results').hidden = false;
  try {
    const response = await fetch('/api/ask?' + new URLSearchParams({q: question}), {signal: request.signal});
    if (searchRequest !== request) return;
    if (response.status === 404) { $('ask-message').textContent = 'Search is not enabled.'; return; }
    if (!response.ok) throw new Error('search unavailable');
    const result = await response.json();
    if (searchRequest !== request) return;
    $('ask-engine').textContent = result.engine ? `found with ${result.engine}` : '';
    $('ask-message').textContent = result.ok ? (result.rows.length ? '' : result.message || 'No matching entries.') : result.message;
    if (!result.ok) return;
    $('ask-rows').replaceChildren(...result.rows.slice(0, 5).map(row => {
      const node = make(row.action_id ? 'button' : 'div', null, 'ask-row');
      node.append(make('time', clock(row.at), 'ask-time'), make('span', row.text, 'ask-text'), make('span', `score ${row.score}`, 'ask-score'));
      if (row.action_id) node.addEventListener('click', () => showChain(row.action_id));
      return node;
    }));
  } catch (error) {
    if (searchRequest === request && error.name !== 'AbortError') $('ask-message').textContent = 'Search is unavailable. Try again.';
  }
}
$('ask-form').addEventListener('submit', askAudit);
$('ask-close').addEventListener('click', closeAsk);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('ask-panel').hidden && !$('chain-dialog').open && !$('numbers-dialog').open) {
    event.preventDefault(); closeAsk(); return;
  }
  if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
  const key = event.key.toUpperCase();
  if (key === 'F') { event.preventDefault(); toggleStage(); return; }
  if (!'123SKWRUVMPXT/F'.includes(key) || key.length !== 1 || $('chain-dialog').open) return;
  if ($('numbers-dialog').open && key !== 'M') $('numbers-dialog').close();
  event.preventDefault(); keypress(key);
});
document.querySelectorAll('[data-key]').forEach(button => button.addEventListener('click', () => keypress(button.dataset.key)));
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
const events = new EventSource('/api/events');
events.addEventListener('rebuild', refresh);
events.addEventListener('change', event => {
  const payload = JSON.parse(event.data);
  if (payload.state) {
    liveState = payload.state;
    if (!selectedRun) render(payload.state);
    connection(payload.connected ? 'Live' : 'Updates reconnecting');
    pollRuns();
    pollTargets();
  } else {
    connection('Atlas pending · keeping confirmed state');
  }
});
events.onerror = () => connection('Reconnecting · keeping confirmed state');
restoreStage();
refresh(); pollTargets(); pollRuns();
setInterval(pollTargets, 1500);
setInterval(pollOperation, 750);
setInterval(() => { if (pending || $('connection').textContent !== 'Live') refresh(); }, 2000);
window.addEventListener('pagehide', () => events.close());

function connection(message) {
  $('connection').textContent = message;
  $('connection').classList.toggle('connected', message === 'Live');
  $('env-atlas').textContent = message === 'Live' ? 'Connected' : 'Reconnecting';
}
function updateEnvironment() {
  const run = liveState?.run || (!selectedRun ? current?.run : null);
  $('env-cluster').textContent = `${run?.cluster || '—'} · ${targetStates?.cluster?.reachable ? 'reachable' : 'unknown'}`;
  $('env-router').textContent = targetStates ? (targetStates.router?.reachable ? 'Reachable' : 'Unreachable') : 'Pending';
  const planner = run?.planner;
  $('env-model').textContent = !planner ? '—' : ['scripted', 'saved'].includes(planner) ? 'saved plan' : planner.replace(/^deep agent\s*[·:]?\s*/, '');
  $('env-trace').textContent = run ? (run.trace_url ? 'On' : 'Off') : '—';
}
function setStage(enabled) {
  document.body.classList.toggle('stage', enabled);
  $('stage-toggle').setAttribute('aria-pressed', String(enabled));
  $('stage-toggle').querySelector('span').textContent = enabled ? 'Console' : 'Stage';
  try { localStorage.setItem('stopline-stage', String(enabled)); } catch (_) { /* Storage may be disabled. */ }
}
function restoreStage() {
  try { setStage(localStorage.getItem('stopline-stage') === 'true'); } catch (_) { setStage(false); }
}
function toggleStage() { setStage(!document.body.classList.contains('stage')); }
$('stage-toggle').addEventListener('click', toggleStage);
$('ready-button').addEventListener('click', () => { if (!selectedRun && current?.ready_key) keypress(current.ready_key); });
document.querySelectorAll('[data-section]').forEach(button => button.addEventListener('click', () => {
  const section = $(button.dataset.section);
  if (section.hidden) { latestKey = ''; status('No receipt yet. A confirmed stop creates the StopLine receipt.'); return; }
  section.scrollIntoView({block:'start', behavior:'instant'});
  document.querySelectorAll('[data-section]').forEach(item => item.classList.toggle('active', item === button));
}));
async function pollRuns() {
  if (runsLoading) return;
  runsLoading = true;
  try {
    const response = await fetch('/api/runs');
    if (!response.ok) throw new Error('pending');
    runList = await response.json();
    renderRuns();
  } catch (_) { if (!runList.length) $('runs-list').replaceChildren(make('p', 'Runs pending.', 'quiet')); }
  finally { runsLoading = false; }
}
function renderRuns() {
  $('runs-count').textContent = runList.length || '';
  const selected = selectedRun || liveState?.run?._id || current?.run?._id;
  const signature = JSON.stringify([runList, selected]);
  if (signature === runSignature) return;
  runSignature = signature;
  const focused = document.activeElement?.dataset.runId;
  $('runs-list').replaceChildren(...runList.map((run, index) => {
    const label = String(run.planner || '').startsWith('deep agent') ? 'Live agent' : ({flag:'Stop flag', stopline:'StopLine', fixed:'Fixed rules'})[run.mode] || run.mode;
    const button = make('button', null, 'run-item');
    button.dataset.runId = run.id;
    button.setAttribute('aria-current', String(run.id === selected));
    button.title = `${label} · ${clock(run.created_at)} · ${run.status} · ${run.id}`;
    button.setAttribute('aria-label', button.title);
    const time = make('time', clock(run.created_at)); time.dateTime = run.created_at;
    button.append(make('span', null, `run-dot ${run.status === 'running' ? 'running' : run.status === 'stopped' ? 'stopped' : ''}`), make('span', label, 'run-mode'), time, make('span', index === 0 ? 'live' : run.receipt_status || run.status, 'run-status'));
    button.addEventListener('click', () => index === 0 ? backToLive() : openRun(run.id));
    return button;
  }));
  if (!runList.length) $('runs-list').append(make('p', 'No runs yet. Press 1 to begin.', 'quiet'));
  if (focused) [...$('runs-list').children].find(button => button.dataset.runId === focused)?.focus({preventScroll:true});
}
async function openRun(id) {
  const version = ++navigationVersion;
  try {
    const response = await fetch('/api/state?run_id=' + encodeURIComponent(id));
    const state = await response.json();
    if (!response.ok || !state.run) throw new Error('missing');
    if (version !== navigationVersion) return;
    selectedRun = id;
    closeAsk();
    $('history-banner').hidden = false;
    $('history-id').textContent = id;
    $('target-scope').textContent = 'Stored evidence';
    renderHistoricalTargets(state);
    document.querySelectorAll('[data-key]').forEach(button => { button.disabled = !['/', 'P', 'T', 'M'].includes(button.dataset.key); });
    render(state); renderRuns();
    $('workspace').scrollTop = 0;
    latestKey = ''; status('Earlier run · read-only.');
  } catch (_) { if (version === navigationVersion) status('Run unavailable. Keeping the confirmed view.'); }
}
function backToLive() {
  navigationVersion += 1;
  selectedRun = null;
  $('history-banner').hidden = true;
  $('target-scope').textContent = 'Live';
  document.querySelectorAll('[data-key]').forEach(button => { button.disabled = false; });
  targetSignature = '';
  if (liveState) render(liveState);
  renderRuns(); refresh(); pollTargets();
  latestKey = ''; status('Returned to live.');
}
$('back-live').addEventListener('click', backToLive);
setInterval(pollRuns, 5000);
