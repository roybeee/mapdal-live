// Real Chrome and production render/sort functions; database rows are local fixtures.
// NODE_PATH=/tmp/mapdal-soldout-test/node_modules node --test tests/kpop-registration-order.test.cjs
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const renderFixture = String.raw`
import ast, json, pathlib, re, sys, time
data = json.load(sys.stdin)
tree = ast.parse(pathlib.Path('admin_v2.py').read_text())
direct, linked = data['direct'], data['linked']
scope = dict(re=re, json=json, time=time, ensure_ready=lambda: None,
    _state={'pcols': {'id','name','stock','soldout','price','created_at'}, 'pname':'name','pprice':'price'},
    _prod_sched_sel=lambda: '', rows=lambda sql, args: direct if args[0]=='mp::%' else linked,
    kst_now=lambda: None, product_visible=lambda *args: True, norm_cat=lambda value: value,
    num=lambda value: int(value or 0), badge_color=lambda value: '#050505',
    derived_pct=lambda *args: 0, is_new_product=lambda value: False,
    _k2g_cat_cache={'t':0,'body':None}, _k2g_removed_set=lambda: set())
constants = {'_MP_CAT_TAG','_MP_COVERS','_MP_IMG_OK','_FB_TOOLBAR_CSS','_FB_TOOLS_JS'}
functions = {'_mp_shop_cards','_k2g_catalog_json','_fb_toolbar_html','_feedback_apply'}
for node in tree.body:
    names = [target.id for target in getattr(node,'targets',[]) if isinstance(target,ast.Name)]
    if set(names)&constants or isinstance(node,ast.FunctionDef) and node.name in functions:
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<production>','exec'),scope)
shop = pathlib.Path('static/shop.html').read_text()
script = shop[shop.index('function k2gTag('):]
script = script[:script.index('</script>')]
catalog = scope['_k2g_catalog_json']() or '[]'
mode = data.get('mode','kpop')
html = '<html><head></head><body>' + ('<!--MP_KPOP-->' if mode=='kpop' else '')
html += '<div class="filter-bar"><button data-f="all"></button><button data-f="album"></button><button data-f="food"></button></div><input id="shopQ">'
html += '<div class="journal-grid" id="shopGrid">' + scope['_mp_shop_cards']() + '</div><div id="lmWrap"><button id="lmBtn"></button></div>'
html += '<script>const K2G='+catalog+';const K2G_FIRST=40,K2G_STEP=100,K2G_IMG="data:image/svg+xml,";let F="all",Q="",ptr=0,VIEW=[];' + script + '</script></body></html>'
print(json.dumps({'html':scope['_feedback_apply'](html),'catalog':json.loads(catalog)}))
`;

function row(id, created, extra = {}) {
  return { id, name: id, created_at: created, price: 1000, stock: 10, soldout: 0, category: 'album', ...extra };
}
let browser;
before(async () => { browser = await chromium.launch({ channel: 'chrome' }); });
after(async () => { await browser.close(); });
async function fixture(t, direct, linked, mode = 'kpop') {
  const result = JSON.parse(execFileSync('python3', ['-c', renderFixture], {
    cwd: root, input: JSON.stringify({ direct, linked, mode }), encoding: 'utf8',
  }));
  const page = await browser.newPage({ timezoneId: 'America/Los_Angeles' });
  t.after(() => page.close());
  await page.route('**/*', route => route.abort());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent(result.html);
  assert.deepEqual(errors, [], 'production scripts run without browser errors');
  return { page, ...result };
}
async function visible(page) {
  return page.locator('#shopGrid > a').evaluateAll(cards => cards.filter(c => c.style.display !== 'none')
    .map(c => c.classList.contains('k2g-card') ? `k2g::${new URL(c.href, 'http://local').searchParams.get('uid')}` : c.getAttribute('href').slice(3)));
}

test('initial list merges registration dates, normalizes KST, and safely emits metadata', async t => {
  const { page, catalog, html } = await fixture(t, [
    row('mp::old', '2026-10-15T17:38:38'),
    row('mp::latest', '2026-10-16T14:38:50'),
    row('mp::middle', '2026-10-16T14:17:36'),
    row('mp::invalid', '\" onmouseover=\"bad'),
  ], [row('k2g::older', '2026-10-15T00:00:00Z'), row('k2g::newest', '2026-10-16T06:00:00Z')]);
  assert.equal(catalog[1][10], '2026-10-16T06:00:00Z');
  assert.ok(html.includes('data-mp-created="&quot; onmouseover=&quot;bad"'));
  assert.deepEqual(await visible(page), ['k2g::newest', 'mp::latest', 'mp::middle', 'mp::old', 'k2g::older', 'mp::invalid']);
});

