"""
MAPDAL SEOUL — 통합 관리자 v3  (파일명은 admin_v2.py 유지 → app.py 수정 불필요)
─────────────────────────────────────────────────────────────────────────
v2 전체 기능 + ① 고객(회원) CRM  ② 알림톡/SMS 발송(솔라피)  ③ 관리자 권한 4등급 + 감사로그

접속: /admin/dashboard?token=토큰
 · 마스터: Render 환경변수 ADMIN_TOKEN (항상 OWNER, 잠금 방지용)
 · 부관리자: [관리자] 탭에서 발급 (OWNER/MANAGER/STAFF/VIEWER)

알림 발송 환경변수(솔라피): SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER(발신번호),
 SOLAPI_PF_ID(카카오채널 pfId — 알림톡용). 미설정 시 발송 대신 로그만 기록(DRY).
"""
import os, re, json, sqlite3, base64, hashlib, hmac, secrets, datetime, time
import urllib.request, urllib.error
from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, Response, JSONResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
IS_PG = DATABASE_URL.startswith('postgres')
admin_router = APIRouter()

def _from_app(name, default=''):
    try:
        import app as _app
        return getattr(_app, name, default)
    except Exception:
        return default

def admin_token():  return os.environ.get('ADMIN_TOKEN') or _from_app('ADMIN_TOKEN', '')
def toss_secret():  return os.environ.get('TOSS_SECRET_KEY') or _from_app('TOSS_SECRET_KEY', '')
def solapi_conf():
    return {'key': os.environ.get('SOLAPI_API_KEY', ''), 'sec': os.environ.get('SOLAPI_API_SECRET', ''),
            'sender': os.environ.get('SOLAPI_SENDER', ''), 'pf': os.environ.get('SOLAPI_PF_ID', '')}

# ── DB 어댑터 ───────────────────────────────────────────────────────────
def _conn():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(os.path.join(BASE, 'mapdal.db'), timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _q(s): return s.replace('?', '%s') if IS_PG else s

def rows(sql, args=()):
    with _conn() as c:
        return [dict(r) for r in c.execute(_q(sql), args).fetchall()]

def one(sql, args=()):
    r = rows(sql, args); return r[0] if r else None

def run(sql, args=()):
    with _conn() as c:
        cur = c.execute(_q(sql), args); c.commit(); return cur.rowcount

def runmany(pairs):
    with _conn() as c:
        for sql, args in pairs: c.execute(_q(sql), args)
        c.commit()

def num(x):
    if x is None: return 0
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return 0

def now_iso(): return datetime.datetime.utcnow().isoformat(timespec='seconds')
def kst_now(): return datetime.datetime.utcnow() + datetime.timedelta(hours=9)
def kst_today(): return kst_now().date()
def jload(s, d):
    try: return json.loads(s) if s else d
    except Exception: return d
def digits(p): return re.sub(r'\D', '', str(p or ''))
def uid(): return secrets.token_hex(6)

# ── 스키마 감지 + 지연 초기화 (import 시점 DB 접속 없음) ─────────────────
_state = {'ready': False, 'ocols': set(), 'pcols': set(), 'paykey': None, 'pname': None, 'pprice': None}

def _cols(t):
    try:
        if IS_PG:
            return {r['c'] for r in rows("SELECT column_name AS c FROM information_schema.columns WHERE table_name=?", (t,))}
        return {r['name'] for r in rows("PRAGMA table_info(%s)" % t)}
    except Exception:
        return set()

SEED_TPL = [
    ('발송완료 안내', 'sms', '', '[맵달SEOUL] #{이름}님, 주문 #{주문번호} 상품이 발송되었습니다.\n송장번호: #{송장}\n감사합니다. Shop Seongsu, from Anywhere!'),
    ('결제완료 안내', 'sms', '', '[맵달SEOUL] #{이름}님, 주문 #{주문번호} 결제가 완료되었습니다. (총 #{금액}원)\n빠르게 준비해 발송하겠습니다.'),
    ('배송완료 안내', 'sms', '', '[맵달SEOUL] #{이름}님, 주문 #{주문번호} 배송이 완료되었습니다. 맛있게 즐겨주세요!'),
]

def ensure_ready():
    if _state['ready']: return
    oc = _cols('orders')
    if oc:
        for col, typ in (('fulfill', "TEXT DEFAULT 'NEW'"), ('tracking', 'TEXT'), ('admin_memo', 'TEXT')):
            if col not in oc:
                try: run("ALTER TABLE orders ADD COLUMN %s %s" % (col, typ))
                except Exception: pass
        oc = _cols('orders')
    for ddl in (
        """CREATE TABLE IF NOT EXISTS customers(phone TEXT PRIMARY KEY, phones_raw TEXT, name TEXT,
           zip TEXT, addr TEXT, first_order TEXT, last_order TEXT, order_cnt INTEGER DEFAULT 0,
           total_spend INTEGER DEFAULT 0, grade TEXT DEFAULT 'WELCOME', grade_manual INTEGER DEFAULT 0,
           memo TEXT, marketing_ok INTEGER DEFAULT 0, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS admins(id TEXT PRIMARY KEY, name TEXT, role TEXT,
           token_hash TEXT, active INTEGER DEFAULT 1, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS notify_templates(id TEXT PRIMARY KEY, name TEXT, kind TEXT,
           template_id TEXT, body TEXT, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS notify_log(id TEXT PRIMARY KEY, created TEXT, order_id TEXT,
           phone TEXT, kind TEXT, template TEXT, status TEXT, detail TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS audit_log(id TEXT PRIMARY KEY, created TEXT, actor TEXT,
           role TEXT, action TEXT, target TEXT, detail TEXT)""",
        """CREATE TABLE IF NOT EXISTS admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT,
           created TEXT, expires TEXT)""",
        """CREATE TABLE IF NOT EXISTS page_edits(path TEXT PRIMARY KEY, html TEXT,
           updated TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS page_history(id TEXT PRIMARY KEY, path TEXT, html TEXT,
           saved TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY, provider TEXT, sub TEXT,
           email TEXT, name TEXT, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_sessions(id TEXT PRIMARY KEY, member_id TEXT,
           created TEXT, expires TEXT)""",
    ):
        try: run(ddl)
        except Exception: pass
    try:
        if not one('SELECT id FROM notify_templates LIMIT 1'):
            runmany([('INSERT INTO notify_templates VALUES(?,?,?,?,?,?)',
                      (uid(), n, k, t, b, now_iso())) for n, k, t, b in SEED_TPL])
    except Exception: pass
    ac = _cols('admins')
    for col in ('username', 'pw'):
        if ac and col not in ac:
            try: run("ALTER TABLE admins ADD COLUMN %s TEXT" % col)
            except Exception: pass
    # 환경변수 ADMIN_USER / ADMIN_PASS 로 대표(OWNER) 계정 자동 생성
    try:
        au = (os.environ.get('ADMIN_USER') or '').strip().lower()
        ap = os.environ.get('ADMIN_PASS') or ''
        if au and ap:
            row = one('SELECT * FROM admins WHERE username=?', (au,))
            if not row:
                run("INSERT INTO admins(id,name,role,token_hash,active,created,username,pw) VALUES(?,?,?,?,1,?,?,?)",
                    (uid(), '대표', 'OWNER', '', now_iso(), au, pw_hash(ap)))
            elif not (row.get('pw') or ''):
                run("UPDATE admins SET pw=?, role='OWNER', active=1 WHERE username=?", (pw_hash(ap), au))
    except Exception:
        pass
    mcx = _cols('members')
    if mcx and 'pw' not in mcx:
        try: run("ALTER TABLE members ADD COLUMN pw TEXT")
        except Exception: pass
    pcx = _cols('products')
    for col in ('img', 'descr'):
        if pcx and col not in pcx:
            try: run("ALTER TABLE products ADD COLUMN %s TEXT" % col)
            except Exception: pass
    pc = _cols('products')
    _state.update(ocols=oc, pcols=pc,
                  paykey=next((c for c in ('pay_key', 'payment_key', 'paykey') if c in oc), None),
                  pname=next((c for c in ('name', 'title', 'n') if c in pc), None),
                  pprice=next((c for c in ('price', 'p', 'amount') if c in pc), None), ready=True)

# ── 인증 + 역할 ─────────────────────────────────────────────────────────
RANK = {'VIEWER': 0, 'STAFF': 1, 'MANAGER': 2, 'OWNER': 3}
RNAME = {'OWNER': '대표(전체)', 'MANAGER': '매니저', 'STAFF': '스태프', 'VIEWER': '조회전용'}

def get_actor(request: Request):
    try: ensure_ready()
    except Exception: pass
    # 1) 세션 (ID/PW 로그인)
    sid = request.cookies.get('mp_sess') or ''
    if sid:
        srow = one('SELECT * FROM admin_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
        if srow and (srow.get('expires') or '') > now_iso():
            if srow['admin_id'] == '__master__':
                return {'name': '마스터', 'role': 'OWNER', 'master': True, 'sid': sid}
            adm = one('SELECT * FROM admins WHERE id=? AND active=1', (srow['admin_id'],))
            if adm:
                return {'name': adm['name'], 'role': adm['role'], 'master': False,
                        'id': adm['id'], 'username': adm.get('username') or '', 'sid': sid}
    # 2) 마스터 토큰 (비상용)
    tok = request.query_params.get('token') or request.cookies.get('mp_admin') or ''
    mt = admin_token()
    if tok and mt and tok == mt:
        return {'name': '마스터', 'role': 'OWNER', 'master': True, 'token': tok}
    # 3) 구버전 토큰 계정 (하위호환)
    if tok:
        row = one('SELECT * FROM admins WHERE token_hash=? AND active=1',
                  (hashlib.sha256(tok.encode()).hexdigest(),))
        if row:
            return {'name': row['name'], 'role': row['role'], 'master': False, 'id': row['id'], 'token': tok}
    raise HTTPException(403, 'forbidden')

def need(actor, lvl, what='이 작업'):
    if RANK.get(actor['role'], 0) < lvl:
        raise HTTPException(403, '%s 권한이 없습니다 (필요 등급: %s 이상)' %
                            (what, [k for k, v in RANK.items() if v == lvl][0]))

def audit(actor, action, target='', detail=''):
    try:
        run('INSERT INTO audit_log VALUES(?,?,?,?,?,?,?)',
            (uid(), now_iso(), actor['name'], actor['role'], action, str(target)[:120], str(detail)[:300]))
    except Exception: pass


# ── 비밀번호(PBKDF2) · 세션 · 로그인 시도제한 ──────────────────────────
def pw_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + '$' + hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200000).hex()

def pw_verify(pw, stored):
    try:
        salt, h = (stored or '').split('$', 1)
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200000).hex(), h)
    except Exception:
        return False

def make_session(admin_id, days=7):
    sid = secrets.token_urlsafe(24)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat(timespec='seconds')
    try: run('DELETE FROM admin_sessions WHERE expires < ?', (now_iso(),))
    except Exception: pass
    run('INSERT INTO admin_sessions VALUES(?,?,?,?)',
        (hashlib.sha256(sid.encode()).hexdigest(), admin_id, now_iso(), exp))
    return sid

_fails = {}
def guard(key):
    r = _fails.get(key)
    if r and r[0] >= 5 and time.time() < r[1]:
        raise HTTPException(429, '로그인 시도 초과 — 10분 후 다시 시도하세요')
def fail_hit(key):
    r = _fails.setdefault(key, [0, 0]); r[0] += 1
    if r[0] >= 5: r[1] = time.time() + 600
def fail_clear(key): _fails.pop(key, None)

def esc_csv(v):
    s = '' if v is None else str(v)
    return '"' + s.replace('"', '""') + '"' if any(ch in s for ch in (',', '"', '\n')) else s

# ═══════════════════════════ 기본 API (v2 계승 + 권한) ═══════════════════
@admin_router.post('/admin/api/login')
def api_login(request: Request, body: dict = Body(...)):
    try: ensure_ready()
    except Exception: pass
    ip = (request.client.host if request.client else '') or '-'
    tok = (body.get('token') or '').strip()
    if tok:  # 마스터 토큰 비상 로그인
        key = 'tk:' + ip; guard(key)
        if admin_token() and tok == admin_token():
            fail_clear(key)
            sid = make_session('__master__')
            audit({'name': '마스터', 'role': 'OWNER'}, '로그인', '', '마스터 토큰 / ' + ip)
            resp = JSONResponse({'ok': True, 'name': '마스터', 'role': 'OWNER'})
            resp.set_cookie('mp_sess', sid, httponly=True, samesite='lax', secure=True, max_age=604800)
            return resp
        fail_hit(key)
        raise HTTPException(403, '토큰이 올바르지 않습니다')
    u = (body.get('username') or '').strip().lower()
    p = body.get('password') or ''
    if not u or not p: raise HTTPException(400, '아이디와 비밀번호를 입력하세요')
    key = u + ':' + ip; guard(key)
    row = one('SELECT * FROM admins WHERE username=? AND active=1', (u,))
    if not row or not pw_verify(p, row.get('pw') or ''):
        fail_hit(key)
        audit({'name': u, 'role': '-'}, '로그인실패', u, ip)
        raise HTTPException(403, '아이디 또는 비밀번호가 올바르지 않습니다')
    fail_clear(key)
    sid = make_session(row['id'])
    audit({'name': row['name'], 'role': row['role']}, '로그인', u, ip)
    resp = JSONResponse({'ok': True, 'name': row['name'], 'role': row['role']})
    resp.set_cookie('mp_sess', sid, httponly=True, samesite='lax', secure=True, max_age=604800)
    return resp

