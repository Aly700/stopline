const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function player() {
  const keys = ['1', 'S', '2', 'X', 'K', 'S', 'W', 'R', 'U', 'M', 'V'];
  const state = (id = null, key = null, n = 0) => ({run: id ? {_id:id, mode:id === 'a' ? 'flag' : 'stopline', status:'running'} : null,
    permissions:[], workers:[], rows:[], receipt:null, ready_key:key === '1' ? 'S' : null,
    last_key:key ? {key, n, message:'Recorded ' + key, pending:false} : null});
  const data = {version:1, initial:{state:state(), targets:{marker:'initial'}},
    keys:keys.map((key, i) => ({key, answer:{ok:true, message:'Recorded ' + key},
      frames:[{ms:0, state:state(i < 2 ? 'a' : 'b', key, i + 1), targets:{marker:i}},
        {ms:9000, state:state(i < 2 ? 'a' : 'b', key, i + 1), targets:{marker:i + '.end'}}]})),
    runs:[{id:'b', mode:'stopline'}, {id:'a', mode:'flag'}], states:{}, chains:{}, ask:{'who stopped the run':{ok:true, rows:[{text:'Operator'}]}}};
  const delays = [], network = [], handlers = {}, attributes = {};
  const button = {textContent:'Autoplay', setAttribute:(k,v) => {attributes[k]=v;}, addEventListener:(k,fn) => {handlers['button:' + k]=fn;}};
  const document = {baseURI:'http://localhost/stopline/', readyState:'complete',
    getElementById:id => id === 'autoplay' ? button : null,
    querySelectorAll:() => [], addEventListener:(k,fn) => {handlers[k]=fn;}, body:{}};
  const context = {document, URL, Request, Response, EventTarget, Event, MessageEvent, structuredClone,
    MutationObserver:class {observe() {}}, console,
    setTimeout:(fn,ms) => {delays.push(ms); queueMicrotask(fn); return 1;}, clearTimeout:() => {},
    fetch:async url => {network.push(String(url)); return new Response(JSON.stringify(data));},
    EventSource:class {}, addEventListener:() => {}, open:() => {}};
  context.window = context;
  vm.createContext(context);
  const script = path.resolve(__dirname, '../../docs/replay.js');
  assert.ok(fs.existsSync(script), 'The replay adapter exists');
  vm.runInContext(fs.readFileSync(script, 'utf8'), context);
  const read = async url => (await context.fetch(url)).json();
  const press = async key => (await context.fetch('api/key/' + key, {method:'POST'})).json();
  return {context, data, keys, read, press, delays, network, handlers, attributes};
}

test('API calls stay local and wrong keys leave the recording unchanged', async () => {
  const p = player();
  assert.equal((await p.read('api/state')).run, null);
  assert.equal((await p.press('2')).message, 'This is a recording. Press 1.');
  assert.equal((await p.read('api/state')).last_key.message, 'This is a recording. Press 1.');
  assert.equal((await p.read('api/targets')).marker, 'initial');
  await p.read('api/unknown');
  assert.equal(p.network.length, 1);
  assert.match(p.network[0], /recording\.json$/);
});

test('frames emit changes and cap each gap at two seconds', async () => {
  const p = player(), changes = [];
  const events = new p.context.EventSource('api/events');
  events.addEventListener('change', event => changes.push(JSON.parse(event.data)));
  await p.press('1');
  assert.equal((await p.read('api/state')).ready_key, 'S');
  assert.equal((await p.read('api/targets')).marker, '0.end');
  assert.ok(changes.some(event => event.state?.run?._id === 'a'));
  assert.ok(p.delays.includes(2000));
  assert.ok(p.delays.every(delay => delay <= 2000));
  events.close();
  assert.equal(events.readyState, 2);
});

test('history appears as recorded and key 1 restarts after the final key', async () => {
  const p = player();
  assert.deepEqual(await p.read('api/runs'), []);
  for (const key of p.keys) await p.press(key);
  assert.equal((await p.read('api/runs')).length, 2);
  assert.equal((await p.read('api/state?run_id=a')).run._id, 'a');
  assert.equal((await p.read('api/state?run_id=a')).read_only, true);
  await p.press('1');
  assert.equal((await p.read('api/state')).run._id, 'a');
  assert.equal((await p.read('api/runs')).length, 1);
  assert.equal((await p.press('U')).message, 'This is a recording. Press S.');
});

test('ask returns only recorded answers and unsupported questions explain the limit', async () => {
  const p = player();
  for (const key of p.keys) await p.press(key);
  assert.equal((await p.read('api/ask?q=Who%20stopped%20the%20run%3F')).rows[0].text, 'Operator');
  const missing = await p.read('api/ask?q=another%20question');
  assert.equal(missing.ok, false);
  assert.match(missing.message, /record/i);
});

test('autoplay runs the complete sequence and a visitor key stops it', async () => {
  const p = player(), played = [];
  p.context.keypress = async key => {played.push(key); await p.press(key);};
  await p.handlers['button:click']();
  assert.deepEqual(played, p.keys);
  assert.equal(p.attributes['aria-pressed'], 'false');
  played.length = 0;
  p.context.keypress = async key => {
    played.push(key);
    p.handlers.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault() {}, stopImmediatePropagation() {}});
    await p.press(key);
  };
  await p.handlers['button:click']();
  assert.deepEqual(played, ['1']);
  assert.equal(p.attributes['aria-pressed'], 'false');
});

test('history navigation pauses autoplay and an unknown letter gives the next key', async () => {
  const p = player(), played = [];
  p.context.keypress = async key => {
    played.push(key);
    await p.press(key);
    p.handlers.click({target:{closest:selector => selector === 'button, a, input' ? {} : null}});
  };
  await p.handlers['button:click']();
  assert.deepEqual(played, ['1']);
  p.handlers.keydown({key:'z', target:{tagName:'BODY'}, preventDefault() {}, stopImmediatePropagation() {}});
  assert.equal((await p.read('api/state')).last_key.message, 'This is a recording. Press S.');
});

test('Enter on the autoplay button stops playback without triggering another click', async () => {
  const p = player();
  let prevented = false;
  p.context.keypress = async key => {
    await p.press(key);
    p.handlers.keydown({key:'Enter', target:{tagName:'BUTTON', closest:() => ({})},
      preventDefault() {prevented = true;}, stopImmediatePropagation() {}});
  };
  await p.handlers['button:click']();
  assert.equal(prevented, true);
  assert.equal(p.attributes['aria-pressed'], 'false');
});
