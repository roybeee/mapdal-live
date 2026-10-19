"""Real SQLite transactions exercise shared stock without resetting live sales."""
import ast
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import unified_inventory as ui


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    path = tmp_path / 'inventory.db'
    def connect():
        c = sqlite3.connect(path, timeout=2)
        c.row_factory = sqlite3.Row
        return c
    admin = SimpleNamespace(_conn=connect, _q=lambda s: s, IS_PG=False,
                            now_iso=lambda: '2026-10-19T17:00:00')
    monkeypatch.setattr(ui, '_admin', lambda: admin)
    with connect() as c:
        c.executescript('''
        CREATE TABLE products(id TEXT PRIMARY KEY,name TEXT,price INTEGER,stock INTEGER,
          stock_baseline INTEGER,soldout INTEGER DEFAULT 0,options TEXT,bundle TEXT,
          kind TEXT,img TEXT,category TEXT,show_from TEXT,show_until TEXT,created_at TEXT);
        CREATE TABLE product_variants(id TEXT PRIMARY KEY,legacy_product_id TEXT UNIQUE,
          group_id TEXT,sku TEXT UNIQUE,option_name TEXT,source TEXT,sale_status TEXT,
          stock_mode TEXT,metadata TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE inventory_balances(variant_id TEXT PRIMARY KEY,location_id TEXT,is_tracked INTEGER,
          on_hand INTEGER,reserved INTEGER,incoming INTEGER,reorder_point INTEGER,
          external_status TEXT,updated_at TEXT);
        CREATE TABLE inventory_movements(id TEXT PRIMARY KEY,variant_id TEXT,kind TEXT,quantity INTEGER,
          before_qty INTEGER,after_qty INTEGER,reason TEXT,by_admin TEXT,created_at TEXT);
        CREATE TABLE product_identifiers(id TEXT PRIMARY KEY,variant_id TEXT,kind TEXT,value TEXT);
        CREATE TABLE audit_log(id TEXT PRIMARY KEY,created TEXT,actor TEXT,role TEXT,action TEXT,target TEXT,detail TEXT);
        ''')
        opts = [dict(name='STAR', price=0, product_id='star', hidden=1, managed=1),
                dict(name='RACE', price=0, product_id='race', hidden=1, managed=1),
                dict(name='Random', price=25200, product_id='random', managed=1,
                     bundle=[dict(name='STAR',qty=1),dict(name='RACE',qty=1)],bundle_mode='any'),
                dict(name='Set', price=50400, product_id='set', managed=1,
                     bundle=[dict(name='STAR',qty=1),dict(name='RACE',qty=1)])]
        parts = [dict(pid='star',qty=1,mode='any'),dict(pid='race',qty=1)]
        c.executemany('INSERT INTO products(id,name,price,stock,stock_baseline,options,bundle) VALUES(?,?,?,?,?,?,?)',[
            ('parent','Album',25200,None,None,json.dumps(opts),json.dumps(parts)),
            ('star','STAR',0,3400,3750,'[]',''),('race','RACE',0,3401,3750,'[]',''),
            ('random','Random',25200,None,None,'[]',json.dumps(parts)),
            ('set','Set',50400,None,None,'[]',json.dumps([dict(pid='star',qty=1),dict(pid='race',qty=1)])),
            ('foreign','Other stock',0,123,200,'[]',''),
            ('stayg','STAYG',20000,None,None,'[]','')])
        for pid in ['parent','star','race','random','set','stayg']:
            mode = 'TRACKED' if pid in ['star','race'] else 'EXTERNAL'
            c.execute('INSERT INTO product_variants VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      ('v_'+pid,pid,'group','SKU-'+pid,pid,'DIRECT','ACTIVE',mode,'{}','',''))
            c.execute('INSERT INTO inventory_balances VALUES(?,?,?,?,?,?,?,?,?)',
                      ('v_'+pid,'SEOUL',int(mode=='TRACKED'),9999,0,0,5,'AVAILABLE',''))
    ui.ensure_schema()
    return connect


ACTOR = {'name':'Tester','role':'OWNER'}


def row(connect, pid):
    with connect() as c:
        return dict(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())


def test_get_imports_hidden_inventory_without_writing(inventory):
    config = ui.get_inventory_config('parent')
    assert {i['product_id'] for i in config['inventory_items']} == {'star','race'}
    assert {o['product_id'] for o in config['sale_options']} == {'random','set'}
    assert config['sale_options'][0]['mode'] == 'any'
    with inventory() as c:
        assert c.execute('SELECT COUNT(*) FROM product_inventory_items').fetchone()[0] == 0


def test_save_preserves_sales_since_load_and_ids(inventory):
    config = ui.get_inventory_config('parent')
    with inventory() as c:
        c.execute("UPDATE products SET stock=3397 WHERE id='star'")
    config['inventory_items'][0]['baseline'] = 3800
    ui.save_inventory_config(config,ACTOR)
    assert row(inventory,'star')['stock'] == 3447
    assert row(inventory,'star')['stock_baseline'] == 3800
    assert {o['product_id'] for o in ui.get_inventory_config('parent')['sale_options']} == {'random','set'}
    assert json.loads(row(inventory,'random')['bundle'])[0]['pid'] == 'star'


def test_noop_keeps_stock_and_unlimited(inventory):
    ui.save_inventory_config(ui.get_inventory_config('parent'),ACTOR)
    ui.save_inventory_config(ui.get_inventory_config('stayg'),ACTOR)
    assert row(inventory,'star')['stock'] == 3400
    assert row(inventory,'stayg')['stock'] is None
    assert row(inventory,'stayg')['soldout'] == 0


@pytest.mark.parametrize('failure',['wrong_owner','removed','invalid_qty','stale_baseline','stale_options','finite_switch'])
def test_invalid_configs_rollback_everything(inventory,failure):
    config = ui.get_inventory_config('parent')
    before = row(inventory,'parent')['options']
    config['inventory_items'][0]['baseline'] = 3800
    if failure == 'wrong_owner': config['sale_options'][0]['components'][0]['product_id'] = 'foreign'
    elif failure == 'removed': config['inventory_items'].pop()
    elif failure == 'invalid_qty': config['sale_options'][0]['components'][0]['qty'] = 0
    elif failure == 'stale_baseline': config['inventory_items'][1]['expected_baseline'] = 4000
    elif failure == 'stale_options': config['expected_options'] = 'stale'
    elif failure == 'finite_switch': config['inventory_items'][1]['baseline'] = None
    with pytest.raises(HTTPException): ui.save_inventory_config(config,ACTOR)
    assert row(inventory,'star')['stock'] == 3400
    assert row(inventory,'parent')['options'] == before
    with inventory() as c:
        assert c.execute('SELECT COUNT(*) FROM product_inventory_items').fetchone()[0] == 0


def test_new_inventory_created_internally_and_stable(inventory):
    config = ui.get_inventory_config('parent')
    config['inventory_items'].append(dict(client_key='new:item',name='New version',baseline=50,expected_baseline=None))
    config['sale_options'].append(dict(name='New option',price=100,mode='all',
                                       components=[dict(product_id='new:item',qty=1)],items=[]))
    saved = ui.save_inventory_config(config,ACTOR)
    item = next(x for x in saved['inventory_items'] if x['name']=='New version')
    sale = next(x for x in saved['sale_options'] if x['name']=='New option')
    assert item['product_id'].startswith('mpo::')
    assert row(inventory,item['product_id'])['price'] == 0
    assert sale['components'] == [dict(product_id=item['product_id'],qty=1)]
    ui.save_inventory_config(saved,ACTOR)
    assert len(ui.get_inventory_config('parent')['inventory_items']) == 3


def test_retiring_sale_keeps_order_and_cart_identity(inventory):
    config = ui.get_inventory_config('parent')
    config['sale_options'] = config['sale_options'][:1]
    ui.save_inventory_config(config,ACTOR)
    assert row(inventory,'set')['soldout'] == 1
    assert row(inventory,'star')['stock'] == 3400
    assert json.loads(row(inventory,'set')['bundle'])[0]['pid'] == 'star'


def test_delete_protection_for_parent_and_inventory(inventory):
    for pid in ['parent','star']:
        with pytest.raises(HTTPException) as e: ui.protect_product_delete(pid)
        assert e.value.status_code == 409
    ui.protect_product_delete('foreign')


def test_adjust_uses_canonical_stock_not_stale_projection(inventory):
    ui.adjust_inventory('v_star',dict(kind='RECEIVE',quantity=100),ACTOR)
    assert row(inventory,'star')['stock'] == 3500
    assert row(inventory,'star')['stock_baseline'] == 3850
    with inventory() as c:
        assert c.execute("SELECT on_hand FROM inventory_balances WHERE variant_id='v_star'").fetchone()[0] == 3500


def test_absolute_count_requires_fresh_compare_and_swap(inventory):
    with pytest.raises(HTTPException):
        ui.adjust_inventory('v_star',dict(kind='COUNT',quantity=3500,expected_stock=3500),ACTOR)
    assert row(inventory,'star')['stock'] == 3400
    ui.adjust_inventory('v_star',dict(kind='COUNT',quantity=3500,expected_stock=3400),ACTOR)
    assert row(inventory,'star')['stock'] == 3500


def test_bundle_cannot_have_independent_stock_adjustment(inventory):
    with pytest.raises(HTTPException):
        ui.adjust_inventory('v_random',dict(external_status='OUT'),ACTOR)
    assert row(inventory,'random')['soldout'] == 0


def test_lock_order_includes_components_and_sorts_across_cart(inventory):
    queried = []
    class CartConnection:
        def one(self, sql, args):
            queried.append(args[0])
            with inventory() as c:
                r = c.execute(sql,args).fetchone()
                return dict(r) if r else None
    ui.lock_order_inventory(CartConnection(),[{'id':'set'},{'id':'random'}],lambda p:[p])
    assert queried[2:] == sorted(['set','random','star','race'])


def test_configuration_change_during_prelock_rejects_before_deduction(inventory):
    count = 0
    class CartConnection:
        def one(self, sql, args):
            nonlocal count
            count += 1
            with inventory() as c:
                if count == 2:
                    c.execute("UPDATE products SET bundle='[]' WHERE id='random'")
                r = c.execute(sql,args).fetchone()
                return dict(r) if r else None
    with pytest.raises(HTTPException) as error:
        ui.lock_order_inventory(CartConnection(),[{'id':'random'}],lambda p:[p])
    assert error.value.status_code == 409
    assert row(inventory,'star')['stock'] == 3400


def test_save_preserves_cancel_parts_ids(inventory):
    config = ui.get_inventory_config('parent')
    historical_parts = [{'id':'star','q':3},{'id':'race','q':2}]
    ui.save_inventory_config(config,ACTOR)
    with inventory() as c:
        for part in historical_parts:
            c.execute('UPDATE products SET stock=stock+? WHERE id=?',(part['q'],part['id']))
    assert row(inventory,'star')['stock'] == 3403
    assert row(inventory,'race')['stock'] == 3403


def test_repeated_stale_save_conflicts(inventory):
    config = ui.get_inventory_config('parent')
    ui.save_inventory_config(config,ACTOR)
    with pytest.raises(HTTPException) as error:
        ui.save_inventory_config(config,ACTOR)
    assert error.value.status_code == 409


def test_adjust_validation_does_not_partially_update_stock(inventory):
    with pytest.raises(HTTPException):
        ui.adjust_inventory('v_star',dict(quantity=100,incoming=-1),ACTOR)
    assert row(inventory,'star')['stock'] == 3400


def test_direct_parent_random_pool_includes_new_physical_item(inventory):
    config = ui.get_inventory_config('parent')
    config['inventory_items'].append(dict(client_key='new:item',name='New version',baseline=50,expected_baseline=None))
    saved = ui.save_inventory_config(config,ACTOR)
    assert {p['pid'] for p in json.loads(row(inventory,'parent')['bundle'])} == {
        i['product_id'] for i in saved['inventory_items']}


def test_physical_item_cannot_become_a_parent_through_api(inventory):
    with pytest.raises(HTTPException): ui.get_inventory_config('star')
    config = ui.get_inventory_config('stayg')
    config['id'] = 'star'
    with pytest.raises(HTTPException): ui.save_inventory_config(config,ACTOR)
    assert row(inventory,'star')['stock'] == 3400


def test_receipt_movements_only_record_real_delta(inventory):
    saved = ui.save_inventory_config(ui.get_inventory_config('parent'),ACTOR)
    with inventory() as c:
        assert c.execute('SELECT COUNT(*) FROM inventory_movements').fetchone()[0] == 0
    saved['inventory_items'][0]['baseline'] += 50
    ui.save_inventory_config(saved,ACTOR)
    with inventory() as c:
        movements = c.execute('SELECT quantity,before_qty,after_qty FROM inventory_movements').fetchall()
        assert [tuple(m) for m in movements] == [(50,3400,3450)]


def test_unexpected_bundle_failure_rolls_back_stock(inventory, monkeypatch):
    source = ast.parse((Path(__file__).parents[1] / 'app.py').read_text())
    block = next(n for n in ast.walk(source) if isinstance(n, ast.Try) and any(
        isinstance(child, ast.Attribute) and child.attr == 'bundle_take'
        for statement in n.body for child in ast.walk(statement)) and
        any(isinstance(statement, ast.Import) for statement in n.body))
    function = ast.parse('def run(c, row, q, LOCK):\n    pass').body[0]
    function.body = [block]
    ns = {'HTTPException': HTTPException}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])), 'checkout-test', 'exec'),ns)
    def fail(*args): raise RuntimeError('component failure')
    monkeypatch.setitem(sys.modules,'admin_v2',SimpleNamespace(bundle_take=fail))
    with pytest.raises(RuntimeError):
        with inventory() as c:
            c.execute("UPDATE products SET stock=stock-1 WHERE id='star'")
            ns['run'](c,{},1,'')
    assert row(inventory,'star')['stock'] == 3400


