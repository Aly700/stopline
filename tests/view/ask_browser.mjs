// Copy into ~/Projects/witness before running so playwright-core resolves there.
// Start tests.view.demo_server on 8795; pass an absolute .lane directory as argv[2].
import {chromium} from 'playwright-core';
import assert from 'node:assert/strict';

const browser = await chromium.launch({channel:'chrome', headless:true});
const page = await browser.newPage({baseURL:'http://127.0.0.1:8795', viewport:{width:1920, height:1080}});
const errors = [];
const keys = [];
let passed = 0, screenshots = 0;
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.url().includes('/api/key/')) keys.push(request.url()); });
const input = page.locator('#ask-input');
const message = page.locator('#ask-message');
async function test(name, run) { await run(); passed++; console.log('PASS ' + name); }
async function search(question) {
  await input.fill(question);
  const response = page.waitForResponse(r => r.url().includes('/api/ask?'));
  await input.press('Enter');
  await page.waitForFunction(() => document.querySelector('#ask-message').textContent === 'searching');
  await response;
  await page.waitForFunction(() => document.querySelector('#ask-message').textContent !== 'searching');
}
async function capture(beat) {
  for (const [width, height] of [[1920,1080],[1280,720]]) {
    await page.setViewportSize({width,height});
    const geometry = await page.evaluate(() => {
      const panel = document.querySelector('#ask-panel');
      const bounds = panel.getBoundingClientRect();
      const results = document.querySelector('#ask-results');
      return {
        horizontal:document.documentElement.scrollWidth > innerWidth,
        vertical:document.documentElement.scrollHeight > innerHeight,
        fits:bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight,
        resultsOverflow:!results.hidden && results.scrollHeight > results.clientHeight + 1,
        minFont:Math.min(...[...document.querySelectorAll('.ask-text')].map(e => parseFloat(getComputedStyle(e).fontSize))),
      };
    });
    assert.equal(geometry.horizontal, false);
    assert.equal(geometry.vertical, false);
    assert.equal(geometry.fits, true);
    assert.equal(geometry.resultsOverflow, false);
    if (width === 1920) assert.ok(geometry.minFont >= 28);
    await page.screenshot({path:`${process.argv[2]}/ask-${beat}-${width}.png`});
    screenshots++;
  }
}
try {
  await page.goto('http://127.0.0.1:8795');
  await page.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
  for (const key of ['2','K','S','W','R','U']) await page.request.post('/api/key/' + key);
  await page.waitForFunction(() => document.querySelector('#trace').textContent.includes('Undo:'));
  await test('slash opens the focused input without changing the page', async () => {
    assert.match(await page.title(), /StopLine/);
    await page.keyboard.press('/');
    assert.equal(await input.evaluate(e => e === document.activeElement), true);
    assert.equal(await input.getAttribute('placeholder'), 'Ask the audit: what did worker two do after the stop?');
    await capture('input');
  });
  await test('typing never calls stage keys', async () => {
    const before = keys.length;
    await input.pressSequentially('12SKWRUVMPQ/');
    assert.equal(await input.inputValue(), '12SKWRUVMPQ/');
    assert.equal(keys.length, before);
  });
  await test('searching becomes five results with engine, time and score', async () => {
    await search('what did worker two do after the stop?');
    assert.equal(await page.locator('.ask-row').count(), 5);
    assert.equal(await page.locator('#ask-engine').innerText(), 'found with atlas vector search');
    assert.equal(await page.locator('#ask-question').innerText(), 'what did worker two do after the stop?');
    assert.match(await page.locator('.ask-time').first().innerText(), /\d\d:\d\d:\d\d/);
    assert.equal(await page.locator('.ask-score').first().innerText(), 'score 0.98');
    assert.equal(await page.locator('button.ask-row').count(), 1);
    await capture('results');
  });
  await test('result opens its action chain and Escape returns to search', async () => {
    await page.locator('button.ask-row').click();
    await page.waitForFunction(() => document.querySelector('#chain-content').textContent.includes('Version 1'));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#ask-panel').isVisible(), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#ask-panel').isVisible(), false);
  });
  await test('Q opens search and local ranking is named exactly', async () => {
    await page.keyboard.press('q');
    await search('local');
    assert.equal(await page.locator('#ask-engine').innerText(), 'found with local ranking');
  });
  await test('failed search shows the supplied message', async () => {
    await search('error');
    assert.equal(await message.innerText(), 'The audit index is not ready.');
    assert.equal(await page.locator('.ask-row').count(), 0);
  });
  await test('empty search shows the supplied message', async () => {
    await search('empty');
    assert.equal(await message.innerText(), 'No matching audit entries.');
  });
  await test('missing route is explained', async () => {
    await search('missing');
    assert.equal(await message.innerText(), 'Search is not enabled.');
  });
  await test('audit sentences remain literal text', async () => {
    await search('literal');
    assert.match(await page.locator('.ask-text').first().innerText(), /<img src=x/);
    assert.equal(await page.locator('#ask-panel img').count(), 0);
  });
  await test('a later question supersedes an outstanding request', async () => {
    await input.fill('slow'); await input.press('Enter');
    await search('local');
    await page.waitForTimeout(1100);
    assert.equal(await page.locator('#ask-question').innerText(), 'local');
    assert.equal(await page.locator('#ask-engine').innerText(), 'found with local ranking');
  });
  await test('Escape cancels a search without reopening the panel', async () => {
    await input.fill('slow'); await input.press('Enter'); await input.press('Escape');
    await page.waitForTimeout(1100);
    assert.equal(await page.locator('#ask-panel').isVisible(), false);
  });
  await test('Ask and Close buttons work', async () => {
    await page.locator('[data-key="/"]').click();
    assert.equal(await input.evaluate(e => e === document.activeElement), true);
    await page.locator('#ask-close').click();
    assert.equal(await page.locator('#ask-panel').isVisible(), false);
  });
  assert.deepEqual(errors, []);
  console.log(`PASS: ${passed} browser scenarios; ${screenshots} screenshots; zero console/page errors.`);
} finally { await browser.close(); }