@admin_router.post('/admin/api/logout')
def api_logout(request: Request):
    sid = request.cookies.get('mp_sess') or ''
    if sid:
        try: run('DELETE FROM admin_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
        except Exception: pass
    resp = JSONResponse({'ok': True})
    resp.delete_cookie('mp_sess'); resp.delete_cookie('mp_admin')
    return resp

@admin_router.post('/admin/api/password')
def api_password(request: Request, body: dict = Body(...)):
    a = get_actor(request)
    if a.get('master') or not a.get('id'):
        raise HTTPException(400, '마스터 접속에는 비밀번호가 없습니다 — 계정으로 로그인 후 변경하세요')
    old, new = body.get('old') or '', body.get('new') or ''
    if len(new) < 8: raise HTTPException(400, '새 비밀번호는 8자 이상이어야 합니다')
    row = one('SELECT * FROM admins WHERE id=?', (a['id'],))
    if not row or not pw_verify(old, row.get('pw') or ''):
        raise HTTPException(403, '현재 비밀번호가 올바르지 않습니다')
    run('UPDATE admins SET pw=? WHERE id=?', (pw_hash(new), a['id']))
    audit(a, '비밀번호변경', a.get('username', ''), '')
    return {'ok': True}

@admin_router.get('/admin/api/me')
def api_me(request: Request):
    a = get_actor(request)
    return {'name': a['name'], 'role': a['role'], 'master': a['master']}

@admin_router.get('/admin/api/summary')
def api_summary(request: Request):
    a = get_actor(request); need(a, 0)
    today = kst_today(); t = today.isoformat()
    d7 = (today - datetime.timedelta(days=6)).isoformat()
    d30 = (today - datetime.timedelta(days=29)).isoformat()
    tot = one("SELECT COUNT(*) AS c, COALESCE(SUM(CASE WHEN status='PAID' THEN amount END),0) AS s FROM orders") or {}
    st = rows("SELECT status, COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders GROUP BY status")
    day = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (t,)) or {}
    w7 = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (d7,)) or {}
    m30 = one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID' AND created >= ?", (d30,)) or {}
    recent = rows("SELECT created, amount, items FROM orders WHERE status='PAID' AND created >= ? ORDER BY created DESC LIMIT 5000", (d30,))
    daily, top = {}, {}
    for r in recent:
        daily[(r.get('created') or '')[:10]] = daily.get((r.get('created') or '')[:10], 0) + num(r.get('amount'))
        for it in jload(r.get('items'), []):
            nm = it.get('n') or it.get('name') or it.get('id') or '(무명)'
            rec = top.setdefault(nm, {'qty': 0, 'rev': 0})
            rec['qty'] += num(it.get('q') or 1)
            rec['rev'] += num(it.get('p') or it.get('price') or 0) * num(it.get('q') or 1)
    days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    fexpr = "COALESCE(fulfill,'NEW')" if 'fulfill' in _state['ocols'] else "'NEW'"
    pend = one("SELECT COUNT(*) AS c FROM orders WHERE status='PAID' AND %s IN ('NEW','PREPARING')" % fexpr) or {}
    low = []
    if _state['pcols']:
        try:
            low = rows("SELECT id, %s AS name, stock, soldout FROM products WHERE soldout=1 OR stock<=5 ORDER BY soldout DESC, stock ASC LIMIT 12" % (_state['pname'] or 'id'))
        except Exception: pass
    latest = rows("SELECT order_id, created, status, amount, buyer FROM orders ORDER BY created DESC LIMIT 8")
    for r in latest:
        r['buyer_name'] = (jload(r.pop('buyer', None), {}) or {}).get('name', '')
        r['amount'] = num(r.get('amount'))
    cust = one('SELECT COUNT(*) AS c FROM customers') or {}
    return {'today': {'cnt': num(day.get('c')), 'sum': num(day.get('s'))},
            'week': {'cnt': num(w7.get('c')), 'sum': num(w7.get('s'))},
            'month': {'cnt': num(m30.get('c')), 'sum': num(m30.get('s'))},
            'all': {'cnt': num(tot.get('c')), 'paid_sum': num(tot.get('s'))},
            'aov': (num(m30.get('s')) // num(m30.get('c'))) if num(m30.get('c')) else 0,
            'status': [{'k': r['status'], 'c': num(r['c']), 's': num(r['s'])} for r in st],
            'series': [{'d': d, 'v': daily.get(d, 0)} for d in days],
            'top': sorted(({'name': k, **v} for k, v in top.items()), key=lambda x: (x['rev'], x['qty']), reverse=True)[:10],
            'pending_ship': num(pend.get('c')), 'customers': num(cust.get('c')),
            'low_stock': [{'id': r['id'], 'name': r.get('name') or r['id'], 'stock': num(r.get('stock')), 'soldout': num(r.get('soldout'))} for r in low],
            'latest': latest}

@admin_router.get('/admin/api/orders')
def api_orders(request: Request):
    a = get_actor(request); need(a, 0)
    p = request.query_params
    where, args = [], []
    if p.get('query'):
        kw = '%' + p['query'].strip() + '%'; where.append('(order_id LIKE ? OR buyer LIKE ?)'); args += [kw, kw]
    if p.get('status'): where.append('status = ?'); args.append(p['status'])
    if p.get('fulfill') and 'fulfill' in _state['ocols']:
        where.append("COALESCE(fulfill,'NEW') = ?"); args.append(p['fulfill'])
    if p.get('from'): where.append('created >= ?'); args.append(p['from'])
    if p.get('to'): where.append('created <= ?'); args.append(p['to'] + '~')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 20
    total = num((one('SELECT COUNT(*) AS c FROM orders' + w, tuple(args)) or {}).get('c'))
    sel = ['order_id', 'created', 'status', 'amount', 'items', 'buyer', 'ship_method']
    sel += [c for c in ('fulfill', 'tracking', 'receipt_url') if c in _state['ocols']]
    rs = rows('SELECT %s FROM orders%s ORDER BY created DESC LIMIT %d OFFSET %d' % (', '.join(sel), w, size, (page - 1) * size), tuple(args))
    out = []
    for r in rs:
        b = jload(r.get('buyer'), {}); its = jload(r.get('items'), [])
        first = (its[0].get('n') or its[0].get('name') or its[0].get('id') or '') if its else ''
        out.append({'order_id': r['order_id'], 'created': (r.get('created') or '')[:16].replace('T', ' '),
                    'status': r.get('status'), 'fulfill': r.get('fulfill') or 'NEW', 'amount': num(r.get('amount')),
                    'items_label': first[:24] + (' 외 %d' % (len(its) - 1) if len(its) > 1 else ''),
                    'buyer_name': b.get('name', ''), 'phone': b.get('phone', ''),
                    'ship': r.get('ship_method', ''), 'tracking': r.get('tracking') or ''})
    return {'total': total, 'page': page, 'size': size, 'rows': out}

@admin_router.get('/admin/api/orders/{oid}')
def api_order_detail(oid: str, request: Request):
    a = get_actor(request); need(a, 0)
    r = one('SELECT * FROM orders WHERE order_id = ?', (oid,))
    if not r: raise HTTPException(404, 'not found')
    b = jload(r.get('buyer'), {})
    items = [{'id': it.get('id', ''), 'name': it.get('n') or it.get('name') or it.get('id', ''),
              'qty': num(it.get('q') or 1), 'price': num(it.get('p') or it.get('price') or 0)}
             for it in jload(r.get('items'), [])]
    return {'order_id': r['order_id'], 'created': r.get('created'), 'status': r.get('status'),
            'fulfill': r.get('fulfill') or 'NEW', 'amount': num(r.get('amount')),
            'buyer': {'name': b.get('name', ''), 'phone': b.get('phone', ''), 'zip': b.get('zip', ''),
                      'addr': (b.get('addr1', '') + ' ' + b.get('addr2', '')).strip()},
            'items': items, 'ship_method': r.get('ship_method', ''), 'tracking': r.get('tracking') or '',
            'admin_memo': r.get('admin_memo') or '', 'receipt': r.get('receipt_url') or '',
            'method': r.get('method') or '',
            'can_refund': bool(_state['paykey'] and r.get(_state['paykey']) and r.get('status') == 'PAID')}

@admin_router.post('/admin/api/orders/{oid}/fulfill')
def api_fulfill(oid: str, request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '주문 처리')
    if 'fulfill' not in _state['ocols']: raise HTTPException(400, '컬럼 준비 중 — 새로고침 후 재시도')
    f = body.get('fulfill')
    if f not in ('NEW', 'PREPARING', 'SHIPPED', 'DONE', 'CANCELLED'): raise HTTPException(400, 'bad fulfill')
    n = run('UPDATE orders SET fulfill=?, tracking=?, admin_memo=? WHERE order_id=?',
            (f, (body.get('tracking') or '').strip(), (body.get('memo') or '').strip(), oid))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '주문처리변경', oid, '%s / 송장 %s' % (f, body.get('tracking') or '-'))
    return {'ok': True}

