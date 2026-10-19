import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path


PATH = Path(__file__).parents[1] / 'scripts/reconcile_isekai_stock.py'
spec = importlib.util.spec_from_file_location('reconcile', PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.products = {pid: {'id': pid, 'name': pid, 'bundle': '', 'options': '[]'}
                         for pid in mod.REQUIRED_IDS}

    def order(self, lines, status='PAID', oid='one'):
        return {'order_id': oid, 'created': '2026-10-19T12:00:00+09:00',
                'status': status, 'items': json.dumps(lines)}

    def plan(self, *orders):
        return mod.calculate(self.products, orders)

    def test_parts_already_total_and_not_multiplied_by_line_quantity(self):
        pid = mod.PHYSICAL_IDS[0]
        result = self.plan(self.order([{'id': 'bundle', 'q': 3,
                                       'parts': [{'id': pid, 'q': 6}]}]))
        self.assertEqual(result['remaining'][pid], 3744)

    def test_all_reserved_statuses_count_and_cancelled_does_not(self):
        pid = mod.PHYSICAL_IDS[0]
        orders = [self.order([{'id': pid, 'q': 2}], status, str(i))
                  for i, status in enumerate(['PAID', 'PENDING', 'FAILED',
                                               'WAITING_DEPOSIT', 'CANCELLED'])]
        self.assertEqual(self.plan(*orders)['remaining'][pid], 3742)

    def test_parent_backfill_uses_most_remaining_and_total_is_exact(self):
        a, b = mod.PARENT_POOLS[mod.STAR_PARENT]
        result = self.plan(self.order([{'id': a, 'q': 10}]),
                           self.order([{'id': mod.STAR_PARENT, 'q': 3}], oid='two'))
        parts = result['order_updates']['two'][0]['parts']
        self.assertEqual(parts, [{'id': b, 'n': b, 'q': 3}])
        self.assertEqual(sum(result['remaining'][p] for p in (a, b)), 7500 - 13)

    def test_existing_parts_are_preserved_and_rerun_calculation_stable(self):
        order = self.order([{'id': mod.STAR_PARENT, 'q': 3}])
        first = self.plan(order)
        patched = {**order, 'items': json.dumps(first['order_updates']['one'])}
        second = self.plan(patched)
        self.assertEqual(first['remaining'], second['remaining'])
        self.assertEqual(second['order_updates'], {})

    def test_unlimited_stayg_does_not_consume_physical_units(self):
        result = self.plan(self.order([{'id': mod.STAYG_PARENT, 'q': 9000}]))
        self.assertEqual(set(result['remaining'].values()), {3750})

    def test_unknown_status_overdraw_wrong_day_and_bad_quantity_fail_closed(self):
        pid = mod.PHYSICAL_IDS[0]
        cases = [self.order([{'id': pid, 'q': 1}], 'UNRECOGNIZED'),
                 self.order([{'id': pid, 'q': 3751}]),
                 {**self.order([{'id': pid, 'q': 1}]), 'created': '2026-10-18T23:00:00+09:00'},
                 self.order([{'id': pid, 'q': -1}]),
                 self.order([{'id': pid, 'q': True}])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.plan(case)

    def test_unmapped_alias_without_parts_is_rejected(self):
        self.products['alias'] = {'id': 'alias', 'name': 'alias', 'bundle': '[{}]'}
        with self.assertRaises(ValueError):
            self.plan(self.order([{'id': 'alias', 'q': 1}]))

    def test_unexpected_group_product_cannot_become_unlimited(self):
        bundles = {pid: {'bundle': json.dumps([{'pid': mod.PHYSICAL_IDS[0], 'qty': 1}])}
                   for pid in mod.ACTIVE_BUNDLES}
        products = {**self.products, **bundles, 'retired': {'bundle': ''}}
        mod.validate_products(products)
        self.assertNotIn('retired', mod.WRITE_IDS)
        with self.assertRaises(ValueError):
            mod.validate_products({**products, mod.ACTIVE_BUNDLES[0]: {'bundle': ''}})

    def test_options_sync_keeps_other_fields_and_separates_baseline(self):
        raw = json.dumps([{'name': 'STAR', 'product_id': mod.PHYSICAL_IDS[0], 'stock': 1},
                          {'name': 'SET', 'product_id': 'alias', 'stock': 100}])
        result = mod.sync_options(raw)
        self.assertEqual(result[0]['stock'], 3750)
        self.assertEqual(result[0]['stock_baseline'], 3750)
        self.assertEqual(result[0]['name'], 'STAR')
        self.assertIsNone(result[1]['stock'])

    def test_apply_updates_exact_whitelist_and_preserves_retired_alias(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript('''
            CREATE TABLE products(id TEXT PRIMARY KEY,stock INTEGER,stock_baseline INTEGER,
                soldout INTEGER,bundle TEXT,options TEXT);
            CREATE TABLE product_variants(id TEXT PRIMARY KEY,stock_mode TEXT,sale_status TEXT,updated_at TEXT);
            CREATE TABLE inventory_balances(variant_id TEXT PRIMARY KEY,is_tracked INTEGER,
                on_hand INTEGER,reserved INTEGER,external_status TEXT,updated_at TEXT);
            CREATE TABLE inventory_movements(id TEXT PRIMARY KEY,variant_id TEXT,kind TEXT,
                quantity INTEGER,before_qty INTEGER,after_qty INTEGER,reason TEXT,by_admin TEXT,created_at TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY,items TEXT);
        ''')
        products = {**self.products, **{
            pid: {'id': pid, 'name': pid, 'stock': None, 'options': '[]',
                  'bundle': json.dumps([{'pid': mod.PHYSICAL_IDS[0], 'qty': 1}])}
            for pid in mod.ACTIVE_BUNDLES},
            'retired': {'id': 'retired', 'stock': None, 'soldout': 1}}
        variants = {pid: {'id': 'v-' + pid} for pid in products}
        for pid in products:
            conn.execute('INSERT INTO products VALUES(?,100,100,1,NULL,NULL)', (pid,))
            conn.execute('INSERT INTO product_variants(id) VALUES(?)', ('v-' + pid,))
            conn.execute('INSERT INTO inventory_balances(variant_id) VALUES(?)', ('v-' + pid,))
        order = self.order([{'id': mod.STAR_PARENT, 'q': 2}])
        conn.execute('INSERT INTO orders VALUES(?,?)', ('one', order['items']))
        plan = mod.calculate(products, [order])
        class Adapter:
            def execute(self, sql, params):
                return conn.execute(sql.replace('%s', '?'), params)
        mod.apply_products(Adapter(), products, variants, plan, '2026-10-19T12:00:00+09:00')
        self.assertEqual(conn.execute("SELECT stock,stock_baseline,soldout FROM products WHERE id='retired'").fetchone(),
                         (100, 100, 1))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM inventory_movements').fetchone()[0], 16)
        self.assertEqual(conn.execute('SELECT stock,stock_baseline FROM products WHERE id=?',
                                      (mod.PHYSICAL_IDS[0],)).fetchone(),
                         (plan['remaining'][mod.PHYSICAL_IDS[0]], 3750))
        self.assertEqual(conn.execute('SELECT stock,stock_baseline,soldout FROM products WHERE id=?',
                                      (mod.STAYG_CHILD,)).fetchone(), (None, None, 0))
        self.assertEqual(sum(p['q'] for p in json.loads(conn.execute('SELECT items FROM orders').fetchone()[0])[0]['parts']), 2)
        conn.close()


if __name__ == '__main__':
    unittest.main()
