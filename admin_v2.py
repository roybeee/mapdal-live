"""
MAPDAL SEOUL — 통합 관리자 대시보드 v2
─────────────────────────────────────
app.py 는 수정하지 않는 독립 모듈. app.py 맨 아래 app.mount(...) 바로 윗줄에
아래 4줄만 붙여넣으면 연결됩니다.

    try:
        from admin_v2 import admin_router
        app.include_router(admin_router)
    except Exception as _e:
        print('admin load skipped:', _e)

접속:  https://mapdal.kr/admin/dashboard?token=관리자토큰
       (기존 /admin 에 쓰던 것과 같은 ADMIN_TOKEN)

기능:  KPI 대시보드 · 주문 검색/배송처리/송장 · 토스 결제취소(재고 자동복원)
       상품 4,982종 재고/품절/가격 관리 · 주문 CSV 다운로드 · 시스템 상태
설계:  import 시점 DB 접속 없음(첫 요청 때 지연 초기화) · 스키마 자동 감지
"""
import os, json, sqlite3, base64, datetime, urllib.request, urllib.error
from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, Response, JSONResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
IS_PG = DATABASE_URL.startswith('postgres')

admin_router = APIRouter()

# ── 설정값: 환경변수 우선, 없으면 app.py 상수 재사용 ───────────────────
def _from_app(name, default=''):
    try:
        import app as _app
        return getattr(_app, name, default)
    except Exception:
        return default

def admin_token():
    return os.environ.get('ADMIN_TOKEN') or _from_app('ADMIN_TOKEN', '')

def toss_secret():
    return os.environ.get('TOSS_SECRET_KEY') or _from_app('TOSS_SECRET_KEY', '')