test('equal, missing and invalid dates preserve source order with direct ties first', async t => {
  const { page } = await fixture(t, [row('mp::tie', '2026-10-16T14:00'), row('mp::missing', null), row('mp::invalid', 'broken'), row('mp::invalid-day', '2026-02-30')], [
    row('k2g::missing', ''), row('k2g::tie1', '2026-10-16T05:00:00Z'), row('k2g::tie2', '2026-10-16T14:00:00+09:00'), row('k2g::invalid', 'broken'),
  ]);
  assert.deepEqual(await visible(page), ['mp::tie', 'k2g::tie1', 'k2g::tie2', 'mp::missing', 'mp::invalid', 'mp::invalid-day', 'k2g::missing', 'k2g::invalid']);
});

test('catalogue-only lists use registration order on their first render', async t => {
  const { page } = await fixture(t, [], [row('k2g::old', '2026-10-01'), row('k2g::new', '2026-10-19')]);
  assert.deepEqual(await visible(page), ['k2g::new', 'k2g::old']);
  assert.equal(await page.locator('#mpCnt').textContent(), '2');
});

test('pagination is a global newest-first prefix and Total includes deferred direct cards', async t => {
  const linked = Array.from({ length: 145 }, (_, i) => row(`k2g::${i}`, new Date(Date.UTC(2026, 9, 19) - i * 3600000).toISOString()));
  const { page } = await fixture(t, [row('mp::latest', '2026-10-20'), row('mp::between', linked[70].created_at), row('mp::undated', null)], linked);
  assert.equal(await page.locator('#mpCnt').textContent(), '148');
  assert.deepEqual(await visible(page), ['mp::latest', ...linked.slice(0, 40).map(r => r.id)]);
  await page.locator('#lmBtn').click();
  await page.waitForFunction(() => document.querySelector('[href="/p/mp::between"]').style.display !== 'none');
  assert.deepEqual(await visible(page), ['mp::latest', ...linked.slice(0,70).map(r=>r.id), 'mp::between', ...linked.slice(70,140).map(r=>r.id)]);
  assert.equal(await page.locator('#mpCnt').textContent(), '148');
  await page.locator('#lmBtn').click();
  await page.waitForFunction(() => document.querySelector('[href="/p/mp::undated"]').style.display !== 'none');
  assert.equal((await visible(page)).at(-1), 'mp::undated');
  assert.equal((await visible(page)).length, 148);
});

test('search, event and sold filters retain date order; returning to new resets order', async t => {
  const { page } = await fixture(t, [
    row('mp::old', '2026-10-10', { name: 'Artist 럭키드로우 old', price: 100 }),
    row('mp::new', '2026-10-18', { name: 'Artist 럭키드로우 new', price: 900, soldout: 1 }),
  ], [row('k2g::middle', '2026-10-15', { name: 'Artist LUCKY DRAW', price: 500 }), row('k2g::other', '2026-10-19')]);
  await page.locator('#mpSort').selectOption('asc');
  await page.locator('#mpSort').selectOption('new');
  assert.deepEqual(await visible(page), ['k2g::other', 'mp::new', 'k2g::middle', 'mp::old']);
  await page.locator('#shopQ').fill('Artist');
  assert.deepEqual(await visible(page), ['mp::new', 'k2g::middle', 'mp::old']);
  await page.locator('#mpFlucky').check();
  await page.locator('#mpFhide').check();
  assert.deepEqual(await visible(page), ['k2g::middle', 'mp::old']);
  assert.equal(await page.locator('#mpCnt').textContent(), '2');
  await page.locator('[data-f="food"]').click();
  assert.deepEqual(await visible(page), []);
  assert.equal(await page.locator('#mpCnt').textContent(), '0');
});

test('SHOP retains its existing document order despite registration dates', async t => {
  const { page } = await fixture(t, [row('mp::old', '2026-01-01'), row('mp::new', '2026-10-19')], [], 'shop');
  assert.deepEqual(await visible(page), ['mp::old', 'mp::new']);
});
