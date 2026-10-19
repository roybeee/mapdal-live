// Real browser + production HTML transform, with isolated stock fixtures.
// NODE_PATH=/tmp/mapdal-soldout-test/node_modules node --test tests/own-stock.test.cjs
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(execFileSync('/tmp/mapdal-related-venv/bin/python', ['-c', `
import json, pathlib, admin_v2 as admin
html = pathlib.Path('static/product-goods-logo-hoodie.html').read_text()
result = {}
for mode in ('all', 'partial', 'restocked'):
    admin.rows = lambda *args: [dict(id='product-goods-logo-hoodie.html::o'+str(i),
        soldout=int(mode == 'all' or mode == 'partial' and i < 3), stock=None) for i in range(4)]
    result[mode] = admin._own_stock_apply(html, '/product-goods-logo-hoodie')
print(json.dumps(result))
`], { cwd: root, encoding: 'utf8' }));

let browser;
before(async () => { browser = await chromium.launch({ channel: 'chrome' }); });
after(async () => { await browser?.close(); });

async function detail(mode) {
  const page = await browser.newPage();
  await page.route('**/*', route => route.abort());
  const html = fixtures[mode];
  const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(code => code.includes('const OPTS='));
  await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, ''));
  await page.addScriptTag({ content: script });
  return page;
}

test('all sold options and purchase controls stay disabled after render()', async () => {
  const page = await detail('all');
  try {
    assert.equal(await page.locator('#optSel').isDisabled(), true);
    assert.equal(await page.locator('#optSel option[value="o0"]').isDisabled(), true);
    await page.evaluate(() => { cart = { o0: 1, o3: 2 }; render(); });
    assert.equal(await page.locator('#totQty').textContent(), '0');
    for (const id of ['btnCart', 'btnBuy']) {
      assert.equal(await page.locator('#' + id).isDisabled(), true);
      assert.equal(await page.locator('#' + id).textContent(), 'SOLD OUT');
    }
  } finally { await page.close(); }
});

test('partial stock rejects sold options but allows available selection', async () => {
  const page = await detail('partial');
  try {
    assert.equal(await page.locator('#optSel').isDisabled(), false);
    assert.equal(await page.locator('#optSel option[value="o0"]').isDisabled(), true);
    await page.selectOption('#optSel', 'o3');
    assert.equal(await page.locator('#btnBuy').isEnabled(), true);
    await page.evaluate(() => { cart = { o0: 2, o3: 1 }; render(); });
    assert.equal(await page.locator('#totQty').textContent(), '1');
    assert.equal(await page.locator('#optLines .opt-line').count(), 1);
  } finally { await page.close(); }
});

test('restock next response allows buying an option previously sold out', async () => {
  const page = await detail('restocked');
  try {
    await page.selectOption('#optSel', 'o0');
    assert.equal(await page.locator('#btnBuy').isEnabled(), true);
    assert.equal(await page.locator('#totQty').textContent(), '1');
  } finally { await page.close(); }
});
