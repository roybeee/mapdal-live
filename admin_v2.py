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
from fastapi import APIRouter, HTTPException, Request, Body, UploadFile, File
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
def _genv(k):
    return (os.environ.get(k) or '').strip()

def solapi_conf():
    return {'key': _genv('SOLAPI_API_KEY'), 'sec': _genv('SOLAPI_API_SECRET'),
            'sender': _genv('SOLAPI_SENDER'), 'pf': _genv('SOLAPI_PF_ID')}

# ── 이미지 스토리지 (Cloudflare R2 / S3 호환, 미설정 시 base64 폴백) ──────
#   필요한 환경변수:
#     R2_ACCOUNT_ID   : Cloudflare 계정 ID (R2 엔드포인트 구성용)
#     R2_ACCESS_KEY   : R2 API 토큰의 Access Key ID
#     R2_SECRET_KEY   : R2 API 토큰의 Secret Access Key
#     R2_BUCKET       : 버킷 이름 (예: mapdal-assets)
#     R2_PUBLIC_URL   : 공개 베이스 URL (예: https://assets.mapdal.kr 또는 r2.dev 주소)
#   위 값이 모두 있으면 R2에 업로드하고 공개 URL을 반환.
#   하나라도 없으면 DB에 넣을 수 있는 base64 data-URI 로 폴백(임시 운영 가능).
def r2_conf():
    return {
        'account': _genv('R2_ACCOUNT_ID'),
        'key': _genv('R2_ACCESS_KEY'),
        'secret': _genv('R2_SECRET_KEY'),
        'bucket': _genv('R2_BUCKET'),
        'public': _genv('R2_PUBLIC_URL').rstrip('/'),
    }

def r2_ready():
    c = r2_conf()
    return all([c['account'], c['key'], c['secret'], c['bucket'], c['public']])

_ALLOWED_IMG = {
    'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
    'image/webp': '.webp', 'image/gif': '.gif', 'image/avif': '.avif',
}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB (브라우저에서 리사이즈 후 올리므로 여유값)

def _r2_client():
    import boto3
    from botocore.config import Config
    c = r2_conf()
    endpoint = 'https://%s.r2.cloudflarestorage.com' % c['account']
    return boto3.client(
        's3', endpoint_url=endpoint,
        aws_access_key_id=c['key'], aws_secret_access_key=c['secret'],
        config=Config(signature_version='s3v4', region_name='auto'),
    ), c

def store_image(data: bytes, content_type: str, prefix: str = 'products'):
    """이미지 바이트를 저장하고 참조 가능한 URL(또는 base64 data-URI)을 반환."""
    ct = (content_type or '').split(';')[0].strip().lower()
    ext = _ALLOWED_IMG.get(ct)
    if not ext:
        raise HTTPException(400, '지원하지 않는 이미지 형식입니다 (jpg/png/webp/gif/avif만 가능)')
    if not data:
        raise HTTPException(400, '빈 파일입니다')
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, '이미지 용량이 너무 큽니다 (최대 8MB)')
    if r2_ready():
        try:
            cli, c = _r2_client()
            key = '%s/%s/%s%s' % (prefix.strip('/'),
                                  datetime.datetime.utcnow().strftime('%Y%m'),
                                  uid() + secrets.token_hex(4), ext)
            cli.put_object(Bucket=c['bucket'], Key=key, Body=data,
                           ContentType=ct, CacheControl='public, max-age=31536000, immutable')
            return {'url': c['public'] + '/' + key, 'stored': 'r2'}
        except HTTPException:
            raise
        except Exception as e:
            # R2 설정 오류 시 폴백하지 않고 명확히 알림 (조용한 실패 방지)
            raise HTTPException(502, 'R2 업로드 실패: %s' % (str(e)[:200]))
    # 폴백: DB assets 테이블에 저장 후 /admin/asset/{id}{ext} 로 서빙
    # (R2 키 설정 전까지 임시 운영 가능 — 키를 넣으면 이후 업로드는 자동으로 R2에 저장)
    aid = secrets.token_hex(12)
    run('INSERT INTO assets(id, ctype, ext, data, created) VALUES(?,?,?,?,?)',
        (aid, ct, ext, base64.b64encode(data).decode(), now_iso()))
    return {'url': '/admin/asset/%s%s' % (aid, ext), 'stored': 'db'}

@admin_router.get('/admin/asset/{name}')
def serve_asset(name: str):
    """DB 폴백으로 저장된 이미지를 서빙 (R2 미설정 시에만 사용됨). 공개 접근 허용."""
    aid = re.sub(r'\.\w+$', '', name)
    if not re.fullmatch(r'[0-9a-f]{24}', aid):
        raise HTTPException(404, 'not found')
    r = one('SELECT ctype, data FROM assets WHERE id=?', (aid,))
    if not r:
        raise HTTPException(404, 'not found')
    raw = base64.b64decode(r['data'])
    return Response(content=raw, media_type=r.get('ctype') or 'application/octet-stream',
                    headers={'Cache-Control': 'public, max-age=31536000, immutable'})


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
        """CREATE TABLE IF NOT EXISTS member_addresses(id TEXT PRIMARY KEY, member_id TEXT, label TEXT,
           rname TEXT, phone TEXT, zip TEXT, addr1 TEXT, addr2 TEXT, is_default INTEGER DEFAULT 0, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_likes(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_restock(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT,
           phone TEXT, created TEXT, notified INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS member_requests(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT,
           rtype TEXT, reason TEXT, created TEXT, status TEXT DEFAULT '접수', admin_memo TEXT, updated TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_inquiries(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT,
           title TEXT, body TEXT, created TEXT, status TEXT DEFAULT '접수', answer TEXT, answered_at TEXT, answered_by TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_pqna(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT,
           question TEXT, created TEXT, status TEXT DEFAULT '접수', answer TEXT, answered_at TEXT, answered_by TEXT)""",
        """CREATE TABLE IF NOT EXISTS phone_verifications(id TEXT PRIMARY KEY, member_id TEXT, phone TEXT,
           code TEXT, created TEXT, expires TEXT, used INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, ctype TEXT, ext TEXT,
           data TEXT, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS k2g_removed(uid TEXT PRIMARY KEY, name TEXT,
           created TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS site_settings(key TEXT PRIMARY KEY, value TEXT,
           updated TEXT, by_admin TEXT)""",
    ):
        try: run(ddl)
        except Exception: pass
    try:
        if not one('SELECT id FROM notify_templates LIMIT 1'):
            runmany([('INSERT INTO notify_templates VALUES(?,?,?,?,?,?)',
                      (uid(), n, k, t, b, now_iso())) for n, k, t, b in SEED_TPL])
    except Exception: pass
    try:
        for pth, doc in (('privacy.html', PRIVACY_HTML), ('terms.html', TERMS_HTML)):
            if not one('SELECT path FROM page_edits WHERE path=?', (pth,)):
                run('INSERT INTO page_edits VALUES(?,?,?,?)', (pth, doc, now_iso(), '시스템'))
        for pth in ('privacy.html', 'terms.html'):
            row = one('SELECT html FROM page_edits WHERE path=?', (pth,))
            if row and ('주식회사 밀집' in (row['html'] or '') or '등록 후 표기' in (row['html'] or '')):
                fixed = (row['html']
                         .replace('주식회사 밀집(이하 "회사")은', '맵달서울성수(이하 "회사")는')
                         .replace('주식회사 밀집(이하 "회사")이 운영하는', '맵달서울성수(이하 "회사")가 운영하는')
                         .replace('주식회사 밀집', '맵달서울성수')
                         .replace('황인범 (대표이사)', '황인범 (공동대표)')
                         .replace('(문의 이메일 등록 후 표기)', 'ceo@mealzip.kr')
                         .replace('(대표번호 등록 후 표기)', '010-8176-8525')
                         .replace('(사업자등록번호 등록 후 표기)', '394-85-03267')
                         .replace('(통신판매업 신고 후 표기)', '제2026-서울성동-0426호'))
                run('UPDATE page_edits SET html=?, updated=? WHERE path=?', (fixed, now_iso(), pth))
    except Exception:
        pass
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
    for col, typ in (('pw','TEXT'),('phone','TEXT'),('phone_verified','INTEGER DEFAULT 0'),
                     ('points','INTEGER DEFAULT 0'),('bank','TEXT'),('acct','TEXT'),
                     ('acct_name','TEXT'),('fav_store','INTEGER DEFAULT 0'),
                     ('gender','TEXT'),('age_range','TEXT'),('birth','TEXT'),('ci','TEXT')):
        if mcx and col not in mcx:
            try: run("ALTER TABLE members ADD COLUMN %s %s" % (col, typ))
            except Exception: pass
    lcx = _cols('member_likes')
    for col, typ in (('page', 'TEXT'), ('pname', 'TEXT'), ('pprice', 'INTEGER'), ('pimg', 'TEXT')):
        if lcx and col not in lcx:
            try: run("ALTER TABLE member_likes ADD COLUMN %s %s" % (col, typ))
            except Exception: pass
    pcx = _cols('products')
    for col in ('img', 'descr', 'category', 'detail_html', 'gallery', 'badge'):
        if pcx and col not in pcx:
            try: run("ALTER TABLE products ADD COLUMN %s TEXT" % col)
            except Exception: pass
    for col in ('list_price', 'sort_order'):
        if pcx and col not in pcx:
            try: run("ALTER TABLE products ADD COLUMN %s INTEGER" % col)
            except Exception: pass
    pc = _cols('products')
    _state.update(ocols=oc, pcols=pc,
                  paykey=next((c for c in ('pay_key', 'payment_key', 'paykey') if c in oc), None),
                  pname=next((c for c in ('name', 'title', 'n') if c in pc), None),
                  pprice=next((c for c in ('price', 'p', 'amount') if c in pc), None), ready=True)
    try: _k2g_migrate_from_static()   # K2G 카탈로그 → DB 단일 출처 백필 (1회, 멱등)
    except Exception: pass

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
    try:
        cs = (num((one("SELECT COUNT(*) AS c FROM member_inquiries WHERE status='접수'") or {}).get('c'))
              + num((one("SELECT COUNT(*) AS c FROM member_pqna WHERE status='접수'") or {}).get('c'))
              + num((one("SELECT COUNT(*) AS c FROM member_requests WHERE status IN ('접수','처리중')") or {}).get('c')))
    except Exception:
        cs = 0
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
            'latest': latest, 'pending_cs': cs}

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
    old = one('SELECT stock, soldout FROM products WHERE id=?', (pid,)) or {}
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
    try: _k2g_cache_bust()
    except Exception: pass
    try:
        nowr = one('SELECT stock, soldout FROM products WHERE id=?', (pid,)) or {}
        was_off = num(old.get('soldout')) or num(old.get('stock')) <= 0
        now_on = (not num(nowr.get('soldout'))) and num(nowr.get('stock')) > 0
        if was_off and now_on:
            pn = (one('SELECT %s AS n FROM products WHERE id=?' % (_state['pname'] or 'id'), (pid,)) or {}).get('n') or pid
            subs = rows('SELECT * FROM member_restock WHERE product_id=? AND notified=0 LIMIT 500', (pid,))
            for sub in subs:
                if sub.get('phone'):
                    system_sms(sub['phone'], '[맵달SEOUL] 재입고 알림 — %s 상품이 다시 입고되었습니다. 서두르세요!\nhttps://mapdal.kr/p/%s' % (str(pn)[:30], pid), '재입고알림')
                run('UPDATE member_restock SET notified=1 WHERE id=?', (sub['id'],))
            if subs: audit(a, '재입고알림발송', pid, '%d명' % len(subs))
    except Exception:
        pass
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

