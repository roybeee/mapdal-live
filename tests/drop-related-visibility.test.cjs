// Run: node --test tests/drop-related-visibility.test.cjs
// Executes production functions; DOM and API boundaries are mocked.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'static/new-drops.html'), 'utf8');
const admin = fs.readFileSync(path.join(root, 'admin_v2.py'), 'utf8');
const render = page.slice(page.indexOf('function won('), page.indexOf('function bindRelSide('));
const scope = vm.createContext({ esc: String, CATFALL: '#eee' });
vm.runInContext(render, scope);
const products = [{ name: '앨범', price: 12000, url: '/album', soldout: true }];
const related = [{ id: 2, title: '다른 이벤트' }];

test('hidden related section suppresses albums even if cached payload contains them', () => {
  assert.equal(scope.relSideHTML({ show_related: false, related_products: products }), '');
});
test('hidden related section also suppresses related-event fallback', () => {
  assert.equal(scope.relSideHTML({ show_related: false, related }), '');
});
for (const value of [undefined, true]) {
  test(`visible/default setting renders album cards (${value})`, () => {
    assert.match(scope.relSideHTML({ show_related: value, related_products: products }), /관련 상품/);
  });
  test(`visible/default setting preserves event fallback (${value})`, () => {
    assert.match(scope.relSideHTML({ show_related: value, related }), /다른 이벤트/);
  });
}
test('no related items renders no empty section', () => {
  assert.equal(scope.relSideHTML({ show_related: true }), '');
});

for (const record of [null, { id: 1 }, { id: 1, show_related: true }, { id: 1, show_related: false }]) {
  test(`admin editor restores visibility checkbox (${JSON.stringify(record)})`, () => {
    const box = { classList: { add: () => {} }, style: {}, innerHTML: '' };
    const noop = () => '';
    const context = vm.createContext({
      DR: record ? [record] : [], DRCATS: [], $: () => box, esc: String,
      drPvBoxHTML: noop, drSchedRow: noop, drOptCard: noop, drXTabCard: noop,
      drWinRow: noop, drImgPrev: noop, drOptInputInit: noop, drPubUI: noop, can: () => true,
    });
    const code = admin.slice(admin.indexOf('function dropEdit('), admin.indexOf('function drPvBoxHTML('));
    vm.runInContext(code, context);
    context.dropEdit(record ? record.id : 0);
    const input = box.innerHTML.match(/<input\b[^>]*id="dr_related"[^>]*>/);
    assert.ok(input, 'related visibility checkbox exists');
    assert.equal(/\bchecked\b/.test(input[0]), record?.show_related !== false);
  });
}

for (const checked of [false, true]) {
  test(`admin save sends boolean visibility (${checked})`, async () => {
    let request;
    const context = vm.createContext({
      document: { querySelectorAll: () => [] },
      $: selector => ({ value: '', checked: selector === '#dr_related' ? checked : false }),
      drPubMode: () => 'now', drOptsCollect: () => [], drXTabsCollect: () => [],
      api: async (url, options) => { request = JSON.parse(options.body); return { id: 1 }; },
      toast: () => {}, closeM: () => {}, loadDrops: () => {},
    });
    const code = admin.slice(admin.indexOf('async function saveDrop('), admin.indexOf('async function delDrop('));
    vm.runInContext(code, context);
    await context.saveDrop(1);
    assert.equal(request.show_related, checked);
  });
}
