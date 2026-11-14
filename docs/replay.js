/* The console consumes recorded API responses. No command reaches a server. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const NativeEventSource = window.EventSource;
  const base = new URL('.', document.baseURI);
  const streams = new Set();
  const history = new Map();
  const operations = new Map();
  const copy = value => structuredClone(value);
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  let recording, snapshot, cursor = 0, playing = false, serial = 0, recordedSerial;
  let autoplay = false, autoplayVersion = 0, loadError = false;

  function apiPath(input) {
    const url = new URL(input instanceof Request ? input.url : String(input), base);
    if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) return null;
    const path = url.pathname.slice(base.pathname.length);
    return path.startsWith('api/') ? {path, url} : null;
  }
  const json = (value, status = 200) => new Response(JSON.stringify(value), {
    status, headers:{'Content-Type':'application/json', 'Cache-Control':'no-store'}
  });
  function emit(type, payload = {}) {
    for (const stream of streams) stream.send(type, payload);
  }
  function publish() {
    emit('change', {revision:serial, connected:true, state:copy(snapshot.state)});
  }
  function apply(frame) {
    snapshot = copy({state:frame.state, targets:frame.targets});
    const pressed = snapshot.state.last_key;
    if (pressed) {
      if (pressed.n !== recordedSerial) { recordedSerial = pressed.n; serial += 1; }
      pressed.n = serial;
    }
    if (snapshot.state.run) history.set(snapshot.state.run._id, copy(snapshot.state));
    publish();
  }
  function reset() {
    cursor = 0;
    recordedSerial = undefined;
    history.clear();
    operations.clear();
    apply(recording.initial);
  }
  const loaded = nativeFetch(new URL('recording.json', base)).then(async response => {
    if (!response.ok) throw new Error('Recording unavailable');
    recording = await response.json();
    if (recording.version !== 1 || !recording.keys?.length) throw new Error('Recording unavailable');
    reset();
    return true;
  }).catch(() => {
    loadError = true;
    const button = document.getElementById('autoplay');
    if (button) { button.disabled = true; button.textContent = 'Recording unavailable'; }
    return false;
  });

  function nextKey() { return recording?.keys[cursor]?.key || '1'; }
  function notice(key, message) {
    if (snapshot) {
      snapshot.state.last_key = {n:++serial, key, message, pending:false};
      publish();
    }
    return {ok:false, pending:false, message};
  }
  function wrongKey(key) { return notice(key, `This is a recording. Press ${nextKey()}.`); }
  async function playKey(key) {
    if (playing || key !== nextKey()) return wrongKey(key);
    if (cursor === recording.keys.length) reset();
    const step = recording.keys[cursor++];
    playing = true;
    operations.set(key, {pending:true, message:'Playing the recorded frames.'});
    try {
      let previous = 0;
      for (const frame of step.frames) {
        await pause(Math.min(2000, Math.max(0, frame.ms - previous)));
        apply(frame);
        previous = frame.ms;
      }
      const result = step.operation || (step.answer.pending
        ? {ok:step.answer.ok, pending:false, message:'Recorded frames finished.'} : step.answer);
      operations.set(key, copy(result));
      if (cursor === recording.keys.length) {
        notice(key, `${result.message} Recording complete. Press 1 to start again.`);
      }
      // Preserve the original key answer; the console polls its operation when pending.
      return copy(step.answer);
    } finally { playing = false; }
  }
  function runSummaries() {
    return recording.runs.filter(run => history.has(run.id)).map(run => {
      const state = history.get(run.id);
      return {...run, status:state.run.status, stop_at:state.stop?.at || null,
        receipt_status:state.receipt?.status || null, counts:state.counts || {}};
    });
  }
  function chainFor(id) {
    const chain = recording.chains[id];
    if (!chain) return null;
    // The stored chain is the final accounting. While playing, show the effect
    // and permission state visible in that frame, so unknown stays unknown.
    for (const state of [...history.values()].reverse()) {
      const rows = [...state.rows, ...(state.receipt?.rows || [])];
      const row = rows.find(item => item.action_id === id);
      if (!row) continue;
      const result = copy(chain);
      result.permissions = result.permissions.map(permission =>
        state.permissions.find(item => item._id === permission._id) || permission);
      if (result.effect && row.kind !== 'refusal') {
        result.effect = {...result.effect, ...row, evidence:row.evidence || null};
        result.audit = result.audit.filter(entry =>
          (entry.kind !== 'outcome' || row.evidence) && (entry.kind !== 'undo' || row.undo));
      }
      return result;
    }
    return null;
  }
  const normalize = question => question.trim().toLowerCase().replace(/[?.!]+$/, '');
  window.fetch = async (input, options = {}) => {
    const route = apiPath(input);
    if (!route) return nativeFetch(input, options);
    if (!await loaded) return json({pending:true, message:'The recording could not be loaded.'}, 503);
    const {path, url} = route;
    if (path === 'api/state') {
      const id = url.searchParams.get('run_id');
      if (!id) return json(snapshot.state);
      const state = history.get(id);
      return state ? json({...state, ready_key:null, last_key:null, read_only:true})
        : json({message:'This run has not played yet.'}, 404);
    }
    if (path === 'api/targets') return json(snapshot.targets);
    if (path === 'api/runs') return json(runSummaries());
    if (path.startsWith('api/key/')) {
      const method = options.method || (input instanceof Request ? input.method : 'GET');
      if (method.toUpperCase() !== 'POST') return json({ok:false, message:'Use the console keys.'}, 405);
      return json(await playKey(decodeURIComponent(path.slice('api/key/'.length)).toUpperCase()));
    }
    if (path.startsWith('api/operation/')) return json(operations.get(path.split('/').pop()) || {pending:false, message:'No recorded operation.'});
    if (path.startsWith('api/chain/')) {
      const chain = chainFor(decodeURIComponent(path.slice('api/chain/'.length)));
      return chain ? json(chain) : json({message:'This action has not played yet.'}, 404);
    }
    if (path === 'api/ask') {
      const question = normalize(url.searchParams.get('q') || '');
      const answer = recording.ask?.[question];
      if (answer && cursor === recording.keys.length && !playing) return json(answer);
      const questions = Object.keys(recording.ask || {});
      return json({ok:false, rows:[], message:questions.length
        ? `This recording includes answers after playback finishes. Try: ${questions.join('; ')}.`
        : 'This recording has no recorded audit search answers.'});
    }
    return json({ok:false, message:'This API is not included in the recording.'}, 404);
  };

  class RecordedEventSource extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;
    constructor(url, options) {
      super();
      if (apiPath(url)?.path !== 'api/events') return new NativeEventSource(url, options);
      this.url = new URL(url, base).href;
      this.readyState = 0;
      this.withCredentials = false;
      streams.add(this);
      loaded.then(ok => {
        if (this.readyState === 2) return;
        if (!ok) { this.send('error'); return; }
        this.readyState = 1;
        this.send('open');
        this.send('rebuild');
        this.send('change', {connected:true, state:copy(snapshot.state)});
      });
    }
    send(type, payload = {}) {
      if (this.readyState === 2) return;
      const event = new MessageEvent(type, {data:JSON.stringify(payload)});
      this.dispatchEvent(event);
      this['on' + type]?.(event);
    }
    close() { this.readyState = 2; streams.delete(this); }
  }
  window.EventSource = RecordedEventSource;

  function stopAutoplay() {
    autoplay = false;
    autoplayVersion += 1;
    const button = document.getElementById('autoplay');
    if (button && !loadError) { button.textContent = 'Autoplay'; button.setAttribute('aria-pressed', 'false'); }
  }
  async function startAutoplay() {
    if (autoplay) { stopAutoplay(); return; }
    if (!await loaded || playing) return;
    autoplay = true;
    const version = ++autoplayVersion;
    const button = document.getElementById('autoplay');
    button.textContent = 'Pause autoplay';
    button.setAttribute('aria-pressed', 'true');
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    window.backToLive?.();
    reset();
    while (autoplay && version === autoplayVersion && cursor < recording.keys.length) {
      const key = nextKey();
      await window.keypress(key);
      if (autoplay && version === autoplayVersion) await pause(key === 'M' ? 1400 : 700);
    }
    if (version === autoplayVersion) stopAutoplay();
  }
  function manualKey(key, event) {
    stopAutoplay();
    if (key === 'P' || key === 'T') {
      event.preventDefault(); event.stopImmediatePropagation();
      if (key === 'P') {
        window.open('report.html', '_blank', 'noopener');
        notice(key, "Opened the recorded reviewer's report.");
      } else notice(key, 'This recording has no model trace.');
    } else if (/^[A-Z0-9]$/.test(key) && key !== 'F' && recording && (playing || key !== nextKey())) {
      event.preventDefault(); event.stopImmediatePropagation();
      wrongKey(key);
    }
  }
  document.addEventListener('keydown', event => {
    // Every visitor key cancels autoplay, including Escape and typing a question.
    const wasAutoplay = autoplay;
    stopAutoplay();
    if (wasAutoplay && event.target.closest?.('#autoplay') && ['Enter', ' '].includes(event.key)) {
      event.preventDefault(); event.stopImmediatePropagation(); return;
    }
    if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
    manualKey(event.key.toUpperCase(), event);
  }, true);
  document.addEventListener('click', event => {
    if (event.target.closest('#autoplay')) return;
    if (event.target.closest('button, a, input')) stopAutoplay();
    const button = event.target.closest('[data-key], #ready-button');
    if (button) manualKey(button.dataset.key || snapshot?.state.ready_key || '', event);
  }, true);

  function setup() {
    document.getElementById('autoplay')?.addEventListener('click', startAutoplay);
    // Keep the copied console's connection labels honest about their source.
    const relabel = () => {
      for (const [id, before, after] of [
        ['connection', 'Live', 'Recorded'], ['env-atlas', 'Connected', 'Recorded'],
        ['target-scope', 'Live', 'Recorded'], ['back-live', 'Back to live →', 'Back to recording →']
      ]) {
        const node = document.getElementById(id);
        if (node?.textContent === before) node.textContent = after;
      }
      document.querySelectorAll('.run-status').forEach(node => { if (node.textContent === 'live') node.textContent = 'recorded'; });
    };
    new MutationObserver(relabel).observe(document.body, {childList:true, subtree:true, characterData:true});
    relabel();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
})();