@admin_router.post('/admin/api/notify/test')
def api_notify_test(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '실발송 테스트')
    ph = digits(body.get('phone'))
    if len(ph) < 10: raise HTTPException(400, '수신번호를 확인하세요')
    ok, dry = system_sms(ph, '[맵달SEOUL] 발송 테스트입니다. 이 문자가 도착했다면 문자 연동이 정상입니다.', '연동테스트')
    audit(a, '발송테스트', ph, 'DRY' if dry else ('SENT' if ok else 'FAILED'))
    if dry: return {'ok': True, 'dry': True, 'msg': '발송사 미설정 — 기록 모드로 저장했습니다'}
    if not ok: raise HTTPException(400, '발송 실패 — 발송 기록의 상세 사유를 확인하세요 (발신번호 미등록/잔액 부족 등)')
    return {'ok': True, 'dry': False, 'msg': '발송 성공! 수신 확인해 보세요'}

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
            'google_oauth': bool(_genv('GOOGLE_CLIENT_ID')),
            'apple_oauth': bool(_genv('APPLE_CLIENT_ID')),
            'kakao_oauth': bool(_genv('KAKAO_CLIENT_ID')),
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
.dropzone{border:2px dashed #bbb;background:#fafafa;border-radius:4px;padding:22px;text-align:center;color:#999;cursor:pointer;font-size:13px;transition:.15s}
.dropzone:hover{border-color:#141414;color:#555;background:#f4f4f2}
.dropzone.over{border-color:#E8332A;background:#fff5f4;color:#E8332A}
.dropzone.busy{opacity:.6;pointer-events:none}
.dropzone .dz-in{pointer-events:none}
.blk-list{border:1px solid #eee;border-radius:4px;background:#fff;min-height:60px}
.blk{border-bottom:1px solid #f0f0f0;padding:12px}
.blk:last-child{border-bottom:0}
.blk-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:12px;color:#555}
.blk-ctrl{display:flex;gap:4px}
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
  <button class="btn red" id="pnew" onclick="location.href='/admin/products/new'">+ 상품 등록</button>
  <span class="hint">가격 변경·상품 등록은 매니저 이상. 등록 상품은 /p/상품ID 페이지가 자동 생성됩니다.</span></div>
  <div id="plist" class="loading">불러오는 중…</div></section>
<section id="t-pages" style="display:none">
  <div class="panel"><h3>페이지 콘텐츠 관리 <span class="tag">저장 즉시 사이트 반영 · 재배포에도 유지</span></h3>
  <div class="hint" style="margin-bottom:10px">편집 내용은 데이터베이스에 저장되어 원본 파일과 별도로 보존됩니다. [원본 복원]으로 언제든 되돌릴 수 있고, 저장할 때마다 직전 버전이 이력(최근 10개)에 남습니다.</div>
  <div id="pglist" class="loading">불러오는 중…</div></div></section>
<section id="t-ticker" style="display:none">
  <style>@keyframes tkmq{from{transform:translateX(0)}to{transform:translateX(-50%)}}</style>
  <div class="panel"><h3>LED 드롭 티커 <span class="tag">저장 즉시 전 페이지 반영</span></h3>
  <div class="hint" style="margin-bottom:10px">한 줄에 한 항목 · <b>**별표 두 개**</b>로 감싸면 흰색 강조 · 항목을 전부 지우고 저장하면 사이트에서 티커가 숨겨집니다. 항목 수가 바뀌어도 흐르는 속도는 일정하게 자동 조절됩니다.</div>
  <div id="tkbox" class="loading">불러오는 중…</div></div></section>
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
<section id="t-cs" style="display:none">
  <div class="panel"><h3>취소/반품/교환 요청</h3><div id="csreq" class="loading">불러오는 중…</div>
  <div class="hint">취소 요청 승인 시: [완료]로 바꾼 뒤 [주문 관리]에서 해당 주문의 결제취소(환불)를 실행하세요. 재고는 자동 복원됩니다.</div></div>
  <div class="panel"><h3>1:1 문의</h3><div id="csinq" class="loading">불러오는 중…</div></div>
  <div class="panel"><h3>상품 Q&amp;A <span class="tag">답변 시 상품페이지에 공개</span></h3><div id="cspq" class="loading">불러오는 중…</div></div></section>
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
const TABS=[['dash','대시보드',0],['orders','주문',0],['products','상품·재고',0],['pages','페이지',2],['ticker','티커',2],['cust','고객',0],['notify','알림',0],['cs','문의·요청',0],['admins','관리자',3],['system','시스템',0]];
const LOAD={dash:loadDash,orders:()=>loadOrders(1),products:()=>loadProducts(1),pages:loadPages,ticker:loadTicker,cust:()=>loadCust(1),notify:loadNotify,cs:loadCS,admins:loadAdmins,system:loadSys};
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
 <div class="card ${d.pending_ship?'alert':''}"><div class="k">발송 대기</div><div class="v">${d.pending_ship}건</div><div class="s">결제완료 · 미발송</div></div>
 <div class="card ${d.pending_cs?'alert':''}"><div class="k">문의·요청 대기</div><div class="v">${d.pending_cs||0}건</div><div class="s">1:1 · Q&A · 취소/반품</div></div></div>
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
 <td style="white-space:nowrap">${can(1)?`<button class="btn sm" onclick="saveProd('${k}',false)">저장</button> `:''}${can(2)?`<button class="btn sm" onclick="editDetail(window._pk['${k}'])">상세편집</button> `:''}<a class="btn sm ghost" style="text-decoration:none" href="/p/${encodeURIComponent(r.id)}" target="_blank">보기</a>${can(2)&&(r.id.indexOf('mp::')===0||r.id.indexOf('k2g::')===0)?` <button class="btn sm ghost" style="color:#c0392b;border-color:#c0392b" onclick="delProd('${k}')">삭제</button>`:''}</td></tr>`}).join('')||'<tr><td colspan=6 class="loading">없음</td></tr>'}</table>
 ${pager(p,d,'loadProducts')}`;}catch(e){$('#plist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function saveProd(k,tg){const body={id:window._pk[k],soldout:document.getElementById('so'+k).checked?1:0};
 if(!tg){body.stock=Number(document.getElementById('st'+k).value);const pr=document.getElementById('pr'+k);if(pr)body.price=Number(pr.value)}
 try{await api('/admin/api/products/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('반영되었습니다')}catch(e){toast(e.message);loadProducts(ppage)}}
async function delProd(k){const id=window._pk[k];const isK2g=id.indexOf('k2g::')===0;
 const warn=isK2g
  ?'이 앨범을 카탈로그에서 삭제할까요?\n\n· 상품 ID: '+id+'\n· SHOP 앨범 목록과 앨범 상세에서 즉시 사라지고 구매가 차단됩니다.\n· 삭제 기록이 남아 카탈로그를 다시 불러와도 목록에 재노출되지 않습니다.\n· 기존 주문·문의 이력은 보존됩니다.\n· 이 작업은 되돌릴 수 없습니다.'
  :'이 상품을 삭제할까요?\n\n· 상품 ID: '+id+'\n· SHOP 목록과 /p/ 상세 페이지에서 즉시 사라집니다.\n· 기존 주문·문의 이력은 보존됩니다.\n· 이 작업은 되돌릴 수 없습니다.';
 if(!confirm(warn))return;
 try{await api('/admin/api/products/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
 toast('삭제되었습니다');loadProducts(ppage)}catch(e){toast(e.message)}}

let CMODE='buyers';
function custMode(m){CMODE=m;$('#cm1').className='btn sm'+(m==='buyers'?'':' ghost');$('#cm2').className='btn sm'+(m==='members'?'':' ghost');
 document.querySelectorAll('#t-cust .toolbar')[1].style.display=(m==='buyers'?'':'none');
 if(m==='buyers')loadCust(1);else loadMembers()}
async function loadMembers(){try{const d=await api('/admin/api/members');
 $('#clist').innerHTML=`<div class="hint" style="margin-bottom:8px">소셜 계정(Google/Apple)으로 가입한 회원 목록입니다. 총 ${d.total}명.</div>
 <table><tr><th>이름</th><th>이메일</th><th>가입방법</th><th>휴대폰</th><th>성별/출생</th><th class="right">포인트</th><th>가입일시</th><th></th></tr>
 ${d.rows.map(m=>`<tr><td>${esc(m.name)||'-'}</td><td class="mono">${esc(m.email)||'-'}</td>
 <td>${({google:'Google',apple:'Apple',email:'이메일',kakao:'카카오'})[m.provider]||esc(m.provider)}</td>
 <td class="mono">${esc(m.phone)||'-'}${m.verified?' <span class="st PAID" style="font-size:10px">인증</span>':''}</td>
 <td>${m.gender==='F'?'여':m.gender==='M'?'남':'-'}${m.birth?' · '+esc(m.birth):''}</td>
 <td class="right mono">${m.points.toLocaleString()}P</td><td class="mono">${esc(m.created)}</td>
 <td>${can(2)?`<button class="btn sm ghost" onclick="grantPoints('${m.id}','${esc(m.email)}')">포인트</button>`:''}</td></tr>`).join('')||'<tr><td colspan=4 class="loading">가입 회원 없음 — 사이트의 /account 에서 가입할 수 있습니다</td></tr>'}</table>`;
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

async function testSend(){const ph=$('#tstph').value;if(!ph)return toast('수신번호를 입력하세요');
 try{const r=await api('/admin/api/notify/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:ph})});
 toast(r.msg);loadNotify()}catch(e){alert(e.message)}}
async function loadNotify(){try{const d=await api('/admin/api/notify/templates');TPLCACHE=d.rows;
 const c=d.conf;
 $('#nconf').innerHTML=`<div class="cards">
 <div class="card ${c.key?'':'alert'}"><div class="k">발송사 (솔라피)</div><div class="v" style="font-size:15px">${c.key?'연동됨':'미설정'}</div><div class="s">${c.key?'':'미설정 시 발송 대신 로그만 기록'}</div></div>
 <div class="card"><div class="k">발신번호</div><div class="v" style="font-size:15px">${c.sender?'등록됨':'-'}</div></div>
 <div class="card"><div class="k">카카오 알림톡</div><div class="v" style="font-size:15px">${c.pf?'채널 연동됨':'미연동'}</div><div class="s">${c.pf?'':'연동 전엔 SMS 템플릿 사용'}</div></div>
 <div class="card"><div class="k">실발송 테스트</div><div class="v" style="font-size:13px;margin-top:8px"><input id="tstph" placeholder="010-0000-0000" style="width:120px;padding:5px 7px;font-size:12px"> <button class="btn sm" onclick="testSend()">발송</button></div><div class="s">키 입력 후 본인 번호로 확인</div></div></div>`;
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
// 상품 등록·상세편집은 별도 페이지(/admin/products/new, /admin/products/edit)로 이동합니다.
function editDetail(id){location.href='/admin/products/edit?id='+encodeURIComponent(id)}

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

async function loadTicker(){try{const d=await api('/admin/api/ticker');
 $('#tkbox').innerHTML=`
 <textarea id="tkitems" rows="7" style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:12.5px;line-height:1.8" placeholder="예) NEXT DROP **07.18 SAT 12:00 KST** — SUMMER DROP 08"></textarea>
 <div class="toolbar" style="margin-top:10px;align-items:center">속도
  <label><input type="radio" name="tkspd" value="slow"> 느림</label>
  <label><input type="radio" name="tkspd" value="normal"> 보통</label>
  <label><input type="radio" name="tkspd" value="fast"> 빠름</label>
  ${can(2)?'<button class="btn" onclick="saveTicker()" style="margin-left:auto">저장 (사이트 즉시 반영)</button>':''}
  <a class="btn ghost" href="/" target="_blank" style="text-decoration:none${can(2)?'':';margin-left:auto'}">사이트에서 확인</a></div>
 <div class="hint" style="margin:14px 0 6px">미리보기 — 실제 사이트와 동일하게 흐릅니다</div>
 <div id="tkprev" style="background:#141414;overflow:hidden;padding:9px 0;border-bottom:3px solid #E8332A;border-radius:3px">
  <div id="tkprevtrack" style="display:flex;gap:56px;white-space:nowrap;width:max-content"></div></div>
 ${d.is_default?'<div class="hint" style="margin-top:8px">아직 저장 이력 없음 — 현재 사이트의 기본 문구를 표시 중입니다.</div>'
  :`<div class="hint" style="margin-top:8px">마지막 저장 ${esc((d.updated||'').slice(0,16).replace('T',' '))} UTC · ${esc(d.by_admin||'')}</div>`}`;
 document.getElementById('tkitems').value=(d.items||[]).join('\n');
 document.querySelectorAll('input[name=tkspd]').forEach(r=>{r.checked=(r.value===(d.speed||'normal'));r.onchange=tkPrev});
 document.getElementById('tkitems').oninput=tkPrev;tkPrev();
}catch(e){$('#tkbox').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function tkItems(){return document.getElementById('tkitems').value.split('\n').map(s=>s.trim()).filter(Boolean)}
function tkSpd(){const r=document.querySelector('input[name=tkspd]:checked');return r?r.value:'normal'}
function tkPrev(){const t=document.getElementById('tkprevtrack');if(!t)return;
 const items=tkItems(),box=document.getElementById('tkprev');
 if(!items.length){box.style.opacity=.4;t.style.animation='none';
  t.innerHTML='<span style="font:12px \'IBM Plex Mono\',monospace;color:#888;padding-left:16px">항목 없음 — 저장하면 사이트에서 티커가 숨겨집니다</span>';return}
 box.style.opacity=1;
 const unit=items.map(s=>'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;letter-spacing:.08em;color:#FFB000">'
  +esc(s).replace(/\*\*([^*]+)\*\*/g,'<b style="color:#fff;font-weight:500">$1</b>')+'</span>').join('');
 t.innerHTML=unit+unit+unit+unit;
 const px={slow:38,normal:55,fast:80}[tkSpd()]||55;
 requestAnimationFrame(()=>{const half=t.scrollWidth/2;if(half>0){
  t.style.animation='none';void t.offsetWidth;
  t.style.animation='tkmq '+Math.max(10,Math.round(half/px))+'s linear infinite'}})}
async function saveTicker(){try{
 const d=await api('/admin/api/ticker/save',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({items:tkItems(),speed:tkSpd()})});
 toast('저장 완료 — 전 페이지 즉시 반영 ('+d.items.length+'개 항목)');loadTicker()}catch(e){toast(e.message)}}

async function loadCS(){try{const d=await api('/admin/api/cs');
 const stag=v=>'<span class="st '+(v==='완료'||v==='답변완료'?'PAID':v==='거절'?'CANCELLED':'PENDING')+'">'+esc(v)+'</span>';
 $('#csreq').innerHTML=d.reqs.length?`<table><tr><th>일시</th><th>유형</th><th>주문번호</th><th>회원</th><th>사유</th><th>상태</th><th></th></tr>
 ${d.reqs.map(r=>`<tr><td class="mono">${esc(r.created)}</td><td><b>${esc(r.rtype)}</b></td><td class="mono" style="font-size:11px">${esc(r.order_id)}</td>
 <td>${esc(r.mname)}<br><span class="mono" style="font-size:10.5px;color:#888">${esc(r.mphone)}</span></td>
 <td style="font-size:12px">${esc(r.reason)}${r.memo?'<br><span style="color:#888">메모: '+esc(r.memo)+'</span>':''}</td>
 <td>${stag(r.status)}</td><td>${can(1)?`<button class="btn sm ghost" onclick="csReq('${r.id}','${esc(r.status)}')">처리</button>`:''}</td></tr>`).join('')}</table>`:'<div class="loading">요청 없음</div>';
 const block=(rows,kind)=>rows.length?rows.map(q=>`<div style="border-bottom:1px solid var(--line);padding:10px 4px">
 <b>${esc(kind==='inq'?q.title:q.pname)}</b> ${stag(q.status)} <span class="hint" style="display:inline">${esc(q.created)} · ${esc(q.mname)}${kind==='inq'&&q.order_id?' · '+esc(q.order_id):''}</span>
 <div style="margin-top:6px;font-size:12.5px;white-space:pre-wrap">${esc(kind==='inq'?q.body:q.question)}</div>
 ${q.answer?`<div style="margin-top:6px;background:#faf9f5;padding:8px;font-size:12.5px;white-space:pre-wrap"><b>답변</b> ${esc(q.answer)}</div>`:''}
 ${can(1)?`<div style="margin-top:8px"><button class="btn sm" onclick="csAnswer('${kind}','${q.id}')">${q.answer?'답변 수정':'답변하기'}</button></div>`:''}</div>`).join(''):'<div class="loading">없음</div>';
 $('#csinq').innerHTML=block(d.inq,'inq');$('#cspq').innerHTML=block(d.pqna,'pqna');
}catch(e){$('#csreq').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function csAnswer(kind,id){const ans=prompt('답변 내용을 입력하세요 (회원에게 표시됩니다)');if(!ans)return;
 try{await api('/admin/api/cs/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,id,answer:ans})});toast('답변 등록');loadCS()}catch(e){toast(e.message)}}
async function csReq(id,cur){const st=prompt('상태 입력: 접수 / 처리중 / 완료 / 거절',cur==='접수'?'처리중':'완료');if(!st)return;
 const memo=prompt('회원에게 표시할 메모 (선택)','')||'';
 try{await api('/admin/api/cs/req-update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status:st,memo})});toast('처리되었습니다');loadCS()}catch(e){toast(e.message)}}
async function grantPoints(id,email){const v=prompt(email+' 님에게 지급(+) / 차감(-)할 포인트','1000');if(!v)return;
 const reason=prompt('사유 (감사로그 기록)','CS 보상')||'';
 try{const r=await api('/admin/api/members/points',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,delta:Number(v),reason})});
 toast('현재 '+r.points.toLocaleString()+'P');loadMembers()}catch(e){toast(e.message)}}
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
# shop.html 필터 탭과 1:1로 매칭되는 카테고리 (value → 표기 라벨)
PRODUCT_CATEGORIES = [
    ('album',   '앨범 / 음반'),
    ('md',      '굿즈 / MD'),
    ('kfood',   'K-FOOD'),
    ('apparel', '어패럴'),
    ('living',  '리빙 / 홈'),
]
_CAT_KEYS = {k for k, _ in PRODUCT_CATEGORIES}
_CAT_LABEL = dict(PRODUCT_CATEGORIES)

def norm_cat(v):
    v = (v or '').strip().lower()
    return v if v in _CAT_KEYS else ''

@admin_router.get('/admin/api/products/categories')
def api_product_categories(request: Request):
    a = get_actor(request); need(a, 0)
    return {'categories': [{'value': k, 'label': l} for k, l in PRODUCT_CATEGORIES]}

# ── 이미지 업로드 (대표 이미지 + 상세페이지 이미지 블록 공용) ──────────────
@admin_router.post('/admin/api/upload')
async def api_upload(request: Request, file: UploadFile = File(...)):
    a = get_actor(request); need(a, 2, '이미지 업로드')
    data = await file.read()
    res = store_image(data, file.content_type)  # 형식/용량 검증 + R2 또는 DB 폴백 저장
    audit(a, '이미지업로드', res['url'][:120],
          '%s · %.0fKB · %s' % (file.content_type, len(data) / 1024, res['stored']))
    return {'ok': True, 'url': res['url'], 'storage': res['stored']}

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
    if 'img' in _state['pcols']:
        cols.append('img'); vals.append(_safe_url(body.get('img')))
    if 'descr' in _state['pcols']:
        cols.append('descr'); vals.append((body.get('descr') or '').strip()[:4000])
    if 'category' in _state['pcols']:
        cols.append('category'); vals.append(norm_cat(body.get('category')))
    if 'badge' in _state['pcols']:
        cols.append('badge'); vals.append((body.get('badge') or '').strip()[:30])
    if 'detail_html' in _state['pcols']:
        # detail_blocks(JSON) 우선, 없으면 레거시 detail_html 텍스트 허용
        blocks = body.get('detail_blocks')
        cols.append('detail_html')
        vals.append(clean_blocks(blocks) if blocks is not None else (body.get('detail_html') or '').strip()[:100000])
    if 'gallery' in _state['pcols']:
        cols.append('gallery'); vals.append(_clean_gallery(body.get('gallery')))
    run('INSERT INTO products(%s) VALUES(%s)' % (','.join(cols), ','.join(['?'] * len(vals))), tuple(vals))
    audit(a, '상품등록', pid, '%s / %s원 / 재고 %d / %s' % (name, format(price, ','), stock, _CAT_LABEL.get(norm_cat(body.get('category')), '미분류')))
    return {'ok': True, 'id': pid, 'url': '/p/' + pid}

@admin_router.post('/admin/api/products/delete')
def api_product_delete(request: Request, body: dict = Body(...)):
    """상품 삭제. mp::(직접등록)와 k2g::(앨범 카탈로그) 모두 삭제 가능.
    k2g는 삭제 기록(k2g_removed)을 남겨 SHOP·앨범상세의 인라인 카탈로그에서도 즉시 감춘다.
    주문·Q&A 이력은 보존하고, 재입고 알림 대기만 함께 정리한다."""
    a = get_actor(request); need(a, 2, '상품 삭제')
    pid = str(body.get('id') or '').strip()
    if not (pid.startswith('mp::') or pid.startswith('k2g::')):
        raise HTTPException(400, '삭제할 수 없는 상품 유형입니다')
    r = one('SELECT %s AS name FROM products WHERE id=?' % (_state['pname'] or 'id'), (pid,))
    if not r:
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    run('DELETE FROM products WHERE id=?', (pid,))
    try: run('DELETE FROM member_restock WHERE product_id=?', (pid,))
    except Exception: pass
    if pid.startswith('k2g::'):
        try:
            run('INSERT INTO k2g_removed(uid, name, created, by_admin) VALUES(?,?,?,?)',
                (pid[5:], str(r.get('name') or '')[:300], now_iso(), a['name']))
        except Exception:
            pass  # 이미 기록됨(PK 충돌) — 무시
        _k2g_rm_cache['set'] = None  # 삭제목록 캐시 즉시 무효화
    try: _k2g_cache_bust()           # 카탈로그 캐시 즉시 무효화
    except Exception: pass
    audit(a, '상품삭제', pid, str(r.get('name') or ''))
    return {'ok': True}

def _clean_gallery(v):
    """줄바꿈으로 구분된 이미지 URL 목록을 정리해 개행 문자열로 저장 (최대 12장)."""
    if isinstance(v, list):
        items = v
    else:
        items = re.split(r'[\r\n]+', str(v or ''))
    out = []
    for u in items:
        u = (u or '').strip()
        if u.startswith('http') and u not in out:
            out.append(u)
        if len(out) >= 12:
            break
    return '\n'.join(out)

def _safe_url(u):
    """이미지 URL 허용 검사: http(s) 절대주소 또는 /admin/asset/ 폴백 경로만."""
    u = (u or '').strip()
    if u.startswith('http://') or u.startswith('https://') or u.startswith('/admin/asset/'):
        return u[:2000]
    return ''

def clean_blocks(v):
    """상세페이지 블록 목록을 정규화해 JSON 문자열로 반환.
    블록: {type:'text', text:str} 또는 {type:'image', url:str, caption:str}.
    최대 60블록, 이미지 60장. 게시판 글쓰기처럼 이미지/글을 자유 순서로 쌓는다."""
    if isinstance(v, str):
        try: v = json.loads(v)
        except Exception: v = []
    if not isinstance(v, list): v = []
    out = []
    for b in v:
        if not isinstance(b, dict): continue
        t = b.get('type')
        if t == 'text':
            txt = str(b.get('text') or '').strip()
            if txt: out.append({'type': 'text', 'text': txt[:20000]})
        elif t == 'image':
            url = _safe_url(b.get('url'))
            if url:
                cap = str(b.get('caption') or '').strip()[:300]
                out.append({'type': 'image', 'url': url, 'caption': cap})
        if len(out) >= 60: break
    return json.dumps(out, ensure_ascii=False)

def render_blocks(raw):
    """저장된 상세 블록(JSON) 또는 레거시 HTML/텍스트를 안전한 HTML로 렌더."""
    def esc(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    raw = (raw or '').strip()
    if not raw: return ''
    blocks = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list): blocks = parsed
    except Exception:
        blocks = None
    parts = []
    if blocks is not None:
        for b in blocks:
            if not isinstance(b, dict): continue
            if b.get('type') == 'image':
                url = _safe_url(b.get('url'))
                if not url: continue
                cap = esc(b.get('caption'))
                caph = ('<figcaption>%s</figcaption>' % cap) if cap else ''
                parts.append('<figure><img src="%s" alt="%s" loading="lazy">%s</figure>' % (esc(url), cap, caph))
            elif b.get('type') == 'text':
                txt = esc(b.get('text')).replace('\n', '<br>')
                if txt.strip(): parts.append('<p>%s</p>' % txt)
    else:
        # 레거시: 예전에 HTML 텍스트로 저장된 상품 → 스크립트만 제거하고 그대로 사용
        safe = re.sub(r'(?is)<\s*script.*?<\s*/\s*script\s*>', '', raw)
        safe = re.sub(r'(?is)<\s*script[^>]*>', '', safe)
        safe = re.sub(r'(?i)\son\w+\s*=\s*"[^"]*"', '', safe)
        safe = re.sub(r"(?i)\son\w+\s*=\s*'[^']*'", '', safe)
        safe = re.sub(r'(?i)javascript:', '', safe)
        parts.append(safe)
    return '\n'.join(parts)

# ── 상품 상세페이지 내용 편집 (admin) ─────────────────────────────
@admin_router.get('/admin/api/products/detail')
def api_product_detail_get(request: Request):
    """상세페이지 편집 화면용: 한 상품의 모든 편집 가능 필드를 반환."""
    a = get_actor(request); need(a, 1, '상품 상세 조회')
    pid = request.query_params.get('id')
    if not pid: raise HTTPException(400, 'id required')
    sel = 'id, %s AS name, stock, soldout' % (_state['pname'] or 'id')
    if _state['pprice']: sel += ', %s AS price' % _state['pprice']
    for c in ('img', 'descr', 'category', 'detail_html', 'gallery', 'badge'):
        if c in _state['pcols']: sel += ', ' + c
    r = one('SELECT %s FROM products WHERE id=?' % sel, (pid,))
    if not r: raise HTTPException(404, '상품을 찾을 수 없습니다')
    # detail_html이 JSON 블록이면 파싱, 레거시 텍스트면 단일 text 블록으로 변환
    raw_detail = r.get('detail_html') or ''
    try:
        parsed = json.loads(raw_detail)
        blocks = parsed if isinstance(parsed, list) else None
    except Exception:
        blocks = None
    if blocks is None:
        blocks = [{'type': 'text', 'text': raw_detail}] if raw_detail.strip() else []
    return {
        'id': r['id'], 'name': r.get('name') or r['id'],
        'price': num(r.get('price')) if _state['pprice'] else None,
        'stock': num(r.get('stock')), 'soldout': num(r.get('soldout')),
        'img': r.get('img') or '', 'descr': r.get('descr') or '',
        'category': norm_cat(r.get('category')),
        'badge': (r.get('badge') or '').strip(),
        'detail_blocks': blocks,
        'gallery': r.get('gallery') or '',
        'categories': [{'value': k, 'label': l} for k, l in PRODUCT_CATEGORIES],
        'url': '/p/' + r['id'],
    }

@admin_router.post('/admin/api/products/detail/update')
def api_product_detail_update(request: Request, body: dict = Body(...)):
    """상세페이지 내용(카테고리/설명/이미지/갤러리/상세 HTML)을 수정."""
    a = get_actor(request); need(a, 2, '상품 상세 수정')
    pid = body.get('id')
    if not pid: raise HTTPException(400, 'id required')
    if not one('SELECT id FROM products WHERE id=?', (pid,)):
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    sets, args, log = [], [], []
    field_map = {
        'name': (_state['pname'], 300, None),
        'img': ('img', None, _safe_url),
        'descr': ('descr', 4000, None),
        'detail_blocks': ('detail_html', None, clean_blocks),
        'detail_html': ('detail_html', 100000, None),
        'category': ('category', 40, norm_cat),
        'badge': ('badge', 30, None),
        'gallery': ('gallery', None, _clean_gallery),
    }
    for k, (col, limit, fn) in field_map.items():
        if k not in body or not col or (col not in _state['pcols'] and col != _state['pname']):
            continue
        # detail_blocks가 오면 detail_html(레거시)은 무시
        if k == 'detail_html' and 'detail_blocks' in body:
            continue
        val = body.get(k)
        if fn is not None:
            val = fn(val)
        else:
            val = (val or '').strip()
            if limit: val = val[:limit]
        sets.append('%s=?' % col); args.append(val); log.append(k)
    # 가격 · 재고 (별도 페이지 편집 화면에서 한 번에 저장)
    if body.get('price') is not None and _state['pprice']:
        v = num(body['price'])
        if v < 0: raise HTTPException(400, '가격은 0 이상')
        sets.append('%s=?' % _state['pprice']); args.append(v); log.append('price')
    if body.get('stock') is not None:
        s = num(body['stock'])
        if s < 0: raise HTTPException(400, '재고는 0 이상')
        sets.append('stock=?'); args.append(s)
        sets.append('soldout=?'); args.append(1 if s == 0 else 0)
        log.append('stock')
    if not sets: raise HTTPException(400, '변경할 값 없음')
    run('UPDATE products SET %s WHERE id=?' % ', '.join(sets), tuple(args + [pid]))
    try: _k2g_cache_bust()
    except Exception: pass
    audit(a, '상품상세수정', pid, '수정 항목: ' + ', '.join(log))
    return {'ok': True, 'id': pid, 'url': '/p/' + pid}

# ═══════════════ 상품 등록/편집 — 별도 페이지 (모달 아님) ═══════════════
def _page_guard(request, what):
    """페이지용 인증 가드: 미로그인 → 로그인 화면 / 권한부족 → 안내 페이지 / 통과 → None."""
    try:
        actor = get_actor(request); need(actor, 2, what)
        return None
    except HTTPException as e:
        if e.status_code == 403 and 'forbidden' in str(e.detail):
            return HTMLResponse(LOGIN_HTML)          # 미로그인/세션만료 → 로그인 화면
        if e.status_code == 403:
            return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center">'
                                '<h3>%s</h3><a href="/admin/dashboard">대시보드로 돌아가기</a>' % e.detail, status_code=403)
        raise

@admin_router.get('/admin/products/new', response_class=HTMLResponse)
def product_new_page(request: Request):
    blocked = _page_guard(request, '상품 등록')
    if blocked is not None: return blocked
    return HTMLResponse(_PRODUCT_FORM_HTML.replace('__PAGE__',
        json.dumps({'mode': 'new', 'id': ''}, ensure_ascii=False)))

@admin_router.get('/admin/products/edit', response_class=HTMLResponse)
def product_edit_page(request: Request):
    blocked = _page_guard(request, '상품 상세 수정')
    if blocked is not None: return blocked
    pid = (request.query_params.get('id') or '').strip()
    if not pid:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h3>상품 ID가 없습니다</h3><a href="/admin/dashboard">대시보드로</a>', status_code=400)
    return HTMLResponse(_PRODUCT_FORM_HTML.replace('__PAGE__',
        json.dumps({'mode': 'edit', 'id': pid}, ensure_ascii=False)))

_PRODUCT_FORM_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>상품 등록 · 편집 — MAPDAL SEOUL</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--paper);color:var(--black);padding-bottom:96px}
header{background:var(--black);color:#fff;padding:14px 22px;display:flex;align-items:center;gap:16px}
header h1{font-family:'Black Han Sans';font-size:19px;font-weight:400}header h1 span{color:var(--red)}
header a.back{color:#bbb;text-decoration:none;font-size:13px}header a.back:hover{color:#fff}
main{max-width:820px;margin:26px auto;padding:0 18px}
h2{font-size:19px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e3e1db;padding:24px;margin-bottom:18px}
.card h3{font-size:14px;border-left:4px solid var(--red);padding-left:9px;margin-bottom:16px}
.f{margin-bottom:15px}
.f label{display:block;font-size:12px;font-weight:700;color:#777;margin-bottom:6px}
.f input[type=text],.f input[type=number],.f select,.f textarea{width:100%;font:inherit;font-size:14px;padding:10px 12px;border:1px solid #ccc;background:#fff}
.f input:focus,.f select:focus,.f textarea:focus{outline:2px solid var(--black)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.row2{grid-template-columns:1fr}}
.btn{font:inherit;font-weight:700;font-size:14px;border:0;padding:11px 20px;cursor:pointer;background:var(--black);color:#fff}
.btn.ghost{background:#fff;color:var(--black);border:1px solid #999}
.btn.sm{padding:5px 10px;font-size:12px}
.btn:disabled{opacity:.4;cursor:not-allowed}
.dropzone{border:2px dashed #bbb;background:#fafafa;padding:26px;text-align:center;color:#999;cursor:pointer;font-size:13px;transition:.15s}
.dropzone:hover{border-color:var(--black);color:#555}
.dropzone.over{border-color:var(--red);background:#fff5f4;color:var(--red)}
.dropzone.busy{opacity:.6;pointer-events:none}
.dz-in{pointer-events:none}
.blk-list{border:1px solid #eee;background:#fff;min-height:64px}
.blk{border-bottom:1px solid #f0f0f0;padding:13px}
.blk:last-child{border-bottom:0}
.blk-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px;font-size:12px;color:#555}
.blk-ctrl{display:flex;gap:4px}
.blk textarea,.blk input{width:100%;font:inherit;font-size:14px;padding:8px 10px;border:1px solid #ddd}
.hint{font-size:11.5px;color:#888;line-height:1.7;margin-top:8px}
.savebar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #ddd;padding:12px 18px;display:flex;gap:10px;justify-content:center;z-index:50}
.savebar .in{width:100%;max-width:820px;display:flex;gap:10px;justify-content:flex-end;align-items:center}
.savebar .stat{margin-right:auto;font-size:12px;color:#888}
#toast{position:fixed;bottom:76px;left:50%;transform:translateX(-50%);background:var(--black);color:#fff;padding:10px 20px;display:none;z-index:200;font-weight:700}
</style></head><body>
<header><a class="back" href="/admin/dashboard">← 대시보드</a><h1>MAPDAL<span>SEOUL</span></h1><span id="ptitle" style="font-size:13px;color:#bbb"></span></header>
<main>
<h2 id="h2title"></h2>
<div class="card"><h3>기본 정보</h3>
 <div class="f"><label>상품명 *</label><input type="text" id="fn" placeholder="예: 맵달 굿즈 키링"></div>
 <div class="row2">
  <div class="f"><label>카테고리 *</label><select id="fc"></select></div>
  <div class="f"><label>가격(원) *</label><input type="number" id="fp" min="0" placeholder="12900"></div>
 </div>
 <div class="row2">
  <div class="f"><label id="fslabel">초기 재고 *</label><input type="number" id="fs" min="0" placeholder="100"></div>
  <div class="f"><label>카드 배지 <span style="font-weight:400;color:#aaa">— SHOP 카드 좌상단 표기</span></label>
   <input type="text" id="fb" list="badgeOpts" maxlength="30" placeholder="비우면 카테고리명이 표기됩니다">
   <datalist id="badgeOpts"><option value="BEST"><option value="NEW"><option value="LIMITED"><option value="EVENT"><option value="GIFT"><option value="성수 한정"><option value="세트"><option value="사인회"><option value="영상통화"><option value="예약판매"></datalist></div>
 </div>
 <div class="f"><label>짧은 설명</label><textarea id="fd" rows="3" placeholder="목록·상단 요약 설명 (선택)"></textarea></div>
</div>
<div class="card"><h3>대표 이미지</h3>
 <div id="fzone" class="dropzone"><div class="dz-in" data-empty="이미지를 드래그하거나 클릭해 업로드"></div></div>
 <input id="ffile" type="file" accept="image/*" style="display:none"><input id="fi" type="hidden">
 <div id="fpv" style="margin-top:10px"></div>
 <div class="hint">업로드 시 자동으로 리사이즈·압축됩니다 (최대 1600px).</div>
</div>
<div class="card"><h3>상세 페이지 (이미지 + 글)</h3>
 <div id="blkList" class="blk-list"></div>
 <input id="blkImgInput" type="file" accept="image/*" multiple style="display:none" onchange="onBlkImg(this.files)">
 <div style="display:flex;gap:8px;margin-top:12px"><button class="btn sm" type="button" onclick="addImgBlk()">＋ 이미지 추가</button><button class="btn sm" type="button" onclick="addTextBlk()">＋ 글 추가</button></div>
 <div class="hint">이미지와 글을 원하는 순서로 쌓아 게시판 글처럼 구성하세요. ↑↓로 순서 변경, 이미지는 여러 장 한꺼번에 선택할 수 있습니다.</div>
</div>
</main>
<div class="savebar"><div class="in">
 <span class="stat" id="stat"></span>
 <a class="btn ghost" id="viewBtn" style="text-decoration:none;display:none" target="_blank">상품 페이지 ↗</a>
 <a class="btn ghost" href="/admin/dashboard" style="text-decoration:none">취소</a>
 <button class="btn" id="saveBtn" onclick="save()">저장</button>
</div></div>
<div id="toast"></div>
<script>
const PAGE=__PAGE__;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2600)}
async function api(p,opt){const r=await fetch(p,opt);
 if(r.status===403){alert('세션이 만료되었거나 로그인이 필요합니다. 로그인 화면으로 이동합니다.');location.href='/admin/dashboard';throw new Error('세션 만료')}
 if(!r.ok){let m='오류';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
const PCATS=[['','— 카테고리 선택 —'],['album','앨범 / 음반'],['md','굿즈 / MD'],['kfood','K-FOOD'],['apparel','어패럴'],['living','리빙 / 홈']];
function catOptions(sel){return PCATS.map(c=>`<option value="${c[0]}"${c[0]===(sel||'')?' selected':''}>${c[1]}</option>`).join('')}

function shrinkImage(file,maxDim=1600,quality=0.85){
 return new Promise((resolve)=>{
  if(!/^image\//.test(file.type)||file.type==='image/gif'){resolve(file);return;}
  const img=new Image();const url=URL.createObjectURL(file);
  img.onload=()=>{URL.revokeObjectURL(url);
   let{width:w,height:h}=img;
   if(w<=maxDim&&h<=maxDim&&file.size<600*1024){resolve(file);return;}
   const s=Math.min(1,maxDim/Math.max(w,h));const cw=Math.round(w*s),ch=Math.round(h*s);
   const cv=document.createElement('canvas');cv.width=cw;cv.height=ch;
   cv.getContext('2d').drawImage(img,0,0,cw,ch);
   cv.toBlob(b=>resolve(b&&b.size<file.size?new File([b],file.name.replace(/\.\w+$/,'')+'.jpg',{type:'image/jpeg'}):file),'image/jpeg',quality);
  };
  img.onerror=()=>{URL.revokeObjectURL(url);resolve(file);};
  img.src=url;
 });
}
async function uploadFile(file){
 const small=await shrinkImage(file);
 const fd=new FormData();fd.append('file',small,small.name||'image.jpg');
 const r=await api('/admin/api/upload',{method:'POST',body:fd});
 return r.url;
}
function setMainImg(u){$('#fi').value=u||'';
 $('#fpv').innerHTML=u?`<img src="${esc(u)}" style="max-width:100%;max-height:220px;border:1px solid #e3e1db">
  <div style="margin-top:6px"><button class="btn sm ghost" type="button" onclick="setMainImg('')">이미지 제거</button></div>`:'';}
(function mountZone(){
 const zone=$('#fzone'),inp=$('#ffile'),dz=zone.querySelector('.dz-in');
 const EMPTY=dz.getAttribute('data-empty');dz.textContent=EMPTY;
 async function handle(file){if(!file)return;zone.classList.add('busy');dz.textContent='업로드 중…';
  try{setMainImg(await uploadFile(file));}catch(e){if(e.message!=='세션 만료')toast(e.message);}
  zone.classList.remove('busy');dz.textContent=EMPTY;}
 zone.onclick=()=>inp.click();
 inp.onchange=e=>handle(e.target.files[0]);
 ['dragover','dragenter'].forEach(ev=>zone.addEventListener(ev,e=>{e.preventDefault();zone.classList.add('over');}));
 ['dragleave','drop'].forEach(ev=>zone.addEventListener(ev,e=>{e.preventDefault();zone.classList.remove('over');}));
 zone.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f)handle(f);});
})();

let _blocks=[];
function renderBlocks(){
 const host=$('#blkList');
 if(!_blocks.length){host.innerHTML='<div class="hint" style="padding:16px;text-align:center">아래 버튼으로 이미지나 글을 추가하세요.</div>';return;}
 host.innerHTML=_blocks.map((b,i)=>{
  const ctrl=`<div class="blk-ctrl">
    <button class="btn sm ghost" type="button" onclick="moveBlk(${i},-1)" ${i===0?'disabled':''}>↑</button>
    <button class="btn sm ghost" type="button" onclick="moveBlk(${i},1)" ${i===_blocks.length-1?'disabled':''}>↓</button>
    <button class="btn sm ghost" type="button" onclick="delBlk(${i})">삭제</button></div>`;
  if(b.type==='image'){return `<div class="blk"><div class="blk-h"><b>🖼 이미지</b>${ctrl}</div>
    <img src="${esc(b.url)}" style="max-width:100%;max-height:240px;border:1px solid #e3e1db;display:block;margin:6px 0">
    <input placeholder="이미지 설명 (선택)" value="${esc(b.caption||'')}" oninput="_blocks[${i}].caption=this.value"></div>`;}
  return `<div class="blk"><div class="blk-h"><b>📝 글</b>${ctrl}</div>
    <textarea rows="4" placeholder="내용을 입력하세요" oninput="_blocks[${i}].text=this.value">${esc(b.text||'')}</textarea></div>`;
 }).join('');
}
function moveBlk(i,d){const j=i+d;if(j<0||j>=_blocks.length)return;const t=_blocks[i];_blocks[i]=_blocks[j];_blocks[j]=t;renderBlocks();}
function delBlk(i){_blocks.splice(i,1);renderBlocks();}
function addTextBlk(){_blocks.push({type:'text',text:''});renderBlocks();}
function addImgBlk(){const inp=$('#blkImgInput');inp.value='';inp.click();}
async function onBlkImg(files){for(const f of files){try{const u=await uploadFile(f);_blocks.push({type:'image',url:u,caption:''});renderBlocks();}catch(e){if(e.message!=='세션 만료')toast(e.message);}}}

async function init(){
 $('#fc').innerHTML=catOptions('');
 if(PAGE.mode==='new'){
  $('#h2title').textContent='신규 상품 등록';$('#ptitle').textContent='상품 등록';
  $('#saveBtn').textContent='등록';renderBlocks();return;
 }
 $('#h2title').textContent='상품 상세 편집';$('#ptitle').textContent='상세 편집';
 $('#fslabel').textContent='재고 *';
 try{
  const d=await api('/admin/api/products/detail?id='+encodeURIComponent(PAGE.id));
  $('#fn').value=d.name;$('#fc').innerHTML=catOptions(d.category);
  if(d.price!=null)$('#fp').value=d.price;
  $('#fs').value=d.stock;$('#fd').value=d.descr;$('#fb').value=d.badge||'';
  setMainImg(d.img);
  _blocks=Array.isArray(d.detail_blocks)?d.detail_blocks:[];
  renderBlocks();
  const v=$('#viewBtn');v.href=d.url;v.style.display='';
  $('#stat').textContent=d.soldout?'상태: 품절':'상태: 판매중';
  if(new URLSearchParams(location.search).get('created')==='1'){toast('등록 완료! 상세 내용을 이어서 편집할 수 있습니다.');history.replaceState(null,'','/admin/products/edit?id='+encodeURIComponent(PAGE.id));}
 }catch(e){if(e.message!=='세션 만료'){alert('상품을 불러올 수 없습니다: '+e.message);location.href='/admin/dashboard';}}
}
async function save(){
 const name=$('#fn').value.trim(),cat=$('#fc').value;
 if(!name)return toast('상품명을 입력하세요');
 if(!cat)return toast('카테고리를 선택하세요');
 const btn=$('#saveBtn');btn.disabled=true;
 try{
  if(PAGE.mode==='new'){
   const r=await api('/admin/api/products/create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,category:cat,price:Number($('#fp').value||0),stock:Number($('#fs').value||0),
     img:$('#fi').value,descr:$('#fd').value,badge:$('#fb').value.trim(),detail_blocks:_blocks})});
   location.href='/admin/products/edit?id='+encodeURIComponent(r.id)+'&created=1';return;
  }
  await api('/admin/api/products/detail/update',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({id:PAGE.id,name,category:cat,price:Number($('#fp').value||0),stock:Number($('#fs').value||0),
    img:$('#fi').value,descr:$('#fd').value,badge:$('#fb').value.trim(),detail_blocks:_blocks})});
  toast('저장되었습니다');
 }catch(e){if(e.message!=='세션 만료')toast(e.message);}
 btn.disabled=false;
}
init();
</script></body></html>'''

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
.cat{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;color:#141414;background:#f0efe9;border:1px solid #e0ded7;padding:3px 10px;margin-bottom:10px;text-decoration:none}
.cat:hover{background:#141414;color:#fff}
.gal{display:flex;gap:8px;overflow-x:auto;margin-top:10px;padding-bottom:4px}
.gal img{width:84px;height:84px;object-fit:cover;border:1px solid #e3e1db;cursor:pointer;flex:0 0 auto}
.detail{max-width:860px;margin:22px auto 0;background:#fff;border:1px solid #e3e1db;padding:26px;font-size:14.5px;line-height:1.85;color:#333}
.detail h2{font-size:16px;border-left:4px solid #E8332A;padding-left:8px;margin:0 0 14px}
.detail figure{margin:16px 0}
.detail img{max-width:100%%;height:auto;display:block;margin:0 auto;border-radius:2px}
.detail figcaption{font-size:12px;color:#888;text-align:center;margin-top:6px}
.detail p{margin:12px 0;white-space:normal}
.foot{font-family:'IBM Plex Mono';font-size:10px;color:#aaa;text-align:center;margin-top:26px}</style></head><body>
<header><a href="/">MAPDAL<span>SEOUL</span></a><a class="shop" href="/shop.html">SHOP 전체보기</a></header>
<main><div class="wrap"><div><div class="ph" id="mainPh">%(imgtag)s</div>%(galhtml)s</div><div>
%(cathtml)s
<h1>%(name)s</h1><div class="price">₩%(price)s</div>
<span class="badge %(bcls)s">%(bmsg)s</span>
<div class="desc">%(descr)s</div>
<div style="display:flex;gap:8px;margin-top:18px">
<button id="likeBtn" onclick="toggleLike()" style="flex:1;font:700 14px 'IBM Plex Sans KR';padding:13px;border:1px solid #141414;background:#fff;cursor:pointer">&#9825; 좋아요</button>
<button id="rsBtn" onclick="toggleRestock()" style="flex:1;display:none;font:700 14px 'IBM Plex Sans KR';padding:13px;border:0;background:#FFB000;color:#141414;cursor:pointer">재입고 알림 신청</button>
</div>
<a class="cta" href="/shop.html">SHOP에서 주문하기</a></div></div>
%(detailhtml)s
<div style="max-width:860px;margin:22px auto 0;background:#fff;border:1px solid #e3e1db;padding:22px">
<h2 style="font-size:15px;border-left:4px solid #E8332A;padding-left:8px;margin-bottom:6px">상품 Q&amp;A</h2>
<div id="qnaList" style="font-size:13px;color:#999;padding:10px 4px">불러오는 중…</div>
<button onclick="askQ()" style="font:700 12.5px 'IBM Plex Sans KR';padding:9px 16px;border:0;background:#141414;color:#fff;cursor:pointer">상품 문의하기</button>
<div style="font-size:11px;color:#999;margin-top:8px">문의 답변은 마이페이지 &gt; 상품 Q&amp;A 내역에서도 확인할 수 있습니다.</div></div>
<div class="foot">SHOP SEONGSU, FROM ANYWHERE · %(pid)s</div></main>
<script>
var PID=%(pidjs)s, SOLD=%(soldjs)s, ST={login:false,liked:false,restock:false};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function swapMain(src){var ph=document.getElementById('mainPh');if(ph)ph.innerHTML='<img src="'+esc(src)+'" alt="">';}
function paint(){var lb=document.getElementById('likeBtn');
 lb.innerHTML=(ST.liked?'&#9829; 좋아요 취소':'&#9825; 좋아요');
 lb.style.background=ST.liked?'#141414':'#fff';lb.style.color=ST.liked?'#FFB000':'#141414';
 var rb=document.getElementById('rsBtn');
 if(SOLD){rb.style.display='block';rb.textContent=ST.restock?'재입고 알림 신청됨 (해제)':'재입고 알림 신청';}}
fetch('/api/member/pdp-state?product_id='+encodeURIComponent(PID)).then(function(r){return r.json()}).then(function(d){ST=d;paint()}).catch(function(){paint()});
function needLogin(){if(confirm('로그인이 필요합니다. 로그인 페이지로 이동할까요?'))location.href='/account';}
function post(u,b,cb){fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
 .then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.detail||'오류');return j})}).then(cb)
 .catch(function(e){alert(e.message)})}
function toggleLike(){if(!ST.login)return needLogin();
 post('/api/member/likes',{product_id:PID,on:!ST.liked},function(){ST.liked=!ST.liked;paint()})}
function toggleRestock(){if(!ST.login)return needLogin();
 post('/api/member/restock',ST.restock?{product_id:PID,off:true}:{product_id:PID},function(j){ST.restock=!!j.on;paint()})}
function askQ(){if(!ST.login)return needLogin();
 var q=prompt('상품에 대해 궁금한 점을 남겨주세요');if(!q)return;
 post('/api/member/pqna',{product_id:PID,question:q},function(){alert('문의가 접수되었습니다. 답변은 마이페이지에서 확인하세요.');})}
fetch('/api/pqna?product_id='+encodeURIComponent(PID)).then(function(r){return r.json()}).then(function(d){
 var el=document.getElementById('qnaList');
 if(!d.rows.length){el.textContent='아직 등록된 문의가 없습니다.';return}
 el.innerHTML=d.rows.map(function(x){return '<div style="border-bottom:1px solid #eee;padding:10px 2px;color:#141414">'+
 '<div style="font-weight:700">Q. '+esc(x.q)+' <span style="color:#aaa;font-weight:400;font-size:11px">'+esc(x.name)+' · '+esc(x.at)+'</span></div>'+
 '<div style="margin-top:6px;background:#faf9f5;padding:9px;white-space:pre-wrap">A. '+esc(x.a)+'</div></div>'}).join('')});
</script></body></html>'''

@admin_router.get('/p/{pid:path}', response_class=HTMLResponse)
def pdp(pid: str):
    try: ensure_ready()
    except Exception: pass
    if not _state['pcols']: raise HTTPException(404)
    sel = 'id, %s AS name, stock, soldout' % (_state['pname'] or 'id')
    if _state['pprice']: sel += ', %s AS price' % _state['pprice']
    for c in ('img', 'descr', 'category', 'detail_html', 'gallery'):
        if c in _state['pcols']: sel += ', ' + c
    r = one('SELECT %s FROM products WHERE id=?' % sel, (pid,))
    if not r:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>상품을 찾을 수 없습니다</h2><a href="/shop.html">SHOP으로</a>', status_code=404)
    def h(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    soldout = num(r.get('soldout')) or num(r.get('stock')) <= 0
    img = (r.get('img') or '').strip()
    # 카테고리 칩 → 해당 목록으로 이동 (앨범은 KPOP(음반) 전용관 직행)
    cat = norm_cat(r.get('category'))
    _cu = '/kpop' if cat == 'album' else ('/shop.html?cat=' + cat)
    _cl = 'KPOP(음반)' if cat == 'album' else _CAT_LABEL.get(cat, '')
    cathtml = ('<a class="cat" href="%s">%s</a>' % (_cu, h(_cl))) if cat else ''
    # 갤러리(추가 이미지) — 클릭 시 메인 이미지 교체
    gal = [u.strip() for u in re.split(r'[\r\n]+', r.get('gallery') or '') if u.strip().startswith('http')]
    galhtml = ''
    if gal:
        thumbs = ''.join('<img src="%s" alt="" onclick="swapMain(this.src)">' % h(u) for u in ([img] + gal if img else gal))
        galhtml = '<div class="gal">%s</div>' % thumbs
    # 상세 페이지 본문 — 게시판 블록(이미지+글)을 렌더 (레거시 HTML도 자동 처리)
    detailhtml = ''
    body_html = render_blocks(r.get('detail_html'))
    if body_html.strip():
        detailhtml = '<div class="detail"><h2>상세 정보</h2>%s</div>' % body_html
    return HTMLResponse(_PDP_HTML % {
        'name': h(r.get('name')), 'price': format(num(r.get('price')), ','),
        'bcls': 'no' if soldout else 'ok',
        'bmsg': '품절 (SOLD OUT)' if soldout else '구매 가능 · 재고 %d' % num(r.get('stock')),
        'descr': h(r.get('descr')) or 'MAPDAL SEOUL 상품입니다.',
        'imgtag': ('<img src="%s" alt="">' % h(img)) if img else 'MAPDAL SEOUL',
        'og': ('<meta property="og:image" content="%s">' % h(img)) if img else '',
        'cathtml': cathtml, 'galhtml': galhtml, 'detailhtml': detailhtml,
        'pid': h(pid), 'pidjs': json.dumps(pid), 'soldjs': 'true' if soldout else 'false'})

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

def kphone_norm(p):
    d = digits(p)
    if d.startswith('82'): d = '0' + d[2:]
    return d

def member_upsert(provider, sub, email, name):
    row = one('SELECT * FROM members WHERE provider=? AND sub=?', (provider, sub))
    if row:
        run('UPDATE members SET email=COALESCE(NULLIF(?, \'\'), email), name=COALESCE(NULLIF(?, \'\'), name) WHERE id=?',
            (email or '', name or '', row['id']))
        return row['id'], False
    mid = uid()
    run('INSERT INTO members(id,provider,sub,email,name,created) VALUES(?,?,?,?,?,?)',
        (mid, provider, sub, email or '', name or '', now_iso()))
    return mid, True

def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

import urllib.parse

@admin_router.get('/auth/google')
def auth_google(request: Request):
    cid = _genv('GOOGLE_CLIENT_ID')
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
            'code': p['code'], 'client_id': _genv('GOOGLE_CLIENT_ID'),
            'client_secret': _genv('GOOGLE_CLIENT_SECRET'),
            'redirect_uri': _burl(request) + '/auth/google/callback', 'grant_type': 'authorization_code'})
        req = urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',
                                     headers={'Authorization': 'Bearer ' + tok.get('access_token', '')})
        with urllib.request.urlopen(req, timeout=15) as r2:
            ui = json.loads(r2.read().decode())
    except urllib.error.HTTPError as e:
        try: err = json.loads(e.read().decode())
        except Exception: err = {}
        print('GOOGLE TOKEN ERROR:', err)
        code = err.get('error', 'unknown')
        hint = {'invalid_client': 'Render 환경변수 GOOGLE_CLIENT_SECRET 값이 콘솔의 클라이언트 보안 비밀과 다릅니다. 앞뒤 공백 없이 다시 붙여넣으세요.',
                'redirect_uri_mismatch': 'Google 콘솔 > 사용자 인증 정보 > 승인된 리디렉션 URI에 https://mapdal.kr/auth/google/callback 를 한 글자도 다르지 않게 등록하세요.',
                'invalid_grant': '인증 코드가 만료되었습니다. 아래 [다시 시도]를 눌러 처음부터 진행하세요.',
                'unauthorized_client': 'OAuth 클라이언트 유형이 웹 애플리케이션인지 확인하세요.'}.get(code, '클라이언트 ID/시크릿과 리디렉션 URI 설정을 확인하세요.')
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px;max-width:560px"><h3>Google 인증에 실패했습니다 <small style="color:#c0392b">(%s)</small></h3><p style="line-height:1.7">%s</p><a href="/account">다시 시도</a>' % (code, hint), status_code=400)
    except Exception:
        import traceback; traceback.print_exc()
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>Google 인증에 실패했습니다</h3><p>일시적 통신 오류일 수 있습니다. 다시 시도해 주세요.</p><a href="/account">다시 시도</a>', status_code=400)
    try:
        mid, is_new = member_upsert('google', str(ui.get('sub', '')), ui.get('email', ''), ui.get('name', ''))
        sid = member_session_make(mid)
    except Exception:
        import traceback; traceback.print_exc()
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>가입 처리 중 오류가 발생했습니다</h3><p>잠시 후 다시 시도해 주세요. 문제가 계속되면 관리자에게 문의하세요.</p><a href="/account">다시 시도</a>', status_code=500)
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('/account', status_code=302)
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    resp.delete_cookie('mp_oauth')
    return resp

@admin_router.get('/auth/kakao')
def auth_kakao(request: Request):
    cid = _genv('KAKAO_CLIENT_ID')
    if not cid:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>카카오 로그인 준비 중</h3><p>관리자가 KAKAO_CLIENT_ID(REST API 키) 환경변수를 설정하면 활성화됩니다.</p><a href="/account">돌아가기</a>')
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({'client_id': cid, 'redirect_uri': _burl(request) + '/auth/kakao/callback',
                                'response_type': 'code', 'state': state})
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('https://kauth.kakao.com/oauth/authorize?' + q, status_code=302)
    resp.set_cookie('mp_oauth', state, max_age=600, httponly=True, secure=True, samesite='none')
    return resp

@admin_router.get('/auth/kakao/callback')
def auth_kakao_cb(request: Request):
    try: ensure_ready()
    except Exception: pass
    p = request.query_params
    if p.get('error'):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>카카오 로그인이 취소되었습니다</h3><a href="/account">다시 시도</a>', status_code=400)
    if not p.get('code') or p.get('state') != (request.cookies.get('mp_oauth') or '_'):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>인증 세션이 만료되었습니다</h3><a href="/account">다시 시도</a>', status_code=400)
    form = {'grant_type': 'authorization_code', 'client_id': _genv('KAKAO_CLIENT_ID'),
            'redirect_uri': _burl(request) + '/auth/kakao/callback', 'code': p['code']}
    if _genv('KAKAO_CLIENT_SECRET'):
        form['client_secret'] = _genv('KAKAO_CLIENT_SECRET')
    try:
        tok = _post_form('https://kauth.kakao.com/oauth/token', form)
        req = urllib.request.Request('https://kapi.kakao.com/v2/user/me',
                                     headers={'Authorization': 'Bearer ' + tok.get('access_token', '')})
        with urllib.request.urlopen(req, timeout=15) as r2:
            ui = json.loads(r2.read().decode())
    except urllib.error.HTTPError as e:
        try: err = json.loads(e.read().decode())
        except Exception: err = {}
        print('KAKAO TOKEN ERROR:', err)
        kcode = err.get('error_code') or err.get('error', 'unknown')
        hint = {'KOE101': 'KAKAO_CLIENT_ID 값이 REST API 키가 맞는지 확인하세요 (네이티브/JS 키 아님).',
                'KOE006': '카카오 개발자 콘솔 > 카카오 로그인 > Redirect URI에 https://mapdal.kr/auth/kakao/callback 를 정확히 등록하세요.',
                'KOE010': 'KAKAO_CLIENT_SECRET 값이 콘솔의 Client Secret과 다릅니다.',
                'invalid_client': 'REST API 키 또는 Client Secret이 올바르지 않습니다.',
                'KOE320': '인증 코드가 만료되었습니다. 다시 시도해 주세요.',
                'invalid_grant': '인증 코드가 만료되었습니다. 다시 시도해 주세요.'}.get(kcode, '앱 키·Redirect URI·Client Secret 설정을 확인하세요.')
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px;max-width:560px"><h3>카카오 인증에 실패했습니다 <small style="color:#c0392b">(%s)</small></h3><p style="line-height:1.7">%s</p><a href="/account">다시 시도</a>' % (kcode, hint), status_code=400)
    except Exception:
        import traceback; traceback.print_exc()
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>카카오 인증에 실패했습니다</h3><p>잠시 후 다시 시도해 주세요.</p><a href="/account">다시 시도</a>', status_code=400)
    acct = ui.get('kakao_account') or {}
    nick = ((acct.get('profile') or {}).get('nickname') or '').strip()
    rname = (acct.get('name') or '').strip() or nick   # 실명 동의 시 실명 우선
    try:
        mid, is_new = member_upsert('kakao', str(ui.get('id', '')), acct.get('email', '') or '', rname)
        # 검수 승인된 항목이 응답에 실릴 때마다 자동 반영 (미승인 항목은 그냥 없음)
        sets, args = [], []
        kp = kphone_norm(acct.get('phone_number') or '')
        if len(kp) >= 10:  # 카카오 본인인증 번호 → 즉시 인증 처리(주문 자동연동)
            sets += ['phone=?', 'phone_verified=1']; args.append(kp)
        if acct.get('gender'): sets.append('gender=?'); args.append('F' if acct['gender'] == 'female' else 'M')
        if acct.get('age_range'): sets.append('age_range=?'); args.append(str(acct['age_range'])[:10])
        by, bd = str(acct.get('birthyear') or ''), str(acct.get('birthday') or '')
        if by or bd:
            sets.append('birth=?'); args.append((by + ('-' + bd[:2] + '-' + bd[2:4] if len(bd) == 4 else '')).strip('-')[:10])
        if acct.get('ci'): sets.append('ci=?'); args.append(str(acct['ci'])[:120])
        if sets:
            run('UPDATE members SET %s WHERE id=?' % ', '.join(sets), tuple(args + [mid]))
        # 배송지 동의 시: 카카오 배송지 → 배송지 관리에 자동 등록 (최초 1회)
        try:
            if not one('SELECT id FROM member_addresses WHERE member_id=? LIMIT 1', (mid,)):
                req2 = urllib.request.Request('https://kapi.kakao.com/v1/user/shipping_address',
                                              headers={'Authorization': 'Bearer ' + tok.get('access_token', '')})
                with urllib.request.urlopen(req2, timeout=10) as r3:
                    sa = json.loads(r3.read().decode())
                for ad in (sa.get('shipping_addresses') or [])[:1]:
                    run('INSERT INTO member_addresses VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (uid(), mid, (ad.get('name') or '기본')[:20], (ad.get('receiver_name') or rname)[:30],
                         digits(ad.get('receiver_phone_number1') or kp), str(ad.get('zone_number') or '')[:10],
                         (ad.get('base_address') or '')[:120], (ad.get('detail_address') or '')[:80],
                         1, now_iso()))
        except Exception:
            pass
        sid = member_session_make(mid)
    except Exception:
        import traceback; traceback.print_exc()
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>가입 처리 중 오류가 발생했습니다</h3><a href="/account">다시 시도</a>', status_code=500)
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse('/account', status_code=302)
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    resp.delete_cookie('mp_oauth')
    return resp

def _apple_conf():
    return {k: _genv(k) for k in ('APPLE_CLIENT_ID', 'APPLE_TEAM_ID', 'APPLE_KEY_ID', 'APPLE_PRIVATE_KEY')}

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
    try:
        mid, is_new = member_upsert('apple', str(claims.get('sub', '')), claims.get('email', ''), name)
        sid = member_session_make(mid)
    except Exception:
        import traceback; traceback.print_exc()
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:50px"><h3>가입 처리 중 오류가 발생했습니다</h3><p>잠시 후 다시 시도해 주세요.</p><a href="/account">다시 시도</a>', status_code=500)
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


_MYPAGE_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>마이페이지 — MAPDAL SEOUL</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2;--amber:#FFB000;--line:#e3e1db}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans KR',sans-serif;background:var(--paper);color:var(--black);font-size:14px}
header{background:var(--black);color:#fff;padding:0 20px;height:54px;display:flex;align-items:center;justify-content:space-between}
header a.logo{color:#fff;text-decoration:none;font-family:'Black Han Sans';font-size:19px}header a.logo span{color:var(--red)}
header .r a{color:#ccc;font-size:12px;font-weight:700;text-decoration:none;margin-left:16px}
main{max-width:1080px;margin:0 auto;padding:24px 16px 90px;display:grid;grid-template-columns:190px 1fr;gap:26px}
@media(max-width:820px){main{grid-template-columns:1fr}}
h1{font-size:24px;margin-bottom:18px}
aside .grp{font-size:15px;font-weight:800;margin:18px 0 10px}
aside a{display:block;font-size:13.5px;color:#555;text-decoration:none;padding:5px 0}
aside a.on{color:var(--red);font-weight:700}
aside hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.banner{background:var(--black);color:#fff;padding:16px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.banner .hi{font-weight:700}.banner .gr{background:var(--red);color:#fff;font-size:11px;font-weight:800;padding:3px 10px}
.banner .sp{margin-left:auto;display:flex;gap:22px;font-size:12.5px}
.banner .sp b{font-family:'IBM Plex Mono';font-size:16px;display:block;color:var(--amber)}
.steps{display:flex;background:#fff;border:1px solid var(--line);border-top:0;padding:20px 8px}
.steps .st{flex:1;text-align:center;position:relative}
.steps .st b{font-family:'IBM Plex Mono';font-size:26px;display:block;color:#bbb}
.steps .st.on b{color:var(--black)}
.steps .st span{font-size:12px;color:#777}
.steps .st:not(:last-child):after{content:'›';position:absolute;right:-4px;top:8px;color:#ccc;font-size:18px}
.panel{background:#fff;border:1px solid var(--line);padding:18px;margin-top:16px}
.panel h3{font-size:14px;border-left:4px solid var(--red);padding-left:8px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#faf9f5;border-bottom:2px solid var(--black);font-size:11.5px;padding:8px;text-align:left}
td{border-bottom:1px solid var(--line);padding:9px 8px;vertical-align:middle}
.r{text-align:right}.mono{font-family:'IBM Plex Mono'}
.tagst{font-size:11px;font-weight:800;padding:2px 8px;background:#eee}
.tagst.s1{background:#fff6e0;color:#9a6b00}.tagst.s2{background:#e9f7ee;color:#0a7d38}
.tagst.s3{background:#fff2f1;color:var(--red)}.tagst.s4{background:#e8f3ff;color:#1a5fb4}
.tagst.s5{background:#141414;color:#FFB000}.tagst.s0{background:#f0f0f0;color:#999}
button.b,a.b{font:inherit;font-weight:700;font-size:12px;border:0;padding:7px 12px;cursor:pointer;background:var(--black);color:#fff;text-decoration:none;display:inline-block}
button.b.ghost,a.b.ghost{background:#fff;color:var(--black);border:1px solid #999}
button.b.red{background:var(--red)}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid #ccc;background:#fff;width:100%}
input:focus,textarea:focus{outline:2px solid var(--red)}
label{display:block;font-size:11.5px;font-weight:700;color:#555;margin:12px 0 4px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.hint{font-size:11.5px;color:#888;margin-top:8px;line-height:1.7}
.empty{color:#999;text-align:center;padding:30px 10px}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--black);color:#fff;padding:10px 20px;display:none;z-index:200;font-weight:700}
.qa{border-bottom:1px solid var(--line);padding:12px 4px}
.qa .q{font-weight:700}.qa .a{margin-top:8px;color:#444;background:#faf9f5;padding:10px;white-space:pre-wrap}
.mob{display:none}@media(max-width:820px){aside{display:flex;flex-wrap:wrap;gap:4px 14px}aside .grp,aside hr{display:none}aside a{padding:6px 8px;background:#fff;border:1px solid var(--line)}}
</style></head><body>
<header><a class="logo" href="/">MAPDAL<span>SEOUL</span></a>
<div class="r"><a href="/shop.html">SHOP</a><a href="/cart.html">장바구니</a><a href="#" onclick="logout()">로그아웃</a></div></header>
<main>
<aside>
 <h1>마이페이지</h1>
 <div class="grp">쇼핑 활동</div>
 <a data-p="orders" class="on" href="#orders">주문/배송 조회</a>
 <a data-p="requests" href="#requests">취소/반품/교환 내역</a>
 <a data-p="receipts" href="#receipts">거래증빙서류 확인</a>
 <a href="/cart.html">장바구니</a>
 <a data-p="likes" href="#likes">좋아요</a>
 <a data-p="restock" href="#restock">재입고 알림</a>
 <hr><div class="grp">마이 정보</div>
 <a data-p="profile" href="#profile">회원정보 수정</a>
 <a data-p="addr" href="#addr">배송지/환불계좌 관리</a>
 <a data-p="store" href="#store">관심 매장 관리</a>
 <a data-p="withdraw" href="#withdraw">회원탈퇴</a>
 <hr><div class="grp">문의</div>
 <a data-p="inq" href="#inq">1:1 문의내역</a>
 <a data-p="pqna" href="#pqna">상품 Q&amp;A 내역</a>
</aside>
<section>
 <div class="banner"><span class="hi" id="hi"></span><span class="gr" id="gr"></span>
  <div class="sp"><span>포인트<b id="pt">0P</b></span><span>쿠폰<b>0개 <small style="color:#888;font-size:10px">(준비 중)</small></b></span>
  <span><a href="#profile" data-p="profile" style="color:var(--amber);font-size:12px;font-weight:700;text-decoration:none">나의 프로필 ›</a></span></div></div>
 <div class="steps" id="steps"></div>
 <div id="pane"></div>
</section></main>
<div id="toast"></div>
<script>
const MD = __MDATA__;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>'₩'+Number(n||0).toLocaleString('ko-KR');
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2400)}
async function api(u,opt){const r=await fetch(u,opt);if(!r.ok){let m='오류';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
async function post(u,b){return api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})}
async function logout(){await fetch('/api/member/logout',{method:'POST'});location.href='/'}
let OV=null;
async function boot(){OV=await api('/api/member/overview');
 $('#hi').textContent=OV.name+'님, 환영합니다';$('#gr').textContent=OV.grade;$('#pt').textContent=OV.points.toLocaleString()+'P';
 const names=['주문접수','결제완료','배송준비중','배송중','배송완료'];
 $('#steps').innerHTML=names.map((n,i)=>{const c=OV.counters[String(i+1)]||0;
  return '<div class="st'+(c?' on':'')+'"><b>'+c+'</b><span>'+n+'</span></div>'}).join('');
 route()}
const PANES={orders,requests:reqPane,receipts,likes:likesPane,restock:restockPane,profile,addr:addrPane,store:storePane,withdraw:withdrawPane,inq:inqPane,pqna:pqnaPane};
function route(){const p=(location.hash||'#orders').slice(1);
 document.querySelectorAll('aside a[data-p]').forEach(a=>a.className=a.dataset.p===p?'on':'');
 (PANES[p]||orders)()}
window.addEventListener('hashchange',route);
function needPhone(){return '<div class="panel"><h3>휴대폰 인증이 필요합니다</h3><div class="hint">주문은 비회원으로도 가능해서, <b>인증된 휴대폰 번호</b>로 회원님의 주문을 안전하게 연결합니다.<br>[회원정보 수정]에서 휴대폰 인증을 완료하면 해당 번호로 주문한 내역이 모두 표시됩니다.</div><div style="margin-top:12px"><a class="b" href="#profile">휴대폰 인증하러 가기</a></div></div>'}

async function orders(){if(!OV.linked){$('#pane').innerHTML=needPhone();return}
 const d=await api('/api/member/orders?range=3m');
 $('#pane').innerHTML='<div class="panel"><h3>주문/배송 조회 <small style="color:#888;font-weight:400">(최근 3개월)</small></h3>'+
 (d.rows.length?'<table><tr><th>주문번호/일시</th><th>상품</th><th class="r">금액</th><th>상태</th><th></th></tr>'+
 d.rows.map(o=>'<tr><td class="mono" style="font-size:11.5px">'+esc(o.order_id)+'<br><span style="color:#999">'+esc(o.created)+'</span></td>'+
 '<td>'+esc(o.label)+'</td><td class="r mono">'+won(o.amount)+'</td>'+
 '<td><span class="tagst s'+o.step+'">'+esc(o.status_kr)+'</span>'+(o.tracking?'<br><span class="mono" style="font-size:11px;color:#1a5fb4">'+esc(o.tracking)+'</span>':'')+'</td>'+
 '<td><button class="b ghost" onclick="orderDetail(\''+esc(o.order_id)+'\')">상세</button></td></tr>').join('')+'</table>':'<div class="empty">최근 3개월 주문이 없습니다</div>')+'</div><div id="odetail"></div>'}
async function orderDetail(oid){const o=await api('/api/member/orders/'+encodeURIComponent(oid));
 $('#odetail').innerHTML='<div class="panel"><h3>주문 상세 — '+esc(oid)+' <span class="tagst s'+o.step+'">'+esc(o.status_kr)+'</span></h3>'+
 '<table><tr><th>품목</th><th class="r">단가</th><th class="r">수량</th></tr>'+
 o.items.map(i=>'<tr><td>'+esc(i.name)+'</td><td class="r mono">'+won(i.price)+'</td><td class="r mono">'+i.qty+'</td></tr>').join('')+'</table>'+
 '<div class="hint">배송지 '+esc(o.addr)+' · 결제금액 '+won(o.amount)+(o.tracking?' · 송장 '+esc(o.tracking):'')+'</div>'+
 (o.open_request?'<div class="hint" style="color:var(--red)">'+({cancel:'취소',return:'반품',exchange:'교환'}[o.open_request.rtype]||'')+' 요청이 '+esc(o.open_request.status)+' 상태입니다.</div>':'')+
 '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">'+
 (o.receipt?'<a class="b ghost" target="_blank" href="'+esc(o.receipt)+'">토스 영수증</a>':'')+
 '<a class="b ghost" target="_blank" href="/api/member/receipt/'+encodeURIComponent(oid)+'">거래명세서</a>'+
 (!o.open_request&&o.can_cancel?'<button class="b red" onclick="reqOrder(\''+esc(oid)+'\',\'cancel\')">취소 요청</button>':'')+
 (!o.open_request&&o.can_return?'<button class="b" onclick="reqOrder(\''+esc(oid)+'\',\'return\')">반품 요청</button><button class="b ghost" onclick="reqOrder(\''+esc(oid)+'\',\'exchange\')">교환 요청</button>':'')+
 '</div></div>';$('#odetail').scrollIntoView({behavior:'smooth'})}
async function reqOrder(oid,t){const kr={cancel:'취소',return:'반품',exchange:'교환'}[t];
 const reason=prompt(kr+' 사유를 입력해 주세요');if(!reason)return;
 try{await post('/api/member/orders/'+encodeURIComponent(oid)+'/request',{rtype:t,reason});
 toast(kr+' 요청이 접수되었습니다');location.hash='#requests'}catch(e){alert(e.message)}}

async function reqPane(){const d=await api('/api/member/requests');
 $('#pane').innerHTML='<div class="panel"><h3>취소/반품/교환 요청</h3>'+
 (d.requests.length?'<table><tr><th>일시</th><th>주문번호</th><th>유형</th><th>사유</th><th>상태</th></tr>'+
 d.requests.map(r=>'<tr><td class="mono">'+esc(r.created)+'</td><td class="mono" style="font-size:11.5px">'+esc(r.order_id)+'</td><td><b>'+esc(r.rtype)+'</b></td><td>'+esc(r.reason)+(r.memo?'<br><span class="hint">답변: '+esc(r.memo)+'</span>':'')+'</td><td><span class="tagst '+(r.status==='완료'?'s2':r.status==='거절'?'s0':'s1')+'">'+esc(r.status)+'</span></td></tr>').join('')+'</table>':'<div class="empty">요청 내역이 없습니다</div>')+'</div>'+
 '<div class="panel"><h3>취소 완료된 주문</h3>'+(d.cancelled_orders.length?'<table><tr><th>주문번호</th><th>일자</th><th class="r">금액</th></tr>'+d.cancelled_orders.map(c=>'<tr><td class="mono">'+esc(c.order_id)+'</td><td class="mono">'+esc(c.created)+'</td><td class="r mono">'+won(c.amount)+'</td></tr>').join('')+'</table>':'<div class="empty">없음</div>')+'</div>'}

async function receipts(){if(!OV.linked){$('#pane').innerHTML=needPhone();return}
 const d=await api('/api/member/orders?range=all');const rs=d.rows.filter(o=>o.paid||o.receipt);
 $('#pane').innerHTML='<div class="panel"><h3>거래증빙서류</h3>'+
 (rs.length?'<table><tr><th>주문번호</th><th>일시</th><th class="r">금액</th><th>증빙</th></tr>'+
 rs.map(o=>'<tr><td class="mono" style="font-size:11.5px">'+esc(o.order_id)+'</td><td class="mono">'+esc(o.created)+'</td><td class="r mono">'+won(o.amount)+'</td>'+
 '<td>'+(o.receipt?'<a class="b ghost" target="_blank" href="'+esc(o.receipt)+'">토스 영수증</a> ':'')+
 '<a class="b ghost" target="_blank" href="/api/member/receipt/'+encodeURIComponent(o.order_id)+'">거래명세서</a></td></tr>').join('')+'</table>':'<div class="empty">결제 완료된 주문이 없습니다</div>')+
 '<div class="hint">세금계산서·현금영수증은 결제 시 신청 내역에 따라 토스 영수증에서 확인됩니다.</div></div>'}

async function likesPane(){const d=await api('/api/member/likes');
 const thumb=p=>p.img?'<img src="'+esc(p.img)+'" style="width:64px;height:64px;object-fit:cover;display:block" onerror="this.outerHTML=likePh()">':likePh();
 window.likePh=()=>'<div style=\"width:64px;height:64px;background:#141414;color:#FFB000;display:flex;align-items:center;justify-content:center;font-family:Black Han Sans;font-size:20px\">M</div>';
 $('#pane').innerHTML='<div class="panel"><h3>좋아요 <small style="color:#888;font-weight:400">'+d.rows.length+'개</small></h3>'+
 (d.rows.length?'<table>'+d.rows.map(p=>'<tr>'+
 '<td style="width:72px;padding:8px 4px"><a href="'+esc(p.link)+'">'+thumb(p)+'</a></td>'+
 '<td><a href="'+esc(p.link)+'" style="color:inherit;font-weight:500">'+esc(p.name)+'</a></td>'+
 '<td class="r mono" style="white-space:nowrap">'+(p.price?won(p.price):'-')+'</td>'+
 '<td>'+(p.soldout?'<span class="tagst s3">품절</span>':'<span class="tagst s2">구매가능</span>')+'</td>'+
 '<td class="r"><button class="b ghost" onclick="unlike(\''+p.rid+'\')">해제</button></td></tr>').join('')+'</table>':'<div class="empty">SHOP과 상품 페이지의 ♥ 버튼으로 담아보세요</div>')+'</div>'}
async function unlike(rid){await post('/api/member/likes/remove',{rid});likesPane()}

async function restockPane(){const d=await api('/api/member/restock');
 $('#pane').innerHTML='<div class="panel"><h3>재입고 알림</h3>'+
 (d.rows.length?'<table><tr><th>상품</th><th>신청일</th><th>상태</th><th></th></tr>'+
 d.rows.map(r=>'<tr><td><a href="/p/'+encodeURIComponent(r.id)+'" style="color:inherit">'+esc(r.name)+'</a></td><td class="mono">'+esc(r.created)+'</td>'+
 '<td>'+(r.notified?'<span class="tagst s2">알림 발송됨</span>':r.soldout?'<span class="tagst s1">입고 대기</span>':'<span class="tagst s2">재입고됨</span>')+'</td>'+
 '<td class="r">'+(!r.notified?'<button class="b ghost" onclick="restockOff(\''+esc(r.id).replace(/'/g,"\\'")+'\')">해제</button>':'')+'</td></tr>').join('')+'</table>':'<div class="empty">품절 상품 페이지에서 [재입고 알림]을 신청하세요</div>')+
 '<div class="hint">재입고 시 인증된 휴대폰으로 문자를 보내드립니다.</div></div>'}
async function restockOff(id){await post('/api/member/restock',{product_id:id,off:true});restockPane()}

async function profile(){const m=OV;
 $('#pane').innerHTML='<div class="panel"><h3>회원정보 수정</h3>'+
 '<label>이름</label><input id="pfn" value="'+esc(m.name)+'">'+
 '<label>이메일 ('+esc(m.provider)+' 가입)</label><input value="'+esc(m.email)+'" disabled style="background:#f4f3ef">'+
 '<div class="row2"><div><label>성별</label><select id="pfg"><option value="">선택 안 함</option><option value="F" '+(m.gender==='F'?'selected':'')+'>여성</option><option value="M" '+(m.gender==='M'?'selected':'')+'>남성</option></select></div>'+
 '<div><label>생년월일</label><input id="pfb" type="date" value="'+esc(m.birth)+'"></div></div>'+
 '<div style="margin-top:14px"><button class="b" onclick="saveName()">저장</button></div>'+
 (m.has_pw?'<hr style="border:0;border-top:1px solid var(--line);margin:18px 0"><h3>비밀번호 변경</h3>'+
 '<div class="row2"><div><label>현재 비밀번호</label><input id="pw0" type="password"></div><div></div>'+
 '<div><label>새 비밀번호 (8자 이상)</label><input id="pw1" type="password"></div><div><label>새 비밀번호 확인</label><input id="pw2" type="password"></div></div>'+
 '<div style="margin-top:12px"><button class="b" onclick="savePw()">비밀번호 변경</button></div>':'')+
 '</div><div class="panel"><h3>휴대폰 인증 '+(m.phone_verified?'<span class="tagst s2">인증됨 · '+esc(m.phone)+'</span>':'<span class="tagst s3">미인증</span>')+'</h3>'+
 '<div class="hint">인증된 번호로 주문내역이 연동되고, 재입고·배송 알림을 받습니다.</div>'+
 '<div class="row2" style="margin-top:10px"><div><label>휴대폰 번호</label><input id="phn" placeholder="010-0000-0000" value="'+esc(m.phone)+'"></div>'+
 '<div><label>&nbsp;</label><button class="b" style="width:100%;padding:10px" onclick="sendCode()">인증번호 발송</button></div></div>'+
 '<div class="row2" id="vrow" style="display:none;margin-top:4px"><div><label>인증번호 6자리</label><input id="vcd" maxlength="6"></div>'+
 '<div><label>&nbsp;</label><button class="b red" style="width:100%;padding:10px" onclick="verifyCode()">확인</button></div></div></div>'}
async function saveName(){try{await post('/api/member/profile',{name:$('#pfn').value,gender:$('#pfg').value,birth:$('#pfb').value});toast('저장되었습니다');OV=await api('/api/member/overview');boot()}catch(e){toast(e.message)}}
async function savePw(){if($('#pw1').value!==$('#pw2').value)return toast('새 비밀번호가 서로 다릅니다');
 try{await post('/api/member/password',{old:$('#pw0').value,new:$('#pw1').value});toast('변경되었습니다')}catch(e){toast(e.message)}}
async function sendCode(){try{const r=await post('/api/member/phone/send',{phone:$('#phn').value});
 $('#vrow').style.display='grid';toast(r.dry?'테스트 모드: 인증번호가 관리자 알림 로그에 기록되었습니다':'인증번호를 발송했습니다')}catch(e){toast(e.message)}}
async function verifyCode(){try{await post('/api/member/phone/verify',{code:$('#vcd').value});
 toast('인증 완료! 주문내역이 연동됩니다');OV=await api('/api/member/overview');boot();location.hash='#orders'}catch(e){toast(e.message)}}

async function addrPane(){const d=await api('/api/member/addresses');const m=OV;
 $('#pane').innerHTML='<div class="panel"><h3>배송지 관리</h3>'+
 (d.rows.length?'<table><tr><th>배송지명</th><th>받는분</th><th>주소</th><th></th></tr>'+
 d.rows.map(a=>'<tr><td><b>'+esc(a.label)+'</b>'+(a.is_default?' <span class="tagst s5">기본</span>':'')+'</td><td>'+esc(a.rname)+'<br><span class="mono" style="font-size:11px">'+esc(a.phone)+'</span></td>'+
 '<td style="font-size:12.5px">['+esc(a.zip)+'] '+esc(a.addr1)+' '+esc(a.addr2)+'</td>'+
 '<td class="r" style="white-space:nowrap">'+(!a.is_default?'<button class="b ghost" onclick="addrAct(\''+a.id+'\',\'default\')">기본설정</button> ':'')+'<button class="b ghost" onclick="addrAct(\''+a.id+'\',\'delete\')">삭제</button></td></tr>').join('')+'</table>':'<div class="empty">등록된 배송지가 없습니다</div>')+
 '<h3 style="margin-top:18px">새 배송지 추가</h3>'+
 '<div class="row2"><div><label>배송지명</label><input id="al" placeholder="집 / 회사"></div><div><label>받는분 *</label><input id="an"></div>'+
 '<div><label>연락처 *</label><input id="ap" placeholder="010-0000-0000"></div><div><label>우편번호 *</label><input id="az"></div></div>'+
 '<label>주소 *</label><input id="a1"><label>상세주소</label><input id="a2">'+
 '<div style="margin-top:12px"><button class="b" onclick="addrAdd()">배송지 추가</button></div></div>'+
 '<div class="panel"><h3>환불계좌 관리</h3><div class="hint">가상계좌·현금성 결제 환불 시 사용됩니다.</div>'+
 '<div class="row2" style="margin-top:8px"><div><label>은행명</label><input id="rb" value="'+esc(m.bank)+'"></div><div><label>예금주</label><input id="rn" value="'+esc(m.acct_name)+'"></div></div>'+
 '<label>계좌번호</label><input id="ra" value="'+esc(m.acct)+'">'+
 '<div style="margin-top:12px"><button class="b" onclick="saveAcct()">환불계좌 저장</button></div></div>'}
async function addrAdd(){try{await post('/api/member/addresses',{label:$('#al').value,rname:$('#an').value,phone:$('#ap').value,zip:$('#az').value,addr1:$('#a1').value,addr2:$('#a2').value});toast('추가되었습니다');addrPane()}catch(e){toast(e.message)}}
async function addrAct(id,act){if(act==='delete'&&!confirm('이 배송지를 삭제할까요?'))return;
 await post('/api/member/addresses',{act,id});addrPane()}
async function saveAcct(){try{await post('/api/member/profile',{bank:$('#rb').value,acct:$('#ra').value,acct_name:$('#rn').value});toast('저장되었습니다');OV=await api('/api/member/overview')}catch(e){toast(e.message)}}

async function storePane(){const on=OV.fav_store;
 $('#pane').innerHTML='<div class="panel"><h3>관심 매장 관리</h3>'+
 '<table><tr><td><b>맵달SEOUL 성수 플래그십</b><br><span class="hint">서울 성동구 성수이로16길 5 · 매일 11:00–21:00 · 825평 K-컬처 복합공간</span></td>'+
 '<td class="r" style="white-space:nowrap">'+(on?'<span class="tagst s2">관심 매장</span> <button class="b ghost" onclick="favStore(0)">해제</button>':'<button class="b red" onclick="favStore(1)">관심 매장 등록</button>')+'</td></tr></table>'+
 '<div class="hint">관심 매장으로 등록하면 오프라인 드롭·팬미팅·시식 이벤트 소식을 우선 안내해 드립니다.</div></div>'}
async function favStore(v){await post('/api/member/profile',{fav_store:v});OV.fav_store=v;toast(v?'관심 매장으로 등록했습니다':'해제되었습니다');storePane()}

async function withdrawPane(){const m=OV;
 $('#pane').innerHTML='<div class="panel"><h3>회원탈퇴</h3>'+
 '<div class="hint">탈퇴 시 좋아요·재입고 알림·배송지·문의 내역 등 회원 데이터가 즉시 삭제되며 복구할 수 없습니다.<br>주문·결제 기록은 전자상거래법에 따라 별도 보관됩니다.</div>'+
 (m.has_pw?'<label>비밀번호 확인</label><input id="wpw" type="password" style="max-width:320px">':'<label>아래 입력란에 <b>탈퇴</b> 를 입력해 주세요</label><input id="wcf" style="max-width:320px" placeholder="탈퇴">')+
 '<div style="margin-top:14px"><button class="b red" onclick="doWithdraw('+(m.has_pw?1:0)+')">탈퇴하기</button></div></div>'}
async function doWithdraw(pw){if(!confirm('정말 탈퇴하시겠습니까?'))return;
 try{await post('/api/member/withdraw',pw?{password:$('#wpw').value}:{confirm:$('#wcf').value});
 alert('탈퇴가 완료되었습니다. 이용해 주셔서 감사합니다.');location.href='/'}catch(e){toast(e.message)}}

async function inqPane(){const d=await api('/api/member/inquiries');
 $('#pane').innerHTML='<div class="panel"><h3>1:1 문의하기</h3>'+
 '<label>제목</label><input id="iqt"><label>내용</label><textarea id="iqb" rows="4"></textarea>'+
 '<label>관련 주문번호 (선택)</label><input id="iqo" placeholder="MPD...">'+
 '<div style="margin-top:12px"><button class="b" onclick="inqAdd()">문의 등록</button></div></div>'+
 '<div class="panel"><h3>1:1 문의내역</h3>'+
 (d.rows.length?d.rows.map(q=>'<div class="qa"><div class="q">'+esc(q.title)+' <span class="tagst '+(q.status==='답변완료'?'s2':'s1')+'">'+esc(q.status)+'</span> <span class="hint" style="display:inline">'+esc(q.created)+(q.order_id?' · '+esc(q.order_id):'')+'</span></div>'+
 '<div style="margin-top:6px;white-space:pre-wrap;font-size:13px">'+esc(q.body)+'</div>'+
 (q.answer?'<div class="a"><b>맵달SEOUL 답변</b> <span class="hint" style="display:inline">'+esc(q.answered_at)+'</span><br>'+esc(q.answer)+'</div>':'')+'</div>').join(''):'<div class="empty">문의 내역이 없습니다</div>')+'</div>'}
async function inqAdd(){try{await post('/api/member/inquiries',{title:$('#iqt').value,body:$('#iqb').value,order_id:$('#iqo').value});toast('문의가 접수되었습니다');inqPane()}catch(e){toast(e.message)}}

async function pqnaPane(){const d=await api('/api/member/pqna');
 $('#pane').innerHTML='<div class="panel"><h3>상품 Q&amp;A 내역</h3>'+
 (d.rows.length?d.rows.map(q=>'<div class="qa"><div class="q"><a href="/p/'+encodeURIComponent(q.product_id)+'" style="color:inherit">'+esc(q.product)+'</a> <span class="tagst '+(q.status==='답변완료'?'s2':'s1')+'">'+esc(q.status)+'</span> <span class="hint" style="display:inline">'+esc(q.created)+'</span></div>'+
 '<div style="margin-top:6px;font-size:13px">'+esc(q.question)+'</div>'+
 (q.answer?'<div class="a"><b>맵달SEOUL 답변</b><br>'+esc(q.answer)+'</div>':'')+'</div>').join(''):'<div class="empty">상품 페이지에서 문의를 남겨보세요</div>')+'</div>'}
boot();
</script></body></html>'''

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
        mdata = {'ok': True}
        return HTMLResponse(_MYPAGE_HTML.replace('__MDATA__', json.dumps(mdata, ensure_ascii=False)))
    g_on = bool(_genv('GOOGLE_CLIENT_ID'))
    a_on = all(_apple_conf().values())
    k_on = bool(_genv('KAKAO_CLIENT_ID'))
    social = ('<a class="sbtn kakao%s" href="/auth/kakao" style="background:#FEE500;color:#191919;border-color:#FEE500;font-weight:800">TALK · 카카오로 3초만에 시작하기%s</a>'
              % ('' if k_on else ' off', '' if k_on else ' (준비 중)')
              + '<a class="sbtn%s" href="/auth/google">G · Google 계정으로 계속하기%s</a>'
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
        '<div id="fS" style="display:none"><label>이름 *</label><input id="sn" autocomplete="name">'
        '<label>성별 *</label><div style="display:flex;gap:16px;padding:4px 2px"><label style="margin:0;font-weight:400;font-size:13px"><input type="radio" name="sg" value="F" style="width:auto"> 여성</label><label style="margin:0;font-weight:400;font-size:13px"><input type="radio" name="sg" value="M" style="width:auto"> 남성</label></div>'
        '<label>휴대폰 번호 *</label><input id="sph" placeholder="010-0000-0000" autocomplete="tel">'
        '<label>생년월일 (선택)</label><input id="sbi" type="date">'
        '<label>이메일 *</label><input id="se" type="email" autocomplete="email">'
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
        'const g=(document.querySelector(\'input[name=sg]:checked\')||{}).value;'
        'try{await post("/api/member/signup",{name:document.getElementById("sn").value,gender:g,phone:document.getElementById("sph").value,birth:document.getElementById("sbi").value,email:document.getElementById("se").value,password:p});location.reload()}catch(e){show(e.message)}}'
        '</script></body></html>')

@admin_router.get('/admin/api/members')
def api_members(request: Request):
    a = get_actor(request); need(a, 0)
    rs = rows('SELECT * FROM members ORDER BY created DESC LIMIT 300')
    total = num((one('SELECT COUNT(*) AS c FROM members') or {}).get('c'))
    return {'total': total, 'rows': [{'id': r['id'], 'provider': r.get('provider'), 'email': r.get('email') or '',
            'name': r.get('name') or '', 'created': (r.get('created') or '')[:16].replace('T', ' '),
            'phone': r.get('phone') or '', 'verified': num(r.get('phone_verified')),
            'points': num(r.get('points')), 'gender': r.get('gender') or '', 'birth': ((r.get('birth') or '')[:4] if len(r.get('birth') or '')==10 else (r.get('birth') or ''))} for r in rs]}

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
    gender = body.get('gender') or ''
    phone = digits(body.get('phone'))
    birth = (body.get('birth') or '').strip()[:10]
    if not name: raise HTTPException(400, '이름을 입력하세요')
    if gender not in ('F', 'M'): raise HTTPException(400, '성별을 선택해 주세요')
    if len(phone) < 10: raise HTTPException(400, '휴대폰 번호를 입력해 주세요')
    if not _EMAIL_RE.fullmatch(email): raise HTTPException(400, '이메일 형식을 확인하세요')
    if len(pw) < 8: raise HTTPException(400, '비밀번호는 8자 이상이어야 합니다')
    if one("SELECT id FROM members WHERE provider='email' AND email=?", (email,)):
        raise HTTPException(400, '이미 가입된 이메일입니다 — 로그인해 주세요')
    mid = uid()
    run('INSERT INTO members(id,provider,sub,email,name,created,pw,gender,phone,phone_verified,birth) VALUES(?,?,?,?,?,?,?,?,?,0,?)',
        (mid, 'email', email, email, name, now_iso(), pw_hash(pw), gender, phone, birth))
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


# ═══════════════ 법정 고지: 개인정보처리방침·이용약관·푸터 ═══════════════
def biz_info():
    return {'reg': _genv('BIZ_REG_NO') or '394-85-03267',
            'mail_order': _genv('BIZ_ORDER_NO') or '제2026-서울성동-0426호',
            'phone': _genv('BIZ_PHONE') or '010-8176-8525',
            'email': _genv('BIZ_EMAIL') or 'ceo@mealzip.kr'}

_POLICY_CSS = '''<style>:root{--red:#E8332A;--black:#141414;--paper:#F7F6F2}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'IBM Plex Sans KR','Malgun Gothic',sans-serif;background:var(--paper);color:var(--black);font-size:14px;line-height:1.8}
header{background:var(--black);color:#fff;padding:14px 20px}header a{color:#fff;text-decoration:none;font-family:'Black Han Sans',sans-serif;font-size:19px}header a span{color:var(--red)}
main{max-width:860px;margin:0 auto;padding:30px 20px 90px;background:#fff;border:1px solid #e3e1db;border-top:0}
h1{font-size:22px;border-bottom:3px solid var(--red);padding-bottom:12px;margin-bottom:8px}
h2{font-size:15.5px;margin:26px 0 8px;border-left:4px solid var(--red);padding-left:9px}
p,li{font-size:13.5px;color:#333}ul,ol{padding-left:20px;margin:6px 0}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12.5px}
th,td{border:1px solid #ddd;padding:7px 9px;text-align:left}th{background:#141414;color:#fff;font-size:11.5px}
.meta{font-size:12px;color:#888;margin-bottom:18px}.hl{background:#fff2f1;padding:2px 5px}
</style><link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">'''

PRIVACY_HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>개인정보처리방침 — MAPDAL SEOUL</title>''' + _POLICY_CSS + '''</head><body>
<header><a href="/">MAPDAL<span>SEOUL</span></a></header><main>
<h1>개인정보처리방침</h1>
<div class="meta">맵달서울성수(이하 "회사")는 「개인정보 보호법」 제30조에 따라 정보주체의 개인정보를 보호하고 관련 고충을 신속하게 처리하기 위하여 다음과 같이 개인정보처리방침을 수립·공개합니다.<br>공고일: 2026년 7월 7일 · 시행일: 2026년 7월 7일</div>

<h2>제1조 (개인정보의 처리 목적 및 수집 항목)</h2>
<p>회사는 다음 목적을 위해 개인정보를 처리하며, 목적이 변경되는 경우 별도 동의를 받습니다.</p>
<table><tr><th>구분</th><th>수집 항목</th><th>처리 목적</th><th>수집 방법</th></tr>
<tr><td>회원가입(필수)</td><td>이름, 성별, 연령대, 생년월일, 휴대폰 번호, 이메일 주소, 비밀번호(이메일 가입 시, 일방향 암호화 저장)</td><td>회원 식별·관리, 주문내역 연동, 본인 확인, 연령·성별 기반 상품 추천 및 통계, 생일 혜택 제공</td><td>회원가입 화면, 카카오·Google 계정 연동(동의 항목에 한함)</td></tr>
<tr><td>선택</td><td>배송지 정보(수령인, 주소, 연락처), 환불계좌(은행·계좌번호·예금주), 마케팅 수신 동의 여부</td><td>배송지 자동 입력 편의, 환불 처리, 이벤트·혜택 안내</td><td>마이페이지, 카카오 배송지 연동(동의 시)</td></tr>
<tr><td>주문/결제</td><td>주문자·수령인 정보(이름, 연락처, 주소), 주문·결제 내역</td><td>계약 이행(상품 배송), 결제·환불 처리, 고객 상담</td><td>주문서 작성 화면</td></tr>
<tr><td>자동 수집</td><td>접속 기록, 쿠키(로그인 세션 유지 목적)</td><td>서비스 제공 및 부정 이용 방지</td><td>서비스 이용 과정에서 자동 생성</td></tr></table>
<p>※ 회사는 주민등록번호, CI(연계정보) 등 고유식별정보를 수집하지 않습니다. 만 14세 미만 아동의 회원가입은 받지 않습니다.</p>

<h2>제2조 (개인정보의 보유 및 이용 기간)</h2>
<ul><li>회원 정보: 회원 탈퇴 시까지 (탈퇴 즉시 파기)</li>
<li>다만 관계 법령에 따라 다음 기간 동안 보존합니다 — 계약·청약철회 기록 5년, 대금결제·재화공급 기록 5년, 소비자 불만·분쟁처리 기록 3년(전자상거래법), 접속 기록 3개월(통신비밀보호법)</li></ul>

<h2>제3조 (개인정보 처리의 위탁)</h2>
<table><tr><th>수탁자</th><th>위탁 업무</th></tr>
<tr><td>토스페이먼츠 주식회사</td><td>전자결제(결제 승인·취소) 처리</td></tr>
<tr><td>주식회사 솔라피(SOLAPI)</td><td>휴대폰 본인확인·주문/배송 안내 문자 및 알림톡 발송</td></tr>
<tr><td>지정 택배사</td><td>상품 배송</td></tr></table>

<h2>제4조 (개인정보의 국외 이전)</h2>
<table><tr><th>이전받는 자</th><th>국가</th><th>이전 항목</th><th>이전 방법·일시</th><th>이용 목적·보유기간</th></tr>
<tr><td>Render Services, Inc.</td><td>미국</td><td>서비스 운영에 필요한 회원·주문 정보 일체</td><td>서비스 이용 시 정보통신망을 통한 전송</td><td>클라우드 서버 운영 및 데이터 보관 / 위 제2조의 보유기간</td></tr></table>
<p>정보주체는 국외 이전을 거부할 수 있으나, 이 경우 서비스 이용이 제한될 수 있습니다.</p>

<h2>제5조 (개인정보의 제3자 제공)</h2>
<p>회사는 정보주체의 동의 또는 법령의 규정에 의한 경우를 제외하고 개인정보를 제3자에게 제공하지 않습니다.</p>

<h2>제6조 (정보주체의 권리·의무 및 행사 방법)</h2>
<p>정보주체는 언제든지 개인정보 열람·정정·삭제·처리정지를 요구할 수 있습니다. 마이페이지에서 직접 조회·수정·탈퇴할 수 있으며, 아래 개인정보 보호책임자에게 서면·이메일로도 요청할 수 있습니다. 회사는 요청을 받은 날로부터 지체 없이 조치합니다.</p>

<h2>제7조 (개인정보의 파기)</h2>
<p>보유기간 경과·처리목적 달성 시 지체 없이 파기합니다. 전자적 파일은 복구 불가능한 방법으로 삭제하고, 출력물은 분쇄·소각합니다.</p>

<h2>제8조 (안전성 확보 조치)</h2>
<ul><li>비밀번호 일방향 암호화(PBKDF2-SHA256) 저장, 전 구간 SSL/TLS 암호화 전송</li>
<li>관리자 권한 등급 분리 및 접근·처리 기록(감사로그) 보관, 로그인 시도 제한</li>
<li>개인정보 취급 인원 최소화 및 교육</li></ul>

<h2>제9조 (쿠키의 운용)</h2>
<p>회사는 로그인 세션 유지를 위한 필수 쿠키만 사용하며, 광고 목적의 추적 쿠키를 사용하지 않습니다. 브라우저 설정에서 쿠키를 거부할 수 있으나 로그인 서비스 이용이 제한됩니다.</p>

<h2>제10조 (개인정보 보호책임자)</h2>
<table><tr><th>구분</th><th>내용</th></tr>
<tr><td>개인정보 보호책임자</td><td>황인범 (공동대표)</td></tr>
<tr><td>연락처</td><td>이메일: ceo@mealzip.kr / 전화: 010-8176-8525</td></tr></table>
<p>기타 개인정보 침해 신고·상담: 개인정보침해신고센터(privacy.kisa.or.kr, 국번없이 118), 개인정보분쟁조정위원회(kopico.go.kr, 1833-6972)</p>

<h2>제11조 (개인정보처리방침의 변경)</h2>
<p>본 방침의 내용 추가·삭제·수정 시 시행 7일 전부터 홈페이지 공지사항을 통해 고지합니다.</p>
</main></body></html>'''

TERMS_HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>이용약관 — MAPDAL SEOUL</title>''' + _POLICY_CSS + '''</head><body>
<header><a href="/">MAPDAL<span>SEOUL</span></a></header><main>
<h1>이용약관</h1>
<div class="meta">시행일: 2026년 7월 7일</div>

<h2>제1조 (목적)</h2>
<p>이 약관은 맵달서울성수(이하 "회사")가 운영하는 MAPDAL SEOUL 온라인 몰(mapdal.kr, 이하 "몰")에서 제공하는 전자상거래 서비스의 이용 조건 및 절차, 회사와 이용자의 권리·의무를 규정함을 목적으로 합니다.</p>

<h2>제2조 (정의)</h2>
<ol><li>"회원"이란 몰에 개인정보를 제공하여 가입한 자로서 몰의 서비스를 계속 이용할 수 있는 자를 말합니다.</li>
<li>"드롭(DROP)"이란 회사가 지정한 일시에 한정 수량으로 판매를 개시하는 방식을, "래플(RAFFLE)"이란 응모자 중 추첨을 통해 구매 자격을 부여하는 방식을 말합니다.</li></ol>

<h2>제3조 (약관의 명시와 개정)</h2>
<p>회사는 이 약관과 상호, 대표자, 주소, 사업자등록번호, 통신판매업 신고번호, 연락처 등을 몰의 초기 화면(하단)에 게시합니다. 회사는 관련 법령을 위배하지 않는 범위에서 약관을 개정할 수 있으며, 개정 시 적용일자 7일 전(회원에게 불리한 변경은 30일 전)부터 공지합니다.</p>

<h2>제4조 (회원가입 및 탈퇴)</h2>
<ol><li>회원가입은 이메일 또는 카카오·Google 계정 연동으로 신청하며, 회사가 승낙함으로써 성립합니다.</li>
<li>만 14세 미만은 회원으로 가입할 수 없습니다.</li>
<li>회원은 마이페이지의 회원탈퇴 기능으로 언제든지 탈퇴할 수 있으며, 회사는 관계 법령상 보존 의무가 있는 정보를 제외하고 즉시 회원 정보를 파기합니다.</li>
<li>타인 정보 도용, 허위 정보 기재, 부정한 방법의 래플 응모(다계정·매크로 등)의 경우 회사는 이용을 제한하거나 자격을 상실시킬 수 있습니다.</li></ol>

<h2>제5조 (구매신청 및 계약의 성립)</h2>
<p>이용자는 몰에서 상품 선택, 주문자·배송지 정보 입력, 결제의 절차로 구매를 신청합니다. 계약은 회사의 주문 확인(결제 승인) 통지가 이용자에게 도달한 때 성립합니다. 재고 소진, 정보 오기재, 부정 주문의 경우 회사는 주문을 취소할 수 있으며 이 경우 결제 금액 전액을 환불합니다.</p>

<h2>제6조 (드롭·래플에 관한 특칙)</h2>
<ol><li>래플 응모 자격, 응모 기간, 당첨자 수, 결제 기한은 각 상품 페이지에 공지하며, 추첨은 공정한 방식으로 진행합니다.</li>
<li>당첨자가 결제 기한 내 결제하지 않으면 당첨은 자동 취소되며, 회사는 예비 당첨자에게 기회를 부여할 수 있습니다.</li>
<li>부정 응모가 확인되면 당첨 취소 및 향후 응모가 제한될 수 있습니다.</li></ol>

<h2>제7조 (결제)</h2>
<p>대금 결제는 토스페이먼츠를 통한 신용·체크카드, 계좌이체, 간편결제 등 몰이 제공하는 방법으로 할 수 있습니다. 회사는 결제 정보를 직접 저장하지 않으며, 결제 금액은 서버에서 재검증됩니다.</p>

<h2>제8조 (배송)</h2>
<p>회사는 결제 확인 후 영업일 기준 통상 2~5일 이내 상품을 발송합니다(냉장·냉동 식품은 콜드체인 배송). 천재지변, 물류 사정 등 불가항력 사유가 있는 경우 그 기간은 배송 기간에서 제외됩니다.</p>

<h2>제9조 (청약철회 및 반품·교환)</h2>
<ol><li>이용자는 상품을 공급받은 날부터 7일 이내에 청약철회를 할 수 있습니다.</li>
<li>다만 「전자상거래 등에서의 소비자보호에 관한 법률」 제17조 제2항에 따라 다음의 경우 청약철회가 제한됩니다.
<ul><li>이용자의 책임 있는 사유로 상품이 멸실·훼손된 경우</li>
<li><span class="hl">신선·냉장·냉동식품 등 시간이 지나 다시 판매하기 곤란할 정도로 가치가 현저히 감소한 경우(개봉·해동 포함)</span></li>
<li>이용자의 사용 또는 일부 소비로 가치가 현저히 감소한 경우</li></ul></li>
<li>상품이 표시·광고 내용과 다르거나 계약 내용과 다르게 이행된 경우, 공급받은 날부터 3개월 이내 또는 그 사실을 안 날부터 30일 이내에 청약철회를 할 수 있습니다.</li>
<li>청약철회 시 회사는 상품 반환을 받은 날부터 3영업일 이내에 대금을 환급합니다. 단순 변심에 의한 반품 배송비는 이용자가 부담합니다.</li></ol>

<h2>제10조 (포인트)</h2>
<p>회사는 회원에게 포인트를 부여할 수 있습니다. 포인트의 적립 기준, 사용 방법, 유효기간은 별도 공지하며, 현재 결제 시 사용 기능은 준비 중입니다. 포인트는 현금으로 환급되지 않으며 회원 탈퇴 시 소멸합니다.</p>

<h2>제11조 (회사와 이용자의 의무)</h2>
<p>회사는 법령과 이 약관에 따라 지속적이고 안정적으로 서비스를 제공하며, 이용자의 개인정보를 개인정보처리방침에 따라 보호합니다. 이용자는 타인의 정보 도용, 몰 운영 방해, 지식재산권 침해 행위를 하여서는 안 됩니다.</p>

<h2>제12조 (면책 및 분쟁 해결)</h2>
<p>회사는 천재지변 등 불가항력으로 인한 서비스 장애에 대해 책임을 지지 않습니다. 회사는 이용자의 불만 및 분쟁을 신속히 처리하며, 처리가 곤란한 경우 공정거래위원회 또는 시·도 소비자분쟁조정기구의 조정에 따를 수 있습니다. 회사와 이용자 간 소송은 민사소송법상의 관할법원에 제기합니다.</p>

<h2>부칙</h2>
<p>이 약관은 2026년 7월 7일부터 시행합니다.</p>
</main></body></html>'''

FOOTER_SNIPPET_TPL = '''<footer id="mpFooter" style="background:#141414;color:#fff;font:12px/1.9 'IBM Plex Sans KR',sans-serif;margin:0;padding:0;border-top:1px solid rgba(255,255,255,.09)">
<div style="max-width:1440px;margin:0 auto;padding:26px 48px 40px">
<div style="margin-bottom:10px"><a href="/terms.html" style="color:#fff;text-decoration:none;margin-right:16px">이용약관</a><a href="/privacy.html" style="color:#FFB000;font-weight:800;text-decoration:none">개인정보처리방침</a></div>
<div><span style="color:#fff;font-weight:700">맵달서울성수</span> · 공동대표 황인범, 김동경 · 서울특별시 성동구 성수이로16길 5 (성수동2가)<br>
사업자등록번호 {reg} · 통신판매업신고 {mail_order} · 전화 {phone} · 이메일 {email}<br>
호스팅서비스 제공: Render Services, Inc.<br>
<span style="font-size:11px;color:#777">MAPDAL SEOUL — SHOP SEONGSU, FROM ANYWHERE.</span></div>
</div></footer>'''

def footer_snippet():
    return FOOTER_SNIPPET_TPL.format(**biz_info())

LIKE_SNIPPET = r"""<script id="mpLikeJs">(function(){
var RX=/(^|\/)(product-[A-Za-z0-9._-]+\.html|album-detail\.html\?uid=[A-Za-z0-9_-]+)([?#]|$)/;
var ST={login:false,liked:{}},seen={};
function hrefOf(a){try{return a.getAttribute('href')||''}catch(e){return ''}}
function key(h){return h.split('#')[0]}
function nameOf(a){var t=(a.textContent||'').replace(/\s+/g,' ').trim();var i=t.indexOf('\u20A9');if(i>0)t=t.slice(0,i);return t.trim().slice(0,60)}
function priceOf(a){var m=(a.textContent||'').match(/\u20A9\s*([\d,]+)/);return m?Number(m[1].replace(/,/g,'')):0}
function imgOf(a){var i=a.querySelector('img');if(!i)return '';return (i.currentSrc||i.getAttribute('src')||'').slice(0,300)}
function mkBtn(a,h){var b=document.createElement('button');b.className='mpLike';b.type='button';
 b.style.cssText='position:absolute;top:8px;right:8px;z-index:5;width:32px;height:32px;border:0;border-radius:50%;background:rgba(255,255,255,.92);color:#E8332A;font-size:16px;line-height:32px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.18);padding:0';
 paint(b,!!ST.liked[h]);
 b.onclick=function(ev){ev.preventDefault();ev.stopPropagation();
  if(!ST.login){if(confirm('로그인이 필요합니다. 로그인 페이지로 이동할까요?'))location.href='/account';return}
  var on=!ST.liked[h];
  fetch('/api/member/likes/page',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({href:h,name:nameOf(a),price:priceOf(a),img:imgOf(a),on:on})})
  .then(function(r){if(!r.ok)throw 0;ST.liked[h]=on?1:0;paint(b,on)})
  .catch(function(){alert('잠시 후 다시 시도해 주세요')})};
 var st=getComputedStyle(a).position;if(st==='static'||!st)a.style.position='relative';
 a.appendChild(b)}
function paint(b,on){b.innerHTML=on?'\u2665':'\u2661';b.style.color=on?'#E8332A':'#141414';b.title=on?'\uC88B\uC544\uC694 \uCDE8\uC18C':'\uC88B\uC544\uC694'}
function scan(){var as=document.querySelectorAll('a[href]'),fresh=[];
 for(var i=0;i<as.length;i++){var a=as[i],h=key(hrefOf(a));
  if(!RX.test(h))continue;if(!a.querySelector('img'))continue;if(seen[h])continue;
  seen[h]=1;fresh.push([a,h])}
 if(!fresh.length)return;
 var pages=fresh.map(function(x){return x[1]});
 fetch('/api/member/likes/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pages:pages})})
 .then(function(r){return r.json()}).catch(function(){return{login:false,liked:[]}})
 .then(function(d){ST.login=!!d.login;(d.liked||[]).forEach(function(h){ST.liked[h]=1});
  fresh.forEach(function(x){mkBtn(x[0],x[1])})})}
function go(){scan();try{new MutationObserver(function(){clearTimeout(go._t);go._t=setTimeout(scan,400)}).observe(document.body,{childList:true,subtree:true})}catch(e){}}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go)}else{go()}
})();</script>"""

MOBNAV_SNIPPET = r"""<style id="mpMobNav">
/* ── 모바일 상시 카테고리 바 ── */
#mpCatBar{display:none;align-items:stretch;gap:2px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;background:var(--paper,#F7F6F2);border-top:1px solid var(--line,#E3E1DB);padding:0 8px}
#mpCatBar::-webkit-scrollbar{display:none}
#mpCatBar a{flex:0 0 auto;display:flex;align-items:center;padding:11px 9px 9px;font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--ink,#141414);text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
#mpCatBar a.red{color:var(--red,#E8332A)}
#mpCatBar a.on{color:var(--red,#E8332A);border-bottom-color:var(--red,#E8332A)}
@media(max-width:1024px){
 html,body{overflow-x:clip}
 #mpCatBar{display:flex}
 .header-inner{padding:0 12px;height:54px}
 .logo{font-size:21px;white-space:nowrap}
 .util{gap:10px;min-width:0}
 .util a{white-space:nowrap;font-size:12px}
 .util a.cart{padding:5px 10px;font-size:11px}
 .global-bar{flex-wrap:wrap;row-gap:2px;padding:5px 12px;font-size:10px;line-height:1.5}
 .global-bar>*:first-child{white-space:nowrap}
 .global-bar .right{margin-left:auto;gap:10px}
}
@media(max-width:400px){
 .logo{font-size:18px}
 .util{gap:8px}
 .util a{font-size:11px}
 .util a.cart{padding:4px 8px}
 .header-inner{padding:0 10px}
 #mpCatBar a{font-size:12px;letter-spacing:.04em}
}
/* ── 모바일 상품 그리드 (SHOP) ── */
@media(min-width:641px) and (max-width:1024px){
 #shopGrid{grid-template-columns:repeat(3,1fr)!important;gap:14px!important}
}
@media(max-width:640px){
 #shopGrid{grid-template-columns:repeat(2,1fr)!important;gap:10px!important}
 #shopGrid .col-cover{height:190px;padding:14px}
 #shopGrid .col-cover .big{font-size:30px}
 #shopGrid .col-cover .tag{top:10px;left:10px;font-size:8.5px;padding:4px 7px}
 #shopGrid .col-body{padding:12px 12px 14px}
 #shopGrid .col-body h3{font-size:13.5px;margin-bottom:4px}
 #shopGrid .col-body p{font-size:11.5px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 #shopGrid .col-body .meta{font-size:9.5px;margin-top:10px}
 .k2g-body{padding:10px 10px 12px}
 .k2g-price .amt{font-size:12.5px}
 .k2g-price .pct{font-size:13.5px}
}
@media(min-width:1025px){#mpCatBar{display:none!important}}
</style><script>(function(){
if(window.__mpMobNav)return;window.__mpMobNav=1;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function ready(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f()}
ready(function(){
 var header=document.querySelector('header');
 if(!header||document.getElementById('mpCatBar'))return;
 /* 데스크톱 네비에서 상위 카테고리 수확 → 페이지별 링크·활성 상태 자동 동기화 */
 var items=[],nav=document.querySelector('nav.main');
 if(nav){var ds=nav.children;
  for(var i=0;i<ds.length;i++){var a=ds[i].querySelector('a.top')||ds[i].querySelector('a');if(!a)continue;
   items.push({label:(a.textContent||'').replace(/\s+/g,' ').trim(),href:a.getAttribute('href')||'#',
               red:(a.className||'').indexOf('drops')>=0})}}
 if(!items.length)items=[
  {label:'NEW / DROPS',href:'/new-drops.html',red:true},
  {label:'SHOP',href:'/shop.html',red:false},
  {label:'MAPDAL SEOUL',href:'/mapdal-seoul.html',red:false},
  {label:'SUPPORT',href:'/support.html',red:false}];
 var cur=(location.pathname.split('/').pop()||'').toLowerCase();
 function base(h){return (h||'').split(/[?#]/)[0].split('/').pop().toLowerCase()}
 var h='';
 for(var j=0;j<items.length;j++){var t=items[j],on=cur&&base(t.href)===cur;
  h+='<a class="'+(t.red?'red ':'')+(on?'on':'')+'" href="'+esc(t.href)+'">'+esc(t.label)+'</a>'}
 var bar=document.createElement('nav');bar.id='mpCatBar';
 bar.setAttribute('aria-label','\uce74\ud14c\uace0\ub9ac');
 bar.innerHTML=h;
 header.appendChild(bar);
});})();</script>"""

# ── LED 드롭 티커: DB 저장 → 전 페이지 동적 반영 ──────────────────────
#    관리자 대시보드 [티커] 탭에서 문구·속도 수정. 저장 즉시 모든 페이지의
#    .ticker-track이 DB 내용으로 교체된다(정적 HTML 53개는 수정 불필요).
#    저장 이력이 없으면 아래 기본값(현재 하드코딩과 동일)을 반환한다.
TICKER_DEFAULT = {
    'items': ['NEXT DROP **07.11 SAT 12:00 KST** — SUMMER DROP 07',
              '팬사인회 응모 **07.09 마감** — 네온서울 성수 팬미팅',
              'LIVE **매주 금 20:00** — 맵달APP · TikTok Shop · YouTube',
              'NEW **ONLINE NOW** — 맵달 KIMBAP 6종 · 맵달 BOWL 6종'],
    'speed': 'normal'}

def _setting_put(key, value, by=''):
    v = json.dumps(value, ensure_ascii=False)
    if run('UPDATE site_settings SET value=?, updated=?, by_admin=? WHERE key=?',
           (v, now_iso(), by, key)) == 0:
        run('INSERT INTO site_settings VALUES(?,?,?,?)', (key, v, now_iso(), by))

def ticker_conf():
    """저장값 없으면 기본값. 저장값이 있으면(빈 목록 포함) 그대로 — 빈 목록 = 티커 숨김."""
    try:
        r = one('SELECT value, updated, by_admin FROM site_settings WHERE key=?', ('ticker',))
    except Exception:
        r = None
    if not r:
        return dict(TICKER_DEFAULT, updated='', by_admin='', is_default=True)
    d = jload(r.get('value'), {}) or {}
    items = [str(x).strip() for x in (d.get('items') or []) if str(x or '').strip()]
    speed = d.get('speed') if d.get('speed') in ('slow', 'normal', 'fast') else 'normal'
    return {'items': items, 'speed': speed, 'updated': r.get('updated') or '',
            'by_admin': r.get('by_admin') or '', 'is_default': False}

@admin_router.get('/api/ticker')
def api_ticker_public():
    try: ensure_ready()
    except Exception: pass
    c = ticker_conf()
    return JSONResponse({'items': c['items'], 'speed': c['speed']},
                        headers={'Cache-Control': 'no-store'})

@admin_router.get('/admin/api/ticker')
def api_ticker_get(request: Request):
    get_actor(request)
    return ticker_conf()

@admin_router.post('/admin/api/ticker/save')
def api_ticker_save(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '티커 관리')
    raw = body.get('items')
    if not isinstance(raw, list): raise HTTPException(400, 'items는 목록이어야 합니다')
    items = []
    for x in raw:
        s = re.sub(r'\s+', ' ', str(x or '')).strip()
        if s: items.append(s[:200])
    if len(items) > 12: raise HTTPException(400, '티커 항목은 최대 12개까지 가능합니다')
    speed = body.get('speed') if body.get('speed') in ('slow', 'normal', 'fast') else 'normal'
    _setting_put('ticker', {'items': items, 'speed': speed}, a['name'])
    audit(a, '티커저장', '', '%d개 항목 · %s' % (len(items), speed))
    return {'ok': True, 'items': items, 'speed': speed}

# 전 페이지 주입 스크립트: .ticker-track을 /api/ticker 내용으로 교체.
# **문구** → <b>흰색 강조</b> · 항목 0개면 티커 숨김 · API 이상 시 기존 문구 유지(fail-open)
# 항목이 짧아도 화면 폭을 채울 때까지 반복(-50% 루프 이음새 방지) · 체감 속도 일정하게 duration 자동 계산
TICKER_SNIPPET = r"""<script id="mpTickerJs">(function(){
if(window.__mpTicker)return;window.__mpTicker=1;
function ready(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f()}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function toHTML(t){return esc(t).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')}
ready(function(){
 var tk=document.querySelector('.ticker-track');if(!tk)return;
 fetch('/api/ticker',{cache:'no-store'}).then(function(r){return r.ok?r.json():null}).then(function(d){
  if(!d||!Array.isArray(d.items))return;
  var box=tk.closest('.ticker')||tk.parentElement;
  var items=[],i;
  for(i=0;i<d.items.length;i++){var s=String(d.items[i]==null?'':d.items[i]).trim();if(s)items.push(s)}
  if(!items.length){if(box)box.style.display='none';return}
  if(box)box.style.display='';
  var unit0='';
  for(i=0;i<items.length;i++)unit0+='<span>'+toHTML(items[i])+'</span>';
  function render(rep){var u='';for(var k=0;k<rep;k++)u+=unit0;tk.innerHTML=u+u}
  render(1);
  var px={slow:38,normal:55,fast:80}[d.speed]||55;
  (window.requestAnimationFrame||function(f){setTimeout(f,16)})(function(){
   var half=tk.scrollWidth/2,vw=window.innerWidth||1280;
   if(half>0){
    var rep=Math.min(6,Math.max(1,Math.ceil(vw/half)));
    if(rep>1){render(rep);half=tk.scrollWidth/2}
    tk.style.animationDuration=Math.max(10,Math.round(half/px))+'s';
   }
  });
 }).catch(function(){});
});})();</script>"""

def _patch_legacy_footer(html):
    """목업 원본 푸터의 구형 법적표기 블록(.foot-base)을 통째로 제거.
    법정 표기는 표준 푸터(mpFooter) 단일 출처로 일원화한다."""
    html, n1 = re.subn(r'<div class="foot-base">.*?</div>\s*', '', html, flags=re.S)
    html, n2 = re.subn(r'<script id="mpFootWhite">.*?</script>\s*', '', html, flags=re.S)
    html, n3 = re.subn(r'<footer id="mpFooter".*?</footer>\s*', '', html, flags=re.S)
    return html, n1 + n2 + n3

# ── SHOP 목록 연동: 직접등록(mp::) 상품을 shop 그리드에 서버 측 주입 ──────
#   · 대상: id가 'mp::'로 시작하는 상품만 (k2g:: 카탈로그는 페이지에 이미 인라인 → 중복 방지)
#   · 기존 col-card 마크업·클래스를 그대로 사용 → 필터 탭(data-cat)·검색과 자동 연동
#   · 삽입 위치: #shopGrid 여는 태그 직후 → 각 카테고리 탭에서 자체 상품이 먼저 노출
#   · 품절: SOLD OUT 태그 + 흑백 처리 + '담기' 대신 '품절' (숨기지 않고 노출 유지)
_SHOP_GRID_RE = re.compile(r'(<div[^>]*id="shopGrid"[^>]*>)')
_MP_CAT_TAG = {'album': 'ALBUM', 'md': 'MD', 'kfood': 'K-FOOD', 'apparel': 'APPAREL', 'living': 'LIVING'}
_MP_COVERS = ('linear-gradient(160deg,#141414,#3A3A3A)', 'linear-gradient(160deg,#7A1613,#E8332A)',
              'linear-gradient(160deg,#5C3D00,#B87F00)', 'linear-gradient(160deg,#1E1E60,#4B3AE8)',
              'linear-gradient(160deg,#20603C,#57B87B)')
_MP_IMG_OK = re.compile(r'(?:https?://|/admin/asset/)[^\s\'"()<>\\]+\Z')

def _mp_shop_cards():
    """직접등록 상품 → shop 그리드용 카드 HTML. 어떤 오류에도 빈 문자열 반환(페이지 서빙은 계속)."""
    try:
        ensure_ready()
        if not _state['pcols'] or not _state['pname']:
            return ''
        sel = 'id, %s AS name, stock, soldout' % _state['pname']
        if _state['pprice']:
            sel += ', %s AS price' % _state['pprice']
        for c in ('img', 'category', 'badge'):
            if c in _state['pcols']:
                sel += ', ' + c
        rs = rows('SELECT %s FROM products WHERE id LIKE ? ORDER BY id' % sel, ('mp::%',))
    except Exception:
        return ''
    if not rs:
        return ''
    def h(x):
        return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    cards = []
    for i, r in enumerate(rs):
        name = str(r.get('name') or r['id'])
        cat = norm_cat(r.get('category'))
        soldout = bool(num(r.get('soldout')) or num(r.get('stock')) <= 0)
        badge = str(r.get('badge') or '').strip()
        tag = (badge or _MP_CAT_TAG.get(cat, 'MAPDAL')) + (' · SOLD OUT' if soldout else '')
        gray = ';filter:grayscale(.85);opacity:.75' if soldout else ''
        img = (r.get('img') or '').strip()
        if _MP_IMG_OK.fullmatch(img):
            cover = ('<div class="col-cover" style="background:#EDECE7 url(\'%s\') center/cover no-repeat;height:240px%s">'
                     '<span class="tag">%s</span></div>') % (h(img), gray, h(tag))
        else:
            cover = ('<div class="col-cover" style="background:%s;height:240px%s">'
                     '<span class="tag">%s</span><span class="big" style="font-size:44px">%s</span></div>'
                     ) % (_MP_COVERS[i % len(_MP_COVERS)], gray, h(tag), h(name[:2]))
        cards.append('<a class="col-card" data-cat="%s" href="/p/%s">%s'
                     '<div class="col-body"><h3>%s</h3><div class="price-row">'
                     '<span class="price">₩%s</span><span class="add">%s</span></div></div></a>'
                     % (h(cat), h(r['id']), cover, h(name),
                        format(num(r.get('price')), ','), '품절' if soldout else '담기 +'))
    return '<!-- mpShopDyn -->' + ''.join(cards) + '<!-- /mpShopDyn -->'

def _inject_shop_products(html):
    if 'id="shopGrid"' not in html or 'mpShopDyn' in html:
        return html
    cards = _mp_shop_cards()
    if not cards:
        return html
    return _SHOP_GRID_RE.sub(lambda m: m.group(1) + cards, html, count=1)

# ── K2G 카탈로그: DB 단일 출처 ──────────────────────────────────────────
#   shop.html·album-detail.html의 인라인 배열(const K2G=[[...]])을 서빙 시
#   DB(products의 k2g:: 행)로 재구성해 치환한다. 관리자에서의 가격·품절·
#   상품명 변경과 삭제가 사이트에 즉시 반영되며, 정적 스냅숏은 DB 장애 시
#   폴백으로만 사용된다. 최초 1회, 정적 배열의 정가·이미지·정렬 순서를
#   DB로 백필하고 배열에만 있던 앨범은 신규 INSERT한다(삭제 기록 제외).
_k2g_rm_cache = {'t': 0.0, 'set': None}

def _k2g_removed_set():
    if _k2g_rm_cache['set'] is not None and time.time() - _k2g_rm_cache['t'] < 30:
        return _k2g_rm_cache['set']
    try:
        s = {str(r['uid']) for r in rows('SELECT uid FROM k2g_removed')}
    except Exception:
        s = _k2g_rm_cache['set'] or set()
    _k2g_rm_cache.update(t=time.time(), set=s)
    return s

_k2g_bounds_cache = {}

def _find_k2g_array_bounds(html):
    """'const K2G=' 뒤 배열 리터럴의 [시작, 끝) 인덱스.
    C 가속 JSON 파서(raw_decode)로 끝을 찾고 페이지 지문으로 캐시,
    배열이 순수 JSON이 아니면 상태 기계 스캔으로 폴백."""
    k = html.find('const K2G=')
    if k < 0:
        return None
    i = html.find('[', k)
    if i < 0:
        return None
    fp = (len(html), i, html[i:i + 48])
    b = _k2g_bounds_cache.get(fp)
    if b and b[1] <= len(html) and html[b[0]] == '[' and html[b[1] - 1] == ']':
        return b
    try:
        _, end = json.JSONDecoder().raw_decode(html, i)
        b = (i, end)
    except Exception:
        b = _scan_k2g_bounds(html, i)
    if b:
        if len(_k2g_bounds_cache) > 8:
            _k2g_bounds_cache.clear()
        _k2g_bounds_cache[fp] = b
    return b

def _scan_k2g_bounds(html, i):
    """폴백: 문자열·이스케이프·중첩 대괄호를 상태 기계로 추적."""
    depth, in_str, esc_ch, j, n = 0, False, False, i, len(html)
    while j < n:
        ch = html[j]
        if in_str:
            if esc_ch: esc_ch = False
            elif ch == '\\': esc_ch = True
            elif ch == '"': in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    return (i, j + 1)
        j += 1
    return None

def _k2g_migrate_from_static():
    """정적 shop.html의 인라인 배열 → products DB 백필 (멱등).
    · 기존 k2g:: 행: 비어 있는 img/list_price(정가)/sort_order만 채우고,
      배열의 품절 플래그가 1이면 soldout 반영 (관리자 수정값은 보존)
    · 배열에만 있는 앨범: 신규 INSERT (k2g_removed에 기록된 uid는 제외)
    · 모든 k2g 행에 sort_order가 채워지면 이후 호출은 건너뛴다."""
    if one("SELECT 1 FROM products WHERE id LIKE ? AND sort_order IS NULL LIMIT 1", ('k2g::%',)) is None \
       and one("SELECT 1 FROM products WHERE id LIKE ? LIMIT 1", ('k2g::%',)) is not None:
        return
    fp = os.path.join(STATIC_DIR, 'shop.html')
    if not os.path.isfile(fp):
        return
    html = open(fp, encoding='utf-8', errors='replace').read()
    b = _find_k2g_array_bounds(html)
    if not b:
        return
    try:
        arr = json.loads(html[b[0]:b[1]])
    except Exception:
        return
    if not isinstance(arr, list):
        return
    removed = _k2g_removed_set()
    nm, pr = _state['pname'] or 'name', _state['pprice'] or 'price'
    existing = {r['id']: r for r in rows(
        "SELECT id, img, list_price, sort_order, soldout FROM products WHERE id LIKE ?", ('k2g::%',))}
    ops = []
    for i, row in enumerate(arr):
        if not isinstance(row, list) or len(row) < 5:
            continue
        uid = str(row[0])
        if uid in removed:
            continue
        img, name = str(row[1] or ''), str(row[2] or '')
        was, sale = num(row[3]), num(row[4])
        sold = 1 if (len(row) > 5 and num(row[5])) else 0
        pid = 'k2g::' + uid
        ex = existing.get(pid)
        if ex is None:
            ops.append(('INSERT INTO products(id, %s, %s, stock, soldout, img, category, list_price, sort_order) '
                        'VALUES(?,?,?,0,?,?,?,?,?)' % (nm, pr),
                        (pid, name[:300], sale, sold, img[:300], 'album', was, i)))
        else:
            sets, args = [], []
            if not (ex.get('img') or '').strip():
                sets.append('img=?'); args.append(img[:300])
            if ex.get('list_price') is None:
                sets.append('list_price=?'); args.append(was)
            if ex.get('sort_order') is None:
                sets.append('sort_order=?'); args.append(i)
            if sold and not num(ex.get('soldout')):
                sets.append('soldout=1')
            if sets:
                ops.append(('UPDATE products SET %s WHERE id=?' % ', '.join(sets), tuple(args + [pid])))
    if ops:
        runmany(ops)

_k2g_cat_cache = {'t': 0.0, 'body': None}

def _k2g_cache_bust():
    _k2g_cat_cache['body'] = None

def _k2g_catalog_json():
    """k2g:: 상품 → 사이트 인라인 배열 형식 [uid, img, name, 정가, 판매가, 품절]의
    JSON 문자열. 60초 캐시 + 쓰기 API가 즉시 무효화. <script> 내 삽입 안전 처리."""
    if _k2g_cat_cache['body'] is not None and time.time() - _k2g_cat_cache['t'] < 60:
        return _k2g_cat_cache['body']
    ensure_ready()
    if not _state['pcols'] or not _state['pname'] or not _state['pprice']:
        return None
    rs = rows("SELECT id, %s AS name, %s AS price, list_price, img, soldout FROM products "
              "WHERE id LIKE ? ORDER BY COALESCE(sort_order, 999999999), id"
              % (_state['pname'], _state['pprice']), ('k2g::%',))
    if not rs:
        return '[]' if _k2g_removed_set() else None   # 전부 삭제한 상태면 빈 카탈로그, 미백필이면 정적 폴백
    out = []
    for r in rs:
        sale, was = num(r.get('price')), num(r.get('list_price'))
        out.append([r['id'][5:], str(r.get('img') or ''), str(r.get('name') or ''),
                    was if was > sale else 0, sale, 1 if num(r.get('soldout')) else 0])
    body = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
    body = body.replace('</', '<\\/').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    _k2g_cat_cache.update(t=time.time(), body=body)
    return body

def _serve_k2g_from_db(html):
    """서빙 HTML의 인라인 K2G 배열을 DB 카탈로그로 치환.
    실패·미백필 시 정적 스냅숏을 그대로 서빙(무해한 폴백)."""
    if 'const K2G=[' not in html:
        return html
    try:
        body = _k2g_catalog_json()
        if not body:
            return html
        b = _find_k2g_array_bounds(html)
        if not b:
            return html
        return html[:b[0]] + body + html[b[1]:]
    except Exception:
        return html

# ══════════════════════════════════════════════════════════════════════
# KPOP(음반) 카테고리 분리 — 전 페이지 서빙 시점 변환 (정적 HTML 무수정)
#   · 메뉴바: SHOP 앞에 KPOP(음반) 신설 (데스크톱 nav + 모바일 mpCatBar)
#   · /kpop: shop.html을 앨범 전용관으로 변환 서빙 (K2G 카탈로그 + mp:: 앨범)
#   · /shop.html: 앨범 칩·카드 제거 + K2G 배열 비움(응답 경량화), 기본 뷰 ALL
#   · 구형 딥링크 shop.html?cat=album → /kpop 전역 재작성 + JS 리다이렉트
#   ※ _kpop_apply는 반드시 _serve_k2g_from_db 이후에 실행 (SHOP의 K2G 비움이
#      DB 재구성으로 되살아나지 않도록) — _inject_auth 내 호출 순서로 보장.
_KPOP_MARK = '<!--MP_KPOP-->'   # /kpop 라우트가 파이프라인 진입 전에 찍는 모드 마커

# 데스크톱 nav의 SHOP 항목 (home/shop/support/album-detail 등 실측 패턴, class 변형 허용)
_KPOP_NAV_RE = re.compile(
    r'<div>\s*<a class="top(?:[^"]*)" href="(?:\./)?shop\.html"[^>]*>\s*SHOP\s*</a>\s*</div>')
# col-card 마크업 (정적 카드와 mp:: 주입 카드 동일 형태 — 내부에 중첩 <a> 없음)
_KPOP_COL_ALBUM_RE = re.compile(r'\s*<a class="col-card" data-cat="album".*?</a>', re.S)
_KPOP_COL_OTHER_RE = re.compile(
    r'\s*<a class="col-card" data-cat="(?:kfood|md|apparel|living)".*?</a>', re.S)
# 필터 바 (내부는 <button>뿐 — 첫 </div>가 바의 닫힘)
_KPOP_FBAR_RE = re.compile(r'\s*<div class="filter-bar">.*?</div>', re.S)

def _kpop_empty_k2g(html):
    """K2G 인라인 배열(DB 재구성분 포함)을 빈 배열로 치환 — SHOP 경량화 전용."""
    b = _find_k2g_array_bounds(html)
    if not b:
        return html
    return html[:b[0]] + '[]' + html[b[1]:]

def _kpop_apply(html):
    """전 페이지 공통 변환 + 페이지 시그니처별(SHOP/KPOP/앨범상세) 모드 변환."""
    if not isinstance(html, str) or '</html>' not in html:
        return html
    is_kpop = _KPOP_MARK in html[:200]

    # ── [전역 1] 메뉴바: SHOP 앞에 KPOP(음반) 삽입 (멱등) ─────────────
    if 'href="/kpop"' not in html:
        html = _KPOP_NAV_RE.sub(
            lambda m: '<div><a class="top" href="/kpop">KPOP(음반)</a></div>' + m.group(0),
            html, count=1)

    # ── [전역 2] 구형 앨범 딥링크·라벨 재작성 ─────────────────────────
    html = html.replace('shop.html?cat=album', '/kpop')
    html = html.replace('>SHOP · 앨범/음반<', '>KPOP(음반)<')            # 앨범상세 크럼
    html = html.replace('<a href="/kpop">앨범 / 음반</a>',
                        '<a href="/kpop">KPOP(음반)</a>')               # 정적 푸터 라벨

    # ── [모드 판별] shop.html만 shopGrid+filter-bar 동시 보유 (전 페이지 실측) ──
    is_shop = (not is_kpop) and 'id="shopGrid"' in html and 'class="filter-bar"' in html
    is_adet = (not is_kpop) and (not is_shop) and 'const DET_EMB=' in html

    if is_shop:
        # ① 필터 바: 앨범/음반 칩 제거, ALL 기본 활성
        html = html.replace('\n    <button class="on" data-f="album">앨범 / 음반</button>', '', 1)
        html = html.replace('<button class="on" data-f="album">앨범 / 음반</button>', '', 1)
        html = html.replace('<button data-f="all">ALL</button>',
                            '<button class="on" data-f="all">ALL</button>', 1)
        # ② 히어로 카피 · 검색 플레이스홀더
        html = html.replace(
            '<p>굿즈, 앨범, 어패럴, 리빙 — 품목별로 탐색합니다.',
            '<p>굿즈, 어패럴, K-FOOD, 리빙 — 품목별로 탐색합니다. 앨범/음반은 KPOP(음반) 메뉴에서 만나보세요.', 1)
        html = html.replace(
            '상품 · 아티스트 · 앨범 검색 (예: 원호, DAYDREAM, 후디, 사인회)',
            '상품 검색 (예: 후디, 키링, 떡볶이)', 1)
        # ③ 앨범 col-card 제거 (정적 2종 + mp:: 주입 앨범 — 잔여분은 ④ 스윕이 보증)
        html = _KPOP_COL_ALBUM_RE.sub('', html)
        # ④ JS: 기본 F='all' + ?cat=album 리다이렉트 + 앨범 col-card 스윕
        html = html.replace(
            "let F='album',Q='',VIEW=K2G,ptr=0;",
            "let F='all',Q='',VIEW=K2G,ptr=0;"
            "if(new URLSearchParams(location.search).get('cat')==='album'){location.replace('/kpop')}"
            "/*mpShopSweep*/document.querySelectorAll('#shopGrid .col-card[data-cat=\"album\"]')"
            ".forEach(function(c){c.remove()});", 1)
        # ⑤ 앨범 렌더 경로 차단(더보기 포함) + K2G 배열 비움(응답 경량화)
        html = html.replace("function albumEligible(){return F==='all'||F==='album';}",
                            "function albumEligible(){return false;}", 1)
        html = _kpop_empty_k2g(html)

    elif is_kpop:
        # ① 타이틀 · 히어로
        html = html.replace('<title>SHOP — MAPDAL SEOUL</title>',
                            '<title>KPOP(음반) — MAPDAL SEOUL</title>', 1)
        html = html.replace('<div class="kicker">SHOP · BY CATEGORY</div>',
                            '<div class="kicker">KPOP · 앨범 / 음반</div>', 1)
        html = html.replace('<h1>SHOP</h1>', '<h1>KPOP(음반)</h1>', 1)
        html = html.replace(
            '<p>굿즈, 앨범, 어패럴, 리빙 — 품목별로 탐색합니다. 컬렉션(세계관)으로 쇼핑하려면 COLLECTIONS로 이동하세요.</p>',
            '<p>KPOP 앨범·음반 전용관 — 최신 발매반부터 사인회·영상통화 특전 응모까지 한 곳에서 만나보세요.</p>', 1)
        html = html.replace(
            '상품 · 아티스트 · 앨범 검색 (예: 원호, DAYDREAM, 후디, 사인회)',
            '아티스트 · 앨범 검색 (예: 원호, DAYDREAM, 사인회)', 1)
        # ② 카테고리 필터 바 제거 (앨범 전용관 — 카테고리 개념 불필요)
        html = _KPOP_FBAR_RE.sub('', html, count=1)
        # ③ 앨범 외 col-card 제거 (정적 + mp:: 주입분) + JS 스윕(동적 잔여 보증)
        html = _KPOP_COL_OTHER_RE.sub('', html)
        if 'mpKpopSweep' not in html:                    # 멱등 가드
            html = html.replace(
                "let F='album',Q='',VIEW=K2G,ptr=0;",
                "let F='album',Q='',VIEW=K2G,ptr=0;"
                "/*mpKpopSweep*/document.querySelectorAll('#shopGrid .col-card:not([data-cat=\"album\"])')"
                ".forEach(function(c){c.remove()});", 1)
        # ④ 데스크톱 nav 활성 표시: SHOP → KPOP(음반)
        html = html.replace('class="top active-page" href="shop.html"',
                            'class="top" href="shop.html"', 1)
        html = html.replace('class="top" href="/kpop"',
                            'class="top active-page" href="/kpop"', 1)
        html = html.replace(_KPOP_MARK, '', 1)           # 모드 마커 제거

    elif is_adet:
        # 앨범 상세: 활성 메뉴를 KPOP(음반)으로 이관 (크럼은 전역 2에서 처리됨)
        html = html.replace('class="top active-page" href="shop.html"',
                            'class="top" href="shop.html"', 1)
        html = html.replace('class="top" href="/kpop"',
                            'class="top active-page" href="/kpop"', 1)

    return html

# 모바일 mpCatBar 폴백 배열에도 KPOP(음반) 선행 삽입 (nav.main 없는 페이지 대비)
MOBNAV_SNIPPET = MOBNAV_SNIPPET.replace(
    "{label:'SHOP',href:'/shop.html',red:false},",
    "{label:'KPOP(음반)',href:'/kpop',red:false},{label:'SHOP',href:'/shop.html',red:false},", 1)

def _inject_auth(html):
    html = _serve_k2g_from_db(html)
    html = _inject_shop_products(html)
    html = _kpop_apply(html)
    html, patched = _patch_legacy_footer(html)
    add = ''
    if 'mpAuthJs' not in html: add += AUTH_SNIPPET
    if 'mpLikeJs' not in html: add += LIKE_SNIPPET
    if 'mpMobNav' not in html: add += MOBNAV_SNIPPET
    if 'mpTickerJs' not in html: add += TICKER_SNIPPET
    if 'mpFooter' not in html: add += footer_snippet()
    if not add: return html
    i = html.lower().rfind('</body>')
    return (html[:i] + add + html[i:]) if i >= 0 else (html + add)

# ── 관리자: 문의/상품Q&A/취소·반품·교환 요청 처리 + 포인트 ──
@admin_router.get('/admin/api/cs')
def api_cs(request: Request):
    a = get_actor(request); need(a, 0)
    inq = rows('SELECT q.*, m.name AS mname, m.email FROM member_inquiries q LEFT JOIN members m ON m.id=q.member_id ORDER BY q.created DESC LIMIT 100')
    nm = _state['pname'] or 'id'
    pq = rows('SELECT q.*, m.name AS mname, p.%s AS pname FROM member_pqna q LEFT JOIN members m ON m.id=q.member_id LEFT JOIN products p ON p.id=q.product_id ORDER BY q.created DESC LIMIT 100' % nm)
    rq = rows('SELECT r.*, m.name AS mname, m.phone AS mphone FROM member_requests r LEFT JOIN members m ON m.id=r.member_id ORDER BY r.created DESC LIMIT 100')
    krt = {'cancel': '취소', 'return': '반품', 'exchange': '교환'}
    return {'inq': [{'id': r['id'], 'title': r['title'], 'body': r['body'], 'order_id': r.get('order_id') or '',
                     'mname': r.get('mname') or '', 'email': r.get('email') or '',
                     'created': (r['created'] or '')[:16].replace('T', ' '), 'status': r['status'],
                     'answer': r.get('answer') or ''} for r in inq],
            'pqna': [{'id': r['id'], 'pname': r.get('pname') or r.get('product_id'), 'question': r['question'],
                      'mname': r.get('mname') or '', 'created': (r['created'] or '')[:16].replace('T', ' '),
                      'status': r['status'], 'answer': r.get('answer') or ''} for r in pq],
            'reqs': [{'id': r['id'], 'order_id': r['order_id'], 'rtype': krt.get(r['rtype'], r['rtype']),
                      'reason': r['reason'], 'mname': r.get('mname') or '', 'mphone': r.get('mphone') or '',
                      'created': (r['created'] or '')[:16].replace('T', ' '), 'status': r['status'],
                      'memo': r.get('admin_memo') or ''} for r in rq]}

@admin_router.post('/admin/api/cs/answer')
def api_cs_answer(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '문의 답변')
    kind = body.get('kind'); ans = (body.get('answer') or '').strip()[:2000]
    if kind not in ('inq', 'pqna') or not ans: raise HTTPException(400, '답변 내용을 입력하세요')
    t = 'member_inquiries' if kind == 'inq' else 'member_pqna'
    n = run("UPDATE %s SET answer=?, status='답변완료', answered_at=?, answered_by=? WHERE id=?" % t,
            (ans, now_iso(), a['name'], body.get('id')))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '문의답변', body.get('id'), ('1:1' if kind == 'inq' else '상품Q&A'))
    return {'ok': True}

@admin_router.post('/admin/api/cs/req-update')
def api_cs_req(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '요청 처리')
    st = body.get('status')
    if st not in ('접수', '처리중', '완료', '거절'): raise HTTPException(400, 'bad status')
    n = run('UPDATE member_requests SET status=?, admin_memo=?, updated=? WHERE id=?',
            (st, (body.get('memo') or '').strip()[:300], now_iso(), body.get('id')))
    if not n: raise HTTPException(404, 'not found')
    audit(a, '요청처리', body.get('id'), st)
    return {'ok': True}

@admin_router.post('/admin/api/members/points')
def api_member_points(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '포인트 지급')
    m = one('SELECT * FROM members WHERE id=?', (body.get('id'),))
    if not m: raise HTTPException(404, 'not found')
    delta = num(body.get('delta'))
    if not delta: raise HTTPException(400, '지급/차감 포인트를 입력하세요')
    nv = max(0, num(m.get('points')) + delta)
    run('UPDATE members SET points=? WHERE id=?', (nv, m['id']))
    audit(a, '포인트지급', m.get('email') or m['id'], '%+d → %d (%s)' % (delta, nv, (body.get('reason') or '')[:60]))
    return {'ok': True, 'points': nv}

# ═══════════════════ 마이페이지 회원 API (주문연동·요청·찜·문의) ═══════════
def member_required(request: Request):
    m = member_of(request)
    if not m: raise HTTPException(401, '로그인이 필요합니다')
    return m

def phone_variants(d):
    v = {d}
    if len(d) == 11: v.add(d[:3] + '-' + d[3:7] + '-' + d[7:])
    if len(d) == 10:
        v.add(d[:3] + '-' + d[3:6] + '-' + d[6:]); v.add(d[:2] + '-' + d[2:6] + '-' + d[6:])
    return [x for x in v if x]

def member_orders_where(m):
    d = digits(m.get('phone') or '')
    if not (num(m.get('phone_verified')) and len(d) >= 9): return None, ()
    conds, args = [], []
    for p in phone_variants(d):
        conds.append('buyer LIKE ?'); args.append('%' + p + '%')
    return '(' + ' OR '.join(conds) + ')', tuple(args)

def order_step(status, fulfill):
    if status == 'CANCELLED' or (fulfill or '') == 'CANCELLED': return 0, '취소됨'
    if status == 'FAILED': return 0, '결제실패'
    if status == 'PENDING': return 1, '주문접수'
    return {'NEW': (2, '결제완료'), 'PREPARING': (3, '배송준비중'),
            'SHIPPED': (4, '배송중'), 'DONE': (5, '배송완료')}.get(fulfill or 'NEW', (2, '결제완료'))

def _own_order(m, oid):
    w, args = member_orders_where(m)
    if not w: raise HTTPException(403, '휴대폰 인증 후 이용할 수 있습니다')
    r = one('SELECT * FROM orders WHERE order_id=? AND ' + w, (oid,) + args)
    if not r: raise HTTPException(404, '내 주문이 아니거나 찾을 수 없습니다')
    return r

def system_sms(phone, text, tag, order_id=''):
    cf = solapi_conf()
    if not (cf['key'] and cf['sec'] and cf['sender']):
        run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), now_iso(), order_id, phone, 'sms', tag, 'DRY', '발송사 미설정 — ' + text[:150], '시스템'))
        return True, True
    try:
        blen = len(text.encode('euc-kr', errors='replace'))
    except Exception:
        blen = len(text) * 2
    msg = {'to': phone, 'from': digits(cf['sender']), 'text': text,
           'type': 'LMS' if blen > 90 else 'SMS'}
    if msg['type'] == 'LMS': msg['subject'] = '맵달SEOUL 안내'
    try:
        res = solapi_send(msg)
        ok = not (res.get('failedMessageList') or [])
        run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), now_iso(), order_id, phone, 'sms', tag, 'SENT' if ok else 'FAILED', text[:150], '시스템'))
        return ok, False
    except Exception as e:
        run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), now_iso(), order_id, phone, 'sms', tag, 'FAILED', str(e)[:150], '시스템'))
        return False, False

@admin_router.get('/api/member/overview')
def api_m_overview(request: Request):
    m = member_required(request)
    grade = 'WELCOME'
    d = digits(m.get('phone') or '')
    if num(m.get('phone_verified')) and d:
        c = one('SELECT grade FROM customers WHERE phone=?', (d,))
        if c: grade = c.get('grade') or 'WELCOME'
    counters = {str(i): 0 for i in range(1, 6)}
    w, args = member_orders_where(m)
    linked = bool(w)
    if w:
        d30 = (kst_today() - datetime.timedelta(days=30)).isoformat()
        for r in rows('SELECT status, fulfill FROM orders WHERE created>=? AND ' + w, (d30,) + args):
            st, _ = order_step(r.get('status'), r.get('fulfill'))
            if st: counters[str(st)] += 1
    likes = num((one('SELECT COUNT(*) AS c FROM member_likes WHERE member_id=?', (m['id'],)) or {}).get('c'))
    return {'name': m.get('name') or '회원', 'email': m.get('email') or '',
            'provider': {'google': 'Google', 'apple': 'Apple', 'email': '이메일', 'kakao': '카카오'}.get(m.get('provider'), m.get('provider')),
            'has_pw': m.get('provider') == 'email', 'phone': m.get('phone') or '',
            'phone_verified': num(m.get('phone_verified')), 'points': num(m.get('points')),
            'grade': grade, 'linked': linked, 'counters': counters, 'likes': likes,
            'bank': m.get('bank') or '', 'acct': m.get('acct') or '', 'acct_name': m.get('acct_name') or '',
            'fav_store': num(m.get('fav_store')),
            'gender': m.get('gender') or '', 'birth': m.get('birth') or '', 'age_range': m.get('age_range') or ''}

@admin_router.get('/api/member/orders')
def api_m_orders(request: Request):
    m = member_required(request)
    w, args = member_orders_where(m)
    if not w: return {'linked': False, 'rows': []}
    rng = request.query_params.get('range', '1m')
    extra, eargs = '', ()
    if rng in ('1m', '3m'):
        d = (kst_today() - datetime.timedelta(days=30 if rng == '1m' else 90)).isoformat()
        extra, eargs = ' AND created>=?', (d,)
    rs = rows('SELECT order_id, created, status, fulfill, amount, items, tracking, receipt_url FROM orders WHERE '
              + w + extra + ' ORDER BY created DESC LIMIT 100', args + eargs)
    out = []
    for r in rs:
        its = jload(r.get('items'), [])
        first = (its[0].get('n') or its[0].get('name') or '') if its else ''
        st, kr = order_step(r.get('status'), r.get('fulfill'))
        out.append({'order_id': r['order_id'], 'created': (r.get('created') or '')[:16].replace('T', ' '),
                    'step': st, 'status_kr': kr, 'amount': num(r.get('amount')),
                    'label': first[:24] + (' 외 %d' % (len(its) - 1) if len(its) > 1 else ''),
                    'tracking': r.get('tracking') or '', 'receipt': r.get('receipt_url') or '',
                    'paid': r.get('status') == 'PAID'})
    return {'linked': True, 'rows': out}

@admin_router.get('/api/member/orders/{oid}')
def api_m_order_detail(oid: str, request: Request):
    m = member_required(request)
    r = _own_order(m, oid)
    b = jload(r.get('buyer'), {})
    st, kr = order_step(r.get('status'), r.get('fulfill'))
    open_req = one("SELECT rtype, status FROM member_requests WHERE order_id=? AND member_id=? AND status IN ('접수','처리중')", (oid, m['id']))
    return {'order_id': oid, 'created': (r.get('created') or '')[:19].replace('T', ' '), 'step': st, 'status_kr': kr,
            'amount': num(r.get('amount')), 'ship_method': r.get('ship_method') or '',
            'tracking': r.get('tracking') or '', 'receipt': r.get('receipt_url') or '',
            'addr': '[%s] %s %s' % (b.get('zip', ''), b.get('addr1', ''), b.get('addr2', '')),
            'items': [{'name': it.get('n') or it.get('name') or it.get('id', ''), 'qty': num(it.get('q') or 1),
                       'price': num(it.get('p') or it.get('price') or 0)} for it in jload(r.get('items'), [])],
            'can_cancel': (r.get('status') == 'PENDING') or (r.get('status') == 'PAID' and (r.get('fulfill') or 'NEW') in ('NEW', 'PREPARING')),
            'can_return': r.get('status') == 'PAID' and (r.get('fulfill') or '') in ('SHIPPED', 'DONE'),
            'open_request': dict(open_req) if open_req else None}

@admin_router.post('/api/member/orders/{oid}/request')
def api_m_order_request(oid: str, request: Request, body: dict = Body(...)):
    m = member_required(request)
    r = _own_order(m, oid)
    rtype = body.get('rtype')
    reason = (body.get('reason') or '').strip()[:300]
    if rtype not in ('cancel', 'return', 'exchange'): raise HTTPException(400, '요청 유형 오류')
    if not reason: raise HTTPException(400, '사유를 입력해 주세요')
    f = r.get('fulfill') or 'NEW'
    if rtype == 'cancel':
        if not ((r.get('status') == 'PENDING') or (r.get('status') == 'PAID' and f in ('NEW', 'PREPARING'))):
            raise HTTPException(400, '발송 전 주문만 취소 요청이 가능합니다')
    else:
        if not (r.get('status') == 'PAID' and f in ('SHIPPED', 'DONE')):
            raise HTTPException(400, '배송된 주문만 반품/교환 요청이 가능합니다')
    if one("SELECT id FROM member_requests WHERE order_id=? AND member_id=? AND status IN ('접수','처리중')", (oid, m['id'])):
        raise HTTPException(400, '이미 처리 중인 요청이 있습니다')
    run('INSERT INTO member_requests(id,member_id,order_id,rtype,reason,created,status,admin_memo,updated) VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], oid, rtype, reason, now_iso(), '접수', '', now_iso()))
    return {'ok': True}

@admin_router.get('/api/member/requests')
def api_m_requests(request: Request):
    m = member_required(request)
    kr = {'cancel': '취소', 'return': '반품', 'exchange': '교환'}
    canc = []
    w, args = member_orders_where(m)
    if w:
        canc = rows("SELECT order_id, created, amount FROM orders WHERE status='CANCELLED' AND " + w + ' ORDER BY created DESC LIMIT 30', args)
    return {'requests': [{'id': r['id'], 'order_id': r['order_id'], 'rtype': kr.get(r['rtype'], r['rtype']),
                          'reason': r['reason'], 'created': (r['created'] or '')[:16].replace('T', ' '),
                          'status': r['status'], 'memo': r.get('admin_memo') or ''}
                         for r in rows('SELECT * FROM member_requests WHERE member_id=? ORDER BY created DESC LIMIT 50', (m['id'],))],
            'cancelled_orders': [{'order_id': c['order_id'], 'created': (c['created'] or '')[:10], 'amount': num(c['amount'])} for c in canc]}

@admin_router.get('/api/member/receipt/{oid}', response_class=HTMLResponse)
def api_m_receipt(oid: str, request: Request):
    m = member_required(request)
    r = _own_order(m, oid)
    b = jload(r.get('buyer'), {}); its = jload(r.get('items'), [])
    def h(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    tr = ''.join('<tr><td>%s</td><td class="r">%s</td><td class="r">%d</td><td class="r">%s</td></tr>'
                 % (h(it.get('n') or it.get('name') or ''), format(num(it.get('p') or 0), ','),
                    num(it.get('q') or 1), format(num(it.get('p') or 0) * num(it.get('q') or 1), ','))
                 for it in its)
    return HTMLResponse('''<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>거래명세서 — %s</title>
<style>body{font-family:'Malgun Gothic',sans-serif;max-width:720px;margin:30px auto;padding:0 20px;color:#141414}
h1{font-size:20px;border-bottom:3px solid #E8332A;padding-bottom:10px}
table{width:100%%;border-collapse:collapse;margin:14px 0;font-size:13px}
th,td{border:1px solid #ccc;padding:7px 9px}th{background:#141414;color:#fff;font-size:12px}
.r{text-align:right}.meta{font-size:12.5px;line-height:1.9;margin:12px 0}
.tot{font-size:15px;font-weight:700;text-align:right;margin-top:6px}
.btn{background:#141414;color:#fff;border:0;padding:10px 18px;font-weight:700;cursor:pointer}
@media print{.btn{display:none}}</style></head><body>
<h1>거래명세서 <small style="font-weight:400;font-size:12px">MAPDAL SEOUL</small></h1>
<div class="meta"><b>공급자</b> 맵달서울성수 · 공동대표 황인범, 김동경 · 서울특별시 성동구 성수이로16길 5 · 사업자등록번호 394-85-03267<br>
<b>주문번호</b> %s &nbsp;·&nbsp; <b>거래일시</b> %s<br>
<b>받는분</b> %s (%s) · %s</div>
<table><tr><th>품목</th><th class="r">단가</th><th class="r">수량</th><th class="r">금액</th></tr>%s</table>
<div class="tot">합계 (부가세 포함) &nbsp; ₩ %s</div>
<div class="meta" style="color:#888;font-size:11.5px">본 명세서는 전자상거래 주문내역 확인용입니다. 세금계산서/현금영수증은 결제수단 및 신청에 따라 별도 발급됩니다.</div>
<button class="btn" onclick="window.print()">인쇄하기</button></body></html>'''
        % (h(oid), h(oid), h((r.get('created') or '')[:19].replace('T', ' ')),
           h(b.get('name', '')), h(b.get('phone', '')),
           h(('[%s] %s %s' % (b.get('zip', ''), b.get('addr1', ''), b.get('addr2', ''))).strip()),
           tr, format(num(r.get('amount')), ',')))

@admin_router.post('/api/member/phone/send')
def api_m_phone_send(request: Request, body: dict = Body(...)):
    m = member_required(request)
    d = digits(body.get('phone'))
    if len(d) < 10: raise HTTPException(400, '휴대폰 번호를 확인해 주세요')
    ip = (request.client.host if request.client else '') or '-'
    key = 'pv:' + m['id'] + ':' + ip; guard(key); fail_hit(key)
    code = str(secrets.randbelow(900000) + 100000)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).isoformat(timespec='seconds')
    run('INSERT INTO phone_verifications VALUES(?,?,?,?,?,?,0)', (uid(), m['id'], d, code, now_iso(), exp))
    ok, dry = system_sms(d, '[맵달SEOUL] 휴대폰 인증번호는 [%s] 입니다. 5분 내에 입력해 주세요.' % code, '휴대폰인증')
    if not ok: raise HTTPException(400, '문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    return {'ok': True, 'dry': dry}

@admin_router.post('/api/member/phone/verify')
def api_m_phone_verify(request: Request, body: dict = Body(...)):
    m = member_required(request)
    code = (body.get('code') or '').strip()
    v = one('SELECT * FROM phone_verifications WHERE member_id=? AND used=0 ORDER BY created DESC LIMIT 1', (m['id'],))
    if not v or v.get('code') != code or (v.get('expires') or '') <= now_iso():
        raise HTTPException(400, '인증번호가 올바르지 않거나 만료되었습니다')
    run('UPDATE phone_verifications SET used=1 WHERE id=?', (v['id'],))
    run('UPDATE members SET phone=?, phone_verified=1 WHERE id=?', (v['phone'], m['id']))
    return {'ok': True, 'phone': v['phone']}

@admin_router.post('/api/member/profile')
def api_m_profile(request: Request, body: dict = Body(...)):
    m = member_required(request)
    sets, args = [], []
    if body.get('name') is not None:
        nm = (body.get('name') or '').strip()[:40]
        if not nm: raise HTTPException(400, '이름을 입력하세요')
        sets.append('name=?'); args.append(nm)
    if body.get('gender') in ('F', 'M'): sets.append('gender=?'); args.append(body['gender'])
    if (body.get('birth') or '').strip(): sets.append('birth=?'); args.append(body['birth'].strip()[:10])
    for k in ('bank', 'acct', 'acct_name'):
        if k in body: sets.append('%s=?' % k); args.append((body.get(k) or '').strip()[:60])
    if 'fav_store' in body: sets.append('fav_store=?'); args.append(1 if body['fav_store'] else 0)
    if not sets: raise HTTPException(400, '변경할 값 없음')
    run('UPDATE members SET %s WHERE id=?' % ', '.join(sets), tuple(args + [m['id']]))
    return {'ok': True}

@admin_router.post('/api/member/password')
def api_m_password(request: Request, body: dict = Body(...)):
    m = member_required(request)
    if m.get('provider') != 'email': raise HTTPException(400, '소셜 가입 계정은 비밀번호가 없습니다')
    old, new = body.get('old') or '', body.get('new') or ''
    if len(new) < 8: raise HTTPException(400, '새 비밀번호는 8자 이상')
    if not pw_verify(old, m.get('pw') or ''): raise HTTPException(403, '현재 비밀번호가 올바르지 않습니다')
    run('UPDATE members SET pw=? WHERE id=?', (pw_hash(new), m['id']))
    return {'ok': True}

@admin_router.get('/api/member/addresses')
def api_m_addr_list(request: Request):
    m = member_required(request)
    return {'rows': rows('SELECT * FROM member_addresses WHERE member_id=? ORDER BY is_default DESC, created DESC', (m['id'],))}

@admin_router.post('/api/member/addresses')
def api_m_addr_save(request: Request, body: dict = Body(...)):
    m = member_required(request)
    act = body.get('act', 'add')
    if act == 'delete':
        run('DELETE FROM member_addresses WHERE id=? AND member_id=?', (body.get('id'), m['id'])); return {'ok': True}
    if act == 'default':
        run('UPDATE member_addresses SET is_default=0 WHERE member_id=?', (m['id'],))
        run('UPDATE member_addresses SET is_default=1 WHERE id=? AND member_id=?', (body.get('id'), m['id'])); return {'ok': True}
    for k in ('rname', 'phone', 'zip', 'addr1'):
        if not (body.get(k) or '').strip(): raise HTTPException(400, '받는분/연락처/우편번호/주소를 입력하세요')
    first = not one('SELECT id FROM member_addresses WHERE member_id=? LIMIT 1', (m['id'],))
    run('INSERT INTO member_addresses VALUES(?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], (body.get('label') or '기본')[:20], body['rname'][:30], digits(body['phone']),
         body['zip'][:10], body['addr1'][:120], (body.get('addr2') or '')[:80], 1 if first else 0, now_iso()))
    return {'ok': True}

@admin_router.post('/api/member/likes')
def api_m_like(request: Request, body: dict = Body(...)):
    m = member_required(request)
    pid = body.get('product_id') or ''
    if not one('SELECT id FROM products WHERE id=?', (pid,)): raise HTTPException(404, '상품 없음')
    ex = one('SELECT id FROM member_likes WHERE member_id=? AND product_id=?', (m['id'], pid))
    if body.get('on'):
        if not ex: run('INSERT INTO member_likes(id,member_id,product_id,created) VALUES(?,?,?,?)', (uid(), m['id'], pid, now_iso()))
    elif ex:
        run('DELETE FROM member_likes WHERE id=?', (ex['id'],))
    return {'ok': True, 'liked': bool(body.get('on'))}

def _k2g_images():
    if 'k2gimg' in _state: return _state['k2gimg']
    mp = {}
    for fp in (os.path.join(BASE, 'data', 'k2g_catalog.json'), os.path.join(STATIC_DIR, 'k2g_catalog.json')):
        try:
            data = json.load(open(fp, encoding='utf-8'))
            items = data.values() if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for it in items:
                if not isinstance(it, dict): continue
                u = str(it.get('uid') or it.get('id') or it.get('goodsno') or '')
                img = next((it[k] for k in ('img', 'image', 'thumb', 'thumbnail', 'image_url', 'img_url', 'src') if it.get(k)), '')
                if u and img: mp[u] = str(img)[:300]
            if mp: break
        except Exception:
            continue
    _state['k2gimg'] = mp
    return mp

def _page_hero(page):
    try:
        html, _, _ = _page_effective(os.path.basename((page or '').split('?')[0]))
        if not html: return ''
        for mt in re.finditer(r'<img[^>]+src=["\']([^"\']+)', html, re.I):
            src = mt.group(1)
            if 'logo' in src.lower(): continue
            return src[:300]
    except Exception:
        pass
    return ''

def like_image(r):
    if r.get('pimg'): return r['pimg']
    if r.get('jimg'): return r['jimg']
    src = ''
    uid_m = re.search(r'uid=([A-Za-z0-9_-]{3,})', r.get('page') or '') or re.search(r'::?([0-9]{4,})$', r.get('product_id') or '')
    if not uid_m and (r.get('product_id') or '').startswith('k2g::'):
        uid_m = re.match(r'k2g::(.+)$', r['product_id'])
    if uid_m:
        src = _k2g_images().get(uid_m.group(1), '')
    if not src and (r.get('page') or '').split('?')[0].endswith('.html'):
        src = _page_hero(r['page'])
    if src:
        try: run('UPDATE member_likes SET pimg=? WHERE id=?', (src, r['rid']))
        except Exception: pass
    return src

@admin_router.get('/api/member/likes')
def api_m_likes(request: Request):
    m = member_required(request)
    nm = _state['pname'] or 'id'; pr = _state['pprice']
    sel = 'l.id AS rid, l.product_id, l.page, l.pname, l.pprice, l.pimg, p.%s AS jname, p.stock, p.soldout' % nm
    if pr: sel += ', p.%s AS jprice' % pr
    if 'img' in _state['pcols']: sel += ', p.img AS jimg'
    rs = rows('SELECT %s FROM member_likes l LEFT JOIN products p ON p.id=l.product_id WHERE l.member_id=? ORDER BY l.created DESC LIMIT 200' % sel, (m['id'],))
    out = []
    for r in rs:
        pid = r.get('product_id')
        out.append({'rid': r['rid'],
                    'name': r.get('jname') or r.get('pname') or pid or r.get('page') or '',
                    'price': num(r.get('jprice')) if r.get('jprice') is not None else num(r.get('pprice')),
                    'link': ('/p/' + pid) if pid else ('/' + (r.get('page') or '').lstrip('/')),
                    'img': like_image(r),
                    'soldout': (num(r.get('soldout')) or num(r.get('stock')) <= 0) if pid else False})
    return {'rows': out}

@admin_router.post('/api/member/restock')
def api_m_restock(request: Request, body: dict = Body(...)):
    m = member_required(request)
    if not num(m.get('phone_verified')): raise HTTPException(400, '휴대폰 인증 후 신청할 수 있습니다 (마이페이지 > 회원정보 수정)')
    pid = body.get('product_id') or ''
    if not one('SELECT id FROM products WHERE id=?', (pid,)): raise HTTPException(404, '상품 없음')
    if body.get('off'):
        run('DELETE FROM member_restock WHERE member_id=? AND product_id=? AND notified=0', (m['id'], pid)); return {'ok': True, 'on': False}
    if one('SELECT id FROM member_restock WHERE member_id=? AND product_id=? AND notified=0', (m['id'], pid)):
        return {'ok': True, 'on': True}
    run('INSERT INTO member_restock VALUES(?,?,?,?,?,0)', (uid(), m['id'], pid, digits(m.get('phone')), now_iso()))
    return {'ok': True, 'on': True}

def resolve_page_pid(href):
    """카드 링크(href) → 대표 상품 DB id 매칭. 실패 시 None."""
    try:
        href = (href or '').split('#')[0]
        mu = re.search(r'uid=([A-Za-z0-9_-]{3,})', href)
        if mu:
            r = one('SELECT id FROM products WHERE id LIKE ? ORDER BY id LIMIT 1', ('%' + mu.group(1) + '%',))
            return r['id'] if r else None
        page = os.path.basename(href.split('?')[0])
        if page.endswith('.html'):
            r = one('SELECT id FROM products WHERE id LIKE ? ORDER BY id LIMIT 1', (page + '::%',))
            if r: return r['id']
            r = one('SELECT id FROM products WHERE id = ?', (page,))
            return r['id'] if r else None
    except Exception:
        pass
    return None

@admin_router.post('/api/member/likes/remove')
def api_m_like_remove(request: Request, body: dict = Body(...)):
    m = member_required(request)
    n = run('DELETE FROM member_likes WHERE id=? AND member_id=?', (body.get('rid'), m['id']))
    if not n: raise HTTPException(404, 'not found')
    return {'ok': True}

@admin_router.post('/api/member/likes/page')
def api_m_like_page(request: Request, body: dict = Body(...)):
    m = member_required(request)
    href = (body.get('href') or '').strip()[:200]
    if not href or ('..' in href): raise HTTPException(400, '잘못된 링크')
    on = bool(body.get('on'))
    pname = (body.get('name') or '').strip()[:80] or href
    pprice = num(body.get('price'))
    pimg = (body.get('img') or '').strip()[:300]
    pid = resolve_page_pid(href)
    if pid:
        ex = one('SELECT id FROM member_likes WHERE member_id=? AND product_id=?', (m['id'], pid))
        if on and not ex:
            run('INSERT INTO member_likes(id,member_id,product_id,created,page,pname,pprice,pimg) VALUES(?,?,?,?,?,?,?,?)',
                (uid(), m['id'], pid, now_iso(), href, pname, pprice, pimg))
        elif not on and ex:
            run('DELETE FROM member_likes WHERE id=?', (ex['id'],))
    else:
        ex = one('SELECT id FROM member_likes WHERE member_id=? AND page=?', (m['id'], href))
        if on and not ex:
            run('INSERT INTO member_likes(id,member_id,product_id,created,page,pname,pprice,pimg) VALUES(?,?,?,?,?,?,?,?)',
                (uid(), m['id'], None, now_iso(), href, pname, pprice, pimg))
        elif not on and ex:
            run('DELETE FROM member_likes WHERE id=?', (ex['id'],))
    return {'ok': True, 'liked': on}

@admin_router.post('/api/member/likes/state')
def api_m_like_state(request: Request, body: dict = Body(...)):
    try: ensure_ready()
    except Exception: pass
    m = member_of(request)
    pages = [str(x)[:200] for x in (body.get('pages') or [])][:300]
    if not m or not pages: return {'login': bool(m), 'liked': []}
    liked = set()
    mine = rows('SELECT page FROM member_likes WHERE member_id=? AND page IS NOT NULL', (m['id'],))
    have = {r['page'] for r in mine if r.get('page')}
    for p in pages:
        if p in have: liked.add(p)
    return {'login': True, 'liked': sorted(liked)}

@admin_router.get('/api/member/restock')
def api_m_restock_list(request: Request):
    m = member_required(request)
    nm = _state['pname'] or 'id'
    rs = rows('SELECT r.id AS rid, r.notified, r.created, p.id, p.%s AS name, p.soldout, p.stock FROM member_restock r JOIN products p ON p.id=r.product_id WHERE r.member_id=? ORDER BY r.created DESC LIMIT 100' % nm, (m['id'],))
    return {'rows': [{'rid': r['rid'], 'id': r['id'], 'name': r.get('name') or r['id'],
                      'notified': num(r['notified']), 'soldout': num(r.get('soldout')) or num(r.get('stock')) <= 0,
                      'created': (r['created'] or '')[:10]} for r in rs]}

@admin_router.post('/api/member/inquiries')
def api_m_inq_create(request: Request, body: dict = Body(...)):
    m = member_required(request)
    title = (body.get('title') or '').strip()[:80]; bd = (body.get('body') or '').strip()[:2000]
    if not title or not bd: raise HTTPException(400, '제목과 내용을 입력하세요')
    run('INSERT INTO member_inquiries(id,member_id,order_id,title,body,created,status,answer,answered_at,answered_by) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], (body.get('order_id') or '')[:40], title, bd, now_iso(), '접수', '', '', ''))
    return {'ok': True}

@admin_router.get('/api/member/inquiries')
def api_m_inq_list(request: Request):
    m = member_required(request)
    return {'rows': [{'id': r['id'], 'title': r['title'], 'body': r['body'], 'order_id': r.get('order_id') or '',
                      'created': (r['created'] or '')[:16].replace('T', ' '), 'status': r['status'],
                      'answer': r.get('answer') or '', 'answered_at': (r.get('answered_at') or '')[:16].replace('T', ' ')}
                     for r in rows('SELECT * FROM member_inquiries WHERE member_id=? ORDER BY created DESC LIMIT 50', (m['id'],))]}

@admin_router.post('/api/member/pqna')
def api_m_pqna_create(request: Request, body: dict = Body(...)):
    m = member_required(request)
    pid = body.get('product_id') or ''; q = (body.get('question') or '').strip()[:1000]
    if not q: raise HTTPException(400, '문의 내용을 입력하세요')
    if not one('SELECT id FROM products WHERE id=?', (pid,)): raise HTTPException(404, '상품 없음')
    run('INSERT INTO member_pqna(id,member_id,product_id,question,created,status,answer,answered_at,answered_by) VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], pid, q, now_iso(), '접수', '', '', ''))
    return {'ok': True}

@admin_router.get('/api/member/pqna')
def api_m_pqna_list(request: Request):
    m = member_required(request)
    nm = _state['pname'] or 'id'
    rs = rows('SELECT q.*, p.%s AS pname FROM member_pqna q LEFT JOIN products p ON p.id=q.product_id WHERE q.member_id=? ORDER BY q.created DESC LIMIT 50' % nm, (m['id'],))
    return {'rows': [{'id': r['id'], 'product': r.get('pname') or r.get('product_id'), 'product_id': r.get('product_id'),
                      'question': r['question'], 'created': (r['created'] or '')[:16].replace('T', ' '),
                      'status': r['status'], 'answer': r.get('answer') or ''} for r in rs]}

@admin_router.get('/api/pqna')
def api_pqna_public(request: Request):
    try: ensure_ready()
    except Exception: pass
    pid = request.query_params.get('product_id', '')
    rs = rows("SELECT q.question, q.answer, q.answered_at, m.name FROM member_pqna q LEFT JOIN members m ON m.id=q.member_id WHERE q.product_id=? AND q.status='답변완료' ORDER BY q.created DESC LIMIT 20", (pid,))
    return {'rows': [{'q': r['question'], 'a': r.get('answer') or '',
                      'name': ((r.get('name') or '고객')[:1] + '**'),
                      'at': (r.get('answered_at') or '')[:10]} for r in rs]}

@admin_router.get('/api/member/pdp-state')
def api_m_pdp_state(request: Request):
    try: ensure_ready()
    except Exception: pass
    m = member_of(request)
    pid = request.query_params.get('product_id', '')
    if not m: return {'login': False, 'liked': False, 'restock': False, 'verified': False}
    return {'login': True, 'verified': bool(num(m.get('phone_verified'))),
            'liked': bool(one('SELECT id FROM member_likes WHERE member_id=? AND product_id=?', (m['id'], pid))),
            'restock': bool(one('SELECT id FROM member_restock WHERE member_id=? AND product_id=? AND notified=0', (m['id'], pid)))}

@admin_router.post('/api/member/withdraw')
def api_m_withdraw(request: Request, body: dict = Body(...)):
    m = member_required(request)
    if m.get('provider') == 'email':
        if not pw_verify(body.get('password') or '', m.get('pw') or ''):
            raise HTTPException(403, '비밀번호가 올바르지 않습니다')
    elif (body.get('confirm') or '') != '탈퇴':
        raise HTTPException(400, "'탈퇴' 를 정확히 입력해 주세요")
    for t in ('member_sessions', 'member_likes', 'member_restock', 'member_addresses',
              'member_requests', 'member_inquiries', 'member_pqna', 'phone_verifications'):
        try: run('DELETE FROM %s WHERE member_id=?' % t, (m['id'],))
        except Exception: pass
    run('DELETE FROM members WHERE id=?', (m['id'],))
    resp = JSONResponse({'ok': True})
    resp.delete_cookie('mp_member')
    return resp

# ── /kpop: shop.html을 앨범 전용관 모드로 변환 서빙 ──────────────────────
#    ※ 캐치올(serve_site)보다 먼저 등록되어야 한다 (등록 순서 = 매칭 순서).
@admin_router.get('/kpop')
def kpop_page():
    html = None
    try:
        ensure_ready()
        ov = one('SELECT html FROM page_edits WHERE path=?', ('shop.html',))
        if ov: html = ov['html']                 # 관리자 편집본 우선 (serve_site와 동일 규칙)
    except Exception:
        pass
    if html is None:
        fp = os.path.join(STATIC_DIR, 'shop.html')
        if not os.path.isfile(fp):
            return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>KPOP(음반) 준비 중입니다</h2><a href="/">MAPDAL SEOUL 홈으로</a>', status_code=503)
        html = open(fp, 'rb').read().decode('utf-8', errors='replace')
    return HTMLResponse(_inject_auth(_KPOP_MARK + html), headers={'Cache-Control': 'no-cache'})

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
            if ov: return HTMLResponse(_inject_auth(ov['html']), headers={'Cache-Control': 'no-cache'})
        except Exception:
            pass
    fp = os.path.realpath(os.path.join(STATIC_DIR, spath))
    root = os.path.realpath(STATIC_DIR)
    if not fp.startswith(root + os.sep) or not os.path.isfile(fp):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>페이지를 찾을 수 없습니다</h2><a href="/">MAPDAL SEOUL 홈으로</a>', status_code=404)
    mt = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
    data = open(fp, 'rb').read()
    if mt == 'text/html':
        return HTMLResponse(_inject_auth(data.decode('utf-8', errors='replace')), headers={'Cache-Control': 'no-cache'})
    return Response(data, media_type=mt)
