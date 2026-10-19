// Setup: npm install --prefix /tmp/mapdal-soldout-test playwright
// Run: NODE_PATH=/tmp/mapdal-soldout-test/node_modules node --test tests/soldout-cards.test.cjs
// Browser rendering is real; catalogue rows are local fixtures. No network or DB calls.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const shop = fs.readFileSync(path.join(root, 'static/shop.html'), 'utf8');
const styles = [...shop.matchAll(/<style\b[^>]*>[\s\S]*?<\/style>/g)].map(match => match[0]).join('\n');
const admin = fs.readFileSync(path.join(root, 'admin_v2.py'), 'utf8');
const sharedStyles = admin.match(/CARD_CSS_SNIPPET\s*=\s*r"""([\s\S]*?)"""/)[1];
const renderCode = shop.slice(shop.indexOf('function k2gTag('), shop.indexOf('function sync()'));

// Execute the production direct-card renderer with database and clock boundaries stubbed.
const directCards = execFileSync('python3', ['-c', `
import ast, json, re, pathlib
tree = ast.parse(pathlib.Path('admin_v2.py').read_text())
fixtures = [dict(id='mp::sold', name='피철인', category='album', stock=0, soldout=1, price=10000),
            dict(id='mp::live', name='판매중', category='album', stock=10, soldout=0, price=10000)]
scope = dict(re=re, json=json, ensure_ready=lambda: None,
    _state={'pcols': {'id', 'name', 'stock', 'soldout', 'price'}, 'pname': 'name', 'pprice': 'price'},
    _prod_sched_sel=lambda: '', rows=lambda *args: fixtures, kst_now=lambda: None,
    product_visible=lambda *args: True, norm_cat=lambda value: value,
    num=lambda value: int(value or 0), badge_color=lambda value: '#050505',
    derived_pct=lambda *args: 0, is_new_product=lambda value: False)
for node in tree.body:
    names = [target.id for target in getattr(node, 'targets', []) if isinstance(target, ast.Name)]
    if set(names) & {'_MP_CAT_TAG', '_MP_COVERS', '_MP_IMG_OK'} or isinstance(node, ast.FunctionDef) and node.name == '_mp_shop_cards':
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<production>', 'exec'), scope)
print(scope['_mp_shop_cards']())
`], { cwd: root, encoding: 'utf8' });

let browser;
before(async () => {
  browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
    : { channel: 'chrome' });
});
after(async () => { if (browser) await browser.close(); });

async function fixture(viewport) {
  const page = await browser.newPage({ viewport });
  await page.route('**/*', route => route.abort());
  await page.setContent(`${styles}${sharedStyles}<div id="shopGrid">${directCards}</div>
    <div id="lmWrap"><button id="lmBtn"></button></div>`);
  await page.addScriptTag({ content: `
    const K2G_IMG='data:image/svg+xml,';
    const K2G_STEP=100;
    let F='album',ptr=0,VIEW=[
      ['sold','','솔드아웃 앨범',15000,10000,1],
      ['live','','판매중 앨범',15000,10000,0],
      ['later','','더보기 품절 앨범',15000,10000,1]
    ];
    ${renderCode}
    renderBatch(2);
  ` });
  return page;
}

async function coverStyle(page, selector) {
  return page.locator(selector).evaluate(element => {
    const style = getComputedStyle(element);
    return { filter: style.filter, opacity: style.opacity };
  });
}

const soldStyle = { filter: 'grayscale(0.85)', opacity: '0.75' };
const liveStyle = { filter: 'none', opacity: '1' };
for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  const prefix = `${viewport.width}px`;
  function check(name, assertion) {
    test(`${prefix}: ${name}`, async () => {
      const page = await fixture(viewport);
      try { await assertion(page); } finally { await page.close(); }
    });
  }
  check('sold-out catalogue cover matches the direct-product sold-out treatment', async page => {
    assert.deepEqual(await coverStyle(page, '.k2g-card.sold .k2g-cover'), soldStyle);
  });
  check('available catalogue cover retains its original appearance', async page => {
    assert.deepEqual(await coverStyle(page, '.k2g-card:not(.sold) .k2g-cover'), liveStyle);
  });
  check('existing direct sold-out cover retains its appearance', async page => {
    assert.deepEqual(await coverStyle(page, '[href="/p/mp::sold"] .col-cover'), soldStyle);
  });
  check('available direct cover retains its original appearance', async page => {
    assert.deepEqual(await coverStyle(page, '[href="/p/mp::live"] .col-cover'), liveStyle);
  });
  check('direct sold-out badge also styles covers without legacy inline declarations', async page => {
    await page.locator('[href="/p/mp::sold"] .col-cover').evaluate(element => {
      element.style.removeProperty('filter');
      element.style.removeProperty('opacity');
    });
    assert.deepEqual(await coverStyle(page, '[href="/p/mp::sold"] .col-cover'), soldStyle);
  });
  for (const coverClass of ['pimg2', 'bimg2']) {
    check(`sold-out food cover (${coverClass}) matches other sold-out cards`, async page => {
      await page.locator('[href="/p/mp::sold"] .col-cover').evaluate((element, className) => {
        element.style.removeProperty('filter');
        element.style.removeProperty('opacity');
        element.className = className;
      }, coverClass);
      assert.deepEqual(await coverStyle(page, `[href="/p/mp::sold"] .${coverClass}`), soldStyle);
    });
  }
  check('load-more catalogue cards receive the same sold-out appearance', async page => {
    await page.evaluate(() => renderBatch(1));
    assert.deepEqual(await coverStyle(page, '[href="/album-detail?uid=later"] .k2g-cover'), soldStyle);
  });
  check('gallery image opacity resets do not remove the sold-out cover treatment', async page => {
    await page.locator('.k2g-card.sold img').evaluate(element => { element.style.opacity = '1'; });
    assert.deepEqual(await coverStyle(page, '.k2g-card.sold .k2g-cover'), soldStyle);
  });
  check('sold-out detail links and status badges remain visible and interactive', async page => {
    for (const selector of ['.k2g-card.sold', '[href="/p/mp::sold"]']) {
      const card = page.locator(selector);
      assert.equal(await card.locator('.card-state.sold').innerText(), 'SOLD OUT');
      assert.equal(await card.locator('.card-state.sold').isVisible(), true);
      assert.equal(await card.evaluate(element => getComputedStyle(element).pointerEvents), 'auto');
      assert.ok(await card.getAttribute('href'));
    }
  });
}
