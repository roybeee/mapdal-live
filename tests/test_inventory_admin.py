"""Regression tests for admin stock roles and destructive legacy actions."""
import ast
import json
import re
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path


SOURCE = Path(__file__).parents[1] / 'admin_v2.py'


def functions(*names, **environment):
    tree = ast.parse(SOURCE.read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in selected:
        node.decorator_list = []
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), environment)
    return environment


class InventoryAdminTests(unittest.TestCase):
    def test_metadata_projection_preserves_manually_stopped_status(self):
        with sqlite3.connect(':memory:') as c:
            c.row_factory = sqlite3.Row
            c.executescript('''CREATE TABLE products(id TEXT,name TEXT,price INTEGER,stock INTEGER,soldout INTEGER);
              CREATE TABLE product_variants(id TEXT,legacy_product_id TEXT,group_id TEXT,option_name TEXT,
                sale_status TEXT,metadata TEXT,updated_at TEXT);
              CREATE TABLE product_groups(id TEXT,department TEXT,category TEXT,product_type TEXT,image TEXT,
                confidence INTEGER,review_state TEXT,metadata TEXT,updated_at TEXT);
              INSERT INTO products VALUES('sale','Renamed',100,10,1);
              INSERT INTO product_variants VALUES('variant','sale','group','Old','PAUSED','{}','');''')
            def one(sql, args=()):
                row = c.execute(sql, args).fetchone()
                return dict(row) if row else None
            parsed = dict(option_name='Renamed', metadata={}, reasons=[], department='KPOP',
                          category='album', product_type='ALBUM', confidence=100, review_state='READY')
            ns = functions('catalog_product_from_legacy', _state={'pname':'name','pprice':'price','pcols':set()},
                           one=one, run=c.execute, _catalog_parse_product=lambda row: parsed,
                           now_iso=lambda: '', num=lambda value: int(value or 0), json=json,
                           catalog_inventory_from_legacy=lambda pid: None)
            for status in ('PAUSED', 'HIDDEN'):
                c.execute('UPDATE product_variants SET sale_status=?', (status,))
                ns['catalog_product_from_legacy']('sale')
                row = one('SELECT option_name,sale_status FROM product_variants')
                self.assertEqual(row, {'option_name':'Renamed', 'sale_status':status})

    def test_metadata_inheritance_locks_children_and_parent_in_global_order(self):
        options = json.dumps([{'managed':1, 'product_id':'a-child', 'name':'STAR', 'hidden':1}])
        calls = []
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def commit(self): pass
            def execute(self, sql, args=()):
                calls.append((sql, args))
                row = {'id':args[0], 'options':options, 'name':'Album', 'img':'cover.jpg'} if args else None
                return types.SimpleNamespace(fetchone=lambda: row)
        ns = functions('_product_children_inherit', _state={'pcols':{'img','options'}, 'pname':'name'},
            _conn=Connection, IS_PG=True, _q=lambda value: value, _prod_opts_norm=json.loads)
        ns['_product_children_inherit']('z-parent')
        self.assertEqual([args[0] for sql, args in calls if 'FOR UPDATE' in sql], ['a-child', 'z-parent'])
        updates = [sql for sql, args in calls if sql.startswith('UPDATE')]
        self.assertEqual(len(updates), 1)
        self.assertNotIn('stock', updates[0])
        self.assertNotIn('price', updates[0])

    def test_legacy_stock_endpoint_rejects_before_any_write(self):
        class Rejected(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        writes = []
        module = types.ModuleType('unified_inventory')
        module.inventory_owner = lambda pid: None
        ns = functions('api_product_update', Request=object, Body=lambda value: None,
            HTTPException=Rejected, get_actor=lambda request: {}, need=lambda *args: None,
            one=lambda *args: {'stock':12, 'soldout':0}, num=lambda value: int(value or 0),
            _state={'pprice':'price'}, apply_pricing=lambda *args, **kwargs: writes.append('price'),
            run=lambda *args: writes.append('stock'), audit=lambda *args: None)
        with patch.dict(sys.modules, {'unified_inventory':module}), self.assertRaises(Rejected) as error:
            ns['api_product_update'](None, {'id':'parent', 'stock':9999, 'price':100})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(writes, [])

    def test_legacy_endpoint_rejects_physical_item_status_changes(self):
        class Rejected(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        writes = []
        module = types.ModuleType('unified_inventory')
        module.inventory_owner = lambda pid: 'parent'
        ns = functions('api_product_update', Request=object, Body=lambda value: None,
            HTTPException=Rejected, get_actor=lambda request: {}, need=lambda *args: None,
            one=lambda *args: {'stock':12, 'soldout':0}, _state={'pprice':None},
            run=lambda *args: writes.append(args), audit=lambda *args: None)
        with patch.dict(sys.modules, {'unified_inventory':module}), self.assertRaises(Rejected) as error:
            ns['api_product_update'](None, {'id':'physical', 'soldout':1})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(writes, [])

    def test_legacy_sync_preserves_omitted_physical_stock(self):
        writes = []
        ns = functions('_prod_opts_sync', re=re,
            _prod_opts_norm=lambda value: value,
            _state={'pcols': {'stock', 'name', 'price'}, 'pname':'name', 'pprice':'price'},
            one=lambda *args: {'id':'physical', 'name':'album'},
            run=lambda sql, args: writes.append((sql, args)),
            _prod_opt_attach_group=lambda *args: None, _bundle_resolve=lambda *args: None)
        physical = {'name':'STAR', 'product_id':'physical', 'hidden':1, 'managed':1, 'price':0, 'stock':3750}
        result = ns['_prod_opts_sync']('parent', [], [physical], {})
        self.assertEqual(result[0]['product_id'], 'physical')
        self.assertFalse(any('soldout=1' in sql for sql, _ in writes))

    def test_delete_guard_rejects_before_database_writes(self):
        writes = []
        class Blocked(Exception):
            pass
        module = types.ModuleType('unified_inventory')
        def protect(pid):
            self.assertEqual(pid, 'parent')
            raise Blocked()
        module.protect_product_delete = protect
        ns = functions('api_product_delete', Request=object, Body=lambda value: None,
            get_actor=lambda request: {}, need=lambda *args: None,
            _state={'pname':'name', 'pcols':{'related_ids'}}, one=lambda *args: {'name':'album'},
            run=lambda *args: writes.append(args), _related_unlink_deleted=lambda *args: writes.append(args))
        with patch.dict(sys.modules, {'unified_inventory':module}), self.assertRaises(Blocked):
            ns['api_product_delete'](None, {'id':'parent'})
        self.assertEqual(writes, [])

    def test_roles_distinguish_parent_random_set_and_physical(self):
        ns = functions('_catalog_inventory_roles', jload=lambda value, default: json.loads(value) if value else default)
        rows = [
            {'legacy_product_id': 'parent', 'options': json.dumps([
                {'product_id': 'star', 'hidden': 1}, {'product_id': 'race', 'hidden': 1},
                {'product_id': 'random'}, {'product_id': 'set'}])},
            {'legacy_product_id': 'star'}, {'legacy_product_id': 'race'},
            {'legacy_product_id': 'random', 'bundle': json.dumps([{'mode': 'any', 'pid': 'star', 'qty': 1}])},
            {'legacy_product_id': 'set', 'bundle': json.dumps([{'pid': 'star', 'qty': 1}])},
        ]
        result = ns['_catalog_inventory_roles'](rows)
        self.assertEqual([r['inventory_role'] for r in result], ['parent', 'physical', 'physical', 'random', 'set'])
        self.assertEqual(result[1]['inventory_parent_id'], 'parent')
        self.assertNotIn('inventory_role', rows[0])

    def test_delete_protection_precedes_any_mutation(self):
        tree = ast.parse(SOURCE.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api_product_delete')
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
        guard = next((n for n in calls if isinstance(n.func, ast.Name) and n.func.id == 'protect_product_delete'), None)
        self.assertIsNotNone(guard)
        writes = [n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id in {'run', '_related_unlink_deleted', '_catalog_delete_legacy'}]
        self.assertLess(guard.lineno, min(writes))

    def test_edit_form_does_not_submit_legacy_options(self):
        text = SOURCE.read_text()
        start = text.index("await api('/admin/api/products/detail/update'")
        payload = text[start:text.index("toast('저장되었습니다')", start)]
        self.assertNotIn('options:poCollect()', payload)
        self.assertNotIn('stock_baseline:', payload)


if __name__ == '__main__':
    unittest.main()
