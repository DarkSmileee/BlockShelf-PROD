const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/base.html'), 'utf8');
const early = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const main = html.split('// THEME SCRIPT')[1].split('</script>')[0];

for (const blocked of [false, true]) {
  for (const preference of ['light', 'dark', 'system', '']) {
    const attributes = {'data-cookie-theme': preference, 'data-bs-theme': preference};
    let saved = 'light';
    let click, change;
    const media = {matches: true, addEventListener: (_, listener) => { change = listener; }};
    const document = {
      cookie: '',
      documentElement: {
        getAttribute: key => attributes[key] || '',
        setAttribute: (key, value) => { attributes[key] = value; },
      },
      getElementById: () => ({addEventListener: (_, listener) => { click = listener; }}),
    };
    const context = vm.createContext({document, window: {matchMedia: () => media}, localStorage: {
      getItem: () => { if (blocked) throw Error('Storage blocked'); return saved; },
      setItem: (_, value) => { if (blocked) throw Error('Storage blocked'); saved = value; },
    }});
    const expected = preference === 'dark' || preference === 'system' || (!preference && blocked) ? 'dark' : 'light';
    vm.runInContext(early, context);
    assert.equal(attributes['data-bs-theme'], expected);
    vm.runInContext('// THEME SCRIPT' + main, context);
    assert.equal(attributes['data-theme'], expected);
    change({matches: false});
    const followsSystem = preference === 'system' || (!preference && blocked);
    assert.equal(attributes['data-theme'], followsSystem ? 'light' : expected);
    click();
    const next = (followsSystem ? 'light' : expected) === 'light' ? 'dark' : 'light';
    assert.equal(attributes['data-bs-theme'], next);
    assert.ok(document.cookie.startsWith('theme=' + next + ';'));
    change({matches: next !== 'dark'});
    assert.equal(attributes['data-theme'], next);
  }
}
