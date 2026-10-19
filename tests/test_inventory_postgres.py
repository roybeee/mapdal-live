"""Opt-in real PostgreSQL contention tests, isolated to a temporary local schema."""
import ast
import concurrent.futures
import datetime
import json
import os
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

import unified_inventory as inventory


DSN = os.environ.get('MAPDAL_TEST_POSTGRES_URL', '')
pytestmark = pytest.mark.skipif(not DSN, reason='local PostgreSQL test URL not supplied')


class ConnectionAdapter:
    def __init__(self, connection):
        self.connection = connection

    def exec(self, query, args=()):
        return self.connection.execute(query.replace('?', '%s'), args)

    def one(self, query, args=()):
        return self.exec(query, args).fetchone()


@pytest.fixture
def pg(monkeypatch):
    assert urlparse(DSN).hostname in {'localhost', '127.0.0.1'}, 'Only an isolated local database is allowed'
    psycopg = pytest.importorskip('psycopg')
    from psycopg.rows import dict_row
    schema = 'inventory_test_' + uuid.uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute('CREATE SCHEMA ' + schema)

    def connect():
        return psycopg.connect(DSN, row_factory=dict_row,
                              options='-csearch_path=%s -cstatement_timeout=5000' % schema)

    admin = SimpleNamespace(_conn=connect, _q=lambda s: s.replace('?', '%s'), IS_PG=True,
                            now_iso=lambda: '2026-10-19T17:00:00')
    monkeypatch.setattr(inventory, '_admin', lambda: admin)
    with connect() as c:
        c.execute('''CREATE TABLE products(id TEXT PRIMARY KEY,name TEXT,price INTEGER,stock INTEGER,
          stock_baseline INTEGER,soldout INTEGER DEFAULT 0,options TEXT,bundle TEXT,kind TEXT,
          img TEXT,category TEXT,show_from TEXT,show_until TEXT,created_at TEXT);
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
        CREATE TABLE orders(order_id TEXT PRIMARY KEY,status TEXT,items TEXT,created TEXT,
          amount INTEGER,buyer TEXT,ship_method TEXT,customer_id TEXT,member_id TEXT,contact_phone_norm TEXT);''')
        options = [dict(name='STAR', price=0, product_id='star', hidden=1, managed=1),
                   dict(name='RACE', price=0, product_id='race', hidden=1, managed=1),
                   dict(name='Random', price=100, product_id='random', managed=1,
                        bundle=[dict(name='STAR', qty=1), dict(name='RACE', qty=1)], bundle_mode='any'),
                   dict(name='Set', price=200, product_id='set', managed=1,
                        bundle=[dict(name='STAR', qty=1), dict(name='RACE', qty=1)], bundle_mode='all')]
        parts = [dict(pid='star', qty=1, mode='any'), dict(pid='race', qty=1)]
        for row in [('parent', 'Album', 100, None, None, json.dumps(options), json.dumps(parts)),
                    ('star', 'STAR', 0, 100, 3750, '[]', ''),
                    ('race', 'RACE', 0, 100, 3750, '[]', ''),
                    ('random', 'Random', 100, None, None, '[]', json.dumps(parts)),
                    ('set', 'Set', 200, None, None, '[]', json.dumps([dict(pid='star', qty=1), dict(pid='race', qty=1)]))]:
            c.execute('INSERT INTO products(id,name,price,stock,stock_baseline,options,bundle) VALUES(%s,%s,%s,%s,%s,%s,%s)', row)
            pid = row[0]
            c.execute('INSERT INTO product_variants VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      ('v_' + pid, pid, 'group', 'SKU-' + pid, pid, 'DIRECT', 'ACTIVE',
                       'TRACKED' if pid in {'star', 'race'} else 'BUNDLE', '{}', '', ''))
            c.execute('INSERT INTO inventory_balances VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      ('v_' + pid, 'SEOUL', int(pid in {'star', 'race'}), 9999, 0, 0, 5, 'AVAILABLE', ''))
    inventory.ensure_schema()
    namespace = {'HTTPException': HTTPException, 'json': json, '_conn': connect, '_q': admin._q,
                 'IS_PG': True, '_state': {'pname': 'name'}}
    source = ast.parse((Path(__file__).parents[1] / 'admin_v2.py').read_text())
    names = {'num', 'jload', 'bundle_parts_of', 'bundle_mode_of', 'bundle_take', '_cancel_order_stock'}
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<production-functions>', 'exec'), namespace)
    try:
        yield connect, namespace
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute('DROP SCHEMA ' + schema + ' CASCADE')


ACTOR = {'name': 'Test operator', 'role': 'OWNER'}


def purchase(pg, pids, order_id=None):
    connect, functions = pg
    with connect() as c:
        adapter = ConnectionAdapter(c)
        inventory.lock_order_inventory(adapter, [{'id': pid} for pid in pids], lambda pid: [pid])
        lines = []
        for pid in pids:
            row = adapter.one('SELECT * FROM products WHERE id=? FOR UPDATE', (pid,))
            parts = functions['bundle_take'](adapter, row, 1, ' FOR UPDATE')
            lines.append({'id': pid, 'q': 1, 'parts': parts})
        if order_id:
            c.execute('INSERT INTO orders(order_id,status,items) VALUES(%s,%s,%s)', (order_id, 'PENDING', json.dumps(lines)))
        return sum(part['q'] for line in lines for part in line['parts'])


def remaining(pg):
    with pg[0]() as c:
        return {r['id']: r['stock'] for r in c.execute("SELECT id,stock FROM products WHERE id IN ('star','race')")}


def test_last_stock_random_and_set_never_oversell(pg):
    with pg[0]() as c:
        c.execute("UPDATE products SET stock=5 WHERE id IN ('star','race')")
    def attempt(index):
        try:
            return purchase(pg, ['random' if index % 2 else 'set'])
        except HTTPException as error:
            assert error.status_code in {400, 409}
            return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sold = sum(pool.map(attempt, range(20)))
    stock = remaining(pg)
    assert sold == 10
    assert sum(stock.values()) == 0
    assert min(stock.values()) >= 0


def test_opposite_cart_orders_and_single_set_do_not_deadlock(pg):
    requests = [['random', 'set'], ['set', 'random'], ['set']] * 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sold = sum(pool.map(lambda ids: purchase(pg, ids), requests))
    assert sum(remaining(pg).values()) == 200 - sold


def test_configuration_receipt_preserves_parallel_purchases(pg):
    config = inventory.get_inventory_config('parent')
    config['inventory_items'][0]['baseline'] += 50
    barrier = threading.Barrier(2)
    def save():
        barrier.wait(timeout=3)
        return inventory.save_inventory_config(config, ACTOR)
    def buy():
        barrier.wait(timeout=3)
        return sum(purchase(pg, ['random', 'set']) for _ in range(20))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        saved, bought = pool.submit(save), pool.submit(buy)
        assert saved.result(timeout=10)['ok']
        sold = bought.result(timeout=10)
    assert sum(remaining(pg).values()) == 250 - sold


def test_cancellation_uses_historical_parts_after_unified_save(pg):
    purchase(pg, ['set'], 'existing-order')
    inventory.save_inventory_config(inventory.get_inventory_config('parent'), ACTOR)
    restored = pg[1]['_cancel_order_stock']('existing-order', ["status='CANCELLED'"], [], True)
    assert set(restored) == {'star', 'race'}
    assert remaining(pg) == {'star': 100, 'race': 100}
    with pytest.raises(HTTPException):
        pg[1]['_cancel_order_stock']('existing-order', ["status='CANCELLED'"], [], True)
    assert remaining(pg) == {'star': 100, 'race': 100}


def test_stale_count_and_invalid_configuration_rollback(pg):
    old = inventory.get_inventory_config('parent')
    purchase(pg, ['set'])
    with pytest.raises(HTTPException):
        inventory.adjust_inventory('v_star', {'kind': 'COUNT', 'quantity': 105, 'expected_stock': 100}, ACTOR)
    old['inventory_items'][0]['baseline'] += 100
    old['sale_options'][0]['components'][0]['product_id'] = 'outside-parent'
    with pytest.raises(HTTPException):
        inventory.save_inventory_config(old, ACTOR)
    assert remaining(pg) == {'star': 99, 'race': 99}


def test_configuration_lock_does_not_block_an_unrelated_purchase(pg):
    # Row locks are scoped; no lock is taken on the products or orders table.
    with pg[0]() as held:
        held.execute("SELECT id FROM products WHERE id='parent' FOR UPDATE")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(purchase, pg, ['set']).result(timeout=2) == 2


def test_domestic_and_international_orders_share_lock_order(pg, monkeypatch):
    import globalshop
    @contextmanager
    def db():
        with pg[0]() as c:
            yield ConnectionAdapter(c)
    functions = pg[1]
    def one(sql, args=()):
        with pg[0]() as c:
            return c.execute(sql.replace('?', '%s'), args).fetchone()
    functions['one'] = one
    source = ast.parse((Path(__file__).parents[1] / 'admin_v2.py').read_text())
    availability = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'bundle_availability')
    exec(compile(ast.Module(body=[availability], type_ignores=[]), '<production-availability>', 'exec'), functions)
    app = SimpleNamespace(db=db, LOCK=' FOR UPDATE', _product_id_candidates=lambda pid: [pid],
                          kst_naive=lambda: datetime.datetime(2026, 10, 19, 17),
                          kst_iso=lambda: '2026-10-19T17:00:00')
    monkeypatch.setattr(globalshop, '_app', lambda: app)
    monkeypatch.setattr(globalshop, '_av', lambda: SimpleNamespace(**functions))
    monkeypatch.setattr(globalshop, 'ship_fee', lambda *args: 0)
    monkeypatch.setattr(globalshop, 'ddp_rates', lambda: {})
    monkeypatch.setattr(globalshop, '_usd_rate', lambda: .00072)
    def buy(index):
        if index % 2:
            return purchase(pg, ['random', 'set'])
        result = globalshop._intl_order_tx(app, [{'id': 'set', 'q': 1}, {'id': 'random', 'q': 1}],
                                          'US', {'email': 'fixture@example.test'}, '', '', '', None)
        return sum(p['q'] for item in result[4] for p in item['parts'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        sold = sum(pool.map(buy, range(20)))
    assert sum(remaining(pg).values()) == 200 - sold


@pytest.mark.parametrize('operation', ['cancel', 'external_available'])
def test_retirement_wins_when_stock_operation_waits_for_product_lock(pg, operation):
    connect, functions = pg
    with connect() as c:
        c.execute("UPDATE products SET bundle='',stock=%s WHERE id='random'",
                  (10 if operation == 'cancel' else None,))
        c.execute("INSERT INTO orders(order_id,status,items) VALUES('retire-order','PENDING',%s)",
                  (json.dumps([{'id': 'random', 'q': 1}]),))
    def action():
        if operation == 'cancel':
            return functions['_cancel_order_stock']('retire-order', ["status='CANCELLED'"], [], True)
        return inventory.adjust_inventory('v_random', {'external_status': 'AVAILABLE'}, ACTOR)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with connect() as held:
            blocker = held.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
            held.execute("UPDATE products SET soldout=1 WHERE id='random'")
            held.execute("UPDATE product_variants SET sale_status='PAUSED' WHERE legacy_product_id='random'")
            future = pool.submit(action)
            deadline = time.monotonic() + 1
            with connect() as observer:
                while True:
                    waiting = observer.execute('SELECT EXISTS(SELECT 1 FROM pg_stat_activity '
                                               'WHERE %s=ANY(pg_blocking_pids(pid))) AS waiting',
                                               (blocker,)).fetchone()['waiting']
                    if waiting:
                        break
                    assert time.monotonic() < deadline, 'The stock operation did not wait for retirement'
                    time.sleep(.01)
            held.commit()
        future.result(timeout=3)
    with connect() as c:
        row = c.execute("SELECT stock,soldout FROM products WHERE id='random'").fetchone()
        assert row['stock'] == (11 if operation == 'cancel' else None)
        assert row['soldout'] == 1
