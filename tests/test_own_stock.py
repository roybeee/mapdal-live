"""Real SQLite stock reads and server HTML transformations for legacy SHOP products."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin

ROOT = Path(__file__).resolve().parents[1]
DETAIL = (ROOT / 'static/product-goods-logo-hoodie.html').read_text()
CARD = '<a class="col-card" href="/product-goods-logo-hoodie"><span class="add">담기 +</span></a>'


@pytest.fixture
def stock(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'BASE', str(tmp_path))
    monkeypatch.setattr(admin, 'IS_PG', False)
    with sqlite3.connect(tmp_path / 'mapdal.db') as db:
        db.execute('CREATE TABLE products(id TEXT PRIMARY KEY,soldout INTEGER,stock INTEGER)')
    def save(values):
        with sqlite3.connect(tmp_path / 'mapdal.db') as db:
            db.execute('DELETE FROM products')
            db.executemany('INSERT INTO products VALUES(?,?,?)', values)
    return save


def records(sold=1, stock=None, suffix='.html'):
    return [(f'product-goods-logo-hoodie{suffix}::o{i}', sold, stock) for i in range(4)]


@pytest.mark.parametrize('suffix', ['', '.html'])
@pytest.mark.parametrize('path', ['/product-goods-logo-hoodie', '/product-goods-logo-hoodie.html'])
def test_all_sold_options_and_buy_controls(stock, suffix, path):
    stock(records(suffix=suffix))
    html = admin._own_stock_apply(DETAIL, path)
    assert 'id="optSel" disabled' in html
    assert 'value="o0" disabled' in html
    assert 'S — ₩68,000 · SOLD OUT' in html
    assert 'id="btnBuy" disabled>SOLD OUT</button>' in html
    assert 'id="btnCart" disabled>SOLD OUT</button>' in html
    assert 'mpOwnStock' in html
    assert admin._own_stock_apply(html, path) == html


@pytest.mark.parametrize('sold,remaining', [(1, 4), (0, 0), (0, -1)])
def test_sold_card_and_restock_next_request(stock, sold, remaining):
    stock(records(sold, remaining))
    html = admin._own_stock_apply(CARD, '/shop')
    assert '<span class="add">SOLD OUT</span>' in html
    assert admin._own_stock_apply(html, '/shop') == html
    stock(records(0, None))
    assert admin._own_stock_apply(CARD, '/shop') == CARD


def test_partial_stock_leaves_card_and_available_option_enabled(stock):
    stock(records()[:3] + [('product-goods-logo-hoodie.html::o3', 0, None)])
    assert admin._own_stock_apply(CARD, '/shop') == CARD
    html = admin._own_stock_apply(DETAIL, '/product-goods-logo-hoodie')
    assert 'id="optSel">' in html
    assert 'value="o0" disabled' in html
    assert 'value="o3">XL' in html
    assert 'id="btnBuy" disabled>바로구매' in html


def test_missing_stock_preserves_unknown_products_and_options(stock):
    stock([])
    assert admin._own_stock_apply(CARD, '/shop') == CARD
    assert admin._own_stock_apply(DETAIL, '/product-goods-logo-hoodie') == DETAIL
    stock(records()[:1])
    html = admin._own_stock_apply(DETAIL, '/product-goods-logo-hoodie')
    assert 'id="optSel">' in html
    assert 'value="o1">M' in html


def test_database_failure_leaves_html_unchanged(monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError('unavailable')
    monkeypatch.setattr(admin, 'rows', fail)
    assert admin._own_stock_apply(CARD, '/shop') == CARD
    assert admin._own_stock_apply(DETAIL, '/product-goods-logo-hoodie') == DETAIL


def test_non_product_cards_unchanged(stock):
    stock(records())
    html = CARD.replace('/product-goods-logo-hoodie', '/p/admin-product')
    assert admin._own_stock_apply(html, '/shop') == html