@admin_router.post('/admin/api/orders/{oid}/cancel')
def api_cancel(oid: str, request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '결제취소(환불)')
    r = one('SELECT * FROM orders WHERE order_id = ?', (oid,))
    if not r: raise HTTPException(404, 'not found')
    reason = (body.get('reason') or '관리자 취소').strip()[:200]
    refunded = False
    if r.get('status') == 'PAID':
        pk = r.get(_state['paykey']) if _state['paykey'] else None
        if not pk: raise HTTPException(400, '결제키가 없어 자동 환불 불가 — 토스 상점관리자에서 직접 취소하세요.')
        sk = toss_secret()
        if not sk: raise HTTPException(400, 'TOSS_SECRET_KEY 미설정')
        req = urllib.request.Request('https://api.tosspayments.com/v1/payments/%s/cancel' % pk,
                                     data=json.dumps({'cancelReason': reason}).encode(),
                                     headers={'Authorization': 'Basic ' + base64.b64encode((sk + ':').encode()).decode(),
                                              'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp: json.loads(resp.read().decode())
            refunded = True
        except urllib.error.HTTPError as e:
            try: msg = json.loads(e.read().decode()).get('message', 'toss error')
            except Exception: msg = 'toss error'
            raise HTTPException(400, '토스 취소 실패: ' + msg)
    sets, args = ["status='CANCELLED'"], []
    if 'fulfill' in _state['ocols']: sets.append("fulfill='CANCELLED'")
    if 'admin_memo' in _state['ocols']: sets.append('admin_memo=?'); args.append(('[취소] ' + reason)[:300])
    run('UPDATE orders SET %s WHERE order_id=?' % ', '.join(sets), tuple(args + [oid]))
    restored = 0
    if _state['pcols']:
        for it in jload(r.get('items'), []):
            if it.get('id'):
                try: restored += run('UPDATE products SET stock = stock + ?, soldout = 0 WHERE id = ?', (num(it.get('q') or 1), it['id']))
                except Exception: pass
    audit(a, '환불' if refunded else '주문취소', oid, '%s / 금액 %s / 재고복원 %d' % (reason, num(r.get('amount')), restored))
    return {'ok': True, 'refunded': refunded, 'stock_restored_items': restored}

@admin_router.get('/admin/api/products')
def api_products(request: Request):
    a = get_actor(request); need(a, 0)
    if not _state['pcols']: return {'total': 0, 'rows': [], 'page': 1, 'size': 30}
    p = request.query_params
    nm, pr = _state['pname'] or 'id', _state['pprice']
    where, args = [], []
    if p.get('query'):
        kw = '%' + p['query'].strip() + '%'; where.append('(id LIKE ? OR %s LIKE ?)' % nm); args += [kw, kw]
    if p.get('filter') == 'low': where.append('stock <= 5 AND soldout = 0')
    elif p.get('filter') == 'soldout': where.append('soldout = 1')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 30
    total = num((one('SELECT COUNT(*) AS c FROM products' + w, tuple(args)) or {}).get('c'))
    cols = 'id, %s AS name, stock, soldout' % nm + ((', %s AS price' % pr) if pr else '')
    rs = rows('SELECT %s FROM products%s ORDER BY soldout DESC, stock ASC, id LIMIT %d OFFSET %d' % (cols, w, size, (page - 1) * size), tuple(args))
    return {'total': total, 'page': page, 'size': size,
            'rows': [{'id': r['id'], 'name': r.get('name') or r['id'], 'stock': num(r.get('stock')),
                      'soldout': num(r.get('soldout')), 'price': num(r.get('price')) if pr else None} for r in rs]}

@admin_router.post('/admin/api/products/update')
def api_product_update(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '재고 관리')
    pid = body.get('id')
    if not pid: raise HTTPException(400, 'id required')
    sets, args, log = [], [], []
    if body.get('stock') is not None:
        s = num(body['stock'])
        if s < 0: raise HTTPException(400, '재고는 0 이상')
        sets.append('stock=?'); args.append(s); log.append('재고→%d' % s)
    if body.get('soldout') is not None:
        sets.append('soldout=?'); args.append(1 if body['soldout'] else 0); log.append('품절→%s' % ('ON' if body['soldout'] else 'OFF'))
    if body.get('price') is not None and _state['pprice']:
        need(a, 2, '가격 변경')
        v = num(body['price'])
        if v < 0: raise HTTPException(400, '가격은 0 이상')
        sets.append('%s=?' % _state['pprice']); args.append(v); log.append('가격→%d' % v)
    if not sets: raise HTTPException(400, '변경할 값 없음')
    n = run('UPDATE products SET %s WHERE id=?' % ', '.join(sets), tuple(args + [pid]))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '상품수정', pid, ', '.join(log))
    return {'ok': True}

@admin_router.get('/admin/api/orders.csv')
def api_orders_csv(request: Request):
    a = get_actor(request); need(a, 1, 'CSV 다운로드')
    p = request.query_params
    where, args = [], []
    if p.get('from'): where.append('created >= ?'); args.append(p['from'])
    if p.get('to'): where.append('created <= ?'); args.append(p['to'] + '~')
    if p.get('status'): where.append('status = ?'); args.append(p['status'])
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    rs = rows('SELECT * FROM orders%s ORDER BY created DESC LIMIT 20000' % w, tuple(args))
    head = ['주문번호', '일시', '결제상태', '처리상태', '금액', '주문자', '연락처', '우편번호', '주소', '품목', '총수량', '배송방식', '송장번호', '관리자메모', '영수증URL']
    lines = [','.join(head)]
    for r in rs:
        b = jload(r.get('buyer'), {}); its = jload(r.get('items'), [])
        names = ' / '.join('%s x%d' % ((it.get('n') or it.get('name') or it.get('id') or ''), num(it.get('q') or 1)) for it in its)
        lines.append(','.join(esc_csv(v) for v in [
            r.get('order_id'), (r.get('created') or '')[:19].replace('T', ' '), r.get('status'), r.get('fulfill') or 'NEW',
            num(r.get('amount')), b.get('name', ''), b.get('phone', ''), b.get('zip', ''),
            (b.get('addr1', '') + ' ' + b.get('addr2', '')).strip(), names,
            sum(num(it.get('q') or 1) for it in its), r.get('ship_method', ''), r.get('tracking') or '',
            r.get('admin_memo') or '', r.get('receipt_url') or '']))
    audit(a, 'CSV다운로드', 'orders', '%d건' % len(rs))
    return Response('\ufeff' + '\n'.join(lines), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="mapdal_orders_%s.csv"' % kst_today().strftime('%Y%m%d')})

# ═══════════════════════════ ① 고객(회원) CRM ═══════════════════════════
def grade_of(spend, cnt):
    if spend >= 300000 or cnt >= 5: return 'VIP'
    if spend >= 100000 or cnt >= 3: return 'GOLD'
    return 'WELCOME'

@admin_router.post('/admin/api/customers/sync')
def api_cust_sync(request: Request):
    a = get_actor(request); need(a, 1, '고객 동기화')
    orders = rows('SELECT order_id, created, status, amount, buyer FROM orders ORDER BY created ASC LIMIT 50000')
    agg = {}
    for r in orders:
        b = jload(r.get('buyer'), {}); ph = digits(b.get('phone'))
        if len(ph) < 9: continue
        g = agg.setdefault(ph, {'raws': set(), 'name': '', 'zip': '', 'addr': '', 'first': r['created'],
                                'last': r['created'], 'cnt': 0, 'spend': 0})
        g['raws'].add(b.get('phone', '')); g['last'] = r['created']
        if b.get('name'): g['name'] = b['name']
        if b.get('zip'): g['zip'] = b['zip']
        if b.get('addr1'): g['addr'] = (b.get('addr1', '') + ' ' + b.get('addr2', '')).strip()
        if r.get('status') == 'PAID':
            g['cnt'] += 1; g['spend'] += num(r.get('amount'))
    exist = {r['phone']: r for r in rows('SELECT phone, grade, grade_manual, memo, marketing_ok FROM customers')}
    ops, created, updated = [], 0, 0
    for ph, g in agg.items():
        raws = ','.join(sorted(x for x in g['raws'] if x))[:200]
        if ph in exist:
            e = exist[ph]
            grade = e['grade'] if num(e.get('grade_manual')) else grade_of(g['spend'], g['cnt'])
            ops.append(('UPDATE customers SET phones_raw=?, name=?, zip=?, addr=?, first_order=?, last_order=?, order_cnt=?, total_spend=?, grade=? WHERE phone=?',
                        (raws, g['name'], g['zip'], g['addr'], g['first'], g['last'], g['cnt'], g['spend'], grade, ph)))
            updated += 1
        else:
            ops.append(('INSERT INTO customers(phone, phones_raw, name, zip, addr, first_order, last_order, order_cnt, total_spend, grade, grade_manual, memo, marketing_ok, created) VALUES(?,?,?,?,?,?,?,?,?,?,0,\'\',0,?)',
                        (ph, raws, g['name'], g['zip'], g['addr'], g['first'], g['last'], g['cnt'], g['spend'], grade_of(g['spend'], g['cnt']), now_iso())))
            created += 1
    if ops: runmany(ops)
    audit(a, '고객동기화', '', '신규 %d · 갱신 %d' % (created, updated))
    return {'ok': True, 'created': created, 'updated': updated, 'total': len(agg)}

@admin_router.get('/admin/api/customers')
def api_customers(request: Request):
    a = get_actor(request); need(a, 0)
    p = request.query_params
    where, args = [], []
    if p.get('query'):
        kw = '%' + p['query'].strip() + '%'; where.append('(phone LIKE ? OR phones_raw LIKE ? OR name LIKE ?)'); args += [kw, kw, kw]
    if p.get('grade'): where.append('grade = ?'); args.append(p['grade'])
    if p.get('mk') == '1': where.append('marketing_ok = 1')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 20
    total = num((one('SELECT COUNT(*) AS c FROM customers' + w, tuple(args)) or {}).get('c'))
    rs = rows('SELECT * FROM customers%s ORDER BY total_spend DESC, last_order DESC LIMIT %d OFFSET %d' % (w, size, (page - 1) * size), tuple(args))
    return {'total': total, 'page': page, 'size': size,
            'rows': [{'phone': r['phone'], 'raw': (r.get('phones_raw') or '').split(',')[0] or r['phone'],
                      'name': r.get('name') or '', 'grade': r.get('grade') or 'WELCOME',
                      'cnt': num(r.get('order_cnt')), 'spend': num(r.get('total_spend')),
                      'last': (r.get('last_order') or '')[:10], 'mk': num(r.get('marketing_ok')),
                      'memo': r.get('memo') or ''} for r in rs]}

@admin_router.get('/admin/api/customers/{phone}')
def api_customer_detail(phone: str, request: Request):
    a = get_actor(request); need(a, 0)
    r = one('SELECT * FROM customers WHERE phone = ?', (digits(phone),))
    if not r: raise HTTPException(404, 'not found')
    conds, args = [], []
    for raw in {x for x in (r.get('phones_raw') or '').split(',') if x}:
        conds.append('buyer LIKE ?'); args.append('%' + raw + '%')
    hist = rows('SELECT order_id, created, status, amount, items FROM orders WHERE %s ORDER BY created DESC LIMIT 50' % ' OR '.join(conds), tuple(args)) if conds else []
    orders = []
    for h in hist:
        its = jload(h.get('items'), [])
        first = (its[0].get('n') or its[0].get('name') or '') if its else ''
        orders.append({'order_id': h['order_id'], 'created': (h.get('created') or '')[:16].replace('T', ' '),
                       'status': h.get('status'), 'amount': num(h.get('amount')),
                       'label': first[:22] + (' 외 %d' % (len(its) - 1) if len(its) > 1 else '')})
    return {'phone': r['phone'], 'raw': (r.get('phones_raw') or '').split(',')[0] or r['phone'],
            'name': r.get('name') or '', 'zip': r.get('zip') or '', 'addr': r.get('addr') or '',
            'grade': r.get('grade') or 'WELCOME', 'grade_manual': num(r.get('grade_manual')),
            'memo': r.get('memo') or '', 'mk': num(r.get('marketing_ok')),
            'cnt': num(r.get('order_cnt')), 'spend': num(r.get('total_spend')),
            'first': (r.get('first_order') or '')[:10], 'last': (r.get('last_order') or '')[:10], 'orders': orders}

@admin_router.post('/admin/api/customers/update')
def api_customer_update(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '고객 정보 수정')
    ph = digits(body.get('phone'))
    if not ph: raise HTTPException(400, 'phone required')
    sets, args, log = [], [], []
    if 'memo' in body: sets.append('memo=?'); args.append((body.get('memo') or '')[:500]); log.append('메모')
    if 'mk' in body: sets.append('marketing_ok=?'); args.append(1 if body['mk'] else 0); log.append('마케팅동의→%s' % ('Y' if body['mk'] else 'N'))
    if body.get('grade'):
        need(a, 2, '등급 변경')
        if body['grade'] not in ('WELCOME', 'GOLD', 'VIP', 'BLACK'): raise HTTPException(400, 'bad grade')
        sets.append('grade=?'); args.append(body['grade']); sets.append('grade_manual=1'); log.append('등급→' + body['grade'])
    if not sets: raise HTTPException(400, '변경할 값 없음')
    n = run('UPDATE customers SET %s WHERE phone=?' % ', '.join(sets), tuple(args + [ph]))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '고객수정', ph, ', '.join(log))
    return {'ok': True}

@admin_router.get('/admin/api/customers.csv')
def api_customers_csv(request: Request):
    a = get_actor(request); need(a, 2, '고객 CSV')
    rs = rows('SELECT * FROM customers ORDER BY total_spend DESC LIMIT 50000')
    head = ['전화(표준화)', '전화(원본)', '이름', '등급', '주문수', '총구매액', '첫주문', '최근주문', '마케팅동의', '메모', '주소']
    lines = [','.join(head)]
    for r in rs:
        lines.append(','.join(esc_csv(v) for v in [
            r['phone'], (r.get('phones_raw') or '').split(',')[0], r.get('name') or '', r.get('grade') or '',
            num(r.get('order_cnt')), num(r.get('total_spend')), (r.get('first_order') or '')[:10],
            (r.get('last_order') or '')[:10], 'Y' if num(r.get('marketing_ok')) else 'N',
            r.get('memo') or '', r.get('addr') or '']))
    audit(a, 'CSV다운로드', 'customers', '%d건' % len(rs))
    return Response('\ufeff' + '\n'.join(lines), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="mapdal_customers_%s.csv"' % kst_today().strftime('%Y%m%d')})

# ═══════════════════════════ ② 알림톡 / SMS (솔라피) ═════════════════════
def order_vars(oid):
    r = one('SELECT * FROM orders WHERE order_id=?', (oid,)) if oid else None
    if not r: return None, {}
    b = jload(r.get('buyer'), {}); its = jload(r.get('items'), [])
    first = (its[0].get('n') or its[0].get('name') or '') if its else ''
    label = first[:20] + (' 외 %d건' % (len(its) - 1) if len(its) > 1 else '')
    return r, {'#{이름}': b.get('name', '고객'), '#{주문번호}': r['order_id'],
               '#{송장}': r.get('tracking') or '-', '#{금액}': format(num(r.get('amount')), ','),
               '#{상품}': label, '_phone': b.get('phone', '')}

def solapi_send(msg):
    cf = solapi_conf()
    date = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    salt = secrets.token_hex(16)
    sig = hmac.new(cf['sec'].encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request('https://api.solapi.com/messages/v4/send-many/detail',
        data=json.dumps({'messages': [msg]}).encode(),
        headers={'Authorization': 'HMAC-SHA256 apiKey=%s, date=%s, salt=%s, signature=%s' % (cf['key'], date, salt, sig),
                 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

@admin_router.get('/admin/api/notify/templates')
def api_tpl_list(request: Request):
    a = get_actor(request); need(a, 0)
    return {'rows': rows('SELECT * FROM notify_templates ORDER BY created ASC'),
            'conf': {k: bool(v) for k, v in solapi_conf().items()}}

@admin_router.post('/admin/api/notify/templates/save')
def api_tpl_save(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '템플릿 관리')
    name = (body.get('name') or '').strip(); kind = body.get('kind') or 'sms'
    if not name or kind not in ('sms', 'alimtalk'): raise HTTPException(400, '이름/종류 확인')
    tid, bd = (body.get('template_id') or '').strip(), (body.get('body') or '').strip()
    if body.get('id'):
        n = run('UPDATE notify_templates SET name=?, kind=?, template_id=?, body=? WHERE id=?', (name, kind, tid, bd, body['id']))
        if not n: raise HTTPException(404, 'not found')
    else:
        run('INSERT INTO notify_templates VALUES(?,?,?,?,?,?)', (uid(), name, kind, tid, bd, now_iso()))
    audit(a, '템플릿저장', name, kind)
    return {'ok': True}

@admin_router.post('/admin/api/notify/templates/delete')
def api_tpl_del(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '템플릿 관리')
    n = run('DELETE FROM notify_templates WHERE id=?', (body.get('id'),))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '템플릿삭제', body.get('id'), '')
    return {'ok': True}

@admin_router.post('/admin/api/notify/send')
def api_notify_send(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '알림 발송')
    tpl = one('SELECT * FROM notify_templates WHERE id=?', (body.get('template'),))
    if not tpl: raise HTTPException(400, '템플릿을 선택하세요')
    oid = body.get('order_id') or ''
    order, vars_ = order_vars(oid)
    phone = digits(body.get('phone') or vars_.get('_phone'))
    if len(phone) < 9: raise HTTPException(400, '수신번호가 없습니다')
    if body.get('name'): vars_['#{이름}'] = body['name']
    text = tpl.get('body') or ''
    for k, v in vars_.items():
        if k != '_phone': text = text.replace(k, str(v))
    cf = solapi_conf(); kind = tpl['kind']
    if not (cf['key'] and cf['sec'] and cf['sender']):
        run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), now_iso(), oid, phone, kind, tpl['name'], 'DRY', '발송사 미설정 — 내용만 기록: ' + text[:120], a['name']))
        audit(a, '알림발송', oid or phone, '%s / %s / DRY' % (tpl['name'], kind))
        return {'ok': True, 'dry': True, 'preview': text,
                'msg': '솔라피 미설정 상태라 실제 발송 대신 로그만 기록했습니다.'}
    msg = {'to': phone, 'from': digits(cf['sender'])}
    if kind == 'alimtalk':
        if not (cf['pf'] and tpl.get('template_id')):
            raise HTTPException(400, '알림톡은 SOLAPI_PF_ID와 승인된 템플릿ID가 필요합니다. (SMS 템플릿으로 먼저 발송 가능)')
        kv = {k: str(v) for k, v in vars_.items() if k != '_phone'}
        msg['kakaoOptions'] = {'pfId': cf['pf'], 'templateId': tpl['template_id'], 'variables': kv}
    else:
        try: blen = len(text.encode('euc-kr', errors='replace'))
        except Exception: blen = len(text) * 2
        msg['text'] = text
        msg['type'] = 'LMS' if blen > 90 else 'SMS'
        if msg['type'] == 'LMS': msg['subject'] = '맵달SEOUL 안내'
    try:
        res = solapi_send(msg)
        failed = res.get('failedMessageList') or []
        status = 'FAILED' if failed else 'SENT'
        detail = (failed[0].get('statusMessage', '') if failed else 'groupId=' + str(res.get('groupInfo', {}).get('_id', res.get('groupId', ''))))[:200]
    except urllib.error.HTTPError as e:
        status = 'FAILED'
        try: detail = json.loads(e.read().decode()).get('errorMessage', 'provider error')[:200]
        except Exception: detail = 'provider error'
    except Exception as e:
        status, detail = 'FAILED', str(e)[:200]
    run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(), now_iso(), oid, phone, kind, tpl['name'], status, detail, a['name']))
    audit(a, '알림발송', oid or phone, '%s / %s / %s' % (tpl['name'], kind, status))
    if status == 'FAILED': raise HTTPException(400, '발송 실패: ' + detail)
    return {'ok': True, 'dry': False, 'preview': text}

@admin_router.get('/admin/api/notify/log')
def api_notify_log(request: Request):
    a = get_actor(request); need(a, 0)
    return {'rows': rows('SELECT * FROM notify_log ORDER BY created DESC LIMIT 200')}

# ═══════════════════════════ ③ 관리자 계정 + 감사로그 ════════════════════
@admin_router.get('/admin/api/admins')
def api_admins(request: Request):
    a = get_actor(request); need(a, 3, '관리자 관리')
    return {'rows': [{'id': r['id'], 'name': r['name'], 'role': r['role'],
                      'username': r.get('username') or '', 'auth': 'idpw' if (r.get('pw') or '') else 'token',
                      'active': num(r['active']), 'created': (r.get('created') or '')[:10]}
                     for r in rows('SELECT * FROM admins ORDER BY created ASC')]}

@admin_router.post('/admin/api/admins/create')
def api_admin_create(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 3, '관리자 관리')
    name = (body.get('name') or '').strip(); role = body.get('role')
    uname = (body.get('username') or '').strip().lower()
    if not name or role not in ('OWNER', 'MANAGER', 'STAFF', 'VIEWER'): raise HTTPException(400, '이름/역할 확인')
    if not re.fullmatch(r'[a-z0-9_.-]{3,30}', uname):
        raise HTTPException(400, '아이디는 영문 소문자·숫자 3~30자로 입력하세요')
    if one('SELECT id FROM admins WHERE username=?', (uname,)):
        raise HTTPException(400, '이미 사용 중인 아이디입니다')
    temp = 'mpd-' + secrets.token_urlsafe(8)
    run("INSERT INTO admins(id,name,role,token_hash,active,created,username,pw) VALUES(?,?,?,?,1,?,?,?)",
        (uid(), name, role, '', now_iso(), uname, pw_hash(temp)))
    audit(a, '관리자발급', name, '%s / %s' % (uname, role))
    return {'ok': True, 'username': uname, 'temp_password': temp}

@admin_router.post('/admin/api/admins/toggle')
def api_admin_toggle(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 3, '관리자 관리')
    r = one('SELECT * FROM admins WHERE id=?', (body.get('id'),))
    if not r: raise HTTPException(404, 'not found')
    nv = 0 if num(r['active']) else 1
    run('UPDATE admins SET active=? WHERE id=?', (nv, r['id']))
    if not nv:
        try: run('DELETE FROM admin_sessions WHERE admin_id=?', (r['id'],))
        except Exception: pass
    audit(a, '관리자상태', r['name'], '활성' if nv else '비활성')
    return {'ok': True, 'active': nv}

@admin_router.post('/admin/api/admins/resetpw')
def api_admin_resetpw(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 3, '관리자 관리')
    r = one('SELECT * FROM admins WHERE id=?', (body.get('id'),))
    if not r: raise HTTPException(404, 'not found')
    temp = 'mpd-' + secrets.token_urlsafe(8)
    run('UPDATE admins SET pw=? WHERE id=?', (pw_hash(temp), r['id']))
    try: run('DELETE FROM admin_sessions WHERE admin_id=?', (r['id'],))
    except Exception: pass
    audit(a, '비밀번호재설정', r['name'], r.get('username') or '')
    return {'ok': True, 'username': r.get('username') or '', 'temp_password': temp}

@admin_router.get('/admin/api/audit')
def api_audit(request: Request):
    a = get_actor(request); need(a, 2, '감사로그')
    return {'rows': rows('SELECT * FROM audit_log ORDER BY created DESC LIMIT 150')}

@admin_router.get('/admin/api/system')
def api_system(request: Request):
    a = get_actor(request); need(a, 0)
    sk = toss_secret(); cf = solapi_conf()
    mode = '라이브(실결제)' if sk.startswith('live_') else ('테스트(실과금 없음)' if sk.startswith('test_') else '미설정')
    try:
        oc = num((one('SELECT COUNT(*) AS c FROM orders') or {}).get('c'))
        pc = num((one('SELECT COUNT(*) AS c FROM products') or {}).get('c'))
        cc = num((one('SELECT COUNT(*) AS c FROM customers') or {}).get('c'))
        db_ok = True
    except Exception:
        oc = pc = cc = 0; db_ok = False
    return {'db': 'PostgreSQL' if IS_PG else 'SQLite', 'db_ok': db_ok, 'orders': oc, 'products': pc,
            'customers': cc, 'toss_mode': mode,
            'google_oauth': bool(os.environ.get('GOOGLE_CLIENT_ID')),
            'apple_oauth': bool(os.environ.get('APPLE_CLIENT_ID')),
            'solapi': '설정됨 (발신 %s%s)' % (cf['sender'], ' · 알림톡 연동' if cf['pf'] else ' · SMS만') if cf['key'] else '미설정 (기록 모드)',
            'paykey_col': _state['paykey'] or '(감지 안 됨)', 'time_kst': kst_now().strftime('%Y-%m-%d %H:%M')}

