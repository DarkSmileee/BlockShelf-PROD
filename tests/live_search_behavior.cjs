const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const listeners = {};
const input = {value: '', addEventListener: (name, fn) => listeners[name] = fn};
let submit, timeout, delay;
const form = {action: 'https://example.test/inventory/', elements: {q: input},
  addEventListener: (name, fn) => submit = fn};
const results = {dataset: {owner: '7', count: '40'}, innerHTML: 'original',
  setAttribute(name, value) { this[name] = value; }};
const status = {textContent: ''};
const pending = [], urls = [];
const context = {
  document: {getElementById: id => ({inventorySearch: form, inventoryResults: results,
    inventorySearchStatus: status})[id]},
  window: {fetch: true, AbortController, location: {href: 'https://example.test/inventory/?page=3'},
    addEventListener() {}},
  URL, URLSearchParams, AbortController,
  FormData: class { constructor() { return [['q', input.value], ['owner', '7'], ['sort', 'loc'], ['dir', 'desc']]; } },
  DOMParser: class { parseFromString(text) { return {getElementById: () => JSON.parse(text)}; } },
  history: {replaceState: (_, __, url) => urls.push(String(url))},
  setTimeout: (fn, ms) => { timeout = fn; delay = ms; return 1; },
  clearTimeout: () => { timeout = null; },
  fetch: (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject}))
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/inventory-search.js'), 'utf8'), context);
const type = value => {input.value = value; listeners.input({isComposing: false});};
const reply = (request, html, count = '1', owner = '7') => request.resolve({ok: true,
  text: async () => JSON.stringify({innerHTML: html, dataset: {owner, count}})});
(async () => {
  type('1'); type('15'); type('1562');
  assert.equal(pending.length, 0);
  assert.equal(delay, 300);
  const first = timeout();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].url.searchParams.get('owner'), '7');
  assert.equal(pending[0].url.searchParams.get('sort'), 'loc');
  assert.equal(pending[0].url.searchParams.get('dir'), 'desc');
  assert.equal(pending[0].url.searchParams.has('page'), false);
  type('1563');
  assert.equal(pending[0].options.signal.aborted, true);
  reply(pending[0], 'stale'); await first;
  assert.equal(results.innerHTML, 'original');
  const second = timeout(); reply(pending[1], 'new'); await second;
  assert.equal(results.innerHTML, 'new');
  assert.equal(input.value, '1563');
  assert.equal(status.textContent, '1 items found.');
  assert.match(urls.at(-1), /q=1563/);
  assert.equal(results['aria-busy'], 'false');
  type(''); const clear = timeout(); reply(pending[2], 'all', '40'); await clear;
  assert.equal(pending[2].url.searchParams.get('q'), '');
  assert.equal(results.innerHTML, 'all');
  type('missing'); const empty = timeout(); reply(pending[3], 'empty', '0'); await empty;
  assert.match(status.textContent, /No items found/);
  type('failure'); const failed = timeout(); pending[4].reject(new Error('network')); await failed;
  assert.equal(results.innerHTML, 'empty');
  assert.match(status.textContent, /Search failed/);
  type('expired'); const expired = timeout(); pending[5].resolve({ok:true, redirected:true}); await expired;
  assert.match(status.textContent, /Search failed/);
  type('changed owner'); const owner = timeout(); reply(pending[6], 'wrong inventory', '1', '9'); await owner;
  assert.equal(results.innerHTML, 'empty');
  type('composing'); listeners.compositionstart();
  listeners.input({isComposing:true}); assert.equal(timeout, null);
  listeners.compositionend({}); assert.equal(typeof timeout, 'function');
  submit(); assert.equal(timeout, null);
  console.log('Live search: debounce, stale responses, scope, clearing, errors, IME and submit passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