def test_unmanaged_external_sale_link_is_not_mutated(inventory):
    with inventory() as c:
        opts = json.loads(row(inventory,'parent')['options'])
        opts[2]['managed'] = 0
        c.execute("UPDATE products SET options=? WHERE id='parent'",(json.dumps(opts),))
    with pytest.raises(HTTPException):
        ui.save_inventory_config(ui.get_inventory_config('parent'),ACTOR)
    assert row(inventory,'random')['name'] == 'Random'


def test_restock_reopens_inventory_derived_sold_out(inventory):
    with inventory() as c:
        c.execute("UPDATE products SET stock=0,soldout=1 WHERE id='star'")
        c.execute("UPDATE product_variants SET sale_status='SOLD_OUT' WHERE id='v_star'")
    ui.adjust_inventory('v_star', {'kind': 'RECEIVE', 'quantity': 100}, ACTOR)
    assert (row(inventory, 'star')['stock'], row(inventory, 'star')['soldout']) == (100, 0)


def test_unlimited_external_available_reopens_out_status(inventory):
    ui.adjust_inventory('v_stayg', {'external_status': 'OUT'}, ACTOR)
    ui.adjust_inventory('v_stayg', {'external_status': 'AVAILABLE'}, ACTOR)
    assert row(inventory, 'stayg')['soldout'] == 0


def test_retired_sale_is_explicitly_paused(inventory):
    config = ui.get_inventory_config('parent')
    config['sale_options'] = config['sale_options'][:1]
    ui.save_inventory_config(config, ACTOR)
    with inventory() as c:
        assert c.execute("SELECT sale_status FROM product_variants WHERE legacy_product_id='set'").fetchone()[0] == 'PAUSED'


def test_connected_sale_option_cannot_be_deleted(inventory):
    for pid in ['random', 'set']:
        with pytest.raises(HTTPException) as error:
            ui.protect_product_delete(pid)
        assert error.value.status_code == 409


def test_omitted_unmanaged_sale_cannot_stop_external_product(inventory):
    with inventory() as c:
        opts = json.loads(row(inventory, 'parent')['options'])
        opts[2]['managed'] = 0
        c.execute("UPDATE products SET options=? WHERE id='parent'", (json.dumps(opts),))
    config = ui.get_inventory_config('parent')
    config['sale_options'] = [o for o in config['sale_options'] if o['product_id'] != 'random']
    with pytest.raises(HTTPException):
        ui.save_inventory_config(config, ACTOR)
    assert row(inventory, 'random')['soldout'] == 0
