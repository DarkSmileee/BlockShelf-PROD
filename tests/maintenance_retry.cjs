const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../templates/inventory/settings.html'), 'utf8');
const begin = source.indexOf('  async function runBatch(){');
const end = source.indexOf('  if (btnScan) btnScan.addEventListener', begin);
const progress = {textContent: ''};
let wake, delay;
const cursors = [];
let calls = 0;
const context = {
  running: true, total: 1, processed: 0, lastId: 0,
  batchSizeInput: {value: '50'}, bar: null, progText: progress, log: null,
  btnStart: {disabled: true}, appendLog() {},
  FormData: class {constructor() {this.values = {};} append(k,v) {this.values[k]=v;}},
  fetchJSONSafe: async (url, options) => {
    cursors.push(options.body.values.after_id);
    return ++calls === 1
      ? {processed: 0, last_id: 0, done: false, retry_after: 7}
      : {processed: 1, last_id: 8, done: true, updated_names: 1};
  },
  setTimeout: (fn, ms) => {wake = fn; delay = ms;}
};
vm.createContext(context);
vm.runInContext(source.slice(begin, end), context);
(async () => {
  const work = context.runBatch();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
  assert.equal(delay, 7000);
  assert.match(progress.textContent, /Retrying in 7 seconds/);
  assert.equal(context.processed, 0);
  wake(); await work;
  assert.deepEqual(cursors, ['0', '0']);
  assert.equal(context.processed, 1);
  assert.equal(context.running, false);
  assert.equal(context.btnStart.disabled, false);
  console.log('Maintenance retries the same cursor after waiting.');
})().catch(error => {console.error(error); process.exitCode = 1;});