# ── DB 어댑터 (PG/SQLite 겸용, 읽기·쓰기 최소 래퍼) ─────────────────────
def _conn():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(os.path.join(BASE, 'mapdal.db'), timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _q(sql):
    return sql.replace('?', '%s') if IS_PG else sql

def rows(sql, args=()):
    with _conn() as c:
        cur = c.execute(_q(sql), args)
        return [dict(r) for r in cur.fetchall()]

def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None

def run(sql, args=()):
    with _conn() as c:
        cur = c.execute(_q(sql), args)
        c.commit()
        return cur.rowcount

def num(x):
    if x is None: return 0
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return 0

# ── 스키마 자동 감지 + 지연 마이그레이션 (import 시점 DB 접속 금지) ──────
_state = {'ready': False, 'ocols': set(), 'pcols': set(),
          'paykey': None, 'pname': None, 'pprice': None}

def _cols(table):
    try:
        if IS_PG:
            rs = rows("SELECT column_name AS c FROM information_schema.columns WHERE table_name=?", (table,))
            return {r['c'] for r in rs}
        rs = rows("PRAGMA table_info(%s)" % table)
        return {r['name'] for r in rs}
    except Exception:
        return set()

def ensure_ready():
    if _state['ready']:
        return
    oc = _cols('orders')
    if oc:  # 배송처리용 컬럼 3종을 없으면 추가 (있으면 그대로 둠)
        for col, typ in (('fulfill', "TEXT DEFAULT 'NEW'"), ('tracking', 'TEXT'), ('admin_memo', 'TEXT')):
            if col not in oc:
                try:
                    run("ALTER TABLE orders ADD COLUMN %s %s" % (col, typ))
                except Exception:
                    pass
        oc = _cols('orders')
    pc = _cols('products')
    _state['ocols'] = oc
    _state['pcols'] = pc
    _state['paykey'] = next((c for c in ('pay_key', 'payment_key', 'paykey') if c in oc), None)
    _state['pname'] = next((c for c in ('name', 'title', 'n') if c in pc), None)
    _state['pprice'] = next((c for c in ('price', 'p', 'amount') if c in pc), None)
    _state['ready'] = True

# ── 인증: ?token= 또는 쿠키. 첫 통과 시 쿠키 발급 ───────────────────────
def check_auth(request: Request):
    tk = admin_token()
    got = request.query_params.get('token') or request.cookies.get('mp_admin') or ''
    if not tk:
        raise HTTPException(403, 'ADMIN_TOKEN 이 설정되어 있지 않습니다. Render → Environment 에서 추가하세요.')
    if got != tk:
        raise HTTPException(403, 'forbidden')
    try:
        ensure_ready()
    except Exception:
        pass

def kst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()

def esc_csv(v):
    s = '' if v is None else str(v)
    if any(ch in s for ch in (',', '"', '\n')):
        s = '"' + s.replace('"', '""') + '"'
    return s

def jload(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

# ════════════════════════════════════════════════════ API ══════════════

@admin_router.get('/admin/api/summary')
def api_summary(request: Request):
    check_auth(request)
    today = kst_today()
    d7 = (today - datetime.timedelta(days=6)).isoformat()
    d30 = (today - datetime.timedelta(days=29)).isoformat()
    t = today.isoformat()

    tot = one("SELECT COUNT(*) AS c, COALESCE(SUM(CASE WHEN status='PAID' THEN amount END),0) AS s FROM orders") or {}
    st = rows("SELECT status, COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders GROUP BY status")
    day = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (t,)) or {}
    w7 = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (d7,)) or {}
    m30 = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (d30,)) or {}

    # 최근 30일 일별 매출 + 품목 Top (PAID 주문 파싱)
    recent = rows("SELECT created, amount, items FROM orders WHERE status='PAID' AND created >= ? ORDER BY created DESC LIMIT 5000", (d30,))
    daily = {}
    top = {}
    for r in recent:
        dkey = (r.get('created') or '')[:10]
        daily[dkey] = daily.get(dkey, 0) + num(r.get('amount'))
        for it in jload(r.get('items'), []):
            nm = it.get('n') or it.get('name') or it.get('id') or '(무명)'
            qy = num(it.get('q') or 1)
            pr = num(it.get('p') or it.get('price') or 0)
            rec = top.setdefault(nm, {'qty': 0, 'rev': 0})
            rec['qty'] += qy
            rec['rev'] += pr * qy
    days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    series = [{'d': d, 'v': daily.get(d, 0)} for d in days]
    top10 = sorted(({'name': k, **v} for k, v in top.items()),
                   key=lambda x: (x['rev'], x['qty']), reverse=True)[:10]

    # 처리 대기(결제완료 & 미발송) / 재고 경고
    fexpr = "COALESCE(fulfill,'NEW')" if 'fulfill' in _state['ocols'] else "'NEW'"
    pend = one("SELECT COUNT(*) AS c FROM orders WHERE status='PAID' AND %s IN ('NEW','PREPARING')" % fexpr) or {}
    low = []
    if _state['pcols']:
        nm = _state['pname'] or 'id'
        try:
            low = rows("SELECT id, %s AS name, stock, soldout FROM products WHERE soldout=1 OR stock<=5 ORDER BY soldout DESC, stock ASC LIMIT 12" % nm)
        except Exception:
            low = []
    latest = rows("SELECT order_id, created, status, amount, buyer FROM orders ORDER BY created DESC LIMIT 8")
    for r in latest:
        r['buyer_name'] = (jload(r.pop('buyer', None), {}) or {}).get('name', '')
        r['amount'] = num(r.get('amount'))

    paid_cnt = num(tot.get('c')) and next((num(x['c']) for x in st if x['status'] == 'PAID'), 0)
    return {
        'today': {'cnt': num(day.get('c')), 'sum': num(day.get('s'))},
        'week': {'cnt': num(w7.get('c')), 'sum': num(w7.get('s'))},
        'month': {'cnt': num(m30.get('c')), 'sum': num(m30.get('s'))},
        'all': {'cnt': num(tot.get('c')), 'paid_sum': num(tot.get('s'))},
        'aov': (num(m30.get('s')) // num(m30.get('c'))) if num(m30.get('c')) else 0,
        'status': [{'k': r['status'], 'c': num(r['c']), 's': num(r['s'])} for r in st],
        'series': series, 'top': top10, 'pending_ship': num(pend.get('c')),
        'low_stock': [{'id': r['id'], 'name': r.get('name') or r['id'],
                       'stock': num(r.get('stock')), 'soldout': num(r.get('soldout'))} for r in low],
        'latest': latest, 'paid_cnt': paid_cnt,
    }

@admin_router.get('/admin/api/orders')
def api_orders(request: Request):
    check_auth(request)
    p = request.query_params
    where, args = [], []
    if p.get('query'):
        kw = '%' + p['query'].strip() + '%'
        where.append('(order_id LIKE ? OR buyer LIKE ?)')
        args += [kw, kw]
    if p.get('status'):
        where.append('status = ?'); args.append(p['status'])
    if p.get('fulfill') and 'fulfill' in _state['ocols']:
        where.append("COALESCE(fulfill,'NEW') = ?"); args.append(p['fulfill'])
    if p.get('from'):
        where.append('created >= ?'); args.append(p['from'])
    if p.get('to'):
        where.append('created <= ?'); args.append(p['to'] + '~')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 20
    total = num((one('SELECT COUNT(*) AS c FROM orders' + w, tuple(args)) or {}).get('c'))
    sel = ['order_id', 'created', 'status', 'amount', 'items', 'buyer', 'ship_method']
    for c in ('fulfill', 'tracking', 'receipt_url'):
        if c in _state['ocols']: sel.append(c)
    rs = rows('SELECT %s FROM orders%s ORDER BY created DESC LIMIT %d OFFSET %d'
              % (', '.join(sel), w, size, (page - 1) * size), tuple(args))
    out = []
    for r in rs:
        b = jload(r.get('buyer'), {})
        its = jload(r.get('items'), [])
        first = (its[0].get('n') or its[0].get('name') or its[0].get('id') or '') if its else ''
        label = first[:24] + (' 외 %d' % (len(its) - 1) if len(its) > 1 else '')
        out.append({'order_id': r['order_id'], 'created': (r.get('created') or '')[:16].replace('T', ' '),
                    'status': r.get('status'), 'fulfill': r.get('fulfill') or 'NEW',
                    'amount': num(r.get('amount')), 'items_label': label, 'items_cnt': len(its),
                    'buyer_name': b.get('name', ''), 'phone': b.get('phone', ''),
                    'ship': r.get('ship_method', ''), 'tracking': r.get('tracking') or '',
                    'receipt': r.get('receipt_url') or ''})
    return {'total': total, 'page': page, 'size': size, 'rows': out}

@admin_router.get('/admin/api/orders/{oid}')
def api_order_detail(oid: str, request: Request):
    check_auth(request)
    r = one('SELECT * FROM orders WHERE order_id = ?', (oid,))
    if not r:
        raise HTTPException(404, 'not found')
    b = jload(r.get('buyer'), {})
    its = jload(r.get('items'), [])
    items = [{'id': it.get('id', ''), 'name': it.get('n') or it.get('name') or it.get('id', ''),
              'qty': num(it.get('q') or 1), 'price': num(it.get('p') or it.get('price') or 0)} for it in its]
    return {'order_id': r['order_id'], 'created': r.get('created'), 'status': r.get('status'),
            'fulfill': r.get('fulfill') or 'NEW', 'amount': num(r.get('amount')),
            'buyer': {'name': b.get('name', ''), 'phone': b.get('phone', ''), 'zip': b.get('zip', ''),
                      'addr': (b.get('addr1', '') + ' ' + b.get('addr2', '')).strip(), 'memo': b.get('memo', '')},
            'items': items, 'ship_method': r.get('ship_method', ''),
            'tracking': r.get('tracking') or '', 'admin_memo': r.get('admin_memo') or '',
            'receipt': r.get('receipt_url') or '', 'method': r.get('method') or '',
            'can_refund': bool(_state['paykey'] and r.get(_state['paykey']) and r.get('status') == 'PAID')}

@admin_router.post('/admin/api/orders/{oid}/fulfill')
def api_fulfill(oid: str, request: Request, body: dict = Body(...)):
    check_auth(request)
    if 'fulfill' not in _state['ocols']:
        raise HTTPException(400, '컬럼 마이그레이션 실패 — 새로고침 후 재시도')
    f = body.get('fulfill')
    if f not in ('NEW', 'PREPARING', 'SHIPPED', 'DONE', 'CANCELLED'):
        raise HTTPException(400, 'bad fulfill')
    n = run('UPDATE orders SET fulfill=?, tracking=?, admin_memo=? WHERE order_id=?',
            (f, (body.get('tracking') or '').strip(), (body.get('memo') or '').strip(), oid))
    if not n:
        raise HTTPException(404, 'not found')
    return {'ok': True}

@admin_router.post('/admin/api/orders/{oid}/cancel')
def api_cancel(oid: str, request: Request, body: dict = Body(...)):
    """PAID 주문: 토스 결제취소 호출 → 상태 CANCELLED → 재고 자동복원.
       PENDING/FAILED 주문: 표시만 취소."""
    check_auth(request)
    r = one('SELECT * FROM orders WHERE order_id = ?', (oid,))
    if not r:
        raise HTTPException(404, 'not found')
    reason = (body.get('reason') or '관리자 취소').strip()[:200]
    refunded = False
    if r.get('status') == 'PAID':
        pk = r.get(_state['paykey']) if _state['paykey'] else None
        if not pk:
            raise HTTPException(400, '결제키가 없어 자동 환불 불가 — 토스 상점관리자에서 직접 취소하세요.')
        sk = toss_secret()
        if not sk:
            raise HTTPException(400, 'TOSS_SECRET_KEY 미설정')
        req = urllib.request.Request(
            'https://api.tosspayments.com/v1/payments/%s/cancel' % pk,
            data=json.dumps({'cancelReason': reason}).encode(),
            headers={'Authorization': 'Basic ' + base64.b64encode((sk + ':').encode()).decode(),
                     'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read().decode())
            refunded = True
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get('message', 'toss error')
            except Exception:
                msg = 'toss error'
            raise HTTPException(400, '토스 취소 실패: ' + msg)
    sets, args = ["status='CANCELLED'"], []
    if 'fulfill' in _state['ocols']:
        sets.append("fulfill='CANCELLED'")
    if 'admin_memo' in _state['ocols']:
        sets.append('admin_memo=?'); args.append(('[취소] ' + reason)[:300])
    run('UPDATE orders SET %s WHERE order_id=?' % ', '.join(sets), tuple(args + [oid]))
    # 재고 복원
    restored = 0
    if _state['pcols']:
        for it in jload(r.get('items'), []):
            pid, qy = it.get('id'), num(it.get('q') or 1)
            if pid:
                try:
                    restored += run('UPDATE products SET stock = stock + ?, soldout = 0 WHERE id = ?', (qy, pid))
                except Exception:
                    pass
    return {'ok': True, 'refunded': refunded, 'stock_restored_items': restored}

@admin_router.get('/admin/api/products')
def api_products(request: Request):
    check_auth(request)
    if not _state['pcols']:
        return {'total': 0, 'rows': [], 'page': 1, 'size': 30}
    p = request.query_params
    nm = _state['pname'] or 'id'
    pr = _state['pprice']
    where, args = [], []
    if p.get('query'):
        kw = '%' + p['query'].strip() + '%'
        where.append('(id LIKE ? OR %s LIKE ?)' % nm); args += [kw, kw]
    f = p.get('filter', '')
    if f == 'low':
        where.append('stock <= 5 AND soldout = 0')
    elif f == 'soldout':
        where.append('soldout = 1')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 30
    total = num((one('SELECT COUNT(*) AS c FROM products' + w, tuple(args)) or {}).get('c'))
    cols = 'id, %s AS name, stock, soldout' % nm + ((', %s AS price' % pr) if pr else '')
    rs = rows('SELECT %s FROM products%s ORDER BY soldout DESC, stock ASC, id LIMIT %d OFFSET %d'
              % (cols, w, size, (page - 1) * size), tuple(args))
    return {'total': total, 'page': page, 'size': size,
            'rows': [{'id': r['id'], 'name': r.get('name') or r['id'], 'stock': num(r.get('stock')),
                      'soldout': num(r.get('soldout')), 'price': num(r.get('price')) if pr else None} for r in rs]}

@admin_router.post('/admin/api/products/update')
def api_product_update(request: Request, body: dict = Body(...)):
    check_auth(request)
    pid = body.get('id')
    if not pid:
        raise HTTPException(400, 'id required')
    sets, args = [], []
    if body.get('stock') is not None:
        s = num(body['stock'])
        if s < 0: raise HTTPException(400, '재고는 0 이상')
        sets.append('stock=?'); args.append(s)
    if body.get('soldout') is not None:
        sets.append('soldout=?'); args.append(1 if body['soldout'] else 0)
    if body.get('price') is not None and _state['pprice']:
        v = num(body['price'])
        if v < 0: raise HTTPException(400, '가격은 0 이상')
        sets.append('%s=?' % _state['pprice']); args.append(v)
    if not sets:
        raise HTTPException(400, '변경할 값 없음')
    n = run('UPDATE products SET %s WHERE id=?' % ', '.join(sets), tuple(args + [pid]))
    if not n:
        raise HTTPException(404, 'not found')
    return {'ok': True}

@admin_router.get('/admin/api/orders.csv')
def api_orders_csv(request: Request):
    check_auth(request)
    p = request.query_params
    where, args = [], []
    if p.get('from'): where.append('created >= ?'); args.append(p['from'])
    if p.get('to'): where.append('created <= ?'); args.append(p['to'] + '~')
    if p.get('status'): where.append('status = ?'); args.append(p['status'])
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    rs = rows('SELECT * FROM orders%s ORDER BY created DESC LIMIT 20000' % w, tuple(args))
    head = ['주문번호', '일시', '결제상태', '처리상태', '금액', '주문자', '연락처', '우편번호', '주소',
            '품목', '총수량', '배송방식', '송장번호', '관리자메모', '영수증URL']
    lines = [','.join(head)]
    for r in rs:
        b = jload(r.get('buyer'), {})
        its = jload(r.get('items'), [])
        names = ' / '.join('%s x%d' % ((it.get('n') or it.get('name') or it.get('id') or ''), num(it.get('q') or 1)) for it in its)
        qty = sum(num(it.get('q') or 1) for it in its)
        row = [r.get('order_id'), (r.get('created') or '')[:19].replace('T', ' '), r.get('status'),
               r.get('fulfill') or 'NEW', num(r.get('amount')), b.get('name', ''), b.get('phone', ''),
               b.get('zip', ''), (b.get('addr1', '') + ' ' + b.get('addr2', '')).strip(),
               names, qty, r.get('ship_method', ''), r.get('tracking') or '',
               r.get('admin_memo') or '', r.get('receipt_url') or '']
        lines.append(','.join(esc_csv(v) for v in row))
    csv = '\ufeff' + '\n'.join(lines)
    fname = 'mapdal_orders_%s.csv' % kst_today().strftime('%Y%m%d')
    return Response(csv, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % fname})

@admin_router.get('/admin/api/system')
def api_system(request: Request):
    check_auth(request)
    sk = toss_secret()
    mode = '라이브(실결제)' if sk.startswith('live_') else ('테스트(실과금 없음)' if sk.startswith('test_') else '미설정')
    try:
        oc = num((one('SELECT COUNT(*) AS c FROM orders') or {}).get('c'))
        pc = num((one('SELECT COUNT(*) AS c FROM products') or {}).get('c'))
        db_ok = True
    except Exception:
        oc = pc = 0; db_ok = False
    return {'db': 'PostgreSQL' if IS_PG else 'SQLite', 'db_ok': db_ok,
            'orders': oc, 'products': pc, 'toss_mode': mode,
            'paykey_col': _state['paykey'] or '(감지 안 됨)',
            'time_kst': (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}

# ════════════════════════════════════════════ 대시보드 HTML ═════════════

@admin_router.get('/admin/dashboard', response_class=HTMLResponse)
def dashboard(request: Request):
    tk = admin_token()
    got = request.query_params.get('token') or request.cookies.get('mp_admin') or ''
    if not tk or got != tk:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:40px">'
                            '<h2>MAPDAL 관리자</h2><p>접근 권한이 없습니다. '
                            '주소 뒤에 <code>?token=관리자토큰</code> 을 붙여 접속하세요.</p>', status_code=403)
    try:
        ensure_ready()
    except Exception:
        pass
    resp = HTMLResponse(ADMIN_HTML)
    resp.set_cookie('mp_admin', tk, httponly=True, samesite='lax', secure=True, max_age=604800)
    return resp

ADMIN_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>MAPDAL SEOUL — 관리자</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000;--line:#e3e1db;--ok:#0a7d38;--bad:#c0392b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--paper);color:var(--black);font-size:14px}
header{background:var(--black);color:#fff;display:flex;align-items:center;gap:18px;padding:0 20px;height:56px;position:sticky;top:0;z-index:50}
header h1{font-family:'Black Han Sans';font-size:20px;letter-spacing:.5px}header h1 span{color:var(--red)}
header .sub{font-family:'IBM Plex Mono';font-size:11px;color:var(--amber)}
nav{display:flex;gap:2px;margin-left:auto}
nav button{background:none;border:0;color:#bbb;font:inherit;font-weight:700;padding:8px 14px;cursor:pointer;border-bottom:3px solid transparent}
nav button.on{color:#fff;border-color:var(--red)}
main{max-width:1180px;margin:0 auto;padding:22px 16px 80px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--line);padding:14px 16px}
.card .k{font-size:11px;color:#888;font-weight:700;letter-spacing:.5px}
.card .v{font-family:'IBM Plex Mono';font-size:22px;font-weight:600;margin-top:6px}
.card .s{font-size:11.5px;color:#666;margin-top:3px}
.card.alert{border-left:4px solid var(--red)}
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);padding:16px;margin-bottom:14px}
.panel h3{font-size:13px;letter-spacing:.5px;margin-bottom:12px;border-left:4px solid var(--red);padding-left:8px}
.chart{display:flex;align-items:flex-end;gap:3px;height:120px}
.chart .bar{flex:1;background:var(--red);opacity:.85;min-height:2px}
.chart .bar:hover{background:var(--amber);opacity:1}
.chart-x{display:flex;justify-content:space-between;font-family:'IBM Plex Mono';font-size:10px;color:#999;margin-top:4px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:12.5px}
th{background:var(--black);color:#fff;font-size:11px;padding:8px 9px;text-align:left;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:8px 9px;vertical-align:middle}
tr:hover td{background:#faf9f5}
.st{font-weight:700;font-family:'IBM Plex Mono';font-size:11px}
.st.PAID{color:var(--ok)}.st.PENDING{color:var(--amber)}.st.FAILED{color:var(--bad)}.st.CANCELLED{color:#999}
.ff{font-size:11px;font-weight:700;padding:2px 7px;border-radius:2px;background:#eee}
.ff.NEW{background:#fff2f1;color:var(--red)}.ff.PREPARING{background:#fff6e0;color:#9a6b00}
.ff.SHIPPED{background:#e8f3ff;color:#1a5fb4}.ff.DONE{background:#e9f7ee;color:var(--ok)}.ff.CANCELLED{background:#f0f0f0;color:#999}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid #ccc;background:#fff}
input:focus,select:focus{outline:2px solid var(--red)}
button.btn{font:inherit;font-weight:700;border:0;padding:8px 14px;cursor:pointer;background:var(--black);color:#fff}
button.btn.red{background:var(--red)}button.btn.ghost{background:#fff;color:var(--black);border:1px solid #999}
button.btn.sm{padding:4px 9px;font-size:12px}
.pager{display:flex;gap:6px;align-items:center;margin-top:12px;font-family:'IBM Plex Mono';font-size:12px}
.right{text-align:right}.mono{font-family:'IBM Plex Mono'}
.modal-bg{position:fixed;inset:0;background:rgba(20,20,20,.55);display:none;align-items:flex-start;justify-content:center;z-index:100;padding:30px 12px;overflow:auto}
.modal{background:#fff;max-width:640px;width:100%;padding:22px}
.modal h3{font-size:16px;margin-bottom:14px}
.kv{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;font-size:13px;margin-bottom:12px}
.kv b{color:#777;font-weight:700;font-size:11.5px}
.stockin{width:70px;text-align:right}
.hint{font-size:11.5px;color:#888;margin-top:8px}
.tag{display:inline-block;background:var(--black);color:var(--amber);font-family:'IBM Plex Mono';font-size:10.5px;padding:2px 7px;margin-left:6px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--black);color:#fff;padding:10px 20px;display:none;z-index:200;font-weight:700}
.loading{color:#999;padding:26px;text-align:center}
</style></head><body>
<header><h1>MAPDAL<span>SEOUL</span></h1><span class="sub">ADMIN CONSOLE</span>
<nav><button data-t="dash" class="on">대시보드</button><button data-t="orders">주문 관리</button><button data-t="products">상품·재고</button><button data-t="system">시스템</button></nav></header>
<main>
<section id="t-dash"><div class="loading">불러오는 중…</div></section>
<section id="t-orders" style="display:none">
  <div class="toolbar">
    <input id="oq" placeholder="주문번호 · 이름 · 전화 검색" style="width:220px">
    <select id="ost"><option value="">결제상태 전체</option><option>PAID</option><option>PENDING</option><option>FAILED</option><option>CANCELLED</option></select>
    <select id="off"><option value="">처리상태 전체</option><option value="NEW">신규</option><option value="PREPARING">상품준비중</option><option value="SHIPPED">발송완료</option><option value="DONE">배송완료</option><option value="CANCELLED">취소</option></select>
    <input id="ofrom" type="date"><input id="oto" type="date">
    <button class="btn" onclick="loadOrders(1)">검색</button>
    <button class="btn ghost" onclick="csv()">CSV 다운로드</button>
  </div>
  <div id="olist" class="loading">불러오는 중…</div>
</section>
<section id="t-products" style="display:none">
  <div class="toolbar">
    <input id="pq" placeholder="상품명 · ID 검색" style="width:240px">
    <select id="pf"><option value="">전체</option><option value="low">저재고(≤5)</option><option value="soldout">품절</option></select>
    <button class="btn" onclick="loadProducts(1)">검색</button>
    <span class="hint">재고·가격 수정 후 각 행의 [저장]을 누르세요. 품절 토글은 즉시 반영됩니다.</span>
  </div>
  <div id="plist" class="loading">불러오는 중…</div>
</section>
<section id="t-system" style="display:none"><div id="sys" class="loading">불러오는 중…</div></section>
</main>
<div class="modal-bg" id="mbg"><div class="modal" id="mbox"></div></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>'₩'+Number(n||0).toLocaleString('ko-KR');
const FF={NEW:'신규',PREPARING:'상품준비중',SHIPPED:'발송완료',DONE:'배송완료',CANCELLED:'취소'};
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2200)}
async function api(p,opt){const r=await fetch(p,opt);if(!r.ok){let m='오류';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 ['dash','orders','products','system'].forEach(t=>$('#t-'+t).style.display=(t===b.dataset.t?'':'none'));
 if(b.dataset.t==='dash')loadDash();if(b.dataset.t==='orders')loadOrders(1);if(b.dataset.t==='products')loadProducts(1);if(b.dataset.t==='system')loadSys()});

async function loadDash(){try{const d=await api('/admin/api/summary');
 const stMap={};d.status.forEach(s=>stMap[s.k]=s);
 const mx=Math.max(1,...d.series.map(s=>s.v));
 $('#t-dash').innerHTML=`
 <div class="cards">
  <div class="card"><div class="k">오늘 매출</div><div class="v">${won(d.today.sum)}</div><div class="s">${d.today.cnt}건</div></div>
  <div class="card"><div class="k">최근 7일</div><div class="v">${won(d.week.sum)}</div><div class="s">${d.week.cnt}건</div></div>
  <div class="card"><div class="k">최근 30일</div><div class="v">${won(d.month.sum)}</div><div class="s">${d.month.cnt}건 · 객단가 ${won(d.aov)}</div></div>
  <div class="card"><div class="k">누적 결제액</div><div class="v">${won(d.all.paid_sum)}</div><div class="s">전체 주문 ${d.all.cnt}건</div></div>
  <div class="card ${d.pending_ship?'alert':''}"><div class="k">발송 대기</div><div class="v">${d.pending_ship}건</div><div class="s">결제완료 · 미발송</div></div>
 </div>
 <div class="panel"><h3>최근 30일 일별 매출 <span class="tag">PAID 기준</span></h3>
  <div class="chart">${d.series.map(s=>`<div class="bar" style="height:${Math.round(s.v/mx*100)}%" title="${s.d} · ${won(s.v)}"></div>`).join('')}</div>
  <div class="chart-x"><span>${d.series[0].d.slice(5)}</span><span>${d.series[14].d.slice(5)}</span><span>${d.series[29].d.slice(5)}</span></div></div>
 <div class="grid2">
  <div class="panel"><h3>품목 TOP 10 (30일)</h3><table><tr><th>상품</th><th class="right">수량</th><th class="right">매출</th></tr>
   ${d.top.map(t=>`<tr><td>${esc(t.name)}</td><td class="right mono">${t.qty}</td><td class="right mono">${won(t.rev)}</td></tr>`).join('')||'<tr><td colspan=3 class="loading">30일 내 결제 없음</td></tr>'}</table></div>
  <div class="panel"><h3>재고 경고 <span class="tag">품절 · 5개 이하</span></h3><table><tr><th>상품</th><th class="right">재고</th></tr>
   ${d.low_stock.map(l=>`<tr><td>${esc(l.name)}</td><td class="right mono" style="color:${l.soldout?'#c0392b':'#9a6b00'}">${l.soldout?'품절':l.stock}</td></tr>`).join('')||'<tr><td colspan=2 class="loading">경고 없음</td></tr>'}</table>
   <div class="hint">재고 수정은 [상품·재고] 탭에서.</div></div>
 </div>
 <div class="panel"><h3>최근 주문</h3><table><tr><th>주문번호</th><th>일시</th><th>상태</th><th class="right">금액</th><th>주문자</th></tr>
  ${d.latest.map(o=>`<tr onclick="openOrder('${esc(o.order_id)}')" style="cursor:pointer"><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc((o.created||'').slice(5,16).replace('T',' '))}</td><td><span class="st ${esc(o.status)}">${esc(o.status)}</span></td><td class="right mono">${won(o.amount)}</td><td>${esc(o.buyer_name)}</td></tr>`).join('')}</table></div>`;
}catch(e){$('#t-dash').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}

let opage=1;
async function loadOrders(p){opage=p;const q=new URLSearchParams({page:p});
 if($('#oq').value)q.set('query',$('#oq').value);if($('#ost').value)q.set('status',$('#ost').value);
 if($('#off').value)q.set('fulfill',$('#off').value);if($('#ofrom').value)q.set('from',$('#ofrom').value);if($('#oto').value)q.set('to',$('#oto').value);
 try{const d=await api('/admin/api/orders?'+q);
 $('#olist').innerHTML=`<table><tr><th>주문번호</th><th>일시</th><th>결제</th><th>처리</th><th class="right">금액</th><th>품목</th><th>주문자</th><th>연락처</th><th>송장</th><th></th></tr>
 ${d.rows.map(o=>`<tr><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc(o.created)}</td>
  <td><span class="st ${esc(o.status)}">${esc(o.status)}</span></td><td><span class="ff ${esc(o.fulfill)}">${FF[o.fulfill]||esc(o.fulfill)}</span></td>
  <td class="right mono">${won(o.amount)}</td><td>${esc(o.items_label)}</td><td>${esc(o.buyer_name)}</td><td class="mono">${esc(o.phone)}</td>
  <td class="mono">${esc(o.tracking)}</td><td><button class="btn sm ghost" onclick="openOrder('${esc(o.order_id)}')">상세</button></td></tr>`).join('')||'<tr><td colspan=10 class="loading">결과 없음</td></tr>'}</table>
 <div class="pager"><button class="btn sm ghost" ${p<=1?'disabled':''} onclick="loadOrders(${p-1})">이전</button>
 <span>${p} / ${Math.max(1,Math.ceil(d.total/d.size))} · 총 ${d.total}건</span>
 <button class="btn sm ghost" ${p*d.size>=d.total?'disabled':''} onclick="loadOrders(${p+1})">다음</button></div>`;
 }catch(e){$('#olist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}

function csv(){const q=new URLSearchParams();if($('#ofrom').value)q.set('from',$('#ofrom').value);
 if($('#oto').value)q.set('to',$('#oto').value);if($('#ost').value)q.set('status',$('#ost').value);
 location.href='/admin/api/orders.csv?'+q}

async function openOrder(oid){try{const o=await api('/admin/api/orders/'+encodeURIComponent(oid));
 $('#mbox').innerHTML=`<h3>주문 ${esc(o.order_id)} <span class="st ${esc(o.status)}">${esc(o.status)}</span></h3>
 <div class="kv"><b>일시</b><span class="mono">${esc((o.created||'').slice(0,19).replace('T',' '))}</span>
 <b>금액</b><span class="mono">${won(o.amount)} ${o.method?'· '+esc(o.method):''}</span>
 <b>주문자</b><span>${esc(o.buyer.name)} · ${esc(o.buyer.phone)}</span>
 <b>주소</b><span>[${esc(o.buyer.zip)}] ${esc(o.buyer.addr)}</span>
 <b>배송</b><span>${esc(o.ship_method)}</span>
 ${o.receipt?`<b>영수증</b><span><a href="${esc(o.receipt)}" target="_blank">토스 영수증 열기</a></span>`:''}</div>
 <table style="margin-bottom:14px"><tr><th>품목</th><th class="right">단가</th><th class="right">수량</th></tr>
 ${o.items.map(i=>`<tr><td>${esc(i.name)}</td><td class="right mono">${i.price?won(i.price):'-'}</td><td class="right mono">${i.qty}</td></tr>`).join('')}</table>
 <div class="kv"><b>처리상태</b><span><select id="mff">${Object.entries(FF).map(([k,v])=>`<option value="${k}" ${o.fulfill===k?'selected':''}>${v}</option>`).join('')}</select></span>
 <b>송장번호</b><span><input id="mtr" value="${esc(o.tracking)}" placeholder="택배 송장번호" style="width:100%"></span>
 <b>메모</b><span><input id="mmemo" value="${esc(o.admin_memo)}" placeholder="내부 메모" style="width:100%"></span></div>
 <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
  ${o.status!=='CANCELLED'?`<button class="btn red" onclick="cancelOrder('${esc(o.order_id)}',${o.can_refund})">${o.can_refund?'결제취소(환불)':'주문취소 표시'}</button>`:''}
  <button class="btn" onclick="saveFulfill('${esc(o.order_id)}')">저장</button>
  <button class="btn ghost" onclick="closeM()">닫기</button></div>
 ${o.can_refund?'<div class="hint">결제취소 시 토스 환불이 실행되고 재고가 자동 복원됩니다.</div>':''}`;
 $('#mbg').style.display='flex';}catch(e){toast(e.message)}}
function closeM(){$('#mbg').style.display='none'}
$('#mbg').addEventListener('click',e=>{if(e.target.id==='mbg')closeM()});
async function saveFulfill(oid){try{await api('/admin/api/orders/'+encodeURIComponent(oid)+'/fulfill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fulfill:$('#mff').value,tracking:$('#mtr').value,memo:$('#mmemo').value})});toast('저장되었습니다');closeM();loadOrders(opage)}catch(e){toast(e.message)}}
async function cancelOrder(oid,refund){const msg=refund?'토스 결제취소(환불)를 실행합니다. 계속할까요?':'이 주문을 취소로 표시할까요?';
 if(!confirm(msg))return;const reason=prompt('취소 사유','고객 요청')||'고객 요청';
 try{const r=await api('/admin/api/orders/'+encodeURIComponent(oid)+'/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
 toast(r.refunded?'환불 완료 · 재고 복원':'취소 처리 완료');closeM();loadOrders(opage)}catch(e){alert(e.message)}}

let ppage=1;
async function loadProducts(p){ppage=p;const q=new URLSearchParams({page:p});
 if($('#pq').value)q.set('query',$('#pq').value);if($('#pf').value)q.set('filter',$('#pf').value);
 try{const d=await api('/admin/api/products?'+q);
 $('#plist').innerHTML=`<table><tr><th>상품 ID</th><th>상품명</th><th class="right">가격</th><th class="right">재고</th><th>품절</th><th></th></tr>
 ${d.rows.map(r=>{const k=btoa(unescape(encodeURIComponent(r.id))).replace(/=/g,'');return `<tr id="row${k}">
  <td class="mono" style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis">${esc(r.id)}</td>
  <td>${esc(r.name)}</td>
  <td class="right">${r.price==null?'-':`<input class="stockin" style="width:90px" id="pr${k}" type="number" min="0" value="${r.price}">`}</td>
  <td class="right"><input class="stockin" id="st${k}" type="number" min="0" value="${r.stock}"></td>
  <td><input type="checkbox" id="so${k}" ${r.soldout?'checked':''} onchange="saveProd('${k}',true)"></td>
  <td><button class="btn sm" onclick="saveProd('${k}',false)">저장</button></td></tr>`}).join('')||'<tr><td colspan=6 class="loading">결과 없음</td></tr>'}</table>
 <div class="pager"><button class="btn sm ghost" ${p<=1?'disabled':''} onclick="loadProducts(${p-1})">이전</button>
 <span>${p} / ${Math.max(1,Math.ceil(d.total/d.size))} · 총 ${d.total}개</span>
 <button class="btn sm ghost" ${p*d.size>=d.total?'disabled':''} onclick="loadProducts(${p+1})">다음</button></div>`;
 window._pk=window._pk||{};d.rows.forEach(r=>{const k=btoa(unescape(encodeURIComponent(r.id))).replace(/=/g,'');window._pk[k]=r.id});
 }catch(e){$('#plist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function saveProd(k,toggleOnly){const id=window._pk[k];const body={id};
 const so=document.getElementById('so'+k);body.soldout=so.checked?1:0;
 if(!toggleOnly){body.stock=Number(document.getElementById('st'+k).value);
  const pr=document.getElementById('pr'+k);if(pr)body.price=Number(pr.value)}
 try{await api('/admin/api/products/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('반영되었습니다')}catch(e){toast(e.message);loadProducts(ppage)}}

async function loadSys(){try{const s=await api('/admin/api/system');
 $('#sys').innerHTML=`<div class="cards">
 <div class="card"><div class="k">데이터베이스</div><div class="v" style="font-size:16px">${esc(s.db)}</div><div class="s">${s.db_ok?'정상':'연결 오류'}</div></div>
 <div class="card"><div class="k">누적 주문</div><div class="v">${s.orders}</div></div>
 <div class="card"><div class="k">등록 상품</div><div class="v">${s.products.toLocaleString()}</div></div>
 <div class="card ${s.toss_mode.includes('테스트')?'alert':''}"><div class="k">토스 결제 모드</div><div class="v" style="font-size:15px">${esc(s.toss_mode)}</div><div class="s">라이브 전환은 PG 심사 후 키 교체</div></div>
 <div class="card"><div class="k">서버시각 (KST)</div><div class="v" style="font-size:15px">${esc(s.time_kst)}</div></div></div>
 <div class="panel"><h3>운영 체크리스트</h3><div style="line-height:2">
 결제키 컬럼: <b class="mono">${esc(s.paykey_col)}</b> — 결제취소(환불) 기능이 이 컬럼을 사용합니다.<br>
 정식 오픈 전: PG 가맹 심사 → 라이브 키 교체(Render Environment) → 이용약관·개인정보처리방침 게시 → 통신판매업 신고번호 표기.</div></div>`;
 }catch(e){$('#sys').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
loadDash();
</script></body></html>'''
