"""One product editor, one stock authority, and stable checkout/order identities.

Physical items retain their legacy product IDs so in-flight carts and historical
order parts remain valid. products.stock is authoritative; balances are a view.
"""
import json
import secrets
from contextlib import contextmanager

from fastapi import APIRouter, Body, HTTPException, Request


inventory_router = APIRouter()


def _admin():
    import admin_v2
    return admin_v2


def _json(value, default):
    if value in (None, ''):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        raise HTTPException(409, '저장된 상품 구성을 확인해야 합니다')


def _execute(c, sql, args=()):
    return c.execute(_admin()._q(sql), args)


def _one(c, sql, args=()):
    result = _execute(c, sql, args).fetchone()
    return dict(result) if result else None


def _rows(c, sql, args=()):
    return [dict(r) for r in _execute(c, sql, args).fetchall()]


@contextmanager
def _transaction(write=False):
    c = _admin()._conn()
    try:
        if write:
            if _admin().IS_PG:
                c.execute("SET LOCAL lock_timeout = '1500ms'")
                c.execute("SET LOCAL statement_timeout = '5000ms'")
            else:
                c.execute('BEGIN IMMEDIATE')
        yield c
        if write:
            c.commit()
    except Exception as error:
        c.rollback()
        if getattr(error, 'sqlstate', '') in ('55P03', '40P01', '40001', '57014'):
            raise HTTPException(409, '주문 처리 중입니다. 잠시 후 다시 저장하세요') from error
        raise
    finally:
        c.close()