# ═══════════════════════════════ 대시보드 HTML ═══════════════════════════
@admin_router.get('/admin/dashboard', response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        actor = get_actor(request)
    except HTTPException as e:
        return HTMLResponse(LOGIN_HTML, status_code=200 if e.status_code == 403 else e.status_code)
    html = ADMIN_HTML.replace('__ACTOR__', json.dumps(
        {'name': actor['name'], 'role': actor['role'], 'master': bool(actor.get('master'))}, ensure_ascii=False))
    resp = HTMLResponse(html)
    if not actor.get('sid') and actor.get('token'):  # 토큰 직접 접속 → 세션으로 승격
        sid = make_session('__master__' if actor.get('master') else actor.get('id'))
        resp.set_cookie('mp_sess', sid, httponly=True, samesite='lax', secure=True, max_age=604800)
    return resp

LOGIN_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>MAPDAL SEOUL — 관리자 로그인</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--black);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.box{background:var(--paper);width:100%;max-width:400px;padding:36px 32px;border-top:6px solid var(--red)}
h1{font-family:'Black Han Sans';font-size:26px}h1 span{color:var(--red)}
.sub{font-family:'IBM Plex Mono';font-size:11px;color:#888;margin:4px 0 26px;letter-spacing:1px}
label{display:block;font-size:12px;font-weight:700;color:#555;margin:14px 0 5px}
input{width:100%;font:inherit;padding:11px 12px;border:1px solid #ccc;background:#fff}
input:focus{outline:2px solid var(--red)}
button{width:100%;margin-top:22px;font:inherit;font-weight:700;font-size:15px;border:0;padding:13px;cursor:pointer;background:var(--red);color:#fff}
button:hover{background:#c9271f}
.err{display:none;background:#fff2f1;color:#c0392b;font-size:12.5px;padding:10px 12px;margin-top:14px;border-left:3px solid var(--red)}
details{margin-top:20px}summary{font-size:12px;color:#888;cursor:pointer}
details button{background:var(--black);margin-top:10px}
.foot{font-family:'IBM Plex Mono';font-size:10px;color:#aaa;margin-top:24px;text-align:center}
</style></head><body><div class="box">
<h1>MAPDAL<span>SEOUL</span></h1><div class="sub">ADMIN CONSOLE — SIGN IN</div>
<form onsubmit="return go(event)">
<label>아이디</label><input id="u" autocomplete="username" autofocus>
<label>비밀번호</label><input id="p" type="password" autocomplete="current-password">
<button type="submit">로그인</button></form>
<div class="err" id="err"></div>
<details><summary>비상 접속 (마스터 토큰)</summary>
<input id="tk" type="password" placeholder="Render 환경변수 ADMIN_TOKEN 값" style="margin-top:10px">
<button onclick="goTk()">토큰으로 접속</button></details>
<div class="foot">SHOP SEONGSU, FROM ANYWHERE</div></div>
<script>
const E=document.getElementById('err');
function show(m){E.textContent=m;E.style.display='block'}
async function post(b){const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 if(!r.ok){let m='로그인 실패';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
async function go(ev){ev.preventDefault();try{await post({username:document.getElementById('u').value,password:document.getElementById('p').value});location.reload()}catch(e){show(e.message)}return false}
async function goTk(){try{await post({token:document.getElementById('tk').value});location.reload()}catch(e){show(e.message)}}
</script></body></html>'''

ADMIN_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>MAPDAL SEOUL — 관리자</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000;--line:#e3e1db;--ok:#0a7d38;--bad:#c0392b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--paper);color:var(--black);font-size:14px}
header{background:var(--black);color:#fff;display:flex;align-items:center;gap:14px;padding:0 18px;height:56px;position:sticky;top:0;z-index:50;overflow-x:auto}
header h1{font-family:'Black Han Sans';font-size:19px;white-space:nowrap}header h1 span{color:var(--red)}
header .who{font-family:'IBM Plex Mono';font-size:11px;color:var(--amber);white-space:nowrap}
nav{display:flex;gap:0;margin-left:auto}
nav button{background:none;border:0;color:#bbb;font:inherit;font-weight:700;padding:8px 11px;cursor:pointer;border-bottom:3px solid transparent;white-space:nowrap}
nav button.on{color:#fff;border-color:var(--red)}
main{max-width:1180px;margin:0 auto;padding:22px 16px 80px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--line);padding:14px 16px}
.card .k{font-size:11px;color:#888;font-weight:700}.card .v{font-family:'IBM Plex Mono';font-size:21px;font-weight:600;margin-top:6px}
.card .s{font-size:11.5px;color:#666;margin-top:3px}.card.alert{border-left:4px solid var(--red)}
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);padding:16px;margin-bottom:14px}
.panel h3{font-size:13px;margin-bottom:12px;border-left:4px solid var(--red);padding-left:8px}
.chart{display:flex;align-items:flex-end;gap:3px;height:120px}.chart .bar{flex:1;background:var(--red);opacity:.85;min-height:2px}
.chart .bar:hover{background:var(--amber)}.chart-x{display:flex;justify-content:space-between;font-family:'IBM Plex Mono';font-size:10px;color:#999;margin-top:4px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:12.5px}
th{background:var(--black);color:#fff;font-size:11px;padding:8px 9px;text-align:left;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:7px 9px;vertical-align:middle}tr:hover td{background:#faf9f5}
.st{font-weight:700;font-family:'IBM Plex Mono';font-size:11px}
.st.PAID{color:var(--ok)}.st.PENDING{color:var(--amber)}.st.FAILED{color:var(--bad)}.st.CANCELLED{color:#999}
.st.SENT{color:var(--ok)}.st.DRY{color:#1a5fb4}.st.FAILED2{color:var(--bad)}
.ff{font-size:11px;font-weight:700;padding:2px 7px;background:#eee}
.ff.NEW{background:#fff2f1;color:var(--red)}.ff.PREPARING{background:#fff6e0;color:#9a6b00}
.ff.SHIPPED{background:#e8f3ff;color:#1a5fb4}.ff.DONE{background:#e9f7ee;color:var(--ok)}.ff.CANCELLED{background:#f0f0f0;color:#999}
.gr{font-size:11px;font-weight:700;padding:2px 8px}.gr.VIP{background:var(--black);color:var(--amber)}
.gr.GOLD{background:#fff6e0;color:#9a6b00}.gr.WELCOME{background:#eee;color:#666}.gr.BLACK{background:#000;color:#fff}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid #ccc;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid var(--red)}
button.btn{font:inherit;font-weight:700;border:0;padding:8px 14px;cursor:pointer;background:var(--black);color:#fff}
button.btn.red{background:var(--red)}button.btn.ghost{background:#fff;color:var(--black);border:1px solid #999}
button.btn.sm{padding:4px 9px;font-size:12px}button.btn:disabled{opacity:.4;cursor:not-allowed}
.pager{display:flex;gap:6px;align-items:center;margin-top:12px;font-family:'IBM Plex Mono';font-size:12px}
.right{text-align:right}.mono{font-family:'IBM Plex Mono'}
.modal-bg{position:fixed;inset:0;background:rgba(20,20,20,.55);display:none;align-items:flex-start;justify-content:center;z-index:100;padding:30px 12px;overflow:auto}
.modal{background:#fff;max-width:660px;width:100%;padding:22px}.modal h3{font-size:16px;margin-bottom:14px}
.kv{display:grid;grid-template-columns:92px 1fr;gap:6px 10px;font-size:13px;margin-bottom:12px}.kv b{color:#777;font-size:11.5px}
.stockin{width:70px;text-align:right}.hint{font-size:11.5px;color:#888;margin-top:8px;line-height:1.6}
.tag{display:inline-block;background:var(--black);color:var(--amber);font-family:'IBM Plex Mono';font-size:10.5px;padding:2px 7px;margin-left:6px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--black);color:#fff;padding:10px 20px;display:none;z-index:200;font-weight:700}
.loading{color:#999;padding:26px;text-align:center}
.tokenbox{background:#141414;color:#FFB000;font-family:'IBM Plex Mono';padding:12px;word-break:break-all;margin:10px 0}
</style></head><body>
<header><h1>MAPDAL<span>SEOUL</span></h1><span class="who" id="who"></span><button class="btn sm ghost" id="pwbtn" style="background:none;color:#bbb;border-color:#555" onclick="pwModal()">비밀번호</button><button class="btn sm ghost" style="background:none;color:#bbb;border-color:#555" onclick="logout()">로그아웃</button><nav id="nav"></nav></header>
<main>
<section id="t-dash"><div class="loading">불러오는 중…</div></section>
<section id="t-orders" style="display:none">
  <div class="toolbar"><input id="oq" placeholder="주문번호 · 이름 · 전화" style="width:200px">
  <select id="ost"><option value="">결제상태 전체</option><option>PAID</option><option>PENDING</option><option>FAILED</option><option>CANCELLED</option></select>
  <select id="off"><option value="">처리상태 전체</option><option value="NEW">신규</option><option value="PREPARING">상품준비중</option><option value="SHIPPED">발송완료</option><option value="DONE">배송완료</option><option value="CANCELLED">취소</option></select>
  <input id="ofrom" type="date"><input id="oto" type="date">
  <button class="btn" onclick="loadOrders(1)">검색</button>
  <button class="btn ghost" onclick="csv()" id="csvbtn">CSV</button></div>
  <div id="olist" class="loading">불러오는 중…</div></section>
<section id="t-products" style="display:none">
  <div class="toolbar"><input id="pq" placeholder="상품명 · ID" style="width:220px">
  <select id="pf"><option value="">전체</option><option value="low">저재고(≤5)</option><option value="soldout">품절</option></select>
  <button class="btn" onclick="loadProducts(1)">검색</button>
  <button class="btn red" id="pnew" onclick="newProduct()">+ 상품 등록</button>
  <span class="hint">가격 변경·상품 등록은 매니저 이상. 등록 상품은 /p/상품ID 페이지가 자동 생성됩니다.</span></div>
  <div id="plist" class="loading">불러오는 중…</div></section>
<section id="t-pages" style="display:none">
  <div class="panel"><h3>페이지 콘텐츠 관리 <span class="tag">저장 즉시 사이트 반영 · 재배포에도 유지</span></h3>
  <div class="hint" style="margin-bottom:10px">편집 내용은 데이터베이스에 저장되어 원본 파일과 별도로 보존됩니다. [원본 복원]으로 언제든 되돌릴 수 있고, 저장할 때마다 직전 버전이 이력(최근 10개)에 남습니다.</div>
  <div id="pglist" class="loading">불러오는 중…</div></div></section>
<section id="t-cust" style="display:none">
  <div class="toolbar"><button class="btn sm" id="cm1" onclick="custMode('buyers')">구매 고객</button>
  <button class="btn sm ghost" id="cm2" onclick="custMode('members')">가입 회원</button></div>
  <div class="toolbar"><input id="cq" placeholder="이름 · 전화" style="width:180px">
  <select id="cg"><option value="">등급 전체</option><option>VIP</option><option>GOLD</option><option>WELCOME</option><option>BLACK</option></select>
  <label style="font-size:12px"><input type="checkbox" id="cmk"> 마케팅 동의만</label>
  <button class="btn" onclick="loadCust(1)">검색</button>
  <button class="btn ghost" onclick="syncCust()">주문에서 동기화</button>
  <button class="btn ghost" onclick="location.href='/admin/api/customers.csv'" id="ccsv">CSV</button></div>
  <div id="clist" class="loading">먼저 [주문에서 동기화]를 눌러 고객을 생성하세요.</div></section>
<section id="t-notify" style="display:none">
  <div id="nconf"></div>
  <div class="panel"><h3>메시지 템플릿 <span class="tag">#{이름} #{주문번호} #{송장} #{금액} #{상품}</span></h3>
  <div id="tpls" class="loading">불러오는 중…</div>
  <div class="toolbar" style="margin-top:10px"><button class="btn" onclick="editTpl()" id="tpladd">+ 템플릿 추가</button></div></div>
  <div class="panel"><h3>발송 기록 (최근 200)</h3><div id="nlog" class="loading">불러오는 중…</div></div></section>
<section id="t-admins" style="display:none">
  <div class="panel"><h3>관리자 계정</h3><div id="alist" class="loading">불러오는 중…</div>
  <div class="toolbar" style="margin-top:12px"><input id="aname" placeholder="이름 (예: 김스태프)" style="width:130px">
  <input id="auser" placeholder="아이디 (영문/숫자)" style="width:150px">
  <select id="arole"><option value="MANAGER">매니저 — 환불·가격·고객등급 가능</option><option value="STAFF">스태프 — 주문처리·재고·발송</option><option value="VIEWER">조회전용</option><option value="OWNER">대표(전체)</option></select>
  <button class="btn" onclick="createAdmin()">계정 발급</button></div>
  <div class="hint">계정 발급 시 임시 비밀번호가 한 번만 표시됩니다 — 전달 후 본인이 우측 상단 [비밀번호]에서 변경하도록 안내하세요. 비밀번호 분실 시 [비밀번호 재설정]으로 새 임시 비밀번호를 발급합니다. 마스터 토큰(Render의 ADMIN_TOKEN)은 비상용이며 로그인 화면의 '비상 접속'에서만 사용합니다.</div></div>
  <div class="panel"><h3>감사 로그 <span class="tag">누가 · 언제 · 무엇을</span></h3><div id="audit" class="loading">불러오는 중…</div></div></section>
<section id="t-system" style="display:none"><div id="sys" class="loading">불러오는 중…</div></section>
</main>
<div class="modal-bg" id="mbg"><div class="modal" id="mbox"></div></div>
<div id="toast"></div>
<script>
const ACTOR = __ACTOR__;
const RANK={VIEWER:0,STAFF:1,MANAGER:2,OWNER:3};
const can=l=>RANK[ACTOR.role]>=l;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>'₩'+Number(n||0).toLocaleString('ko-KR');
const FF={NEW:'신규',PREPARING:'상품준비중',SHIPPED:'발송완료',DONE:'배송완료',CANCELLED:'취소'};
const RN={OWNER:'대표(전체)',MANAGER:'매니저',STAFF:'스태프',VIEWER:'조회전용'};
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2400)}
async function api(p,opt){const r=await fetch(p,opt);if(!r.ok){let m='오류';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
$('#who').textContent=ACTOR.name+' · '+RN[ACTOR.role];
if(ACTOR.master){const b=$('#pwbtn');if(b)b.style.display='none'}
const TABS=[['dash','대시보드',0],['orders','주문',0],['products','상품·재고',0],['pages','페이지',2],['cust','고객',0],['notify','알림',0],['admins','관리자',3],['system','시스템',0]];
const LOAD={dash:loadDash,orders:()=>loadOrders(1),products:()=>loadProducts(1),pages:loadPages,cust:()=>loadCust(1),notify:loadNotify,admins:loadAdmins,system:loadSys};
TABS.filter(t=>can(t[2])).forEach(([k,label],i)=>{const b=document.createElement('button');b.textContent=label;if(i===0)b.className='on';
 b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 TABS.forEach(([t])=>{const s=$('#t-'+t);if(s)s.style.display=(t===k?'':'none')});LOAD[k]()};$('#nav').appendChild(b)});
if(!can(2)){const e=document.getElementById('pnew');if(e)e.style.display='none'}
if(!can(1)){['csvbtn','tpladd','ccsv'].forEach(id=>{const e=document.getElementById(id);if(e)e.style.display='none'})}

async function loadDash(){try{const d=await api('/admin/api/summary');
 const mx=Math.max(1,...d.series.map(s=>s.v));
 $('#t-dash').innerHTML=`<div class="cards">
 <div class="card"><div class="k">오늘 매출</div><div class="v">${won(d.today.sum)}</div><div class="s">${d.today.cnt}건</div></div>
 <div class="card"><div class="k">최근 7일</div><div class="v">${won(d.week.sum)}</div><div class="s">${d.week.cnt}건</div></div>
 <div class="card"><div class="k">최근 30일</div><div class="v">${won(d.month.sum)}</div><div class="s">${d.month.cnt}건 · 객단가 ${won(d.aov)}</div></div>
 <div class="card"><div class="k">누적 결제액</div><div class="v">${won(d.all.paid_sum)}</div><div class="s">전체 ${d.all.cnt}건 · 고객 ${d.customers}명</div></div>
 <div class="card ${d.pending_ship?'alert':''}"><div class="k">발송 대기</div><div class="v">${d.pending_ship}건</div><div class="s">결제완료 · 미발송</div></div></div>
 <div class="panel"><h3>최근 30일 일별 매출 <span class="tag">PAID</span></h3>
 <div class="chart">${d.series.map(s=>`<div class="bar" style="height:${Math.round(s.v/mx*100)}%" title="${s.d} · ${won(s.v)}"></div>`).join('')}</div>
 <div class="chart-x"><span>${d.series[0].d.slice(5)}</span><span>${d.series[14].d.slice(5)}</span><span>${d.series[29].d.slice(5)}</span></div></div>
 <div class="grid2"><div class="panel"><h3>품목 TOP 10 (30일)</h3><table><tr><th>상품</th><th class="right">수량</th><th class="right">매출</th></tr>
 ${d.top.map(t=>`<tr><td>${esc(t.name)}</td><td class="right mono">${t.qty}</td><td class="right mono">${won(t.rev)}</td></tr>`).join('')||'<tr><td colspan=3 class="loading">없음</td></tr>'}</table></div>
 <div class="panel"><h3>재고 경고 <span class="tag">품절 · ≤5</span></h3><table><tr><th>상품</th><th class="right">재고</th></tr>
 ${d.low_stock.map(l=>`<tr><td>${esc(l.name)}</td><td class="right mono" style="color:${l.soldout?'#c0392b':'#9a6b00'}">${l.soldout?'품절':l.stock}</td></tr>`).join('')||'<tr><td colspan=2 class="loading">없음</td></tr>'}</table></div></div>
 <div class="panel"><h3>최근 주문</h3><table><tr><th>주문번호</th><th>일시</th><th>상태</th><th class="right">금액</th><th>주문자</th></tr>
 ${d.latest.map(o=>`<tr onclick="openOrder('${esc(o.order_id)}')" style="cursor:pointer"><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc((o.created||'').slice(5,16).replace('T',' '))}</td><td><span class="st ${esc(o.status)}">${esc(o.status)}</span></td><td class="right mono">${won(o.amount)}</td><td>${esc(o.buyer_name)}</td></tr>`).join('')}</table></div>`;
}catch(e){$('#t-dash').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}

let opage=1;
async function loadOrders(p){opage=p;const q=new URLSearchParams({page:p});
 ['oq|query','ost|status','off|fulfill','ofrom|from','oto|to'].forEach(x=>{const[i,k]=x.split('|');if($('#'+i).value)q.set(k,$('#'+i).value)});
 try{const d=await api('/admin/api/orders?'+q);
 $('#olist').innerHTML=`<table><tr><th>주문번호</th><th>일시</th><th>결제</th><th>처리</th><th class="right">금액</th><th>품목</th><th>주문자</th><th>송장</th><th></th></tr>
 ${d.rows.map(o=>`<tr><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc(o.created)}</td>
 <td><span class="st ${esc(o.status)}">${esc(o.status)}</span></td><td><span class="ff ${esc(o.fulfill)}">${FF[o.fulfill]||esc(o.fulfill)}</span></td>
 <td class="right mono">${won(o.amount)}</td><td>${esc(o.items_label)}</td><td>${esc(o.buyer_name)}</td><td class="mono">${esc(o.tracking)}</td>
 <td><button class="btn sm ghost" onclick="openOrder('${esc(o.order_id)}')">상세</button></td></tr>`).join('')||'<tr><td colspan=9 class="loading">없음</td></tr>'}</table>
 ${pager(p,d,'loadOrders')}`;}catch(e){$('#olist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
const pager=(p,d,fn)=>`<div class="pager"><button class="btn sm ghost" ${p<=1?'disabled':''} onclick="${fn}(${p-1})">이전</button><span>${p} / ${Math.max(1,Math.ceil(d.total/d.size))} · 총 ${d.total}</span><button class="btn sm ghost" ${p*d.size>=d.total?'disabled':''} onclick="${fn}(${p+1})">다음</button></div>`;
function csv(){const q=new URLSearchParams();['ofrom|from','oto|to','ost|status'].forEach(x=>{const[i,k]=x.split('|');if($('#'+i).value)q.set(k,$('#'+i).value)});location.href='/admin/api/orders.csv?'+q}

let TPLCACHE=[];
async function openOrder(oid){try{const o=await api('/admin/api/orders/'+encodeURIComponent(oid));
 if(!TPLCACHE.length){try{TPLCACHE=(await api('/admin/api/notify/templates')).rows}catch(e){}}
 $('#mbox').innerHTML=`<h3>주문 ${esc(o.order_id)} <span class="st ${esc(o.status)}">${esc(o.status)}</span></h3>
 <div class="kv"><b>일시</b><span class="mono">${esc((o.created||'').slice(0,19).replace('T',' '))}</span>
 <b>금액</b><span class="mono">${won(o.amount)} ${o.method?'· '+esc(o.method):''}</span>
 <b>주문자</b><span>${esc(o.buyer.name)} · ${esc(o.buyer.phone)}</span>
 <b>주소</b><span>[${esc(o.buyer.zip)}] ${esc(o.buyer.addr)}</span>
 ${o.receipt?`<b>영수증</b><span><a href="${esc(o.receipt)}" target="_blank">토스 영수증</a></span>`:''}</div>
 <table style="margin-bottom:12px"><tr><th>품목</th><th class="right">단가</th><th class="right">수량</th></tr>
 ${o.items.map(i=>`<tr><td>${esc(i.name)}</td><td class="right mono">${i.price?won(i.price):'-'}</td><td class="right mono">${i.qty}</td></tr>`).join('')}</table>
 ${can(1)?`<div class="kv"><b>처리상태</b><span><select id="mff">${Object.entries(FF).map(([k,v])=>`<option value="${k}" ${o.fulfill===k?'selected':''}>${v}</option>`).join('')}</select></span>
 <b>송장번호</b><span><input id="mtr" value="${esc(o.tracking)}" style="width:100%"></span>
 <b>메모</b><span><input id="mmemo" value="${esc(o.admin_memo)}" style="width:100%"></span>
 <b>알림 발송</b><span style="display:flex;gap:6px"><select id="mtpl" style="flex:1">${TPLCACHE.map(t=>`<option value="${t.id}">${esc(t.name)} (${t.kind==='alimtalk'?'알림톡':'SMS'})</option>`).join('')}</select>
 <button class="btn sm" onclick="sendNotify('${esc(o.order_id)}')">발송</button></span></div>`:''}
 <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
 ${can(2)&&o.status!=='CANCELLED'?`<button class="btn red" onclick="cancelOrder('${esc(o.order_id)}',${o.can_refund})">${o.can_refund?'결제취소(환불)':'주문취소 표시'}</button>`:''}
 ${can(1)?`<button class="btn" onclick="saveFulfill('${esc(o.order_id)}')">저장</button>`:''}
 <button class="btn ghost" onclick="closeM()">닫기</button></div>
 ${o.can_refund&&can(2)?'<div class="hint">결제취소 시 토스 환불 실행 + 재고 자동 복원. 감사로그에 기록됩니다.</div>':''}`;
 $('#mbg').style.display='flex';}catch(e){toast(e.message)}}
function closeM(){$('#mbg').style.display='none'}
$('#mbg').addEventListener('click',e=>{if(e.target.id==='mbg')closeM()});
async function saveFulfill(oid){try{const f=$('#mff').value;
 await api('/admin/api/orders/'+encodeURIComponent(oid)+'/fulfill',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({fulfill:f,tracking:$('#mtr').value,memo:$('#mmemo').value})});
 toast('저장되었습니다');
 if(f==='SHIPPED'&&$('#mtr').value&&TPLCACHE.length&&confirm('발송완료 알림을 고객에게 보낼까요?')){await sendNotify(oid,true)}
 closeM();loadOrders(opage)}catch(e){toast(e.message)}}
async function sendNotify(oid,auto){try{const tid=auto?(TPLCACHE.find(t=>t.name.includes('발송'))||TPLCACHE[0]).id:$('#mtpl').value;
 const r=await api('/admin/api/notify/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order_id:oid,template:tid})});
 toast(r.dry?'기록 모드: 발송사 미설정 (로그 저장됨)':'발송 완료');}catch(e){alert(e.message)}}
async function cancelOrder(oid,refund){if(!confirm(refund?'토스 결제취소(환불)를 실행합니다. 계속할까요?':'이 주문을 취소로 표시할까요?'))return;
 const reason=prompt('취소 사유','고객 요청')||'고객 요청';
 try{const r=await api('/admin/api/orders/'+encodeURIComponent(oid)+'/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
 toast(r.refunded?'환불 완료 · 재고 복원':'취소 처리 완료');closeM();loadOrders(opage)}catch(e){alert(e.message)}}

let ppage=1;window._pk={};
async function loadProducts(p){ppage=p;const q=new URLSearchParams({page:p});
 if($('#pq').value)q.set('query',$('#pq').value);if($('#pf').value)q.set('filter',$('#pf').value);
 try{const d=await api('/admin/api/products?'+q);
 $('#plist').innerHTML=`<table><tr><th>상품 ID</th><th>상품명</th><th class="right">가격</th><th class="right">재고</th><th>품절</th><th></th></tr>
 ${d.rows.map(r=>{const k=btoa(unescape(encodeURIComponent(r.id))).replace(/=/g,'');window._pk[k]=r.id;return `<tr>
 <td class="mono" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc(r.id)}</td><td>${esc(r.name)}</td>
 <td class="right">${r.price==null?'-':(can(2)?`<input class="stockin" style="width:88px" id="pr${k}" type="number" min="0" value="${r.price}">`:won(r.price))}</td>
 <td class="right">${can(1)?`<input class="stockin" id="st${k}" type="number" min="0" value="${r.stock}">`:r.stock}</td>
 <td><input type="checkbox" id="so${k}" ${r.soldout?'checked':''} ${can(1)?`onchange="saveProd('${k}',true)"`:'disabled'}></td>
 <td style="white-space:nowrap">${can(1)?`<button class="btn sm" onclick="saveProd('${k}',false)">저장</button> `:''}<a class="btn sm ghost" style="text-decoration:none" href="/p/${encodeURIComponent(r.id)}" target="_blank">보기</a></td></tr>`}).join('')||'<tr><td colspan=6 class="loading">없음</td></tr>'}</table>
 ${pager(p,d,'loadProducts')}`;}catch(e){$('#plist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function saveProd(k,tg){const body={id:window._pk[k],soldout:document.getElementById('so'+k).checked?1:0};
 if(!tg){body.stock=Number(document.getElementById('st'+k).value);const pr=document.getElementById('pr'+k);if(pr)body.price=Number(pr.value)}
 try{await api('/admin/api/products/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('반영되었습니다')}catch(e){toast(e.message);loadProducts(ppage)}}

let CMODE='buyers';
function custMode(m){CMODE=m;$('#cm1').className='btn sm'+(m==='buyers'?'':' ghost');$('#cm2').className='btn sm'+(m==='members'?'':' ghost');
 document.querySelectorAll('#t-cust .toolbar')[1].style.display=(m==='buyers'?'':'none');
 if(m==='buyers')loadCust(1);else loadMembers()}
async function loadMembers(){try{const d=await api('/admin/api/members');
 $('#clist').innerHTML=`<div class="hint" style="margin-bottom:8px">소셜 계정(Google/Apple)으로 가입한 회원 목록입니다. 총 ${d.total}명.</div>
 <table><tr><th>이름</th><th>이메일</th><th>가입방법</th><th>가입일시</th></tr>
 ${d.rows.map(m=>`<tr><td>${esc(m.name)||'-'}</td><td class="mono">${esc(m.email)||'-'}</td>
 <td>${m.provider==='google'?'Google':m.provider==='apple'?'Apple':m.provider==='email'?'이메일':esc(m.provider)}</td><td class="mono">${esc(m.created)}</td></tr>`).join('')||'<tr><td colspan=4 class="loading">가입 회원 없음 — 사이트의 /account 에서 가입할 수 있습니다</td></tr>'}</table>`;
 }catch(e){$('#clist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
let cpage=1;
async function syncCust(){try{const r=await api('/admin/api/customers/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 toast(`동기화 완료 — 신규 ${r.created} · 갱신 ${r.updated}`);loadCust(1)}catch(e){toast(e.message)}}
async function loadCust(p){cpage=p;const q=new URLSearchParams({page:p});
 if($('#cq').value)q.set('query',$('#cq').value);if($('#cg').value)q.set('grade',$('#cg').value);if($('#cmk').checked)q.set('mk','1');
 try{const d=await api('/admin/api/customers?'+q);
 $('#clist').innerHTML=`<table><tr><th>고객</th><th>연락처</th><th>등급</th><th class="right">주문</th><th class="right">총구매</th><th>최근주문</th><th>마케팅</th><th></th></tr>
 ${d.rows.map(c=>`<tr><td>${esc(c.name)||'-'}</td><td class="mono">${esc(c.raw)}</td><td><span class="gr ${esc(c.grade)}">${esc(c.grade)}</span></td>
 <td class="right mono">${c.cnt}</td><td class="right mono">${won(c.spend)}</td><td class="mono">${esc(c.last)}</td>
 <td>${c.mk?'동의':'-'}</td><td><button class="btn sm ghost" onclick="openCust('${esc(c.phone)}')">상세</button></td></tr>`).join('')||'<tr><td colspan=8 class="loading">고객 없음 — [주문에서 동기화]를 눌러주세요</td></tr>'}</table>
 ${pager(p,d,'loadCust')}`;}catch(e){$('#clist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function openCust(ph){try{const c=await api('/admin/api/customers/'+ph);
 if(!TPLCACHE.length){try{TPLCACHE=(await api('/admin/api/notify/templates')).rows}catch(e){}}
 $('#mbox').innerHTML=`<h3>${esc(c.name)||'고객'} <span class="gr ${esc(c.grade)}">${esc(c.grade)}</span></h3>
 <div class="kv"><b>연락처</b><span class="mono">${esc(c.raw)}</span><b>주소</b><span>[${esc(c.zip)}] ${esc(c.addr)}</span>
 <b>구매</b><span>${c.cnt}회 · ${won(c.spend)} (첫 ${esc(c.first)} ~ 최근 ${esc(c.last)})</span>
 ${can(2)?`<b>등급</b><span><select id="cgr">${['WELCOME','GOLD','VIP','BLACK'].map(g=>`<option ${c.grade===g?'selected':''}>${g}</option>`).join('')}</select> ${c.grade_manual?'<span class="hint">(수동 지정됨)</span>':''}</span>`:''}
 ${can(1)?`<b>마케팅</b><span><label><input type="checkbox" id="cmk2" ${c.mk?'checked':''}> 수신 동의</label></span>
 <b>메모</b><span><textarea id="cmemo" rows="2" style="width:100%">${esc(c.memo)}</textarea></span>
 <b>알림 발송</b><span style="display:flex;gap:6px"><select id="ctpl" style="flex:1">${TPLCACHE.map(t=>`<option value="${t.id}">${esc(t.name)} (${t.kind==='alimtalk'?'알림톡':'SMS'})</option>`).join('')}</select>
 <button class="btn sm" onclick="custNotify('${esc(c.phone)}','${esc(c.name)}')">발송</button></span>`:''}</div>
 <table style="margin-bottom:12px"><tr><th>주문번호</th><th>일시</th><th>상태</th><th class="right">금액</th><th>품목</th></tr>
 ${c.orders.map(o=>`<tr><td class="mono">${esc(o.order_id)}</td><td class="mono">${esc(o.created)}</td><td><span class="st ${esc(o.status)}">${esc(o.status)}</span></td><td class="right mono">${won(o.amount)}</td><td>${esc(o.label)}</td></tr>`).join('')}</table>
 <div style="display:flex;gap:8px;justify-content:flex-end">${can(1)?`<button class="btn" onclick="saveCust('${esc(c.phone)}')">저장</button>`:''}<button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 $('#mbg').style.display='flex';}catch(e){toast(e.message)}}
async function saveCust(ph){const body={phone:ph,memo:$('#cmemo')?$('#cmemo').value:undefined,mk:$('#cmk2')?($('#cmk2').checked?1:0):undefined};
 if($('#cgr'))body.grade=$('#cgr').value;
 try{await api('/admin/api/customers/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('저장되었습니다');closeM();loadCust(cpage)}catch(e){toast(e.message)}}
async function custNotify(ph,name){try{const r=await api('/admin/api/notify/send',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({phone:ph,name:name,template:$('#ctpl').value})});
 toast(r.dry?'기록 모드 (발송사 미설정)':'발송 완료')}catch(e){alert(e.message)}}

async function loadNotify(){try{const d=await api('/admin/api/notify/templates');TPLCACHE=d.rows;
 const c=d.conf;
 $('#nconf').innerHTML=`<div class="cards">
 <div class="card ${c.key?'':'alert'}"><div class="k">발송사 (솔라피)</div><div class="v" style="font-size:15px">${c.key?'연동됨':'미설정'}</div><div class="s">${c.key?'':'미설정 시 발송 대신 로그만 기록'}</div></div>
 <div class="card"><div class="k">발신번호</div><div class="v" style="font-size:15px">${c.sender?'등록됨':'-'}</div></div>
 <div class="card"><div class="k">카카오 알림톡</div><div class="v" style="font-size:15px">${c.pf?'채널 연동됨':'미연동'}</div><div class="s">${c.pf?'':'연동 전엔 SMS 템플릿 사용'}</div></div></div>`;
 $('#tpls').innerHTML=`<table><tr><th>이름</th><th>종류</th><th>내용 / 템플릿ID</th><th></th></tr>
 ${d.rows.map(t=>`<tr><td><b>${esc(t.name)}</b></td><td>${t.kind==='alimtalk'?'알림톡':'SMS'}</td>
 <td style="font-size:12px;white-space:pre-wrap">${esc(t.kind==='alimtalk'?('템플릿ID: '+(t.template_id||'(미입력)')):t.body)}</td>
 <td style="white-space:nowrap">${can(2)?`<button class="btn sm ghost" onclick='editTpl(${JSON.stringify(t).replace(/'/g,"&#39;")})'>수정</button> <button class="btn sm ghost" onclick="delTpl('${t.id}')">삭제</button>`:''}</td></tr>`).join('')}</table>`;
 const lg=await api('/admin/api/notify/log');
 $('#nlog').innerHTML=`<table><tr><th>일시</th><th>주문</th><th>수신</th><th>종류</th><th>템플릿</th><th>상태</th><th>내용</th><th>발송자</th></tr>
 ${lg.rows.map(l=>`<tr><td class="mono">${esc((l.created||'').slice(5,16).replace('T',' '))}</td><td class="mono">${esc(l.order_id||'-')}</td>
 <td class="mono">${esc(l.phone)}</td><td>${l.kind==='alimtalk'?'알림톡':'SMS'}</td><td>${esc(l.template)}</td>
 <td><span class="st ${l.status==='FAILED'?'FAILED2':esc(l.status)}">${esc(l.status)}</span></td>
 <td style="font-size:11.5px;max-width:260px">${esc(l.detail)}</td><td>${esc(l.by_admin)}</td></tr>`).join('')||'<tr><td colspan=8 class="loading">발송 기록 없음</td></tr>'}</table>`;
}catch(e){$('#tpls').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function editTpl(t){t=t||{};$('#mbox').innerHTML=`<h3>${t.id?'템플릿 수정':'템플릿 추가'}</h3>
 <div class="kv"><b>이름</b><span><input id="tn" value="${esc(t.name||'')}" style="width:100%"></span>
 <b>종류</b><span><select id="tk"><option value="sms" ${t.kind!=='alimtalk'?'selected':''}>SMS/LMS (즉시 사용 가능)</option><option value="alimtalk" ${t.kind==='alimtalk'?'selected':''}>카카오 알림톡 (승인 템플릿 필요)</option></select></span>
 <b>템플릿ID</b><span><input id="tt" value="${esc(t.template_id||'')}" placeholder="알림톡일 때만 — 솔라피에서 승인받은 ID" style="width:100%"></span>
 <b>내용</b><span><textarea id="tb" rows="5" style="width:100%" placeholder="#{이름} #{주문번호} #{송장} #{금액} #{상품} 변수 사용 가능">${esc(t.body||'')}</textarea></span></div>
 <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" onclick="saveTpl('${t.id||''}')">저장</button><button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 $('#mbg').style.display='flex'}
async function saveTpl(id){try{await api('/admin/api/notify/templates/save',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({id:id||undefined,name:$('#tn').value,kind:$('#tk').value,template_id:$('#tt').value,body:$('#tb').value})});
 toast('저장되었습니다');closeM();loadNotify()}catch(e){toast(e.message)}}
async function delTpl(id){if(!confirm('이 템플릿을 삭제할까요?'))return;
 try{await api('/admin/api/notify/templates/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});toast('삭제되었습니다');loadNotify()}catch(e){toast(e.message)}}

async function loadAdmins(){try{const d=await api('/admin/api/admins');
 $('#alist').innerHTML=`<table><tr><th>이름</th><th>아이디</th><th>역할</th><th>상태</th><th>발급일</th><th></th></tr>
 ${d.rows.map(r=>`<tr><td><b>${esc(r.name)}</b></td><td class="mono">${esc(r.username)||'<span style="color:#bbb">(구 토큰계정)</span>'}</td><td>${RN[r.role]||esc(r.role)}</td>
 <td>${r.active?'<span class="st PAID">활성</span>':'<span class="st CANCELLED">비활성</span>'}</td><td class="mono">${esc(r.created)}</td>
 <td><button class="btn sm ghost" onclick="toggleAdmin('${r.id}')">${r.active?'비활성화':'활성화'}</button>
 <button class="btn sm ghost" onclick="resetPw('${r.id}')">비밀번호 재설정</button></td></tr>`).join('')||'<tr><td colspan=6 class="loading">발급된 계정 없음</td></tr>'}</table>`;
 const au=await api('/admin/api/audit');
 $('#audit').innerHTML=`<table><tr><th>일시</th><th>관리자</th><th>역할</th><th>작업</th><th>대상</th><th>내용</th></tr>
 ${au.rows.map(l=>`<tr><td class="mono">${esc((l.created||'').slice(5,16).replace('T',' '))}</td><td>${esc(l.actor)}</td><td>${RN[l.role]||esc(l.role)}</td><td><b>${esc(l.action)}</b></td><td class="mono" style="font-size:11px">${esc(l.target)}</td><td style="font-size:12px">${esc(l.detail)}</td></tr>`).join('')||'<tr><td colspan=6 class="loading">기록 없음</td></tr>'}</table>`;
}catch(e){$('#alist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function createAdmin(){const name=$('#aname').value.trim(),uname=$('#auser').value.trim();
 if(!name||!uname)return toast('이름과 아이디를 입력하세요');
 try{const r=await api('/admin/api/admins/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,username:uname,role:$('#arole').value})});
 $('#mbox').innerHTML=`<h3>계정 발급 완료 — ${esc(name)}</h3><p>아래 정보를 <b>지금 복사해 전달</b>하세요. 임시 비밀번호는 다시 볼 수 없습니다 (재설정만 가능).</p>
 <div class="tokenbox">접속: https://mapdal.kr/admin/dashboard<br>아이디: ${esc(r.username)}<br>임시 비밀번호: ${esc(r.temp_password)}</div>
 <div class="hint">전달받은 직원은 로그인 후 우측 상단 [비밀번호]에서 즉시 변경하도록 안내하세요.</div>
 <div style="text-align:right;margin-top:10px"><button class="btn" onclick="closeM();loadAdmins()">확인</button></div>`;
 $('#mbg').style.display='flex';$('#aname').value='';$('#auser').value=''}catch(e){toast(e.message)}}
async function toggleAdmin(id){try{await api('/admin/api/admins/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadAdmins()}catch(e){toast(e.message)}}
async function resetPw(id){if(!confirm('임시 비밀번호를 새로 발급합니다. 기존 비밀번호와 로그인 세션은 즉시 무효화됩니다. 계속할까요?'))return;
 try{const r=await api('/admin/api/admins/resetpw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
 $('#mbox').innerHTML=`<h3>비밀번호 재설정 완료</h3><div class="tokenbox">아이디: ${esc(r.username)}<br>임시 비밀번호: ${esc(r.temp_password)}</div>
 <div style="text-align:right;margin-top:10px"><button class="btn" onclick="closeM()">확인</button></div>`;$('#mbg').style.display='flex'}catch(e){toast(e.message)}}
async function logout(){try{await fetch('/admin/api/logout',{method:'POST'})}catch(e){}location.reload()}
function pwModal(){$('#mbox').innerHTML=`<h3>비밀번호 변경</h3>
 <div class="kv"><b>현재</b><span><input id="pw0" type="password" style="width:100%"></span>
 <b>새 비밀번호</b><span><input id="pw1" type="password" style="width:100%" placeholder="8자 이상"></span>
 <b>확인</b><span><input id="pw2" type="password" style="width:100%"></span></div>
 <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" onclick="savePw()">변경</button><button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 $('#mbg').style.display='flex'}
async function savePw(){if($('#pw1').value!==$('#pw2').value)return toast('새 비밀번호가 서로 다릅니다');
 try{await api('/admin/api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old:$('#pw0').value,new:$('#pw1').value})});
 toast('변경되었습니다');closeM()}catch(e){toast(e.message)}}
function newProduct(){$('#mbox').innerHTML=`<h3>신규 상품 등록</h3>
 <div class="kv"><b>상품명 *</b><span><input id="npn" style="width:100%" placeholder="예: 맵달 굿즈 키링"></span>
 <b>가격(원) *</b><span><input id="npp" type="number" min="0" style="width:100%" placeholder="12900"></span>
 <b>초기 재고 *</b><span><input id="nps" type="number" min="0" style="width:100%" placeholder="100"></span>
 <b>이미지 URL</b><span><input id="npi" style="width:100%" placeholder="https:// 로 시작하는 이미지 주소 (선택)"></span>
 <b>상품 설명</b><span><textarea id="npd" rows="4" style="width:100%" placeholder="상세 설명 (선택)"></textarea></span></div>
 <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" onclick="saveNewProduct()">등록</button><button class="btn ghost" onclick="closeM()">닫기</button></div>
 <div class="hint">등록 즉시 결제 검증 대상이 되며 전용 페이지가 만들어집니다. SHOP 목록 노출은 [페이지] 탭에서 shop.html에 추가하거나 별도로 요청하세요.</div>`;
 $('#mbg').style.display='flex'}
async function saveNewProduct(){try{const r=await api('/admin/api/products/create',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({name:$('#npn').value,price:Number($('#npp').value||0),stock:Number($('#nps').value||0),img:$('#npi').value,descr:$('#npd').value})});
 $('#mbox').innerHTML=`<h3>등록 완료</h3><p>상품 페이지가 생성되었습니다.</p><div class="tokenbox">https://mapdal.kr${r.url}</div>
 <div style="display:flex;gap:8px;justify-content:flex-end"><a class="btn" style="text-decoration:none" href="${r.url}" target="_blank">페이지 열기</a><button class="btn ghost" onclick="closeM();loadProducts(1)">닫기</button></div>`;
 }catch(e){toast(e.message)}}

async function loadPages(){try{const d=await api('/admin/api/pages');
 $('#pglist').innerHTML=`<table><tr><th>페이지</th><th>상태</th><th>마지막 수정</th><th></th></tr>
 ${d.rows.map(p=>`<tr><td class="mono">${esc(p.path)}</td>
 <td>${p.edited?'<span class="ff SHIPPED">수정됨</span>':'<span class="ff DONE">원본</span>'}</td>
 <td class="mono" style="font-size:11px">${p.edited?esc((p.updated||'').slice(0,16).replace('T',' '))+' · '+esc(p.by):'-'}</td>
 <td style="white-space:nowrap"><button class="btn sm" onclick="editPage('${esc(p.path)}')">편집</button>
 <a class="btn sm ghost" style="text-decoration:none" href="/${esc(p.path)}" target="_blank">미리보기</a>
 ${p.edited?`<button class="btn sm ghost" onclick="revertPage('${esc(p.path)}')">원본 복원</button>
 <button class="btn sm ghost" onclick="histPage('${esc(p.path)}')">이력</button>`:''}</td></tr>`).join('')}</table>`;
 }catch(e){$('#pglist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function editPage(path){try{const d=await api('/admin/api/pages/get?path='+encodeURIComponent(path));
 $('#mbox').innerHTML=`<h3>편집 — ${esc(path)} ${d.edited?'<span class="ff SHIPPED">수정본</span>':''}</h3>
 <div class="toolbar"><input id="pgf" placeholder="찾을 문구" style="flex:1"><input id="pgr" placeholder="바꿀 문구" style="flex:1">
 <button class="btn sm ghost" onclick="pgReplace()">모두 바꾸기</button></div>
 <textarea id="pghtml" rows="20" style="width:100%;font-family:'IBM Plex Mono';font-size:11.5px;line-height:1.5"></textarea>
 <div class="hint">텍스트·가격·문구를 수정한 뒤 저장하세요. 태그(&lt; &gt;) 구조를 깨면 화면이 어긋날 수 있으니, 저장 후 [미리보기]로 꼭 확인하세요. 문제가 생기면 [원본 복원] 또는 [이력]으로 즉시 되돌릴 수 있습니다.</div>
 <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
 <button class="btn" onclick="savePage('${esc(path)}')">저장 (사이트 즉시 반영)</button>
 <button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 document.getElementById('pghtml').value=d.html;
 $('#mbg').style.display='flex'}catch(e){toast(e.message)}}
function pgReplace(){const f=$('#pgf').value;if(!f)return toast('찾을 문구를 입력하세요');
 const t=document.getElementById('pghtml');const n=t.value.split(f).length-1;
 t.value=t.value.split(f).join($('#pgr').value);toast(n+'곳을 바꿨습니다 (저장 전)')}
async function savePage(path){try{await api('/admin/api/pages/save',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({path,html:document.getElementById('pghtml').value})});
 toast('저장 완료 — 사이트에 반영되었습니다');closeM();loadPages()}catch(e){toast(e.message)}}
async function revertPage(path){if(!confirm(path+' 를 원본(배포 파일)으로 되돌릴까요?'))return;
 try{await api('/admin/api/pages/revert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
 toast('원본으로 복원되었습니다');loadPages()}catch(e){toast(e.message)}}
async function histPage(path){try{const d=await api('/admin/api/pages/history?path='+encodeURIComponent(path));
 $('#mbox').innerHTML=`<h3>수정 이력 — ${esc(path)}</h3><table><tr><th>저장 시각</th><th>작성자</th><th class="right">크기</th><th></th></tr>
 ${d.rows.map(h=>`<tr><td class="mono">${esc((h.saved||'').slice(0,19).replace('T',' '))}</td><td>${esc(h.by)}</td>
 <td class="right mono">${(h.size/1000).toFixed(1)}KB</td><td><button class="btn sm ghost" onclick="restorePage('${h.id}')">이 버전으로 복원</button></td></tr>`).join('')||'<tr><td colspan=4 class="loading">이력 없음</td></tr>'}</table>
 <div style="text-align:right;margin-top:10px"><button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 $('#mbg').style.display='flex'}catch(e){toast(e.message)}}
async function restorePage(id){try{await api('/admin/api/pages/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
 toast('해당 버전으로 복원되었습니다');closeM();loadPages()}catch(e){toast(e.message)}}

async function loadSys(){try{const s=await api('/admin/api/system');
 $('#sys').innerHTML=`<div class="cards">
 <div class="card"><div class="k">데이터베이스</div><div class="v" style="font-size:16px">${esc(s.db)}</div><div class="s">${s.db_ok?'정상':'연결 오류'}</div></div>
 <div class="card"><div class="k">주문 / 상품 / 고객</div><div class="v" style="font-size:16px">${s.orders} / ${s.products.toLocaleString()} / ${s.customers}</div></div>
 <div class="card ${s.toss_mode.includes('테스트')?'alert':''}"><div class="k">토스 결제</div><div class="v" style="font-size:15px">${esc(s.toss_mode)}</div></div>
 <div class="card"><div class="k">알림 발송사</div><div class="v" style="font-size:14px">${esc(s.solapi)}</div></div>
 <div class="card"><div class="k">서버시각 (KST)</div><div class="v" style="font-size:15px">${esc(s.time_kst)}</div></div></div>
 <div class="panel"><h3>운영 체크리스트</h3><div style="line-height:2">
 결제키 컬럼: <b class="mono">${esc(s.paykey_col)}</b><br>
 알림톡 실발송 준비: 솔라피 가입 → 발신번호 등록 → 카카오 채널 연동(pfId) → 템플릿 심사 → Render 환경변수 4종 입력.<br>
 정식 오픈 전: PG 가맹 심사 → 라이브 키 교체 → 약관·개인정보처리방침 게시 → 통신판매업 신고번호 표기.</div></div>`;
}catch(e){$('#sys').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
loadDash();
</script></body></html>'''

# ═══════════════════ ④ 페이지 콘텐츠 관리(CMS) ═══════════════════════════
STATIC_DIR = os.path.join(BASE, 'static')
_PAGE_RE = re.compile(r'^[A-Za-z0-9._-]+\.html$')

def _page_effective(path):
    ov = one('SELECT html, updated FROM page_edits WHERE path=?', (path,))
    if ov: return ov['html'], True, ov.get('updated')
    fp = os.path.join(STATIC_DIR, path)
    if os.path.isfile(fp):
        return open(fp, encoding='utf-8', errors='replace').read(), False, None
    return None, False, None

@admin_router.get('/admin/api/pages')
def api_pages(request: Request):
    a = get_actor(request); need(a, 0)
    edits = {r['path']: r for r in rows('SELECT path, updated, by_admin FROM page_edits')}
    out = []
    try: files = sorted(f for f in os.listdir(STATIC_DIR) if f.endswith('.html'))
    except Exception: files = []
    for f in files:
        e = edits.get(f)
        try: size = os.path.getsize(os.path.join(STATIC_DIR, f))
        except Exception: size = 0
        out.append({'path': f, 'size': size, 'edited': bool(e),
                    'updated': (e or {}).get('updated', ''), 'by': (e or {}).get('by_admin', '')})
    for p, e in edits.items():  # 원본 파일이 없어졌어도 편집본은 노출
        if p not in files:
            out.append({'path': p, 'size': 0, 'edited': True, 'updated': e.get('updated', ''), 'by': e.get('by_admin', '')})
    return {'rows': out}

@admin_router.get('/admin/api/pages/get')
def api_page_get(request: Request):
    a = get_actor(request); need(a, 0)
    path = request.query_params.get('path', '')
    if not _PAGE_RE.fullmatch(path): raise HTTPException(400, '잘못된 페이지 경로')
    html, edited, updated = _page_effective(path)
    if html is None: raise HTTPException(404, '페이지 없음')
    return {'path': path, 'html': html, 'edited': edited, 'updated': updated}

@admin_router.post('/admin/api/pages/save')
def api_page_save(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '페이지 편집')
    path = body.get('path', ''); html = body.get('html', '')
    if not _PAGE_RE.fullmatch(path): raise HTTPException(400, '잘못된 페이지 경로')
    if not html.strip(): raise HTTPException(400, '내용이 비어 있습니다')
    if len(html) > 2000000: raise HTTPException(400, '2MB를 초과했습니다')
    cur, _, _ = _page_effective(path)
    if cur is not None:  # 저장 직전 버전을 이력으로 보관 (페이지당 최근 10개)
        run('INSERT INTO page_history VALUES(?,?,?,?,?)',
            (uid(), path, cur, datetime.datetime.utcnow().isoformat(), a['name']))
        old = rows('SELECT id FROM page_history WHERE path=? ORDER BY saved DESC', (path,))[10:]
        for r in old: run('DELETE FROM page_history WHERE id=?', (r['id'],))
    run('DELETE FROM page_edits WHERE path=?', (path,))
    run('INSERT INTO page_edits VALUES(?,?,?,?)', (path, html, now_iso(), a['name']))
    audit(a, '페이지수정', path, '%d자' % len(html))
    return {'ok': True}

@admin_router.post('/admin/api/pages/revert')
def api_page_revert(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '페이지 편집')
    path = body.get('path', '')
    n = run('DELETE FROM page_edits WHERE path=?', (path,))
    if not n: raise HTTPException(404, '편집본이 없습니다')
    audit(a, '페이지원본복원', path, '')
    return {'ok': True}

@admin_router.get('/admin/api/pages/history')
def api_page_history(request: Request):
    a = get_actor(request); need(a, 0)
    path = request.query_params.get('path', '')
    rs = rows('SELECT id, saved, by_admin, LENGTH(html) AS sz FROM page_history WHERE path=? ORDER BY saved DESC LIMIT 10', (path,))
    return {'rows': [{'id': r['id'], 'saved': r['saved'], 'by': r['by_admin'], 'size': num(r['sz'])} for r in rs]}

@admin_router.post('/admin/api/pages/restore')
def api_page_restore(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '페이지 편집')
    h = one('SELECT * FROM page_history WHERE id=?', (body.get('id'),))
    if not h: raise HTTPException(404, '이력 없음')
    run('DELETE FROM page_edits WHERE path=?', (h['path'],))
    run('INSERT INTO page_edits VALUES(?,?,?,?)', (h['path'], h['html'], now_iso(), a['name']))
    audit(a, '페이지이력복원', h['path'], h['saved'])
    return {'ok': True}

# ═══════════════════ ⑤ 신규 상품 등록 + 자동 상품페이지(/p/ID) ═══════════
@admin_router.post('/admin/api/products/create')
def api_product_create(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '상품 등록')
    if not _state['pcols'] or not _state['pname']:
        raise HTTPException(400, '상품 테이블이 준비되지 않았습니다')
    name = (body.get('name') or '').strip()
    price = num(body.get('price')); stock = num(body.get('stock'))
    if not name: raise HTTPException(400, '상품명을 입력하세요')
    if price < 0 or stock < 0: raise HTTPException(400, '가격/재고는 0 이상')
    pid = 'mp::' + uid()
    cols, vals = ['id', _state['pname'], 'stock', 'soldout'], [pid, name, stock, 1 if stock == 0 else 0]
    if _state['pprice']: cols.append(_state['pprice']); vals.append(price)
    for c, k in (('img', 'img'), ('descr', 'descr')):
        if c in _state['pcols']: cols.append(c); vals.append((body.get(k) or '').strip()[:4000])
    run('INSERT INTO products(%s) VALUES(%s)' % (','.join(cols), ','.join(['?'] * len(vals))), tuple(vals))
    audit(a, '상품등록', pid, '%s / %s원 / 재고 %d' % (name, format(price, ','), stock))
    return {'ok': True, 'id': pid, 'url': '/p/' + pid}

_PDP_HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%(name)s — MAPDAL SEOUL</title>
<meta property="og:title" content="%(name)s"><meta property="og:description" content="MAPDAL SEOUL — Shop Seongsu, from Anywhere">%(og)s
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--paper);color:var(--black)}
header{background:var(--black);color:#fff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center}
header a{color:#fff;text-decoration:none;font-family:'Black Han Sans';font-size:19px}header a span{color:var(--red)}
header .shop{font-size:12px;font-weight:700;background:var(--red);padding:7px 14px}
main{max-width:860px;margin:0 auto;padding:28px 18px 80px}
.wrap{display:grid;grid-template-columns:1fr 1fr;gap:28px;background:#fff;border:1px solid #e3e1db;padding:26px}
@media(max-width:720px){.wrap{grid-template-columns:1fr}}
.ph{background:#f0efe9;min-height:280px;display:flex;align-items:center;justify-content:center;color:#bbb;font-family:'IBM Plex Mono';font-size:12px;overflow:hidden}
.ph img{width:100%%;height:100%%;object-fit:cover}
h1{font-size:22px;line-height:1.35;margin-bottom:10px}
.price{font-family:'IBM Plex Mono';font-size:26px;font-weight:600;margin:12px 0 4px}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:3px 10px;margin-bottom:14px}
.ok{background:#e9f7ee;color:#0a7d38}.no{background:#fff2f1;color:#c0392b}
.desc{font-size:14px;line-height:1.8;color:#444;white-space:pre-wrap;border-top:1px solid #eee;margin-top:16px;padding-top:16px}
.cta{display:block;text-align:center;background:var(--black);color:var(--amber);font-weight:700;padding:14px;margin-top:20px;text-decoration:none}
.foot{font-family:'IBM Plex Mono';font-size:10px;color:#aaa;text-align:center;margin-top:26px}</style></head><body>
<header><a href="/">MAPDAL<span>SEOUL</span></a><a class="shop" href="/shop.html">SHOP 전체보기</a></header>
<main><div class="wrap"><div class="ph">%(imgtag)s</div><div>
<h1>%(name)s</h1><div class="price">₩%(price)s</div>
<span class="badge %(bcls)s">%(bmsg)s</span>
<div class="desc">%(descr)s</div>
<a class="cta" href="/shop.html">SHOP에서 주문하기</a></div></div>
<div class="foot">SHOP SEONGSU, FROM ANYWHERE · %(pid)s</div></main></body></html>'''

@admin_router.get('/p/{pid:path}', response_class=HTMLResponse)
def pdp(pid: str):
    try: ensure_ready()
    except Exception: pass
    if not _state['pcols']: raise HTTPException(404)
    sel = 'id, %s AS name, stock, soldout' % (_state['pname'] or 'id')
    if _state['pprice']: sel += ', %s AS price' % _state['pprice']
    for c in ('img', 'descr'):
        if c in _state['pcols']: sel += ', ' + c
    r = one('SELECT %s FROM products WHERE id=?' % sel, (pid,))
    if not r:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>상품을 찾을 수 없습니다</h2><a href="/shop.html">SHOP으로</a>', status_code=404)
    def h(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    soldout = num(r.get('soldout')) or num(r.get('stock')) <= 0
    img = (r.get('img') or '').strip()
    return HTMLResponse(_PDP_HTML % {
        'name': h(r.get('name')), 'price': format(num(r.get('price')), ','),
        'bcls': 'no' if soldout else 'ok',
        'bmsg': '품절 (SOLD OUT)' if soldout else '구매 가능 · 재고 %d' % num(r.get('stock')),
        'descr': h(r.get('descr')) or 'MAPDAL SEOUL 상품입니다.',
        'imgtag': ('<img src="%s" alt="">' % h(img)) if img else 'MAPDAL SEOUL',
        'og': ('<meta property="og:image" content="%s">' % h(img)) if img else '',
        'pid': h(pid)})

# ═══════════════════ ⑥ 소셜 회원가입 (Google / Apple) ════════════════════
def _burl(request: Request):
    return 'https://' + (request.headers.get('host') or 'mapdal.kr')

def member_session_make(mid):
    sid = secrets.token_urlsafe(24)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat(timespec='seconds')
    try: run('DELETE FROM member_sessions WHERE expires < ?', (now_iso(),))
    except Exception: pass
    run('INSERT INTO member_sessions VALUES(?,?,?,?)', (hashlib.sha256(sid.encode()).hexdigest(), mid, now_iso(), exp))
    return sid

def member_of(request: Request):
    sid = request.cookies.get('mp_member') or ''
    if not sid: return None
    s = one('SELECT * FROM member_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
    if not s or (s.get('expires') or '') <= now_iso(): return None
    return one('SELECT * FROM members WHERE id=?', (s['member_id'],))

def member_upsert(provider, sub, email, name):
    row = one('SELECT * FROM members WHERE provider=? AND sub=?', (provider, sub))
    if row:
        run('UPDATE members SET email=COALESCE(NULLIF(?, \'\'), email), name=COALESCE(NULLIF(?, \'\'), name) WHERE id=?',
            (email or '', name or '', row['id']))
        return row['id'], False
    mid = uid()
    run('INSERT INTO members VALUES(?,?,?,?,?,?)', (mid, provider, sub, email or '', name or '', now_iso()))
    return mid, True

def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

import urllib.parse

@admin_router.get('/auth/google')
def auth_google(request: Request):
    cid = os.environ.get('GOOGLE_CLIENT_ID', '')
    if not cid:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>Google 로그인 준비 중</h3><p>관리자가 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 환경변수를 설정하면 활성화됩니다.</p><a href="/account">돌아가기</a>')
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({'client_id': cid, 'redirect_uri': _burl(request) + '/auth/google/callback',
                                'response_type': 'code', 'scope': 'openid email profile',
                                'state': state, 'prompt': 'select_account'})
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + q, status_code=302)
    resp.set_cookie('mp_oauth', state, max_age=600, httponly=True, secure=True, samesite='none')
    return resp

@admin_router.get('/auth/google/callback')
def auth_google_cb(request: Request):
    try: ensure_ready()
    except Exception: pass
    p = request.query_params
    if not p.get('code') or p.get('state') != (request.cookies.get('mp_oauth') or '_'):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>인증 세션이 만료되었습니다</h3><a href="/account">다시 시도</a>', status_code=400)
    try:
        tok = _post_form('https://oauth2.googleapis.com/token', {
            'code': p['code'], 'client_id': os.environ.get('GOOGLE_CLIENT_ID', ''),
            'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
            'redirect_uri': _burl(request) + '/auth/google/callback', 'grant_type': 'authorization_code'})
        req = urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',
                                     headers={'Authorization': 'Bearer ' + tok.get('access_token', '')})
        with urllib.request.urlopen(req, timeout=15) as r2:
            ui = json.loads(r2.read().decode())
    except Exception:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>Google 인증에 실패했습니다</h3><p>설정(클라이언트 ID/시크릿, 리디렉션 URI)을 확인하세요.</p><a href="/account">다시 시도</a>', status_code=400)
    mid, is_new = member_upsert('google', str(ui.get('sub', '')), ui.get('email', ''), ui.get('name', ''))
    sid = member_session_make(mid)
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('/account', status_code=302)
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    resp.delete_cookie('mp_oauth')
    return resp

def _apple_conf():
    return {k: os.environ.get(k, '') for k in ('APPLE_CLIENT_ID', 'APPLE_TEAM_ID', 'APPLE_KEY_ID', 'APPLE_PRIVATE_KEY')}

@admin_router.get('/auth/apple')
def auth_apple(request: Request):
    c = _apple_conf()
    if not all(c.values()):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>Apple 로그인 준비 중</h3><p>Apple 개발자 계정 등록 후 APPLE_CLIENT_ID / APPLE_TEAM_ID / APPLE_KEY_ID / APPLE_PRIVATE_KEY 환경변수를 설정하면 활성화됩니다.</p><a href="/account">돌아가기</a>')
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({'client_id': c['APPLE_CLIENT_ID'], 'redirect_uri': _burl(request) + '/auth/apple/callback',
                                'response_type': 'code', 'response_mode': 'form_post', 'scope': 'name email', 'state': state})
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('https://appleid.apple.com/auth/authorize?' + q, status_code=302)
    resp.set_cookie('mp_oauth', state, max_age=600, httponly=True, secure=True, samesite='none')
    return resp

@admin_router.post('/auth/apple/callback')
async def auth_apple_cb(request: Request):
    try: ensure_ready()
    except Exception: pass
    form = await request.form()
    if form.get('state') != (request.cookies.get('mp_oauth') or '_') or not form.get('code'):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>인증 세션이 만료되었습니다</h3><a href="/account">다시 시도</a>', status_code=400)
    c = _apple_conf()
    try:
        import jwt
        from jwt import PyJWKClient
    except Exception:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>서버 설정 필요</h3><p>requirements.txt 에 PyJWT[crypto] 를 추가한 뒤 재배포하세요.</p>', status_code=500)
    try:
        now = int(time.time())
        client_secret = jwt.encode(
            {'iss': c['APPLE_TEAM_ID'], 'iat': now, 'exp': now + 300,
             'aud': 'https://appleid.apple.com', 'sub': c['APPLE_CLIENT_ID']},
            c['APPLE_PRIVATE_KEY'].replace('\\n', '\n'), algorithm='ES256',
            headers={'kid': c['APPLE_KEY_ID']})
        tok = _post_form('https://appleid.apple.com/auth/token', {
            'client_id': c['APPLE_CLIENT_ID'], 'client_secret': client_secret,
            'code': form.get('code'), 'grant_type': 'authorization_code',
            'redirect_uri': _burl(request) + '/auth/apple/callback'})
        key = PyJWKClient('https://appleid.apple.com/auth/keys').get_signing_key_from_jwt(tok['id_token'])
        claims = jwt.decode(tok['id_token'], key.key, algorithms=['RS256'],
                            audience=c['APPLE_CLIENT_ID'], issuer='https://appleid.apple.com')
    except Exception:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>Apple 인증에 실패했습니다</h3><p>Services ID·키·리디렉션 URI 설정을 확인하세요.</p><a href="/account">다시 시도</a>', status_code=400)
    name = ''
    if form.get('user'):
        try:
            u = json.loads(form.get('user'))
            name = ((u.get('name') or {}).get('lastName', '') + (u.get('name') or {}).get('firstName', '')).strip()
        except Exception: pass
    mid, is_new = member_upsert('apple', str(claims.get('sub', '')), claims.get('email', ''), name)
    sid = member_session_make(mid)
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('/account', status_code=302)
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    resp.delete_cookie('mp_oauth')
    return resp

@admin_router.get('/auth/logout')
def auth_logout(request: Request):
    sid = request.cookies.get('mp_member') or ''
    if sid:
        try: run('DELETE FROM member_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
        except Exception: pass
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('/account', status_code=302)
    resp.delete_cookie('mp_member')
    return resp

_ACCOUNT_CSS = '''<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--black);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.box{background:var(--paper);width:100%;max-width:420px;padding:36px 32px;border-top:6px solid var(--red)}
h1{font-family:'Black Han Sans';font-size:24px}h1 span{color:var(--red)}
.sub{font-family:'IBM Plex Mono';font-size:11px;color:#888;margin:4px 0 24px}
.sbtn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;font:inherit;font-weight:700;font-size:14.5px;border:1px solid #ccc;padding:13px;cursor:pointer;background:#fff;color:#141414;text-decoration:none;margin-top:10px}
.sbtn.apple{background:#000;color:#fff;border-color:#000}
.sbtn.off{opacity:.45;pointer-events:none}
.kv{display:grid;grid-template-columns:80px 1fr;gap:8px 10px;font-size:14px;margin:16px 0 22px}.kv b{color:#777;font-size:11.5px}
.out{display:block;text-align:center;font-size:12.5px;color:#888;margin-top:18px}
.foot{font-family:'IBM Plex Mono';font-size:10px;color:#aaa;margin-top:24px;text-align:center}</style>'''

_ACCOUNT_FORM_CSS = '''<style>
.div{display:flex;align-items:center;gap:10px;margin:20px 0 12px;color:#aaa;font-size:11px}
.div:before,.div:after{content:'';flex:1;height:1px;background:#ddd}
.tabs{display:flex;gap:0;margin-bottom:12px}
.tabs button{flex:1;font:inherit;font-weight:700;font-size:13px;padding:9px;border:1px solid #ccc;background:#fff;color:#888;cursor:pointer}
.tabs button.on{background:#141414;color:#fff;border-color:#141414}
label{display:block;font-size:11.5px;font-weight:700;color:#555;margin:10px 0 4px}
input{width:100%;font:inherit;padding:10px 11px;border:1px solid #ccc;background:#fff}
input:focus{outline:2px solid var(--red)}
.go{width:100%;margin-top:16px;font:inherit;font-weight:700;font-size:14.5px;border:0;padding:12px;cursor:pointer;background:var(--red);color:#fff}
.err{display:none;background:#fff2f1;color:#c0392b;font-size:12.5px;padding:9px 11px;margin-top:12px;border-left:3px solid var(--red)}
</style>'''

@admin_router.get('/account', response_class=HTMLResponse)
def account_page(request: Request):
    try: ensure_ready()
    except Exception: pass
    m = member_of(request)
    def h(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if m:
        pv = {'google': 'Google', 'apple': 'Apple', 'email': '이메일'}.get(m.get('provider'), m.get('provider'))
        return HTMLResponse('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>내 계정 — MAPDAL SEOUL</title>' + _ACCOUNT_CSS +
            '</head><body><div class="box"><h1>MAPDAL<span>SEOUL</span></h1><div class="sub">MY ACCOUNT</div>'
            '<div class="kv"><b>이름</b><span>%s</span><b>이메일</b><span>%s</span><b>가입방법</b><span>%s 계정</span><b>가입일</b><span>%s</span></div>'
            '<a class="sbtn" href="/shop.html">SHOP 바로가기</a><a class="sbtn" href="/">홈으로</a>'
            '<a class="out" href="/auth/logout">로그아웃</a>'
            '<div class="foot">SHOP SEONGSU, FROM ANYWHERE</div></div></body></html>'
            % (h(m.get('name')) or '회원', h(m.get('email')) or '-', pv, h((m.get('created') or '')[:10])))
    g_on = bool(os.environ.get('GOOGLE_CLIENT_ID'))
    a_on = all(_apple_conf().values())
    social = ('<a class="sbtn%s" href="/auth/google">G · Google 계정으로 계속하기%s</a>'
              '<a class="sbtn apple%s" href="/auth/apple">&#63743; · Apple 계정으로 계속하기%s</a>'
              % ('' if g_on else ' off', '' if g_on else ' (준비 중)',
                 '' if a_on else ' off', '' if a_on else ' (준비 중)'))
    return HTMLResponse('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>로그인 — MAPDAL SEOUL</title>' + _ACCOUNT_CSS + _ACCOUNT_FORM_CSS +
        '</head><body><div class="box"><h1>MAPDAL<span>SEOUL</span></h1><div class="sub">SIGN IN / SIGN UP</div>'
        + social +
        '<div class="div"><span>또는 이메일로</span></div>'
        '<div class="tabs"><button id="tbL" class="on" onclick="mode(0)">로그인</button><button id="tbS" onclick="mode(1)">회원가입</button></div>'
        '<div id="fL"><label>이메일</label><input id="le" type="email" autocomplete="email">'
        '<label>비밀번호</label><input id="lp" type="password" autocomplete="current-password">'
        '<button class="go" onclick="doLogin()">로그인</button></div>'
        '<div id="fS" style="display:none"><label>이름</label><input id="sn" autocomplete="name">'
        '<label>이메일</label><input id="se" type="email" autocomplete="email">'
        '<label>비밀번호 (8자 이상)</label><input id="sp" type="password" autocomplete="new-password">'
        '<label>비밀번호 확인</label><input id="sp2" type="password" autocomplete="new-password">'
        '<button class="go" onclick="doSignup()">가입하기</button></div>'
        '<div class="err" id="err"></div>'
        '<a class="out" href="/">홈으로 돌아가기</a>'
        '<div class="foot">SHOP SEONGSU, FROM ANYWHERE</div></div>'
        '<script>'
        'const E=document.getElementById("err");function show(m){E.textContent=m;E.style.display="block"}'
        'function mode(i){document.getElementById("fL").style.display=i?"none":"";document.getElementById("fS").style.display=i?"":"none";'
        'document.getElementById("tbL").className=i?"":"on";document.getElementById("tbS").className=i?"on":"";E.style.display="none"}'
        'async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        'if(!r.ok){let m="오류";try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}'
        'async function doLogin(){try{await post("/api/member/login",{email:document.getElementById("le").value,password:document.getElementById("lp").value});location.reload()}catch(e){show(e.message)}}'
        'async function doSignup(){const p=document.getElementById("sp").value;'
        'if(p!==document.getElementById("sp2").value)return show("비밀번호가 서로 다릅니다");'
        'try{await post("/api/member/signup",{name:document.getElementById("sn").value,email:document.getElementById("se").value,password:p});location.reload()}catch(e){show(e.message)}}'
        '</script></body></html>')

@admin_router.get('/admin/api/members')
def api_members(request: Request):
    a = get_actor(request); need(a, 0)
    rs = rows('SELECT * FROM members ORDER BY created DESC LIMIT 300')
    total = num((one('SELECT COUNT(*) AS c FROM members') or {}).get('c'))
    return {'total': total, 'rows': [{'id': r['id'], 'provider': r.get('provider'), 'email': r.get('email') or '',
            'name': r.get('name') or '', 'created': (r.get('created') or '')[:16].replace('T', ' ')} for r in rs]}

# ═══════════════ 이메일 회원가입/로그인 + 헤더 로그인 버튼 ═══════════════
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

@admin_router.get('/api/member/me')
def api_member_me(request: Request):
    try: ensure_ready()
    except Exception: pass
    m = member_of(request)
    if not m: return {'login': False}
    return {'login': True, 'name': m.get('name') or '회원', 'email': m.get('email') or '',
            'provider': m.get('provider')}

@admin_router.post('/api/member/signup')
def api_member_signup(request: Request, body: dict = Body(...)):
    try: ensure_ready()
    except Exception: pass
    name = (body.get('name') or '').strip()[:40]
    email = (body.get('email') or '').strip().lower()
    pw = body.get('password') or ''
    if not name: raise HTTPException(400, '이름을 입력하세요')
    if not _EMAIL_RE.fullmatch(email): raise HTTPException(400, '이메일 형식을 확인하세요')
    if len(pw) < 8: raise HTTPException(400, '비밀번호는 8자 이상이어야 합니다')
    if one("SELECT id FROM members WHERE provider='email' AND email=?", (email,)):
        raise HTTPException(400, '이미 가입된 이메일입니다 — 로그인해 주세요')
    mid = uid()
    run('INSERT INTO members(id,provider,sub,email,name,created,pw) VALUES(?,?,?,?,?,?,?)',
        (mid, 'email', email, email, name, now_iso(), pw_hash(pw)))
    sid = member_session_make(mid)
    resp = JSONResponse({'ok': True, 'name': name})
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    return resp

@admin_router.post('/api/member/login')
def api_member_login(request: Request, body: dict = Body(...)):
    try: ensure_ready()
    except Exception: pass
    email = (body.get('email') or '').strip().lower()
    pw = body.get('password') or ''
    if not email or not pw: raise HTTPException(400, '이메일과 비밀번호를 입력하세요')
    ip = (request.client.host if request.client else '') or '-'
    key = 'ml:' + email + ':' + ip; guard(key)
    row = one("SELECT * FROM members WHERE provider='email' AND email=?", (email,))
    if not row or not pw_verify(pw, row.get('pw') or ''):
        fail_hit(key)
        raise HTTPException(403, '이메일 또는 비밀번호가 올바르지 않습니다')
    fail_clear(key)
    sid = member_session_make(row['id'])
    resp = JSONResponse({'ok': True, 'name': row.get('name') or '회원'})
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    return resp

@admin_router.post('/api/member/logout')
def api_member_logout(request: Request):
    sid = request.cookies.get('mp_member') or ''
    if sid:
        try: run('DELETE FROM member_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
        except Exception: pass
    resp = JSONResponse({'ok': True})
    resp.delete_cookie('mp_member')
    return resp

# 전 페이지 헤더에 로그인/MY 버튼 자동 삽입 (CART 버튼 왼쪽)
AUTH_SNIPPET = r"""<script id="mpAuthJs">(function(){function go(){
fetch('/api/member/me').then(function(r){return r.json()}).catch(function(){return {login:false}}).then(function(d){
try{if(document.getElementById('mpAuth'))return;
var cart=null,els=document.querySelectorAll('a,button,div,span');
for(var i=0;i<els.length;i++){var t=(els[i].textContent||'').replace(/\s+/g,' ').trim();
if(t.length<12&&t.indexOf('CART')===0){cart=els[i];break}}
var a=document.createElement('a');a.id='mpAuth';a.href='/account';
a.textContent=(d&&d.login)?('MY \u00b7 '+(d.name||'\ud68c\uc6d0')):'\ub85c\uadf8\uc778';
a.style.cssText='font-weight:700;font-size:12px;letter-spacing:.4px;color:inherit;text-decoration:none;margin-right:14px;white-space:nowrap;display:inline-flex;align-items:center;cursor:pointer';
if(cart&&cart.parentNode){cart.parentNode.insertBefore(a,cart)}
else{a.style.cssText='position:fixed;top:12px;right:14px;z-index:99999;background:#141414;color:#FFB000;font:700 12px sans-serif;padding:7px 13px;text-decoration:none';document.body.appendChild(a)}
}catch(e){}})}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go)}else{go()}})();</script>"""

def _inject_auth(html):
    if 'mpAuthJs' in html: return html
    i = html.lower().rfind('</body>')
    return (html[:i] + AUTH_SNIPPET + html[i:]) if i >= 0 else (html + AUTH_SNIPPET)

# ═══════ 정적 서빙 대체 (편집본 우선 · 반드시 모듈 마지막 라우트) ═══════
import mimetypes

@admin_router.get('/{spath:path}')
def serve_site(spath: str):
    if not spath or spath.startswith(('admin', 'api/', 'auth/', 'p/')):
        raise HTTPException(404)
    name = os.path.basename(spath)
    if name.endswith('.html') and _PAGE_RE.fullmatch(name) and '/' not in spath:
        try:
            ensure_ready()
            ov = one('SELECT html FROM page_edits WHERE path=?', (name,))
            if ov: return HTMLResponse(_inject_auth(ov['html']))
        except Exception:
            pass
    fp = os.path.realpath(os.path.join(STATIC_DIR, spath))
    root = os.path.realpath(STATIC_DIR)
    if not fp.startswith(root + os.sep) or not os.path.isfile(fp):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>페이지를 찾을 수 없습니다</h2><a href="/">MAPDAL SEOUL 홈으로</a>', status_code=404)
    mt = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
    data = open(fp, 'rb').read()
    if mt == 'text/html':
        return HTMLResponse(_inject_auth(data.decode('utf-8', errors='replace')))
    return Response(data, media_type=mt)
