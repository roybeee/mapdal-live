"""Regression checks execute production functions against real SQLite transactions."""
import ast
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class StockBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'stock.db'
        self.connections = []
        def conn():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            self.connections.append(c)
            return c
        self.conn = conn
        self.ns = {'_conn': conn, '_q': lambda s: s, 'IS_PG': False,
                   'HTTPException': HTTPException, 'json': json,
                   're': re, 'Request': object, 'Body': lambda x: None,
                   '_state': {'pcols': {'stock_baseline', 'name', 'options', 'show_from', 'show_until'}, 'pname': 'name', 'pprice': None}}
        tree = ast.parse((Path(__file__).parents[1] / 'admin_v2.py').read_text())
        names = {'num', 'rows', 'one', 'run', 'jload', '_product_stock_baselines',
                 '_cancel_order_stock', '_catalog_stock_totals', '_prod_opts_admin', '_prod_opts_sync',
                 'api_product_detail_update', '_prod_opts_norm', '_bundle_norm'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'admin_v2.py', 'exec'), self.ns)
        self.normalize_options = self.ns['_prod_opts_norm']
        self.ns['_DROP_OPT_KIND_SET'] = {'album', 'card', 'ticket', 'gift', 'etc'}
        with conn() as c:
            c.execute('CREATE TABLE products(id TEXT PRIMARY KEY, stock INTEGER, stock_baseline INTEGER, soldout INTEGER,name TEXT,options TEXT,show_from TEXT,show_until TEXT)')
            c.execute('CREATE TABLE orders(order_id TEXT PRIMARY KEY,status TEXT,items TEXT,admin_memo TEXT)')
            c.executemany('INSERT INTO products(id,stock,stock_baseline,soldout) VALUES(?,?,?,?)', [('a', 90, 100, 0), ('b', None, None, 0), ('c', 12, None, 0)])
        noop = lambda *args, **kwargs: None
        self.ns.update({name: noop for name in ('need', 'audit', '_prod_opt_attach_group', '_bundle_resolve',
                       '_k2g_cache_bust', 'catalog_product_from_legacy', '_related_set')})
        self.ns.update({name: lambda x: x for name in ('_safe_url', 'clean_blocks', 'norm_cat', 'badge_color',
                       'hex_or_empty', '_clean_gallery', '_prod_sched_norm', '_prod_opts_norm')})
        self.ns.update(get_actor=lambda req: {}, _prod_opts_load=lambda pid: [],
                       _PROD_SCHED_COLS=('show_from', 'show_until'), _sitemap_cache={})

    def tearDown(self):
        for c in self.connections:
            c.close()
        self.temp.cleanup()

    def product(self, pid='a'):
        with self.conn() as c:
            return dict(c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone())

    def apply(self, changes):
        return self.ns['_product_stock_baselines'](changes)

    def test_baseline_unchanged_after_sale_and_form_save(self):
        with self.conn() as c:
            c.execute("UPDATE products SET stock=87 WHERE id='a'")
        self.apply([{'id': 'a', 'baseline': 100, 'expected': 100}])
        self.assertEqual((self.product()['stock'], self.product()['stock_baseline']), (87, 100))

    def test_baseline_change_preserves_sales_since_form_opened(self):
        with self.conn() as c:
            c.execute("UPDATE products SET stock=87 WHERE id='a'")
        self.apply([{'id': 'a', 'baseline': 120, 'expected': 100}])
        self.assertEqual((self.product()['stock'], self.product()['stock_baseline']), (107, 120))

    def test_stale_baseline_conflicts(self):
        self.apply([{'id': 'a', 'baseline': 120, 'expected': 100}])
        with self.assertRaises(HTTPException) as e:
            self.apply([{'id': 'a', 'baseline': 110, 'expected': 100}])
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(self.product()['stock'], 110)

    def test_unset_and_unlimited_unchanged(self):
        self.apply([{'id': 'b', 'baseline': None, 'expected': None},
                    {'id': 'c', 'baseline': None, 'expected': None}])
        self.assertIsNone(self.product('b')['stock'])
        self.assertEqual(self.product('b')['soldout'], 0)
        self.assertEqual(self.product('c')['stock'], 12)

    def test_unset_cannot_invent_historic_baseline(self):
        with self.assertRaises(HTTPException):
            self.apply([{'id': 'c', 'baseline': 3750, 'expected': None}])
        self.assertEqual(self.product('c')['stock'], 12)

    def test_all_option_changes_rollback_on_conflict(self):
        with self.assertRaises(HTTPException):
            self.apply([{'id': 'a', 'baseline': 120, 'expected': 100},
                        {'id': 'c', 'baseline': 3750, 'expected': None}])
        self.assertEqual(self.product()['stock'], 90)

    def test_cannot_reduce_baseline_below_sold_quantity(self):
        with self.assertRaises(HTTPException):
            self.apply([{'id': 'a', 'baseline': 5, 'expected': 100}])
        self.assertEqual(self.product()['stock'], 90)

    def test_zero_remaining_marks_soldout_and_receipt_reopens(self):
        self.apply([{'id': 'a', 'baseline': 10, 'expected': 100}])
        self.assertEqual((self.product()['stock'], self.product()['soldout']), (0, 1))
        self.apply([{'id': 'a', 'baseline': 20, 'expected': 10}])
        self.assertEqual((self.product()['stock'], self.product()['soldout']), (10, 0))

    def test_invalid_baseline_rejected(self):
        for value in (-1, 1.5, 'invalid', True):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                self.apply([{'id': 'a', 'baseline': value, 'expected': 100}])

    def cancel(self, items, restock=True):
        with self.conn() as c:
            c.execute('INSERT OR IGNORE INTO orders VALUES(?,?,?,?)', ('o', 'CREATED', json.dumps(items), ''))
        return self.ns['_cancel_order_stock']('o', ["status='CANCELLED'"], [], restock)

    def test_cancel_uses_locked_latest_items_and_restores_bundle_once(self):
        items = [{'id': 'b', 'q': 2, 'parts': [{'id': 'a', 'q': 4}]}]
        self.cancel(items)
        self.assertEqual(self.product()['stock'], 94)
        self.assertEqual(self.product()['stock_baseline'], 100)
        self.assertIsNone(self.product('b')['stock'])
        with self.assertRaises(HTTPException):
            self.cancel(items)
        self.assertEqual(self.product()['stock'], 94)

    def test_cancel_failure_rolls_back_status_and_stock(self):
        with self.conn() as c:
            c.execute("CREATE TRIGGER reject_stock BEFORE UPDATE ON products BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.cancel([{'id': 'a', 'q': 2}])
        with self.conn() as c:
            self.assertEqual(c.execute('SELECT status FROM orders').fetchone()[0], 'CREATED')

    def test_cancel_without_restock_preserves_stock(self):
        self.cancel([{'id': 'a', 'q': 2}], restock=False)
        self.assertEqual(self.product()['stock'], 90)

    def test_payment_completed_during_cancel_requires_refund_recheck(self):
        with self.conn() as c:
            c.execute('INSERT INTO orders VALUES(?,?,?,?)', ('o', 'PAID', '[{"id":"a","q":2}]', ''))
        with self.assertRaises(HTTPException) as e:
            self.ns['_cancel_order_stock']('o', ["status='CANCELLED'"], [], True, expected_status='CREATED')
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(self.product()['stock'], 90)

    def test_group_total_ignores_parent_bundle_and_external(self):
        vv = [{'tracked': True, 'counts_stock': False, 'available': 3000},
              {'tracked': True, 'counts_stock': False, 'available': 400},
              {'tracked': False, 'counts_stock': True, 'available': None},
              {'tracked': True, 'counts_stock': True, 'available': 90, 'stock_baseline': 100},
              {'tracked': True, 'counts_stock': True, 'available': 80, 'stock_baseline': 100}]
        self.assertEqual(self.ns['_catalog_stock_totals'](vv), {'available': 170, 'stock_baseline': 200})

    def test_unlimited_group_not_reported_as_zero(self):
        self.assertIsNone(self.ns['_catalog_stock_totals']([{'tracked': True, 'counts_stock': False}])['available'])

    def test_invalid_schedule_does_not_change_stock(self):
        with self.assertRaises(HTTPException):
            self.ns['api_product_detail_update'](None, {'id': 'a', 'stock_baseline': 120,
                'expected_stock_baseline': 100, 'show_from': '2026-11-10', 'show_until': '2026-11-01'})
        self.assertEqual((self.product()['stock'], self.product()['stock_baseline']), (90, 100))

    def test_detail_information_save_preserves_unlimited_stock(self):
        self.ns['api_product_detail_update'](None, {'id': 'b', 'name': 'edited',
            'stock_baseline': None, 'expected_stock_baseline': None})
        self.assertEqual((self.product('b')['stock'], self.product('b')['soldout']), (None, 0))

    def test_old_editor_absolute_stock_is_rejected(self):
        with self.assertRaises(HTTPException) as e:
            self.ns['api_product_detail_update'](None, {'id': 'a', 'stock': 100})
        self.assertEqual(e.exception.status_code, 409)
        self.assertEqual(self.product()['stock'], 90)

    def test_option_editor_separates_baseline_from_remaining(self):
        self.ns['_prod_opts_load'] = lambda pid: [{'managed': 1, 'product_id': 'a'}]
        option = self.ns['_prod_opts_admin']('b')[0]
        self.assertEqual((option['stock'], option['remaining_stock']), (100, 90))

    def test_option_metadata_save_cannot_restore_old_stock(self):
        option = {'name': 'album', 'managed': 1, 'product_id': 'a', 'price': 10, 'stock': 100}
        self.ns['_prod_opts_sync']('b', [option], [option], {})
        self.assertEqual((self.product()['stock'], self.product()['stock_baseline']), (90, 100))

    def test_option_normalization_discards_untrusted_baseline_tokens(self):
        attack = '\" autofocus onfocus=alert(1) x=\"'
        normalized = self.normalize_options([{'name': 'manual option', 'stock': attack,
            'stock_baseline': attack, 'expected_stock_baseline': attack,
            'product_id': 'a', 'managed': 0}])[0]
        self.assertNotIn('stock_baseline', normalized)
        self.assertNotIn('expected_stock_baseline', normalized)
        self.assertEqual(normalized['stock'], 0)


if __name__ == '__main__':
    unittest.main()