def ensure_schema():
    """Add metadata only. Never migrate or reset stock during deployment."""
    with _transaction(True) as c:
        _execute(c, '''CREATE TABLE IF NOT EXISTS product_inventory_items(
            product_id TEXT PRIMARY KEY, parent_product_id TEXT NOT NULL,
            label TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')
        _execute(c, '''CREATE INDEX IF NOT EXISTS idx_product_inventory_owner
            ON product_inventory_items(parent_product_id)''')


def _options(parent):
    opts = _json(parent.get('options'), [])
    if not isinstance(opts, list) or any(not isinstance(o, dict) for o in opts):
        raise HTTPException(409, '저장된 판매 옵션 형식을 확인해야 합니다')
    return opts


def _owned_items(c, parent):
    legacy = {o['product_id']: o.get('name') or o['product_id']
              for o in _options(parent) if o.get('hidden') and o.get('product_id')}
    mapped = _rows(c, 'SELECT product_id,label FROM product_inventory_items WHERE parent_product_id=?',
                   (parent['id'],))
    return {**legacy, **{r['product_id']: r['label'] for r in mapped}}


def _inventory_owner(c, pid):
    owner = _one(c, 'SELECT parent_product_id FROM product_inventory_items WHERE product_id=?', (pid,))
    if owner:
        return owner['parent_product_id']
    for parent in _rows(c, 'SELECT id,options FROM products WHERE options LIKE ?', ('%' + str(pid) + '%',)):
        if any(o.get('hidden') and o.get('product_id') == pid for o in _options(parent)):
            return parent['id']
    return None


def inventory_owner(pid):
    """Recognize both adopted items and legacy hidden items, without writes."""
    with _transaction() as c:
        return _inventory_owner(c, pid)


def protect_product_delete(pid):
    if inventory_owner(pid):
        raise HTTPException(409, '실물재고는 대표상품의 재고 관리에서 관리합니다. 주문 이력 보존을 위해 삭제할 수 없습니다')
    with _transaction() as c:
        linked_parents = _rows(c, 'SELECT id,options FROM products WHERE options LIKE ?', ('%' + str(pid) + '%',))
        if any(any(o.get('product_id') == pid for o in _options(p)) for p in linked_parents):
            raise HTTPException(409, '연결된 판매 옵션은 삭제할 수 없습니다. 대표상품에서 해당 옵션의 판매를 종료하세요')
        parent = _one(c, 'SELECT id,options FROM products WHERE id=?', (pid,))
        if parent and (_owned_items(c, parent) or any(o.get('product_id') for o in _options(parent))):
            raise HTTPException(409, '판매 옵션과 재고가 연결된 대표상품은 삭제할 수 없습니다. 판매중지를 사용하세요')
        referenced = _rows(c, 'SELECT id,bundle FROM products WHERE bundle LIKE ?', ('%' + str(pid) + '%',))
        if any(any(p.get('pid') == pid for p in _json(r['bundle'], [])) for r in referenced):
            raise HTTPException(409, '다른 판매 옵션이 사용하는 재고는 삭제할 수 없습니다')


def _stock_fields(row):
    return {'baseline': row.get('stock_baseline'), 'expected_baseline': row.get('stock_baseline'),
            'remaining': row.get('stock')}


def _config(c, pid):
    parent = _one(c, 'SELECT * FROM products WHERE id=?', (pid,))
    if not parent:
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    items = []
    for item_id, label in _owned_items(c, parent).items():
        row = _one(c, 'SELECT * FROM products WHERE id=?', (item_id,))
        if not row:
            raise HTTPException(409, '연결된 실물재고를 찾을 수 없습니다')
        items.append({'product_id': item_id, 'name': label, **_stock_fields(row)})
    options = []
    for opt in _options(parent):
        if opt.get('hidden'):
            continue
        row = _one(c, 'SELECT * FROM products WHERE id=?', (opt.get('product_id') or '',))
        parts = _json((row or {}).get('bundle'), [])
        options.append({'product_id': opt.get('product_id') or '', 'name': opt.get('name') or '',
                        'price': (row or {}).get('price', opt.get('price') or 0),
                        'mode': ('any' if parts[0].get('mode') == 'any' else 'all') if parts else 'standalone',
                        'components': [{'product_id': p['pid'], 'qty': p['qty']} for p in parts],
                        'items': opt.get('items') or [], **_stock_fields(row or {})})
    return {'id': pid, 'expected_options': parent.get('options') or '',
            'inventory_items': items, 'sale_options': options, 'parent_stock': _stock_fields(parent)}


def get_inventory_config(pid):
    with _transaction() as c:
        if _inventory_owner(c, pid):
            raise HTTPException(409, '실물재고의 대표상품에서 재고 구성을 관리하세요')
        return _config(c, pid)


def _integer(value, name, nullable=False, minimum=0, maximum=2147483647):
    if value is None and nullable:
        return None
    if type(value) is not int or value < minimum or value > maximum:
        raise HTTPException(400, '%s은(는) %d 이상의 정수여야 합니다' % (name, minimum))
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise HTTPException(400, '이름은 1~80자로 입력하세요')
    return value.strip()


def _baseline(row, item):
    if 'baseline' not in item or 'expected_baseline' not in item:
        raise HTTPException(400, '입고 기준과 저장 전 기준이 필요합니다')
    baseline = _integer(item['baseline'], '기본 입고수량', nullable=True)
    expected = _integer(item['expected_baseline'], '이전 입고수량', nullable=True)
    if row.get('stock_baseline') != expected:
        raise HTTPException(409, '입고 기준이 변경되었습니다. 새로고침 후 다시 저장하세요')
    if baseline == expected:
        return row.get('stock'), baseline
    if baseline is None or expected is None or row.get('stock') is None:
        raise HTTPException(409, '유한 재고와 무제한 전환은 판매 정산이 필요합니다')
    remaining = row['stock'] + baseline - expected
    if remaining < 0:
        raise HTTPException(400, '판매·예약된 수량보다 입고 기준을 줄일 수 없습니다')
    return remaining, baseline


def _lock_rows(c, ids):
    result = {}
    suffix = ' FOR UPDATE' if _admin().IS_PG else ''
    for pid in sorted(ids):
        row = _one(c, 'SELECT * FROM products WHERE id=?' + suffix, (pid,))
        if not row:
            raise HTTPException(409, '상품 구성이 변경되었습니다. 새로고침하세요')
        result[pid] = row
    return result


def lock_order_inventory(c, items, candidate_ids):
    """Lock all cart lines and physical parts together, in one global order.

    A configuration changed between the read and lock is rejected before any
    deduction. Existing carts otherwise keep their original product IDs.
    """
    snapshots, targets = {}, set()
    for item in items:
        row = None
        for candidate in candidate_ids(str(item.get('id', ''))):
            row = c.one('SELECT id,bundle FROM products WHERE id=?', (candidate,))
            if row:
                break
        if not row:
            raise HTTPException(400, '상품을 찾을 수 없습니다')
        parts = _json(row.get('bundle'), [])
        if not isinstance(parts, list) or any(not isinstance(p, dict) or not isinstance(p.get('pid'), str) for p in parts):
            raise HTTPException(409, '상품의 재고 구성을 확인해야 합니다')
        snapshots[row['id']] = row.get('bundle') or ''
        targets.add(row['id'])
        targets.update(p['pid'] for p in parts)
    suffix = ' FOR UPDATE' if _admin().IS_PG else ''
    locked = {}
    for pid in sorted(targets):
        row = c.one('SELECT id,bundle FROM products WHERE id=?' + suffix, (pid,))
        if not row:
            raise HTTPException(409, '상품 구성이 변경되었습니다. 다시 확인해 주세요')
        locked[pid] = row
    if any((locked[pid].get('bundle') or '') != bundle for pid, bundle in snapshots.items()):
        raise HTTPException(409, '상품 구성이 변경되었습니다. 다시 확인해 주세요')


def _validate_lists(body):
    for key in ('inventory_items', 'sale_options'):
        value = body.get(key)
        if not isinstance(value, list) or len(value) > 20 or any(not isinstance(i, dict) for i in value):
            raise HTTPException(400, '재고와 판매 옵션은 각각 최대 20개입니다')
    if len(body['inventory_items']) + len(body['sale_options']) > 20:
        raise HTTPException(400, '기존 주문 호환을 위해 재고와 판매 옵션 합계는 최대 20개입니다')


def _prepare_items(c, body, parent, locked):
    owned = _owned_items(c, parent)
    result, keys, names = [], set(), set()
    for item in body['inventory_items']:
        pid = item.get('product_id') or ''
        key = pid or item.get('client_key')
        name = _name(item.get('name'))
        if not isinstance(key, str) or not key or key in keys or name in names:
            raise HTTPException(400, '재고 이름과 식별자는 중복될 수 없습니다')
        keys.add(key); names.add(name)
        if pid:
            if pid not in owned:
                raise HTTPException(400, '다른 상품의 재고를 연결할 수 없습니다')
            stock, baseline = _baseline(locked[pid], item)
            mapped = _one(c, 'SELECT parent_product_id FROM product_inventory_items WHERE product_id=?', (pid,))
            if mapped and mapped['parent_product_id'] != parent['id']:
                raise HTTPException(409, '재고가 다른 대표상품에 연결되어 있습니다')
        else:
            baseline = _integer(item.get('baseline'), '기본 입고수량', nullable=True)
            stock, pid = baseline, 'mpo::' + secrets.token_hex(6)
        result.append({'key': key, 'product_id': pid, 'name': name, 'stock': stock,
                       'baseline': baseline, 'new': pid not in locked})
    if not set(owned).issubset(keys):
        raise HTTPException(409, '기존 실물재고는 주문 취소 복원을 위해 삭제할 수 없습니다')
    return result


def _prepare_sales(body, parent, locked, items):
    previous = {o.get('product_id'): o for o in _options(parent) if not o.get('hidden') and o.get('product_id')}
    if any(not option.get('managed') for option in previous.values()):
        raise HTTPException(409, '외부 상품에 수동 연결된 옵션은 통합 편집으로 변경하거나 종료할 수 없습니다')
    by_key = {i['key']: i for i in items}
    names = {i['name'] for i in items}
    result, seen = [], set()
    for sale in body['sale_options']:
        name, pid = _name(sale.get('name')), sale.get('product_id') or ''
        if name in names or (pid and pid in seen):
            raise HTTPException(400, '옵션 이름과 식별자는 중복될 수 없습니다')
        names.add(name); seen.add(pid)
        price = _integer(sale.get('price'), '판매가격')
        if pid and pid not in previous:
            raise HTTPException(400, '다른 상품의 판매 옵션을 변경할 수 없습니다')
        if pid and not previous[pid].get('managed'):
            raise HTTPException(409, '외부 상품을 수동 연결한 옵션은 원본 상품에서 관리해야 합니다')
        mode = sale.get('mode')
        if mode not in ('any', 'all', 'standalone'):
            raise HTTPException(400, '재고 차감 방식을 선택하세요')
        components = sale.get('components', [])
        if not isinstance(components, list) or len(components) > 12:
            raise HTTPException(400, '구성품은 최대 12개입니다')
        if bool(components) != (mode != 'standalone'):
            raise HTTPException(400, '판매 옵션의 재고 구성이 필요합니다')
        parts, refs = [], set()
        for part in components:
            if not isinstance(part, dict) or part.get('product_id') not in by_key:
                raise HTTPException(400, '이 상품의 실물재고만 구성품으로 연결할 수 있습니다')
            item = by_key[part['product_id']]
            qty = _integer(part.get('qty'), '구성 수량', minimum=1, maximum=99)
            if item['product_id'] in refs:
                raise HTTPException(400, '구성 재고가 중복되었습니다')
            refs.add(item['product_id'])
            parts.append({'pid': item['product_id'], 'qty': qty, 'name': item['name']})
        if mode == 'any' and len({p['qty'] for p in parts}) > 1:
            raise HTTPException(400, '랜덤 구성품의 차감 수량은 같아야 합니다')
        old = locked.get(pid)
        if old and bool(_json(old.get('bundle'), [])) != bool(parts):
            raise HTTPException(409, '판매 중인 옵션의 재고 방식 변경은 정산이 필요합니다. 새 옵션을 추가하세요')
        stock, baseline = (None, None) if parts else (_baseline(old, sale) if old else
                            (_integer(sale.get('baseline'), '입고수량', nullable=True), sale.get('baseline')))
        if parts and old and old.get('stock') is not None:
            raise HTTPException(409, '구성형 옵션의 별도 재고를 먼저 정산해야 합니다')
        details = sale.get('items', [])
        if not isinstance(details, list) or len(details) > 12 or any(
                not isinstance(d, dict) or d.get('k') not in ('album', 'card', 'ticket', 'gift', 'etc')
                or not isinstance(d.get('t'), str) or len(d['t']) > 120 for d in details):
            raise HTTPException(400, '옵션 구성 설명 형식이 올바르지 않습니다')
        result.append({'product_id': pid or 'mpo::' + secrets.token_hex(6), 'name': name,
                       'price': price, 'mode': mode, 'parts': parts, 'items': details,
                       'stock': stock, 'baseline': baseline, 'new': not bool(pid)})
    return result, set(previous) - {s['product_id'] for s in result}


def _write_stock(c, row, stock, baseline):
    if row.get('stock') == stock and row.get('stock_baseline') == baseline:
        return
    variant = _one(c, 'SELECT sale_status FROM product_variants WHERE legacy_product_id=?', (row['id'],))
    blocked = variant and variant.get('sale_status') not in ('ACTIVE', 'SOLD_OUT', None)
    _execute(c, 'UPDATE products SET stock=?,stock_baseline=?,soldout=? WHERE id=?',
             (stock, baseline, int(bool(blocked) or (stock is not None and stock <= 0)), row['id']))


def _create_product(c, item, parent, price):
    values = {'id': item['product_id'], 'name': parent.get('name', '') + ' — ' + item['name'],
              'price': price, 'stock': item['stock'], 'stock_baseline': item['baseline'],
              'soldout': int(item['stock'] is not None and item['stock'] <= 0),
              'options': '[]', 'bundle': '', 'kind': parent.get('kind') or 'album',
              'created_at': _admin().now_iso()}
    for key in ('img', 'category', 'show_from', 'show_until'):
        values[key] = parent.get(key)
    _execute(c, 'INSERT INTO products(%s) VALUES(%s)' % (','.join(values), ','.join('?' for _ in values)), tuple(values.values()))


def _projection(c, pid, label=None, parent_id=None):
    product = _one(c, 'SELECT stock,soldout,bundle FROM products WHERE id=?', (pid,))
    variant = _one(c, 'SELECT * FROM product_variants WHERE legacy_product_id=?', (pid,))
    stamp = _admin().now_iso()
    mode = 'BUNDLE' if _json(product.get('bundle'), []) else ('EXTERNAL' if product['stock'] is None else 'TRACKED')
    if not variant and parent_id:
        parent_variant = _one(c, 'SELECT * FROM product_variants WHERE legacy_product_id=?', (parent_id,))
        if parent_variant:
            vid = 'var_' + secrets.token_hex(8)
            _execute(c, '''INSERT INTO product_variants(id,legacy_product_id,group_id,sku,option_name,
                source,sale_status,stock_mode,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                     (vid, pid, parent_variant['group_id'], 'INV-' + secrets.token_hex(8), label,
                      'DIRECT', 'ACTIVE', mode, '{}', stamp, stamp))
            _execute(c, 'INSERT INTO product_identifiers(id,variant_id,kind,value) VALUES(?,?,?,?)',
                     ('idn_' + secrets.token_hex(8), vid, 'LEGACY_ID', pid))
            _execute(c, '''INSERT INTO inventory_balances(variant_id,location_id,is_tracked,on_hand,reserved,
                incoming,reorder_point,external_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                     (vid, 'SEOUL', int(mode == 'TRACKED'), product['stock'] or 0, 0, 0, 5, 'AVAILABLE', stamp))
            variant = {'id': vid}
    if not variant:
        return
    status = variant.get('sale_status')
    if status in ('ACTIVE', 'SOLD_OUT', None):
        status = 'SOLD_OUT' if product['soldout'] else 'ACTIVE'
    _execute(c, 'UPDATE product_variants SET stock_mode=?,sale_status=?,updated_at=? WHERE id=?',
             (mode, status, stamp, variant['id']))
    if label is not None:
        _execute(c, 'UPDATE product_variants SET option_name=? WHERE id=?', (label, variant['id']))
    _execute(c, '''UPDATE inventory_balances SET on_hand=?,reserved=0,is_tracked=?,external_status=?,updated_at=?
        WHERE variant_id=?''', (product['stock'] or 0, int(mode == 'TRACKED'),
                               'OUT' if product['soldout'] else 'AVAILABLE', stamp, variant['id']))


def _receipt_movement(c, item, locked, actor):
    before = (locked.get(item['product_id']) or {}).get('stock', 0)
    after = item['stock']
    if before is None or after is None or before == after:
        return
    variant = _one(c, 'SELECT id FROM product_variants WHERE legacy_product_id=?', (item['product_id'],))
    if variant:
        _execute(c, '''INSERT INTO inventory_movements(id,variant_id,kind,quantity,before_qty,after_qty,
            reason,by_admin,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                 (secrets.token_hex(12), variant['id'], 'RECEIPT', after-before, before, after,
                  '상품 편집에서 기본 입고수량 변경', actor.get('name') or '', _admin().now_iso()))


def _write_items(c, items, parent, locked, actor):
    options = []
    for item in items:
        pid = item['product_id']
        if item['new']:
            _create_product(c, item, parent, 0)
        else:
            _write_stock(c, locked[pid], item['stock'], item['baseline'])
        _execute(c, '''INSERT INTO product_inventory_items(product_id,parent_product_id,label,active)
            VALUES(?,?,?,1) ON CONFLICT(product_id) DO UPDATE SET label=excluded.label''',
                 (pid, parent['id'], item['name']))
        options.append({'name': item['name'], 'price': 0, 'stock': item['baseline'], 'product_id': pid,
                        'managed': 1, 'hidden': 1, 'bundle': [], 'bundle_mode': 'all', 'items': []})
        _projection(c, pid, item['name'], parent['id'])
        _receipt_movement(c, item, locked, actor)
    return options


def _write_sales(c, sales, parent, locked, actor):
    options = []
    for sale in sales:
        pid = sale['product_id']
        if sale['new']:
            _create_product(c, sale, parent, sale['price'])
        else:
            _write_stock(c, locked[pid], sale['stock'], sale['baseline'])
        parts = [{'pid': p['pid'], 'qty': p['qty']} for p in sale['parts']]
        if parts and sale['mode'] == 'any':
            parts[0] = {**parts[0], 'mode': 'any'}
        _execute(c, 'UPDATE products SET name=?,price=?,bundle=? WHERE id=?',
                 ((parent.get('name') or '') + ' — ' + sale['name'], sale['price'], json.dumps(parts), pid))
        options.append({'name': sale['name'], 'price': sale['price'], 'stock': sale['baseline'],
                        'product_id': pid, 'managed': 1, 'hidden': 0, 'items': sale['items'],
                        'bundle': [{'name': p['name'], 'qty': p['qty']} for p in sale['parts']],
                        'bundle_mode': 'any' if sale['mode'] == 'any' else 'all'})
        _projection(c, pid, sale['name'], parent['id'])
        _receipt_movement(c, sale, locked, actor)
    return options


def _audit(c, actor, target, detail):
    _execute(c, 'INSERT INTO audit_log(id,created,actor,role,action,target,detail) VALUES(?,?,?,?,?,?,?)',
             (secrets.token_hex(12), _admin().now_iso(), actor.get('name') or '', actor.get('role') or '',
              '통합재고관리', target, detail))


def save_inventory_config(body, actor):
    _validate_lists(body)
    pid = body.get('id')
    if not isinstance(pid, str) or not pid:
        raise HTTPException(400, '대표상품 ID가 필요합니다')
    with _transaction(True) as c:
        if _inventory_owner(c, pid):
            raise HTTPException(409, '실물재고의 대표상품에서 재고 구성을 관리하세요')
        parent = _one(c, 'SELECT * FROM products WHERE id=?', (pid,))
        if not parent:
            raise HTTPException(404, '상품을 찾을 수 없습니다')
        owned = _owned_items(c, parent)
        child_ids = set(owned) | {o['product_id'] for o in _options(parent) if o.get('product_id')}
        if pid in child_ids:
            raise HTTPException(409, '대표상품 자신을 재고로 연결할 수 없습니다')
        locked = _lock_rows(c, child_ids | {pid})
        parent = locked[pid]
        if not isinstance(body.get('expected_options'), str) or (parent.get('options') or '') != body['expected_options']:
            raise HTTPException(409, '판매 옵션이 변경되었습니다. 새로고침 후 다시 저장하세요')
        items = _prepare_items(c, body, parent, locked)
        sales, retired = _prepare_sales(body, parent, locked, items)
        parent_stock, parent_baseline = _baseline(parent, body.get('parent_stock', {}))
        parent_parts = _json(parent.get('bundle'), [])
        if parent_parts and any(p.get('pid') not in owned for p in parent_parts):
            raise HTTPException(409, '대표상품의 재고 연결을 확인해야 합니다')
        # Validation is complete before the first mutation. Rows stay locked until commit.
        options = _write_items(c, items, parent, locked, actor) + _write_sales(c, sales, parent, locked, actor)
        _write_stock(c, parent, parent_stock, parent_baseline)
        _receipt_movement(c, {'product_id': pid, 'stock': parent_stock}, locked, actor)
        for old_id in sorted(retired):
            _execute(c, 'UPDATE products SET soldout=1 WHERE id=?', (old_id,))
            _execute(c, "UPDATE product_variants SET sale_status='PAUSED' WHERE legacy_product_id=?", (old_id,))
            _projection(c, old_id)
        if parent_parts and parent_parts[0].get('mode') == 'any' and {p['pid'] for p in parent_parts} == set(owned):
            parent_parts = [{'pid': i['product_id'], 'qty': parent_parts[0]['qty']} for i in items]
            parent_parts[0] = {**parent_parts[0], 'mode': 'any'}
            _execute(c, 'UPDATE products SET bundle=? WHERE id=?', (json.dumps(parent_parts), pid))
        _execute(c, 'UPDATE products SET options=? WHERE id=?', (json.dumps(options, ensure_ascii=False), pid))
        _projection(c, pid)
        _audit(c, actor, pid, '실물재고 %d종 · 판매 옵션 %d개; 기존 주문 ID 유지' % (len(items), len(sales)))
        result = _config(c, pid)
    return {'ok': True, **result}


def adjust_inventory(variant_id, body, actor):
    with _transaction(True) as c:
        variant = _one(c, 'SELECT * FROM product_variants WHERE id=?', (variant_id,))
        if not variant:
            raise HTTPException(404, 'SKU를 찾을 수 없습니다')
        row = _lock_rows(c, [variant['legacy_product_id']])[variant['legacy_product_id']]
        variant = _one(c, 'SELECT * FROM product_variants WHERE id=?', (variant_id,))
        if _json(row.get('bundle'), []):
            raise HTTPException(409, '구성형 옵션은 대표상품의 실물재고에서 조정하세요')
        if row['stock'] is None:
            status = body.get('external_status')
            if status not in ('AVAILABLE', 'OUT', 'UNKNOWN'):
                raise HTTPException(400, '외부재고 상태를 선택하세요')
            _execute(c, 'UPDATE products SET soldout=? WHERE id=?',
                     (int(status == 'OUT' or variant.get('sale_status') in ('PAUSED', 'HIDDEN')), row['id']))
            _projection(c, row['id'])
            _audit(c, actor, row['id'], '외부재고 상태 ' + status)
            return {'ok': True, 'external_status': status}
        return _adjust_count(c, row, variant, body, actor)


def _adjust_count(c, row, variant, body, actor):
    kind = str(body.get('kind') or 'MANUAL').upper()
    quantity = _integer(body.get('quantity'), '조정수량', minimum=-2147483647)
    before = row['stock']
    if kind == 'COUNT':
        if type(body.get('expected_stock')) is not int or body['expected_stock'] != before:
            raise HTTPException(409, '판매 중 재고가 바뀌었습니다. 새로고침 후 다시 실사 수량을 입력하세요')
        after = quantity
    else:
        after = before + quantity
    if after < 0:
        raise HTTPException(400, '재고는 0보다 작을 수 없습니다')
    _integer(after, '조정 후 재고')
    baseline = row.get('stock_baseline')
    baseline = baseline + after - before if baseline is not None else None
    if baseline is not None and baseline < 0:
        raise HTTPException(400, '입고 기준은 0보다 작을 수 없습니다')
    _integer(baseline, '기본 입고수량', nullable=True)
    for field in ('incoming', 'reorder_point'):
        if body.get(field) is not None:
            _integer(body[field], field)
    _write_stock(c, row, after, baseline)
    if variant.get('sale_status') in ('PAUSED', 'HIDDEN'):
        _execute(c, 'UPDATE products SET soldout=1 WHERE id=?', (row['id'],))
    _projection(c, row['id'])
    for field in ('incoming', 'reorder_point'):
        if body.get(field) is not None:
            _execute(c, 'UPDATE inventory_balances SET %s=? WHERE variant_id=?' % field, (body[field], variant['id']))
    _execute(c, '''INSERT INTO inventory_movements(id,variant_id,kind,quantity,before_qty,after_qty,reason,by_admin,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (secrets.token_hex(12), variant['id'], kind, after-before, before, after,
                                     str(body.get('reason') or '')[:300], actor.get('name') or '', _admin().now_iso()))
    _audit(c, actor, row['id'], '%d→%d · %s' % (before, after, kind))
    return {'ok': True, 'on_hand': after, 'available': after}


@inventory_router.get('/admin/api/products/inventory')
def api_inventory_config(request: Request):
    admin = _admin()
    admin.need(admin.get_actor(request), 1, '상품 재고 조회')
    return get_inventory_config(request.query_params.get('id') or '')


@inventory_router.post('/admin/api/products/inventory/save')
def api_inventory_save(request: Request, body: dict = Body(...)):
    admin = _admin()
    actor = admin.get_actor(request)
    admin.need(actor, 2, '상품 재고 구성 변경')
    return save_inventory_config(body, actor)
