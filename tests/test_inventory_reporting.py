import copy
import json

from inventory_reporting import build_sales_aliases, report_identity


def product(pid, name, price=11100, options=None, bundle=None):
    return {'id': pid, 'name': name, 'price': price,
            'options': json.dumps(options or []), 'bundle': json.dumps(bundle or [])}


def test_stayg_parent_and_option_combine_without_changing_orders():
    products = [product('parent', 'STAYG', options=[{'product_id': 'child', 'price': 11100}]),
                product('child', 'STAYG — STAYG ver.')]
    aliases = build_sales_aliases(products)
    orders = [{'id': 'parent', 'n': 'STAYG', 'q': 27, 'p': 11100},
              {'id': 'child', 'n': 'STAYG — STAYG ver.', 'q': 938, 'p': 11100}]
    before = copy.deepcopy(orders)
    grouped = {}
    for item in orders:
        identity = report_identity(item, aliases)
        qty, revenue = grouped.get(identity, (0, 0))
        grouped[identity] = (qty + item['q'], revenue + item['q'] * item['p'])
    assert grouped == {('child', 'STAYG — STAYG ver.'): (965, 10711500)}
    assert orders == before


def test_random_parent_combines_only_with_equivalent_random_option():
    random = [{'pid': 'star', 'qty': 1, 'mode': 'any'}, {'pid': 'race', 'qty': 1}]
    parent_parts = [{**p, 'mode': 'any'} for p in random]
    set_parts = [{'pid': 'star', 'qty': 1}, {'pid': 'race', 'qty': 1}]
    products = [product('parent', 'Album', 25200,
                        [{'product_id': 'random', 'price': 25200}, {'product_id': 'set', 'price': 50400}], parent_parts),
                product('random', 'Album — random', 25200, bundle=random),
                product('set', 'Album — set', 50400, bundle=set_parts)]
    aliases = build_sales_aliases(products)
    assert report_identity({'id': 'parent', 'n': 'old'}, aliases) == ('random', 'Album — random')
    assert report_identity({'id': 'random', 'n': 'old random'}, aliases) == ('random', 'Album — random')
    assert report_identity({'id': 'set', 'n': 'Album — set'}, aliases) == ('set', 'Album — set')


def test_ambiguous_missing_or_different_compositions_are_not_merged():
    for children in [[], [product('child', 'same', 22200)],
                     [product('child', 'same', bundle=[{'pid': 'x', 'qty': 1}])],
                     [product('child', 'same'), product('other', 'same')]]:
        options = [{'product_id': p['id'], 'price': p['price']} for p in children]
        aliases = build_sales_aliases([product('parent', 'same', options=options)] + children)
        assert report_identity({'id': 'parent', 'n': 'same'}, aliases) == ('parent', 'same')


def test_hidden_stock_is_not_a_sale_alias_and_parts_are_untouched():
    products = [product('parent', 'Album', options=[{'product_id': 'stock', 'price': 11100, 'hidden': 1}]),
                product('stock', 'Stock')]
    aliases = build_sales_aliases(products)
    item = {'id': 'parent', 'n': 'Album', 'parts': [{'id': 'stock', 'q': 2}]}
    before = copy.deepcopy(item)
    assert report_identity(item, aliases) == ('parent', 'Album')
    assert item == before


def test_missing_ids_use_snapshot_name():
    assert report_identity({'name': '  Old   item  '}, {}) == ('', 'Old item')
