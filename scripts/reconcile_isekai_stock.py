"""One-time, atomic 2026-10-19 ISEKAI receipts reconciliation.

Run with DATABASE_URL set: python scripts/reconcile_isekai_stock.py [--apply]
Dry-run is the default. No customer or order identifiers are printed. Requires
the stock_baseline migration; run only after deploying the cancellation fix.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
import os
from zoneinfo import ZoneInfo


DAY = date(2026, 10, 19)
KST = ZoneInfo('Asia/Seoul')
BASELINE = 3750
MARKER = 'reconcile_isekai_2026_10_19_v1'
STAR_PARENT = 'mp::347ed2ae4bab'
LOVE_PARENT = 'mp::17ec0243d8c6'
STAYG_PARENT = 'mp::673b668afe93'
STAYG_CHILD = 'mpo::6db4a964b354'
PARENT_POOLS = {
    STAR_PARENT: ('mpo::e892215e0b62', 'mpo::f75ea0ad89e2'),
    LOVE_PARENT: ('mpo::0e0c13225772', 'mpo::72c65d3afe97',
                  'mpo::86d3b8569bd5', 'mpo::8d60e2307b44',
                  'mpo::5d71f0180a0e', 'mpo::7c60f7ec9a40'),
}
PHYSICAL_IDS = tuple(pid for pool in PARENT_POOLS.values() for pid in pool)
PARENTS = tuple(PARENT_POOLS) + (STAYG_PARENT,)
REQUIRED_IDS = PHYSICAL_IDS + PARENTS + (STAYG_CHILD,)
ACTIVE_BUNDLES = ('mpo::5713d174c2d5', 'mpo::50b211f9ac45',
                  'mpo::dd6c00034e48', 'mpo::58921e8a618e')
WRITE_IDS = REQUIRED_IDS + ACTIVE_BUNDLES
CONSUMING_STATUSES = {'PAID', 'PENDING', 'FAILED', 'WAITING_DEPOSIT'}


def quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Invalid target order quantity; no writes applied')
    return value


def on_target_day(created):
    stamp = datetime.fromisoformat(created)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=KST)
    return stamp.astimezone(KST).date() == DAY


def calculate(products, orders):
    """Derive physical consumption, preserving pre-existing component snapshots."""
    if not set(REQUIRED_IDS).issubset(products):
        raise ValueError('Required target products are missing')
    consumed = Counter({pid: 0 for pid in PHYSICAL_IDS})
    status_units = Counter()
    unresolved = []
    updates = {}
    for order in orders:
        lines = json.loads(order['items'])
        if not isinstance(lines, list):
            raise ValueError('Invalid order items JSON')
        for index, line in enumerate(lines):
            parts = line.get('parts') or []
            relevant = line.get('id') in products or any(p.get('id') in products for p in parts)
            if not relevant or order['status'] == 'CANCELLED':
                continue
            if not on_target_day(order['created']):
                raise ValueError('Order date is outside the approved receipt day')
            if order['status'] not in CONSUMING_STATUSES:
                raise ValueError('Unrecognized target order status; manual review required')
            if parts:
                if line.get('id') in products and any(
                        p.get('id') not in PHYSICAL_IDS + (STAYG_CHILD,) for p in parts):
                    raise ValueError('Target order contains an unknown component snapshot')
                for part in parts:
                    if part.get('id') in PHYSICAL_IDS:
                        amount = quantity(part.get('q'))
                        consumed[part['id']] += amount
                        status_units[order['status']] += amount
            elif line.get('id') in PHYSICAL_IDS:
                amount = quantity(line.get('q'))
                consumed[line['id']] += amount
                status_units[order['status']] += amount
            elif line.get('id') in PARENT_POOLS:
                unresolved.append((order, lines, index, quantity(line.get('q'))))
            elif line.get('id') not in (STAYG_PARENT, STAYG_CHILD):
                raise ValueError('Target alias order lacks component snapshot; manual review required')
    remaining = {pid: BASELINE - consumed[pid] for pid in PHYSICAL_IDS}
    if min(remaining.values()) < 0:
        raise ValueError('Physical consumption exceeds receipt; manual review required')
    for order, lines, index, amount in unresolved:
        line = lines[index]
        allocation = Counter()
        pool = PARENT_POOLS[line['id']]
        for _ in range(amount):
            pid = max(pool, key=lambda p: (remaining[p], p))
            if remaining[pid] <= 0:
                raise ValueError('Parent orders exceed physical receipt capacity')
            remaining[pid] -= 1
            allocation[pid] += 1
        patched = updates.setdefault(order['order_id'], list(lines))
        patched[index] = {**line, 'parts': [
            {'id': pid, 'n': products[pid]['name'], 'q': count}
            for pid, count in sorted(allocation.items())]}
        status_units[order['status']] += amount
    return {'remaining': remaining, 'order_updates': updates,
            'consumed_by_status': dict(sorted(status_units.items()))}


def parent_bundle(pid):
    return [{'pid': child, 'qty': 1, 'mode': 'any'} for child in PARENT_POOLS[pid]]


def sync_options(raw):
    options = json.loads(raw or '[]')
    return [{**opt, 'stock': BASELINE if opt.get('product_id') in PHYSICAL_IDS else None,
             'stock_baseline': BASELINE if opt.get('product_id') in PHYSICAL_IDS else None}
            for opt in options]


def validate_products(products):
    if not set(WRITE_IDS).issubset(products):
        raise ValueError('Required target products are missing')
    for pid in ACTIVE_BUNDLES:
        product = products[pid]
        bundle = json.loads(product.get('bundle') or '[]')
        if not bundle or any(part.get('pid') not in PHYSICAL_IDS for part in bundle):
            raise ValueError('Unexpected target-group product without physical bundle mapping')
        for part in bundle:
            quantity(part.get('qty'))


def apply_products(conn, products, variants, plan, stamp):
    for pid in sorted(WRITE_IDS):
        product = products[pid]
        finite = pid in PHYSICAL_IDS
        remaining = plan['remaining'].get(pid)
        bundle = parent_bundle(pid) if pid in PARENT_POOLS else json.loads(product.get('bundle') or '[]')
        if finite or pid in (STAYG_PARENT, STAYG_CHILD):
            bundle = []
        mode = 'TRACKED' if finite else ('BUNDLE' if bundle else 'EXTERNAL')
        soldout = int(finite and remaining <= 0)
        baseline = BASELINE if finite else None
        conn.execute('UPDATE products SET stock=%s,stock_baseline=%s,soldout=%s,bundle=%s,options=%s WHERE id=%s',
                     (remaining, baseline, soldout, json.dumps(bundle, ensure_ascii=False),
                      json.dumps(sync_options(product.get('options')), ensure_ascii=False), pid))
        vid = variants[pid]['id']
        conn.execute('UPDATE product_variants SET stock_mode=%s,sale_status=%s,updated_at=%s WHERE id=%s',
                     (mode, 'SOLD_OUT' if soldout else 'ACTIVE', stamp, vid))
        conn.execute('UPDATE inventory_balances SET is_tracked=%s,on_hand=%s,reserved=0,external_status=%s,updated_at=%s WHERE variant_id=%s',
                     (int(finite), remaining or 0, 'OUT' if soldout else 'AVAILABLE', stamp, vid))
        conn.execute('INSERT INTO inventory_movements(id,variant_id,kind,quantity,before_qty,after_qty,reason,by_admin,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     (MARKER + ':' + vid, vid, 'RECONCILE', (remaining or 0) - (product.get('stock') or 0),
                      product.get('stock'), remaining, '2026-10-19 opening receipt: 3750 per physical SKU; STAYG unlimited',
                      'approved-stock-reconciliation', stamp))
    for oid, lines in plan['order_updates'].items():
        conn.execute('UPDATE orders SET items=%s WHERE order_id=%s',
                     (json.dumps(lines, ensure_ascii=False), oid))


def reconcile(conn, apply=False):
    if apply and datetime.now(KST).date() != DAY:
        raise ValueError('Apply is allowed only on the approved receipt date 2026-10-19 KST')
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '750ms'")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        # NOWAIT fails rather than queuing behind live purchases. This short
        # transaction blocks new writes while deriving and applying one snapshot.
        conn.execute('LOCK TABLE products, orders IN SHARE ROW EXCLUSIVE MODE NOWAIT')
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (MARKER,))
        existing = conn.execute('SELECT value FROM site_settings WHERE key=%s', (MARKER,)).fetchone()
        if existing:
            return {'state': 'already_applied', 'receipt': json.loads(existing['value'])}
        products = {r['id']: r for r in conn.execute('''SELECT p.id,p.name,p.stock,p.stock_baseline,p.soldout,p.bundle,p.options
            FROM products p JOIN product_variants v ON v.legacy_product_id=p.id
            WHERE v.group_id IN (SELECT group_id FROM product_variants WHERE legacy_product_id=ANY(%s))
            ORDER BY p.id FOR UPDATE OF p''', (list(PARENTS),)).fetchall()}
        validate_products(products)
        variants = {r['legacy_product_id']: r for r in conn.execute('''SELECT v.id,v.legacy_product_id,b.reserved
            FROM product_variants v JOIN inventory_balances b ON b.variant_id=v.id
            WHERE v.legacy_product_id=ANY(%s) ORDER BY v.id FOR UPDATE OF v,b''', (list(products),)).fetchall()}
        if set(variants) != set(products) or any(r['reserved'] for r in variants.values()):
            raise ValueError('Missing inventory rows or nonzero reserved inventory; manual review required')
        orders = conn.execute('''SELECT order_id,created,status,items FROM orders
            WHERE created >= %s AND created < %s ORDER BY created,order_id FOR UPDATE''',
            (DAY.isoformat(), (DAY + timedelta(days=1)).isoformat())).fetchall()
        plan = calculate(products, orders)
        report = {'date': DAY.isoformat(), 'baseline_per_physical_sku': BASELINE,
                  'remaining': plan['remaining'], 'consumed_by_status': plan['consumed_by_status'],
                  'parent_orders_patched': len(plan['order_updates']), 'stayg': 'unlimited',
                  'product_rows': len(products)}
        if apply:
            stamp = datetime.now(KST).isoformat(timespec='seconds')
            apply_products(conn, products, variants, plan, stamp)
            conn.execute("UPDATE product_groups SET sale_status='ACTIVE',updated_at=%s WHERE id IN (SELECT group_id FROM product_variants WHERE legacy_product_id=ANY(%s))",
                         (stamp, list(PARENTS)))
            conn.execute('INSERT INTO site_settings(key,value,updated,by_admin) VALUES(%s,%s,%s,%s)',
                         (MARKER, json.dumps(report, ensure_ascii=False), stamp, 'approved-stock-reconciliation'))
        return {'state': 'applied' if apply else 'dry_run', **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    import psycopg
    from psycopg.rows import dict_row
    try:
        with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=5) as conn:
            report = reconcile(conn, args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as exc:
        # Driver exception strings may expose connection strings or order JSON.
        print(json.dumps({'state': 'failed_rolled_back', 'error_type': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, ValueError) else 'Database operation failed; no retry performed'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
