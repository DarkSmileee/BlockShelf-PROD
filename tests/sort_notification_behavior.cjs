const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../templates/inventory/inventory_list.html'), 'utf8');
const script = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
function run(href, notification, saved = {sort: 'loc', dir: 'desc'}) {
  const location = {href, search: new URL(href).search};
  const stored = [];
  vm.runInNewContext(script, {
    URL, URLSearchParams, console,
    window: {location},
    localStorage: {
      getItem: () => JSON.stringify(saved),
      setItem: (key, value) => stored.push(JSON.parse(value)),
    },
    document: {
      querySelector: selector => selector === '[data-flash-message]' && notification ? {} : null,
      getElementById: id => id === 'imgPreviewModal' ? {addEventListener() {}} : null,
      querySelectorAll: () => [],
    },
  });
  return {href: location.href, stored};
}
const original = 'https://example.test/inventory/inventory/?owner=2';
assert.equal(run(original, true).href, original, 'A notification must survive sort restoration');
const sorted = new URL(run(original, false).href);
assert.equal(sorted.searchParams.get('owner'), '2');
assert.equal(sorted.searchParams.get('sort'), 'loc');
assert.equal(sorted.searchParams.get('dir'), 'desc');
assert.equal(run(original, false, null).href, original);
const search = original + '&q=3003';
assert.equal(run(search, true).href, search);
assert.equal(run(search, false).href, search);
const explicit = original + '&sort=name&dir=asc';
assert.equal(run(explicit, true).href, explicit);
assert.deepEqual(run(explicit, true).stored, [{sort: 'name', dir: 'asc'}]);
console.log('Sort restoration preserves notifications and explicit search/sort.');
