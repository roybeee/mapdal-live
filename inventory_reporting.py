"""Read-time sales identities; order snapshots and physical parts stay unchanged."""
import json
import re


def _list(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _composition(raw):
    try:
        parts = json.loads(raw) if isinstance(raw, str) and raw else raw or []
        if not isinstance(parts, list):
            return None
        if not parts:
            return ('all', ())
        if any(not isinstance(p, dict) or not p.get('pid') or
               type(p.get('qty')) is not int or p['qty'] <= 0 for p in parts):
            return None
        mode = 'any' if parts[0].get('mode') == 'any' else 'all'
        return (mode, tuple(sorted((str(p['pid']), p['qty']) for p in parts)))
    except (ValueError, TypeError):
        return None


def build_sales_aliases(products):
    """Merge only an unambiguous parent/offer with equal price and composition."""
    by_id = {str(p['id']): p for p in products}
    aliases = {}
    for parent_id, parent in by_id.items():
        options = [o for o in _list(parent.get('options'))
                   if isinstance(o, dict) and not o.get('hidden') and o.get('product_id')]
        composition = _composition(parent.get('bundle'))
        if composition is None or not parent.get('price'):
            continue
        # With no explicit components, more than one selling option is ambiguous.
        if composition == ('all', ()) and len(options) != 1:
            continue
        matches = []
        for option in options:
            child = by_id.get(str(option['product_id']))
            if (child and child['id'] != parent_id and child.get('price') == parent.get('price')
                    and _composition(child.get('bundle')) == composition):
                matches.append(child)
        if len(matches) == 1:
            child = matches[0]
            identity = (str(child['id']), re.sub(r'\s+', ' ', str(child.get('name') or child['id'])).strip())
            aliases[parent_id] = identity
            aliases[str(child['id'])] = identity
    return aliases


def load_sales_aliases(rows, name_column='name', price_column='price'):
    """Load only product families with configured options, once per report."""
    columns = 'id, %s AS name, %s AS price, options, bundle' % (name_column, price_column)
    parents = rows('SELECT %s FROM products WHERE options IS NOT NULL AND options<>? AND options<>?' % columns,
                   ('', '[]'))
    child_ids = sorted({str(o['product_id']) for p in parents for o in _list(p.get('options'))
                        if isinstance(o, dict) and o.get('product_id') and not o.get('hidden')})
    children = []
    for start in range(0, len(child_ids), 400):
        batch = child_ids[start:start + 400]
        children.extend(rows('SELECT %s FROM products WHERE id IN (%s)' %
                             (columns, ','.join('?' for _ in batch)), tuple(batch)))
    return build_sales_aliases(parents + children)


def report_identity(item, aliases):
    pid = str(item.get('id') or '')
    name = re.sub(r'\s+', ' ', str(item.get('n') or item.get('name') or pid)).strip() or '(무명)'
    return aliases.get(pid, (pid, name))
