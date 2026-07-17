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
import os, re, json, sqlite3, base64, hashlib, hmac, secrets, datetime, time, socket, calendar
import urllib.request, urllib.error, urllib.parse
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
# KG이니시스 설정 (app.py와 동일 소스 — 환경변수 우선, 없으면 app 모듈 기본값=테스트값)
def inicis_mid():    return os.environ.get('INICIS_MID') or _from_app('INICIS_MID', 'INIpayTest')
def inicis_iniapi(): return os.environ.get('INICIS_INIAPI') or _from_app('INICIS_INIAPI', 'ItEQKi3rY7uvDS8l')
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
def is_new_product(created_at, now=None):
    """등록 시각부터 달력 기준 2개월 동안만 NEW로 본다."""
    if not created_at:
        return False
    try:
        made = datetime.datetime.fromisoformat(str(created_at).strip().replace('Z', '+00:00'))
        if made.tzinfo is not None:
            made = made.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        now = now or datetime.datetime.utcnow()
        month_index = made.month - 1 + 2
        year, month = made.year + month_index // 12, month_index % 12 + 1
        day = min(made.day, calendar.monthrange(year, month)[1])
        return made <= now < made.replace(year=year, month=month, day=day)
    except Exception:
        return False

_BADGE_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
def badge_color(value):
    value = str(value or '').strip()
    return value.upper() if _BADGE_COLOR_RE.fullmatch(value) else '#050505'
def jload(s, d):
    try: return json.loads(s) if s else d
    except Exception: return d
def digits(p): return re.sub(r'\D', '', str(p or ''))
def uid(): return secrets.token_hex(6)

# ── 가격·할인 규칙 (단일 구현) ─────────────────────────────────────────
#   · price(판매가) = 실제 청구가 — app.py 체크아웃이 그대로 사용 (무변경)
#   · list_price(정가) = 할인 중일 때만 채움. 할인율은 파생값(저장 안 함):
#       round((1 - 판매가/정가) × 100)  → K2G 카탈로그와 동일 모델
#   · 관리자가 입력하는 '가격'은 항상 정가. 할인율에 따라 판매가 자동 계산.
def disc_price(base, pct):
    """정가 base에 pct% 할인 적용 → 10원 단위 반올림 (정수 연산·결정적)."""
    base, pct = num(base), num(pct)
    if pct <= 0:
        return base
    return max(0, (base * (100 - pct) + 50) // 100 // 10 * 10)

def derived_pct(list_p, price):
    """정가·판매가 → 표기 할인율(%). 사이트 JS Math.round((1-판매가/정가)*100)와
    동일한 half-up 정수식 (파이썬 round는 half-even이라 .5 경계에서 어긋남)."""
    list_p, price = num(list_p), num(price)
    if list_p <= 0 or price <= 0 or price >= list_p:
        return 0
    return (200 * (list_p - price) + list_p) // (2 * list_p)

_DISC_OK = re.compile(r'^(mp|k2g)::')   # 할인 표기는 동적 카드(mp::/k2g::)만 지원

def apply_pricing(pid, base=None, pct=None):
    """상품 1건의 정가(base)/할인율(pct) 변경을 price·list_price에 반영.
    base=None → 정가 유지 · pct=None → 할인율 유지. 반환 (정가, 할인율, 판매가).
    ※ 정적(own) 상품은 카드가 고정 HTML이라 표기 불일치 방지를 위해 할인 차단."""
    cur = one('SELECT %s AS price, list_price FROM products WHERE id=?'
              % (_state['pprice'] or 'price'), (pid,))
    if not cur:
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    cur_price, cur_list = num(cur.get('price')), num(cur.get('list_price'))
    cur_pct = derived_pct(cur_list, cur_price)
    if base is None:
        base = cur_list if cur_pct > 0 else cur_price
    base = num(base)
    pct = cur_pct if pct is None else num(pct)
    if base < 0:
        raise HTTPException(400, '가격은 0 이상')
    if not 0 <= pct <= 90:
        raise HTTPException(400, '할인율은 0~90 사이 정수만 가능합니다')
    if pct > 0:
        if not _DISC_OK.match(str(pid)):
            raise HTTPException(400, '정적 상품은 카드가 고정 HTML이라 할인 표기를 지원하지 않습니다 (직접등록·K2G 상품만 가능)')
        if base <= 0:
            raise HTTPException(400, '할인율을 적용하려면 먼저 정가를 입력하세요')
        sale = disc_price(base, pct)
        run('UPDATE products SET %s=?, list_price=? WHERE id=?' % _state['pprice'],
            (sale, base, pid))
    else:
        sale = base
        run('UPDATE products SET %s=?, list_price=NULL WHERE id=?' % _state['pprice'],
            (sale, pid))
    return base, pct, sale

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
           created TEXT, expires TEXT, ip TEXT, user_agent TEXT, last_seen TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_addresses(id TEXT PRIMARY KEY, member_id TEXT, label TEXT,
           rname TEXT, phone TEXT, zip TEXT, addr1 TEXT, addr2 TEXT, is_default INTEGER DEFAULT 0, created TEXT, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_likes(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT, created TEXT, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_restock(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT,
           phone TEXT, created TEXT, notified INTEGER DEFAULT 0, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_requests(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT,
           rtype TEXT, reason TEXT, created TEXT, status TEXT DEFAULT '접수', admin_memo TEXT, updated TEXT, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_inquiries(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT,
           title TEXT, body TEXT, created TEXT, status TEXT DEFAULT '접수', answer TEXT, answered_at TEXT, answered_by TEXT, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS member_pqna(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT,
           question TEXT, created TEXT, status TEXT DEFAULT '접수', answer TEXT, answered_at TEXT, answered_by TEXT, customer_id TEXT)""",
        """CREATE TABLE IF NOT EXISTS phone_verifications(id TEXT PRIMARY KEY, member_id TEXT, phone TEXT,
           code TEXT, created TEXT, expires TEXT, used INTEGER DEFAULT 0, code_hash TEXT, attempts INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS customer_profiles(id TEXT PRIMARY KEY, customer_no TEXT UNIQUE,
           name TEXT, status TEXT DEFAULT 'ACTIVE', grade TEXT DEFAULT 'WELCOME', points_balance INTEGER DEFAULT 0,
           marketing_ok INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT, withdrawn_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS auth_identities(id TEXT PRIMARY KEY, customer_id TEXT, member_id TEXT,
           provider TEXT, provider_sub TEXT, email_norm TEXT, email_verified INTEGER DEFAULT 0,
           created_at TEXT, last_login_at TEXT, UNIQUE(provider, provider_sub), UNIQUE(member_id))""",
        """CREATE TABLE IF NOT EXISTS customer_contacts(id TEXT PRIMARY KEY, customer_id TEXT, kind TEXT,
           value TEXT, value_norm TEXT, verified INTEGER DEFAULT 0, is_primary INTEGER DEFAULT 0,
           created_at TEXT, verified_at TEXT, UNIQUE(kind, value_norm))""",
        """CREATE TABLE IF NOT EXISTS consent_history(id TEXT PRIMARY KEY, customer_id TEXT, member_id TEXT,
           consent_type TEXT, policy_version TEXT, granted INTEGER, source TEXT, ip TEXT, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS point_ledger(id TEXT PRIMARY KEY, customer_id TEXT, member_id TEXT,
           event_type TEXT, amount INTEGER, balance_after INTEGER, event_key TEXT UNIQUE, order_id TEXT,
           reason TEXT, expires_at TEXT, created_at TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS loyalty_policies(key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0,
           value TEXT, effective_from TEXT, updated_at TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS account_order_links(order_id TEXT PRIMARY KEY, customer_id TEXT,
           member_id TEXT, link_source TEXT, linked_at TEXT, verified_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS order_claims(id TEXT PRIMARY KEY, order_id TEXT, customer_id TEXT,
           member_id TEXT, phone_norm TEXT, code_hash TEXT, attempts INTEGER DEFAULT 0, created_at TEXT,
           expires_at TEXT, used INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS password_resets(id TEXT PRIMARY KEY, member_id TEXT, phone_norm TEXT,
           code_hash TEXT, attempts INTEGER DEFAULT 0, created_at TEXT, expires_at TEXT, used INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS oauth_flows(state TEXT PRIMARY KEY, member_id TEXT, provider TEXT,
           action TEXT, created_at TEXT, expires_at TEXT, used INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS account_security_events(id TEXT PRIMARY KEY, customer_id TEXT,
           member_id TEXT, event_type TEXT, ip TEXT, user_agent TEXT, detail TEXT, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, ctype TEXT, ext TEXT,
           data TEXT, created TEXT)""",
        """CREATE TABLE IF NOT EXISTS k2g_removed(uid TEXT PRIMARY KEY, name TEXT,
           created TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS site_settings(key TEXT PRIMARY KEY, value TEXT,
           updated TEXT, by_admin TEXT)""",
        """CREATE TABLE IF NOT EXISTS catalog_sequences(name TEXT PRIMARY KEY, value INTEGER)""",
        """CREATE TABLE IF NOT EXISTS product_groups(id TEXT PRIMARY KEY, group_no INTEGER UNIQUE,
           group_code TEXT UNIQUE, group_key TEXT UNIQUE, title TEXT, department TEXT, category TEXT,
           product_type TEXT, brand_artist TEXT, collection_name TEXT, source TEXT,
           sale_status TEXT DEFAULT 'ACTIVE', metadata TEXT, confidence INTEGER DEFAULT 0,
           review_state TEXT DEFAULT 'REVIEW', image TEXT, created_at TEXT, updated_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS product_variants(id TEXT PRIMARY KEY, legacy_product_id TEXT UNIQUE,
           group_id TEXT, sku TEXT UNIQUE, option_name TEXT, source TEXT,
           sale_status TEXT DEFAULT 'ACTIVE', stock_mode TEXT DEFAULT 'TRACKED', metadata TEXT,
           created_at TEXT, updated_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS product_identifiers(id TEXT PRIMARY KEY, variant_id TEXT,
           kind TEXT, value TEXT, UNIQUE(kind, value))""",
        """CREATE TABLE IF NOT EXISTS inventory_balances(variant_id TEXT PRIMARY KEY,
           location_id TEXT DEFAULT 'SEOUL', is_tracked INTEGER DEFAULT 1, on_hand INTEGER DEFAULT 0,
           reserved INTEGER DEFAULT 0, incoming INTEGER DEFAULT 0, reorder_point INTEGER DEFAULT 5,
           external_status TEXT, updated_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS inventory_movements(id TEXT PRIMARY KEY, variant_id TEXT,
           kind TEXT, quantity INTEGER, before_qty INTEGER, after_qty INTEGER, reason TEXT,
           by_admin TEXT, created_at TEXT)""",
        """CREATE INDEX IF NOT EXISTS idx_product_groups_department ON product_groups(department, review_state)""",
        """CREATE INDEX IF NOT EXISTS idx_product_variants_group ON product_variants(group_id)""",
        """CREATE INDEX IF NOT EXISTS idx_product_variants_legacy ON product_variants(legacy_product_id)""",
        """CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory_balances(is_tracked, on_hand, reserved)""",
        """CREATE INDEX IF NOT EXISTS idx_inventory_movements_variant ON inventory_movements(variant_id, created_at)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_members_provider_sub ON members(provider, sub)""",
        """CREATE INDEX IF NOT EXISTS idx_members_customer ON members(customer_id)""",
        """CREATE INDEX IF NOT EXISTS idx_auth_identities_customer ON auth_identities(customer_id)""",
        """CREATE INDEX IF NOT EXISTS idx_customer_contacts_customer ON customer_contacts(customer_id)""",
        """CREATE INDEX IF NOT EXISTS idx_consent_customer ON consent_history(customer_id, created_at)""",
        """CREATE INDEX IF NOT EXISTS idx_point_ledger_customer ON point_ledger(customer_id, created_at)""",
        """CREATE INDEX IF NOT EXISTS idx_account_orders_customer ON account_order_links(customer_id, linked_at)""",
        """CREATE INDEX IF NOT EXISTS idx_security_customer ON account_security_events(customer_id, created_at)""",
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
    try:
        # 클린 URL 마이그레이션: DB 편집본 내부 링크의 .html 제거 (1회성·멱등)
        import re as _re
        _SKIP = ('http://', 'https://', '//', 'mailto:', 'tel:', 'javascript:', '#')
        def _clean_html_links(_t):
            def _rep(_m):
                _v = _m.group(3)
                if _v.startswith(_SKIP): return _m.group(0)
                _mm = _re.fullmatch(r"(\./)?([A-Za-z0-9_\-./]+)\.html([?#][^\"\']*)?", _v)
                if not _mm: return _m.group(0)
                _b, _s = _mm.group(2).lstrip('/'), _mm.group(3) or ''
                _tgt = '/home' if _b in ('index', 'mapdal_home_mockup_v1') else '/' + _b
                return _m.group(1) + _m.group(2) + _tgt + _s + _m.group(4)
            return _re.sub(r"((?:href|data-href|action)\s*=\s*|[\"\'](?:u|url|href)[\"\']\s*:\s*|location(?:\.href)?\s*=\s*)([\"\'])([^\"\']+?)(\2)", _rep, _t)
        for _r in rows('SELECT path, html FROM page_edits'):
            _new = _clean_html_links(_r['html'] or '')
            if _new != (_r['html'] or ''):
                run('UPDATE page_edits SET html=?, updated=? WHERE path=?', (_new, now_iso(), _r['path']))
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
                     ('gender','TEXT'),('age_range','TEXT'),('birth','TEXT'),('ci','TEXT'),
                     ('customer_id','TEXT'),('status',"TEXT DEFAULT 'ACTIVE'"),('email_verified','INTEGER DEFAULT 0'),
                     ('last_login_at','TEXT'),('updated_at','TEXT'),('withdrawn_at','TEXT')):
        if mcx and col not in mcx:
            try: run("ALTER TABLE members ADD COLUMN %s %s" % (col, typ))
            except Exception: pass
    cpcx = _cols('customer_profiles')
    if cpcx and 'admin_memo' not in cpcx:
        try: run("ALTER TABLE customer_profiles ADD COLUMN admin_memo TEXT")
        except Exception: pass
    ocx = _cols('orders')
    for col, typ in (('customer_id','TEXT'),('member_id','TEXT'),('contact_phone_norm','TEXT')):
        if ocx and col not in ocx:
            try: run("ALTER TABLE orders ADD COLUMN %s %s" % (col, typ))
            except Exception: pass
    oc = _cols('orders')
    lcx = _cols('member_likes')
    for col, typ in (('page', 'TEXT'), ('pname', 'TEXT'), ('pprice', 'INTEGER'), ('pimg', 'TEXT')):
        if lcx and col not in lcx:
            try: run("ALTER TABLE member_likes ADD COLUMN %s %s" % (col, typ))
            except Exception: pass
    pvcx = _cols('phone_verifications')
    for col, typ in (('code_hash','TEXT'),('attempts','INTEGER DEFAULT 0')):
        if pvcx and col not in pvcx:
            try: run('ALTER TABLE phone_verifications ADD COLUMN %s %s' % (col, typ))
            except Exception: pass
    mscx = _cols('member_sessions')
    for col, typ in (('ip','TEXT'),('user_agent','TEXT'),('last_seen','TEXT')):
        if mscx and col not in mscx:
            try: run('ALTER TABLE member_sessions ADD COLUMN %s %s' % (col, typ))
            except Exception: pass
    for table in ('member_addresses','member_likes','member_restock','member_requests','member_inquiries','member_pqna'):
        tcx=_cols(table)
        if tcx and 'customer_id' not in tcx:
            try: run('ALTER TABLE %s ADD COLUMN customer_id TEXT' % table)
            except Exception: pass
    pcx = _cols('products')
    for col in ('img', 'descr', 'category', 'detail_html', 'gallery', 'badge', 'badge_color', 'created_at', 'related_ids'):
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
    try: _catalog_migrate_missing()   # 기존 상품 ID를 보존하며 그룹·SKU·재고원장 자동 생성
    except Exception: pass
    try: _catalog_migrate_lifestyle() # 이전 LIVING 대분류를 LIFESTYLE로 명칭 전환
    except Exception: pass
    try: _migrate_lifestyle_page_edits() # DB 편집본이 정적 파일을 덮는 경우의 화면 문구도 전환
    except Exception: pass
    try: _migrate_storefront_header_page_edits() # DB 편집본에도 개편된 상단 헤더를 멱등 반영
    except Exception: pass
    try: _account_migrate() # 기존 회원을 단일 고객 ID·동의·포인트 원장 구조로 안전하게 백필
    except Exception as e: print('account migration skipped:', e)

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
    # URL·쿠키의 장기 토큰 인증은 브라우저 기록/로그 노출 위험 때문에 허용하지 않는다.
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

def make_session(admin_id, hours=12):
    sid = secrets.token_urlsafe(24)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).isoformat(timespec='seconds')
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
            resp.set_cookie('mp_sess', sid, httponly=True, samesite='lax', secure=True, max_age=43200)
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
    resp.set_cookie('mp_sess', sid, httponly=True, samesite='lax', secure=True, max_age=43200)
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
            low = rows('''SELECT v.legacy_product_id AS id,p.%s AS name,
                CASE WHEN b.on_hand-b.reserved<0 THEN 0 ELSE b.on_hand-b.reserved END AS stock,
                CASE WHEN b.on_hand-b.reserved<=0 THEN 1 ELSE 0 END AS soldout
                FROM inventory_balances b JOIN product_variants v ON v.id=b.variant_id
                JOIN products p ON p.id=v.legacy_product_id
                WHERE b.is_tracked=1 AND b.on_hand-b.reserved<=b.reorder_point
                ORDER BY soldout DESC,stock ASC LIMIT 12''' % (_state['pname'] or 'id'))
        except Exception: pass
    latest = rows("SELECT order_id, created, status, amount, buyer FROM orders ORDER BY created DESC LIMIT 8")
    for r in latest:
        r['buyer_name'] = (jload(r.pop('buyer', None), {}) or {}).get('name', '')
        r['amount'] = num(r.get('amount'))
    cust = one('SELECT COUNT(*) AS c FROM customer_profiles') or {}
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
                      'addr': ((b.get('addr1', '') + ' ' + b.get('addr2', '')).strip()
                               + ((' · 메모: ' + str(b.get('memo'))) if b.get('memo') else ''))},
            'items': items, 'ship_method': r.get('ship_method', ''), 'tracking': r.get('tracking') or '',
            'admin_memo': r.get('admin_memo') or '', 'receipt': r.get('receipt_url') or '',
            'method': r.get('method') or '',
            'can_refund': bool(_state['paykey'] and r.get(_state['paykey']) and r.get('status') == 'PAID')}

@admin_router.get('/admin/api/orders/{oid}/receipt', response_class=HTMLResponse)
def api_admin_order_receipt(oid: str, request: Request):
    a=get_actor(request); need(a,0)
    r=one('SELECT * FROM orders WHERE order_id=?',(oid,))
    if not r: raise HTTPException(404,'not found')
    b=jload(r.get('buyer'),{}); its=jload(r.get('items'),[])
    def h(x): return str(x or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    tr=''.join('<tr><td>%s</td><td class="r">%s</td><td class="r">%d</td><td class="r">%s</td></tr>' %
               (h(it.get('n') or it.get('name') or it.get('id') or ''),format(num(it.get('p') or it.get('price')),','),
                num(it.get('q') or 1),format(num(it.get('p') or it.get('price'))*num(it.get('q') or 1),',')) for it in its)
    audit(a,'거래명세서조회',oid,'고객지원')
    return HTMLResponse('''<!doctype html><meta charset="utf-8"><title>거래명세서 — %s</title><style>body{font-family:sans-serif;max-width:760px;margin:32px auto;padding:0 20px}h1{font-size:21px;border-bottom:3px solid #E8332A;padding-bottom:10px}table{width:100%%;border-collapse:collapse;margin:16px 0;font-size:13px}th,td{border:1px solid #ccc;padding:8px}th{background:#141414;color:#fff}.r{text-align:right}.meta{font-size:12.5px;line-height:1.9}.btn{background:#141414;color:#fff;border:0;padding:10px 18px}@media print{.btn{display:none}}</style><h1>거래명세서 <small>MAPDAL SEOUL</small></h1><div class="meta"><b>주문번호</b> %s · <b>거래일시</b> %s<br><b>주문자</b> %s (%s)</div><table><tr><th>품목</th><th class="r">단가</th><th class="r">수량</th><th class="r">금액</th></tr>%s</table><h3 class="r">합계 ₩%s</h3><button class="btn" onclick="print()">인쇄</button>''' %
        (h(oid),h(oid),h((r.get('created') or '').replace('T',' ')),h(b.get('name')),h(b.get('phone')),tr,format(num(r.get('amount')),',')))

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
        tid = r.get(_state['paykey']) if _state['paykey'] else None
        if not tid: raise HTTPException(400, '거래번호(TID)가 없어 자동 환불 불가 — 이니시스 상점관리자에서 직접 취소하세요.')
        mid = inicis_mid(); iniapi = inicis_iniapi()
        if not (mid and iniapi): raise HTTPException(400, 'INICIS_MID / INICIS_INIAPI 미설정')
        ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        try: client_ip = socket.gethostbyname(socket.gethostname())
        except Exception: client_ip = '127.0.0.1'
        paymethod = 'Card'
        # hashData = SHA512(INIAPIKey + type + paymethod + timestamp + clientIp + mid + tid)
        hashdata = hashlib.sha512((iniapi + 'Refund' + paymethod + ts + client_ip + mid + tid).encode('utf-8')).hexdigest()
        payload = urllib.parse.urlencode({
            'type': 'Refund', 'paymethod': paymethod, 'timestamp': ts, 'clientIp': client_ip,
            'mid': mid, 'tid': tid, 'msg': reason, 'hashData': hashdata,
        }).encode('utf-8')
        req = urllib.request.Request('https://iniapi.inicis.com/api/v1/refund', data=payload,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'},
                                     method='POST')
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try: msg = json.loads(e.read().decode()).get('resultMsg', 'inicis error')
            except Exception: msg = 'inicis error'
            raise HTTPException(400, '이니시스 취소 실패: ' + msg)
        except Exception:
            raise HTTPException(400, '이니시스 취소 통신 오류')
        if str(res.get('resultCode')) != '00':
            raise HTTPException(400, '이니시스 취소 실패: ' + str(res.get('resultMsg', '알 수 없는 오류')))
        refunded = True
    sets, args = ["status='CANCELLED'"], []
    if 'fulfill' in _state['ocols']: sets.append("fulfill='CANCELLED'")
    if 'admin_memo' in _state['ocols']: sets.append('admin_memo=?'); args.append(('[취소] ' + reason)[:300])
    run('UPDATE orders SET %s WHERE order_id=?' % ', '.join(sets), tuple(args + [oid]))
    restored = 0
    if _state['pcols']:
        for it in jload(r.get('items'), []):
            if it.get('id'):
                try:
                    restored += run('UPDATE products SET stock = stock + ?, soldout = 0 WHERE id = ?', (num(it.get('q') or 1), it['id']))
                    catalog_inventory_from_legacy(it['id'])
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
    filt = p.get('filter') or ''
    if filt == 'low': where.append('stock > 0 AND stock < 5 AND soldout = 0')
    elif filt == 'soldout': where.append('(soldout = 1 OR stock <= 0)')
    elif filt == 'active': where.append('soldout = 0 AND stock > 0')
    elif filt == 'new' and 'created_at' in _state['pcols']: where.append('created_at IS NOT NULL')
    elif filt == 'discount' and pr and 'list_price' in _state['pcols']:
        where.append('list_price IS NOT NULL AND list_price > %s' % pr)
    elif filt == 'direct': where.append('id LIKE ?'); args.append('mp::%')
    elif filt == 'k2g': where.append('id LIKE ?'); args.append('k2g::%')
    elif filt == 'noimage' and 'img' in _state['pcols']:
        where.append("(img IS NULL OR TRIM(img) = '')")
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, int(p.get('page', 1) or 1)); size = 30
    cols = 'id, %s AS name, stock, soldout' % nm + ((', %s AS price' % pr) if pr else '') \
           + (', list_price' if 'list_price' in _state['pcols'] else '') \
           + (', created_at' if 'created_at' in _state['pcols'] else '')
    sort = p.get('sort') or 'created_desc'
    base_price = ('COALESCE(list_price, %s)' % pr) if pr and 'list_price' in _state['pcols'] else (pr or '0')
    created = 'created_at' if 'created_at' in _state['pcols'] else 'id'
    order_map = {
        'created_desc': 'CASE WHEN %s IS NULL THEN 1 ELSE 0 END, %s DESC, id DESC' % (created, created),
        'created_asc': 'CASE WHEN %s IS NULL THEN 1 ELSE 0 END, %s ASC, id ASC' % (created, created),
        'name_asc': '%s ASC, id ASC' % nm,
        'name_desc': '%s DESC, id DESC' % nm,
        'price_desc': '%s DESC, id ASC' % base_price,
        'price_asc': '%s ASC, id ASC' % base_price,
        'stock_asc': 'stock ASC, id ASC',
        'stock_desc': 'stock DESC, id ASC',
    }
    order = order_map.get(sort, order_map['created_desc'])
    if filt == 'new' and 'created_at' in _state['pcols']:
        # 달력 기준 2개월(월말 보정)은 SQL 날짜 함수보다 Python 판정이 정확하다.
        all_rows = rows('SELECT %s FROM products%s ORDER BY %s' % (cols, w, order), tuple(args))
        all_rows = [r for r in all_rows if is_new_product(r.get('created_at'))]
        total = len(all_rows); rs = all_rows[(page - 1) * size:page * size]
    else:
        total = num((one('SELECT COUNT(*) AS c FROM products' + w, tuple(args)) or {}).get('c'))
        rs = rows('SELECT %s FROM products%s ORDER BY %s LIMIT %d OFFSET %d' %
                  (cols, w, order, size, (page - 1) * size), tuple(args))
    return {'total': total, 'page': page, 'size': size,
            'rows': [{'id': r['id'], 'name': r.get('name') or r['id'], 'stock': num(r.get('stock')),
                      'soldout': num(r.get('soldout')), 'price': num(r.get('price')) if pr else None,
                      'list_price': num(r.get('list_price')) or None,
                      'created_at': r.get('created_at') or '',
                      'is_new': is_new_product(r.get('created_at')),
                      'source': 'direct' if str(r['id']).startswith('mp::') else ('k2g' if str(r['id']).startswith('k2g::') else 'own'),
                      'pct': derived_pct(r.get('list_price'), r.get('price')) if pr else 0} for r in rs]}

def _catalog_variant_rows(group_ids):
    if not group_ids: return []
    ph = ','.join(['?'] * len(group_ids))
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    return rows('''SELECT v.id AS variant_id,v.legacy_product_id,v.group_id,v.sku,v.option_name,
        v.source,v.sale_status,v.stock_mode,v.metadata,v.created_at,
        p.%s AS product_name,p.%s AS price,p.list_price,p.stock,p.soldout,p.img,
        b.is_tracked,b.on_hand,b.reserved,b.incoming,b.reorder_point,b.external_status,b.updated_at AS inventory_updated
        FROM product_variants v LEFT JOIN products p ON p.id=v.legacy_product_id
        LEFT JOIN inventory_balances b ON b.variant_id=v.id
        WHERE v.group_id IN (%s) ORDER BY v.group_id,v.sku''' % (nm, pr, ph), tuple(group_ids))

def _catalog_variant_json(r):
    tracked = num(r.get('is_tracked')) == 1
    available = max(0, num(r.get('on_hand')) - num(r.get('reserved'))) if tracked else None
    return {'id': r['variant_id'], 'legacy_id': r.get('legacy_product_id') or '', 'sku': r.get('sku') or '',
            'name': r.get('product_name') or r.get('option_name') or r.get('sku'),
            'option': r.get('option_name') or '', 'source': r.get('source') or '',
            'sale_status': r.get('sale_status') or 'ACTIVE', 'stock_mode': r.get('stock_mode') or 'TRACKED',
            'price': num(r.get('price')), 'list_price': num(r.get('list_price')) or None,
            'soldout': num(r.get('soldout')), 'tracked': tracked, 'on_hand': num(r.get('on_hand')),
            'reserved': num(r.get('reserved')), 'available': available, 'incoming': num(r.get('incoming')),
            'reorder_point': num(r.get('reorder_point')), 'external_status': r.get('external_status') or '',
            'image': r.get('img') or '', 'created_at': r.get('created_at') or '',
            'metadata': jload(r.get('metadata'), {}) or {}}

@admin_router.get('/admin/api/catalog/summary')
def api_catalog_summary(request: Request):
    a = get_actor(request); need(a, 0)
    g = num((one('SELECT COUNT(*) AS n FROM product_groups') or {}).get('n'))
    s = num((one('SELECT COUNT(*) AS n FROM product_variants') or {}).get('n'))
    review = num((one("SELECT COUNT(*) AS n FROM product_groups WHERE review_state<>'READY' OR confidence<80") or {}).get('n'))
    external = num((one('SELECT COUNT(*) AS n FROM inventory_balances WHERE is_tracked=0') or {}).get('n'))
    out = num((one("SELECT COUNT(*) AS n FROM inventory_balances WHERE is_tracked=1 AND on_hand-reserved<=0") or {}).get('n'))
    low = num((one("SELECT COUNT(*) AS n FROM inventory_balances WHERE is_tracked=1 AND on_hand-reserved>0 AND on_hand-reserved<=reorder_point") or {}).get('n'))
    incoming = num((one('SELECT COALESCE(SUM(incoming),0) AS n FROM inventory_balances WHERE is_tracked=1') or {}).get('n'))
    return {'groups': g, 'skus': s, 'review': review, 'external': external,
            'out': out, 'low': low, 'incoming': incoming}

def _catalog_search_params(p):
    query = (p.get('query') or '').strip()
    structured, words = {}, []
    for token in re.findall(r'"[^"]+"|\S+', query):
        token = token.strip('"')
        if ':' in token:
            k, v = token.split(':', 1)
            if k.lower() in ('dept', 'source', 'status', 'sku', 'id', 'artist') and v:
                structured[k.lower()] = v.strip(); continue
        words.append(token)
    return structured, ' '.join(words).strip()

@admin_router.get('/admin/api/catalog/groups')
def api_catalog_groups(request: Request):
    a = get_actor(request); need(a, 0)
    p = request.query_params; where, args = [], []
    st, keyword = _catalog_search_params(p)
    dept = (p.get('department') or st.get('dept') or '').upper()
    if dept == 'LIVING': dept = 'LIFESTYLE'  # 이전 저장 검색어 호환
    source = (p.get('source') or st.get('source') or '').upper()
    status = (p.get('status') or st.get('status') or '').upper()
    if dept in _DEPT_KEYS: where.append('g.department=?'); args.append(dept)
    if source in ('K2G', 'DIRECT', 'OWN'): where.append('g.source=?'); args.append(source)
    if status in ('ACTIVE', 'PAUSED', 'HIDDEN', 'SOLD_OUT'): where.append('g.sale_status=?'); args.append(status)
    issue = p.get('issue') or ''
    if issue == 'review': where.append("(g.review_state<>'READY' OR g.confidence<80)")
    elif issue == 'noimage': where.append("(g.image IS NULL OR TRIM(g.image)='')")
    elif issue == 'ready': where.append("g.review_state='READY' AND g.confidence>=80")
    if keyword:
        kw = '%' + keyword.lower() + '%'
        where.append('''(LOWER(g.title) LIKE ? OR LOWER(g.group_code) LIKE ? OR LOWER(COALESCE(g.brand_artist,'')) LIKE ?
            OR EXISTS(SELECT 1 FROM product_variants vx WHERE vx.group_id=g.id AND
              (LOWER(vx.sku) LIKE ? OR LOWER(vx.legacy_product_id) LIKE ? OR LOWER(COALESCE(vx.option_name,'')) LIKE ?)))''')
        args += [kw] * 6
    for key, col in (('sku', 'vx.sku'), ('id', 'vx.legacy_product_id')):
        if st.get(key):
            where.append('EXISTS(SELECT 1 FROM product_variants vx WHERE vx.group_id=g.id AND LOWER(%s) LIKE ?)' % col)
            args.append('%' + st[key].lower() + '%')
    if st.get('artist'):
        where.append("LOWER(COALESCE(g.brand_artist,'')) LIKE ?"); args.append('%' + st['artist'].lower() + '%')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, num(p.get('page') or 1)); size = 24
    total = num((one('SELECT COUNT(*) AS n FROM product_groups g' + w, tuple(args)) or {}).get('n'))
    order = {'created_desc': 'g.created_at DESC,g.group_no DESC', 'created_asc': 'g.created_at ASC,g.group_no ASC',
             'name_asc': 'g.title ASC,g.group_no ASC', 'name_desc': 'g.title DESC,g.group_no DESC',
             'price_desc': '(SELECT MAX(COALESCE(px.list_price,px.%s)) FROM product_variants vx JOIN products px ON px.id=vx.legacy_product_id WHERE vx.group_id=g.id) DESC,g.group_no DESC' % (_state['pprice'] or 'price'),
             'price_asc': '(SELECT MIN(COALESCE(px.list_price,px.%s)) FROM product_variants vx JOIN products px ON px.id=vx.legacy_product_id WHERE vx.group_id=g.id) ASC,g.group_no ASC' % (_state['pprice'] or 'price'),
             'quality_asc': 'g.confidence ASC,g.group_no DESC', 'code_asc': 'g.group_no ASC'}.get(
                 p.get('sort') or 'created_desc', 'g.created_at DESC,g.group_no DESC')
    gs = rows('''SELECT g.*,(SELECT COUNT(*) FROM product_variants v WHERE v.group_id=g.id) AS variant_count
        FROM product_groups g%s ORDER BY %s LIMIT %d OFFSET %d''' % (w, order, size, (page - 1) * size), tuple(args))
    vr = _catalog_variant_rows([g['id'] for g in gs]); by = {}
    for r in vr: by.setdefault(r['group_id'], []).append(_catalog_variant_json(r))
    out = []
    for g in gs:
        vv = by.get(g['id'], []); tracked = [v for v in vv if v['tracked']]
        out.append({'id': g['id'], 'group_no': num(g.get('group_no')), 'code': g.get('group_code') or '',
                    'title': g.get('title') or '', 'department': g.get('department') or '',
                    'department_label': _DEPT_LABEL.get(g.get('department'), g.get('department') or ''),
                    'category': g.get('category') or '', 'product_type': g.get('product_type') or '',
                    'brand_artist': g.get('brand_artist') or '', 'collection': g.get('collection_name') or '',
                    'source': g.get('source') or '', 'sale_status': g.get('sale_status') or 'ACTIVE',
                    'confidence': num(g.get('confidence')), 'review_state': g.get('review_state') or 'REVIEW',
                    'review_reasons': _catalog_review_reasons(g, vv), 'image': g.get('image') or '',
                    'created_at': g.get('created_at') or '', 'variant_count': len(vv), 'variants': vv,
                    'available': sum(v['available'] or 0 for v in tracked),
                    'external_count': len([v for v in vv if not v['tracked']])})
    return {'total': total, 'page': page, 'size': size, 'rows': out}

@admin_router.get('/admin/api/catalog/group')
def api_catalog_group(request: Request):
    a = get_actor(request); need(a, 0)
    gid = (request.query_params.get('id') or '').strip()
    g = one('SELECT * FROM product_groups WHERE id=?', (gid,))
    if not g: raise HTTPException(404, '상품그룹을 찾을 수 없습니다')
    vv = [_catalog_variant_json(r) for r in _catalog_variant_rows([gid])]
    return {'group': {**g, 'metadata': jload(g.get('metadata'), {}) or {},
                      'review_reasons': _catalog_review_reasons(g, vv)},
            'variants': vv, 'departments': [{'value': k, 'label': v} for k, v in CATALOG_DEPARTMENTS],
            'categories': [{'value': k, 'label': v} for k, v in PRODUCT_CATEGORIES]}

@admin_router.post('/admin/api/catalog/group/update')
def api_catalog_group_update(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '상품그룹 수정')
    gid = str(body.get('id') or '')
    g = one('SELECT * FROM product_groups WHERE id=?', (gid,))
    if not g: raise HTTPException(404, '상품그룹을 찾을 수 없습니다')
    if str(body.get('review_state') or '').upper() == 'READY':
        effective_title = str(body.get('title') if 'title' in body else g.get('title') or '').strip()
        effective_dept = str(body.get('department') if 'department' in body else g.get('department') or '').upper()
        if effective_dept == 'LIVING': effective_dept = 'LIFESTYLE'
        effective_image = _safe_url(body.get('image') if 'image' in body else g.get('image'))
        if not effective_title: raise HTTPException(400, '검토 완료 전 그룹명을 입력하세요')
        if effective_dept not in _DEPT_KEYS: raise HTTPException(400, '검토 완료 전 대분류를 선택하세요')
        if not effective_image: raise HTTPException(400, '검토 완료 전 대표 이미지를 입력하세요')
        priced = one('''SELECT COUNT(*) AS n,
            SUM(CASE WHEN p.%s IS NULL OR p.%s<=0 THEN 1 ELSE 0 END) AS bad
            FROM product_variants v JOIN products p ON p.id=v.legacy_product_id WHERE v.group_id=?'''
            % ((_state['pprice'] or 'price'), (_state['pprice'] or 'price')), (gid,)) or {}
        if not num(priced.get('n')) or num(priced.get('bad')):
            raise HTTPException(400, '검토 완료 전 모든 SKU의 판매가격을 확인하세요')
    allowed = {'title': 300, 'category': 80, 'product_type': 80, 'brand_artist': 160,
               'collection_name': 200, 'image': 2000}
    sets, args, changed = [], [], []
    for key, limit in allowed.items():
        if key in body:
            val = str(body.get(key) or '').strip()[:limit]
            if key == 'image' and val: val = _safe_url(val)
            sets.append('%s=?' % key); args.append(val); changed.append(key)
    if 'department' in body:
        dept = str(body.get('department') or '').upper()
        if dept == 'LIVING': dept = 'LIFESTYLE'
        if dept not in _DEPT_KEYS: raise HTTPException(400, '올바른 대분류가 아닙니다')
        sets.append('department=?'); args.append(dept); changed.append('department')
    if 'sale_status' in body:
        status = str(body.get('sale_status') or '').upper()
        if status not in ('ACTIVE', 'PAUSED', 'HIDDEN', 'SOLD_OUT'): raise HTTPException(400, '판매상태 오류')
        sets.append('sale_status=?'); args.append(status); changed.append('sale_status')
    if 'review_state' in body:
        state = str(body.get('review_state') or '').upper()
        if state not in ('READY', 'REVIEW'): raise HTTPException(400, '검토상태 오류')
        sets.append('review_state=?'); args.append(state); changed.append('review_state')
        if state == 'READY':
            sets.append('confidence=?'); args.append(100)
    if 'metadata' in body:
        meta = body.get('metadata') if isinstance(body.get('metadata'), dict) else {}
        if str(body.get('review_state') or '').upper() == 'READY':
            meta.pop('review_reasons', None)
        sets.append('metadata=?'); args.append(json.dumps(meta, ensure_ascii=False)); changed.append('metadata')
    elif str(body.get('review_state') or '').upper() == 'READY':
        meta = jload(g.get('metadata'), {}) or {}; meta.pop('review_reasons', None)
        sets.append('metadata=?'); args.append(json.dumps(meta, ensure_ascii=False))
    if not sets: raise HTTPException(400, '변경할 값 없음')
    sets.append('updated_at=?'); args.append(now_iso())
    ops = [('UPDATE product_groups SET %s WHERE id=?' % ','.join(sets), tuple(args + [gid]))]
    if 'sale_status' in body:
        status = str(body.get('sale_status') or '').upper()
        ops.append(('UPDATE product_variants SET sale_status=?,updated_at=? WHERE group_id=?', (status, now_iso(), gid)))
        for v in rows('''SELECT v.legacy_product_id,b.is_tracked,b.on_hand,b.reserved,b.external_status
            FROM product_variants v LEFT JOIN inventory_balances b ON b.variant_id=v.id WHERE v.group_id=?''', (gid,)):
            inventory_out = ((num(v.get('is_tracked')) and num(v.get('on_hand')) - num(v.get('reserved')) <= 0)
                             or (not num(v.get('is_tracked')) and v.get('external_status') == 'OUT'))
            ops.append(('UPDATE products SET soldout=? WHERE id=?',
                        (1 if status != 'ACTIVE' or inventory_out else 0, v['legacy_product_id'])))
    dept = str(body.get('department') or g.get('department') or '')
    cat = norm_cat(body.get('category')) or _DEPT_TO_CAT.get(dept, g.get('category') or '')
    if cat and 'category' in _state['pcols']:
        ops.append(('UPDATE products SET category=? WHERE id IN (SELECT legacy_product_id FROM product_variants WHERE group_id=?)', (cat, gid)))
    runmany(ops)
    audit(a, '상품그룹수정', gid, ', '.join(changed))
    return {'ok': True}

@admin_router.post('/admin/api/catalog/groups/merge')
def api_catalog_groups_merge(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '상품그룹 병합')
    ids = list(dict.fromkeys(str(x) for x in (body.get('group_ids') or []) if x))
    target = str(body.get('target_id') or (ids[0] if ids else ''))
    if len(ids) < 2 or target not in ids: raise HTTPException(400, '병합할 그룹을 2개 이상 선택하세요')
    tg = one('SELECT * FROM product_groups WHERE id=?', (target,))
    if not tg: raise HTTPException(404, '기준 그룹이 없습니다')
    ph = ','.join(['?'] * len(ids))
    found = {r['id'] for r in rows('SELECT id FROM product_groups WHERE id IN (%s)' % ph, tuple(ids))}
    if found != set(ids): raise HTTPException(400, '존재하지 않는 그룹이 포함되어 있습니다')
    moved, ops, stamp = 0, [], now_iso()
    for gid in ids:
        if gid == target: continue
        for v in rows('''SELECT vx.id,vx.legacy_product_id,b.is_tracked,b.on_hand,b.reserved,b.external_status
            FROM product_variants vx LEFT JOIN inventory_balances b ON b.variant_id=vx.id
            WHERE vx.group_id=? ORDER BY vx.sku''', (gid,)):
            moved += 1
            # SKU는 발급 후 영구 식별자다. 그룹을 옮겨도 절대 다시 채번하지 않는다.
            ops.append(('UPDATE product_variants SET group_id=?,sale_status=?,updated_at=? WHERE id=?',
                        (target, tg.get('sale_status') or 'ACTIVE', stamp, v['id'])))
            inv_out = ((num(v.get('is_tracked')) and num(v.get('on_hand')) - num(v.get('reserved')) <= 0)
                       or (not num(v.get('is_tracked')) and v.get('external_status') == 'OUT'))
            ops.append(('UPDATE products SET soldout=? WHERE id=?',
                        (1 if (tg.get('sale_status') or 'ACTIVE') != 'ACTIVE' or inv_out else 0,
                         v.get('legacy_product_id'))))
        ops.append(('DELETE FROM product_groups WHERE id=?', (gid,)))
    runmany(ops)
    audit(a, '상품그룹병합', target, '%d개 그룹 · %d SKU 이동' % (len(ids), moved))
    return {'ok': True, 'target_id': target, 'moved': moved}

@admin_router.post('/admin/api/catalog/group/split')
def api_catalog_group_split(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '상품그룹 분리')
    source = str(body.get('group_id') or ''); vids = list(dict.fromkeys(str(x) for x in (body.get('variant_ids') or []) if x))
    g = one('SELECT * FROM product_groups WHERE id=?', (source,))
    if not g or not vids: raise HTTPException(400, '분리할 SKU를 선택하세요')
    existing_vids = {r['id'] for r in rows('SELECT id FROM product_variants WHERE group_id=?', (source,))}
    total = len(existing_vids)
    if any(vid not in existing_vids for vid in vids): raise HTTPException(400, '다른 그룹의 SKU가 포함되어 있습니다')
    if len(vids) >= total: raise HTTPException(400, '전체 SKU는 분리할 수 없습니다. 그룹 정보를 수정하세요')
    mx = num((one('SELECT MAX(group_no) AS n FROM product_groups') or {}).get('n')) + 1
    gid = 'grp_' + uid(); title = str(body.get('title') or (g['title'] + ' (분리)')).strip()[:300]
    vals = (gid, mx, 'PG-%06d' % mx, 'MANUAL|' + gid, title, g['department'], g['category'],
            g['product_type'], g['brand_artist'], g['collection_name'], 'DIRECT', g['sale_status'],
            g['metadata'], g['confidence'], 'REVIEW', g['image'], now_iso(), now_iso())
    ops = [('INSERT INTO product_groups(id,group_no,group_code,group_key,title,department,category,product_type,brand_artist,collection_name,source,sale_status,metadata,confidence,review_state,image,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', vals)]
    for vid in vids:
        # 분리 역시 기존 SKU를 유지해 주문·외부 연계 식별자가 바뀌지 않게 한다.
        ops.append(('UPDATE product_variants SET group_id=?,updated_at=? WHERE id=? AND group_id=?',
                    (gid, now_iso(), vid, source)))
    runmany(ops)
    audit(a, '상품그룹분리', source, '%d SKU → %s' % (len(vids), gid))
    return {'ok': True, 'id': gid}

@admin_router.get('/admin/api/inventory')
def api_inventory(request: Request):
    a = get_actor(request); need(a, 0)
    p = request.query_params; where, args = [], []
    query = (p.get('query') or '').strip().lower()
    if query:
        kw = '%' + query + '%'
        where.append("(LOWER(g.title) LIKE ? OR LOWER(v.sku) LIKE ? OR LOWER(v.legacy_product_id) LIKE ?)"); args += [kw, kw, kw]
    dept = (p.get('department') or '').upper()
    if dept == 'LIVING': dept = 'LIFESTYLE'
    if dept in _DEPT_KEYS: where.append('g.department=?'); args.append(dept)
    filt = p.get('filter') or ''
    if filt == 'out': where.append("((b.is_tracked=1 AND b.on_hand-b.reserved<=0) OR (b.is_tracked=0 AND b.external_status='OUT'))")
    elif filt == 'tracked_out': where.append('b.is_tracked=1 AND b.on_hand-b.reserved<=0')
    elif filt == 'under5': where.append('b.is_tracked=1 AND b.on_hand-b.reserved<5')
    elif filt == 'low': where.append('b.is_tracked=1 AND b.on_hand-b.reserved>0 AND b.on_hand-b.reserved<=b.reorder_point')
    elif filt == 'incoming': where.append('b.is_tracked=1 AND b.incoming>0')
    elif filt == 'external': where.append('b.is_tracked=0')
    elif filt == 'tracked': where.append('b.is_tracked=1')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    page = max(1, num(p.get('page') or 1)); size = 40
    base = ''' FROM product_variants v JOIN product_groups g ON g.id=v.group_id
        JOIN inventory_balances b ON b.variant_id=v.id JOIN products p ON p.id=v.legacy_product_id'''
    total = num((one('SELECT COUNT(*) AS n' + base + w, tuple(args)) or {}).get('n'))
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    order = {'stock_asc': 'b.is_tracked DESC,(b.on_hand-b.reserved) ASC,v.sku',
             'stock_desc': 'b.is_tracked DESC,(b.on_hand-b.reserved) DESC,v.sku',
             'name_asc': 'g.title ASC,v.sku', 'updated_desc': 'b.updated_at DESC,v.sku'}.get(
                 p.get('sort') or 'stock_asc', 'b.is_tracked DESC,(b.on_hand-b.reserved) ASC,v.sku')
    rs = rows('''SELECT v.id AS variant_id,v.legacy_product_id,v.sku,v.stock_mode,v.sale_status,
        g.id AS group_id,g.group_code,g.title,g.department,g.source,p.%s AS product_name,p.%s AS price,p.stock,p.soldout,
        b.is_tracked,b.on_hand,b.reserved,b.incoming,b.reorder_point,b.external_status,b.updated_at%s%s
        ORDER BY %s LIMIT %d OFFSET %d''' % (nm, pr, base, w, order, size, (page - 1) * size), tuple(args))
    return {'total': total, 'page': page, 'size': size, 'rows': [dict(_catalog_variant_json(r),
            group_id=r['group_id'], group_code=r.get('group_code') or '', group_title=r.get('title') or '',
            department=r.get('department') or '', source=r.get('source') or '') for r in rs]}

@admin_router.post('/admin/api/inventory/adjust')
def api_inventory_adjust(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 1, '재고 조정')
    vid = str(body.get('variant_id') or '')
    v = one('''SELECT v.*,b.is_tracked,b.on_hand,b.reserved,b.incoming,b.reorder_point,b.external_status
        FROM product_variants v JOIN inventory_balances b ON b.variant_id=v.id WHERE v.id=?''', (vid,))
    if not v: raise HTTPException(404, 'SKU를 찾을 수 없습니다')
    kind = str(body.get('kind') or 'MANUAL').upper(); reason = str(body.get('reason') or '').strip()[:300]
    if not num(v.get('is_tracked')):
        status = str(body.get('external_status') or '').upper()
        if status not in ('AVAILABLE', 'OUT', 'UNKNOWN'): raise HTTPException(400, '외부재고 상태를 선택하세요')
        sale_blocked = str(v.get('sale_status') or 'ACTIVE') != 'ACTIVE'
        runmany([
            ('UPDATE inventory_balances SET external_status=?,updated_at=? WHERE variant_id=?', (status, now_iso(), vid)),
            ('UPDATE products SET soldout=? WHERE id=?', (1 if status == 'OUT' or sale_blocked else 0, v['legacy_product_id'])),
        ])
        audit(a, '외부재고상태', v.get('sku') or vid, status + (' · ' + reason if reason else ''))
        return {'ok': True, 'external_status': status}
    before = num(v.get('on_hand'))
    if kind == 'COUNT': after = num(body.get('quantity'))
    else: after = before + num(body.get('quantity'))
    if after < 0: raise HTTPException(400, '재고는 0보다 작을 수 없습니다')
    incoming = max(0, num(body.get('incoming'))) if body.get('incoming') is not None else num(v.get('incoming'))
    reorder = max(0, num(body.get('reorder_point'))) if body.get('reorder_point') is not None else num(v.get('reorder_point'))
    available = max(0, after - num(v.get('reserved')))
    sale_blocked = str(v.get('sale_status') or 'ACTIVE') != 'ACTIVE'
    delta = after - before
    stamp = now_iso()
    runmany([
        ('UPDATE inventory_balances SET on_hand=?,incoming=?,reorder_point=?,updated_at=? WHERE variant_id=?',
         (after, incoming, reorder, stamp, vid)),
        ('UPDATE products SET stock=?,soldout=? WHERE id=?',
         (available, 1 if available <= 0 or sale_blocked else 0, v['legacy_product_id'])),
        ('INSERT INTO inventory_movements(id,variant_id,kind,quantity,before_qty,after_qty,reason,by_admin,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
         (uid(), vid, kind, delta, before, after, reason, a['name'], stamp)),
    ])
    audit(a, '재고조정', v.get('sku') or vid, '%d→%d · %s' % (before, after, kind))
    return {'ok': True, 'on_hand': after, 'available': available, 'incoming': incoming, 'reorder_point': reorder}

@admin_router.get('/admin/api/inventory/history')
def api_inventory_history(request: Request):
    a = get_actor(request); need(a, 0)
    vid = (request.query_params.get('variant_id') or '').strip()
    return {'rows': rows('SELECT * FROM inventory_movements WHERE variant_id=? ORDER BY created_at DESC LIMIT 100', (vid,))}

@admin_router.get('/admin/api/catalog.csv')
def api_catalog_csv(request: Request):
    a = get_actor(request); need(a, 1, '상품·재고 CSV 다운로드')
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    rs = rows('''SELECT g.group_code,g.title,g.department,g.category,g.product_type,g.brand_artist,
        g.collection_name,g.source AS group_source,g.sale_status AS group_status,g.review_state,g.confidence,
        v.sku,v.legacy_product_id,v.option_name,v.source,v.sale_status,v.stock_mode,
        p.%s AS product_name,p.%s AS price,p.list_price,p.soldout,
        b.is_tracked,b.on_hand,b.reserved,b.incoming,b.reorder_point,b.external_status,b.updated_at
        FROM product_groups g JOIN product_variants v ON v.group_id=g.id
        JOIN products p ON p.id=v.legacy_product_id LEFT JOIN inventory_balances b ON b.variant_id=v.id
        ORDER BY g.group_no,v.sku''' % (nm, pr))
    head = ['그룹코드','그룹명','대분류','카테고리','상품유형','브랜드/아티스트','컬렉션/앨범',
            '그룹등록경로','그룹판매상태','검토상태','완성도','SKU','기존상품ID','옵션','SKU등록경로',
            'SKU판매상태','재고관리방식','상품명','판매가','정가','품절','실재고','예약재고','가용재고',
            '입고예정','안전재고','외부재고상태','재고갱신일']
    lines = [','.join(esc_csv(x) for x in head)]
    for r in rs:
        tracked = num(r.get('is_tracked')) == 1
        available = max(0, num(r.get('on_hand')) - num(r.get('reserved'))) if tracked else ''
        vals = [r.get('group_code'),r.get('title'),r.get('department'),r.get('category'),r.get('product_type'),
                r.get('brand_artist'),r.get('collection_name'),r.get('group_source'),r.get('group_status'),
                r.get('review_state'),r.get('confidence'),r.get('sku'),r.get('legacy_product_id'),r.get('option_name'),
                r.get('source'),r.get('sale_status'),r.get('stock_mode'),r.get('product_name'),r.get('price'),
                r.get('list_price'),r.get('soldout'),r.get('on_hand') if tracked else '',r.get('reserved') if tracked else '',
                available,r.get('incoming') if tracked else '',r.get('reorder_point') if tracked else '',
                r.get('external_status') if not tracked else '',r.get('updated_at')]
        lines.append(','.join(esc_csv(x) for x in vals))
    audit(a, '상품재고CSV', '', '%d SKU' % len(rs))
    return Response(('\ufeff' + '\n'.join(lines)).encode('utf-8'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="mapdal-catalog-inventory.csv"'})

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
    priced = False
    if (body.get('price') is not None or body.get('discount_pct') is not None) and _state['pprice']:
        need(a, 2, '가격·할인 변경')
        b_, p_, s_ = apply_pricing(pid, base=body.get('price'), pct=body.get('discount_pct'))
        log.append('정가→%s' % format(b_, ',')
                   + (' · 할인 %d%% → 판매 ₩%s' % (p_, format(s_, ',')) if p_ else ''))
        priced = True
    if not sets and not priced: raise HTTPException(400, '변경할 값 없음')
    if sets:
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
    try: catalog_product_from_legacy(pid)
    except Exception: pass
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
            ((b.get('addr1', '') + ' ' + b.get('addr2', '')).strip()
             + ((' · 메모: ' + str(b.get('memo'))) if b.get('memo') else '')), names,
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
    raise HTTPException(410, '전화번호 기반 고객 동기화는 폐기되었습니다. 통합 고객·계정을 이용하세요')
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
    raise HTTPException(410, '구형 고객 API는 폐기되었습니다. /admin/api/accounts를 이용하세요')
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
    raise HTTPException(410, '전화번호 기반 고객 조회는 폐기되었습니다. 통합 고객번호를 이용하세요')
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
    raise HTTPException(410, '구형 고객 수정 API는 폐기되었습니다. 통합 고객·계정을 이용하세요')
    ph = digits(body.get('phone'))
    if not ph: raise HTTPException(400, 'phone required')
    sets, args, log = [], [], []
    if 'memo' in body: sets.append('memo=?'); args.append((body.get('memo') or '')[:500]); log.append('메모')
    if 'mk' in body: raise HTTPException(400, '마케팅 동의는 고객 본인 동의 이력으로만 변경할 수 있습니다')
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
    raise HTTPException(410, '전화번호 기반 고객 CSV는 폐기되었습니다')
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
    cf = solapi_conf()
    _mid = inicis_mid()
    mode = '테스트(실과금 없음)' if _mid == 'INIpayTest' else ('라이브(실결제) · MID ' + _mid)
    try:
        oc = num((one('SELECT COUNT(*) AS c FROM orders') or {}).get('c'))
        pc = num((one('SELECT COUNT(*) AS c FROM products') or {}).get('c'))
        cc = num((one('SELECT COUNT(*) AS c FROM customer_profiles') or {}).get('c'))
        db_ok = True
    except Exception:
        oc = pc = cc = 0; db_ok = False
    return {'db': 'PostgreSQL' if IS_PG else 'SQLite', 'db_ok': db_ok, 'orders': oc, 'products': pc,
            'customers': cc, 'pg_mode': mode,
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
a.btn{display:inline-block;font:inherit;font-weight:700;padding:4px 9px;font-size:12px;background:#fff;color:var(--black);border:1px solid #999;text-decoration:none}
.pager{display:flex;gap:6px;align-items:center;margin-top:12px;font-family:'IBM Plex Mono';font-size:12px}
.right{text-align:right}.mono{font-family:'IBM Plex Mono'}
.modal-bg{position:fixed;inset:0;background:rgba(20,20,20,.55);display:none;align-items:flex-start;justify-content:center;z-index:100;padding:30px 12px;overflow:auto}
.modal{background:#fff;max-width:660px;width:100%;padding:22px}.modal.wide{max-width:1180px}.modal h3{font-size:16px;margin-bottom:14px}
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
.product-tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:14px}
.product-tab{border:0;background:transparent;padding:10px 18px;font:inherit;font-weight:700;color:#777;cursor:pointer;border-bottom:3px solid transparent}
.product-tab.on{color:var(--black);border-color:var(--red)}
.p-summary{grid-template-columns:repeat(6,minmax(120px,1fr))}.p-summary .card{padding:11px 13px}.p-summary .card .v{font-size:18px}
.p-summary .drill{font:inherit;color:inherit;text-align:left;cursor:pointer;position:relative;transition:transform .12s,border-color .12s,box-shadow .12s}
.p-summary .drill:hover,.p-summary .drill:focus{transform:translateY(-2px);border-color:#999;box-shadow:0 5px 16px rgba(0,0,0,.08);outline:none}
.p-summary .drill:after{content:'목록 보기 →';display:block;font-size:10px;font-weight:700;color:#777;margin-top:7px}
.pview{display:none}.pview.on{display:block}.toolbar.grow input[type=text]{min-width:230px;flex:1}
.group-list{display:flex;flex-direction:column;gap:9px}.group-card{background:#fff;border:1px solid var(--line)}
.group-card.needs-review{border-left:4px solid var(--amber)}.group-head{display:grid;grid-template-columns:auto 74px minmax(260px,1fr) 150px 120px auto;gap:10px;align-items:center;padding:12px 14px;cursor:pointer}
.group-head:hover{background:#faf9f5}.group-code{font:11px 'IBM Plex Mono';color:#888}.group-title{font-size:13.5px;font-weight:700}.group-sub{font-size:11px;color:#888;margin-top:4px}
.dept-chip,.meta-chip{display:inline-block;font-size:10px;font-weight:700;padding:3px 7px;background:#eee;margin-right:4px;white-space:nowrap}.dept-chip{background:#141414;color:#fff}.meta-chip.warn{background:#fff0cf;color:#8a5b00}.meta-chip.ok{background:#e6f5eb;color:#087333}
.quality{font:11px 'IBM Plex Mono';font-weight:700}.quality-bar{height:4px;background:#eee;margin-top:4px;width:70px}.quality-bar i{display:block;height:100%;background:var(--ok)}.needs-review .quality-bar i{background:var(--amber)}
.variant-wrap{display:none;border-top:1px solid var(--line);padding:0 14px 12px}.group-card.open .variant-wrap{display:block}.variant-wrap table{margin-top:10px}.variant-name{max-width:360px}.empty-state{background:#fff;border:1px dashed #bbb;padding:42px;text-align:center;color:#888}
.inv-status{font-size:11px;font-weight:700}.inv-status.out{color:var(--bad)}.inv-status.low{color:#9a6b00}.inv-status.ok{color:var(--ok)}
@media(max-width:900px){.p-summary{grid-template-columns:repeat(2,1fr)}.group-head{grid-template-columns:auto 65px 1fr auto}.group-head>.gsource,.group-head>.gstock{display:none}.variant-wrap{overflow-x:auto}}
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
  <div class="product-tabs"><button class="product-tab on" id="pt-catalog" onclick="productMode('catalog')">상품</button>
  <button class="product-tab" id="pt-inventory" onclick="productMode('inventory')">재고</button>
  <button class="product-tab" id="pt-review" onclick="productMode('review')">검토함 <span id="reviewCount"></span></button></div>
  <div id="productSummary" class="cards p-summary"><div class="loading">현황 계산 중…</div></div>
  <div id="pv-catalog" class="pview on">
   <div class="toolbar grow"><input id="catalogQ" type="text" placeholder="상품명 · 아티스트 · 그룹코드 · SKU · 기존ID" onkeydown="if(event.key==='Enter')loadCatalog(1)">
   <select id="catalogDept" onchange="loadCatalog(1)"><option value="">대분류 전체</option><option>KPOP</option><option>KFOOD</option><option>KBEAUTY</option><option>KFASHION</option><option>LIFESTYLE</option></select>
   <select id="catalogSource" onchange="loadCatalog(1)"><option value="">등록경로 전체</option><option value="DIRECT">직접등록</option><option value="K2G">K2G 연동</option><option value="OWN">기존상품</option></select>
   <select id="catalogStatus" onchange="loadCatalog(1)"><option value="">판매상태 전체</option><option value="ACTIVE">판매중</option><option value="PAUSED">일시중지</option><option value="HIDDEN">숨김</option><option value="SOLD_OUT">품절</option></select>
   <select id="catalogIssue" onchange="loadCatalog(1)"><option value="">품질 전체</option><option value="ready">검토완료</option><option value="review">확인필요</option><option value="noimage">대표이미지 없음</option></select>
   <select id="catalogSort" onchange="loadCatalog(1)"><option value="created_desc">최근 등록순</option><option value="created_asc">오래된 등록순</option><option value="name_asc">상품 이름순</option><option value="name_desc">상품 이름 역순</option><option value="price_desc">높은 가격순</option><option value="price_asc">낮은 가격순</option><option value="quality_asc">완성도 낮은순</option><option value="code_asc">그룹코드순</option></select>
   <button class="btn" onclick="loadCatalog(1)">검색</button><button class="btn ghost" onclick="resetCatalog()">초기화</button>
   <button class="btn ghost" id="catalogCsv" onclick="location.href='/admin/api/catalog.csv'">CSV</button>
   <button class="btn red" id="pnew" onclick="location.href='/admin/products/new'">+ 상품 등록</button></div>
   <div class="toolbar"><button class="btn sm ghost" id="mergeBtn" onclick="mergeSelectedGroups()">선택 그룹 병합</button>
   <span class="hint">검색 예: <b>dept:kpop</b> · <b>source:k2g</b> · <b>sku:KPOP-000123</b> · <b>artist:엔하이픈</b>. 상품그룹을 누르면 SKU가 펼쳐집니다.</span></div>
   <div id="catalogList" class="loading">불러오는 중…</div>
  </div>
  <div id="pv-inventory" class="pview">
   <div class="toolbar grow"><input id="inventoryQ" type="text" placeholder="상품명 · SKU · 기존ID" onkeydown="if(event.key==='Enter')loadInventory(1)">
   <select id="inventoryDept" onchange="loadInventory(1)"><option value="">대분류 전체</option><option>KPOP</option><option>KFOOD</option><option>KBEAUTY</option><option>KFASHION</option><option>LIFESTYLE</option></select>
   <select id="inventoryFilter" onchange="loadInventory(1)"><option value="">재고 전체</option><option value="out">품절/판매불가 전체</option><option value="tracked_out">품절 (수량관리)</option><option value="under5">재고 5개 미만</option><option value="low">안전재고 이하</option><option value="incoming">입고예정 있음</option><option value="tracked">수량관리 상품</option><option value="external">외부연동 상품</option></select>
   <select id="inventorySort" onchange="loadInventory(1)"><option value="stock_asc">가용재고 적은순</option><option value="stock_desc">가용재고 많은순</option><option value="updated_desc">최근 조정순</option><option value="name_asc">상품 이름순</option></select>
   <button class="btn" onclick="loadInventory(1)">검색</button><button class="btn ghost" onclick="resetInventory()">초기화</button><button class="btn ghost" id="inventoryCsv" onclick="location.href='/admin/api/catalog.csv'">CSV</button></div>
   <div class="hint" style="margin-bottom:10px">가용재고 = 실재고 − 예약재고. K2G 외부연동 상품은 수량 0으로 오인하지 않고 연동 상태로 별도 표시됩니다.</div>
   <div id="inventoryList" class="loading">불러오는 중…</div>
  </div>
  <div id="pv-review" class="pview">
   <div class="toolbar grow"><input id="reviewQ" type="text" placeholder="검토할 상품명 · 그룹코드" onkeydown="if(event.key==='Enter')loadReview(1)">
   <select id="reviewDept" onchange="loadReview(1)"><option value="">대분류 전체</option><option>KPOP</option><option>KFOOD</option><option>KBEAUTY</option><option>KFASHION</option><option>LIFESTYLE</option></select>
   <button class="btn" onclick="loadReview(1)">검색</button></div>
   <div class="hint" style="margin-bottom:10px">자동 분류 신뢰도가 낮거나 이미지·가격·분류가 빠진 그룹만 모았습니다. 수정 후 검토완료로 바꾸면 이 목록에서 사라집니다.</div>
   <div id="reviewList" class="loading">불러오는 중…</div>
  </div>
 </section>
<section id="t-pages" style="display:none">
  <div class="panel"><h3>페이지 콘텐츠 관리 <span class="tag">저장 즉시 사이트 반영 · 재배포에도 유지</span></h3>
  <div class="hint" style="margin-bottom:10px">편집 내용은 데이터베이스에 저장되어 원본 파일과 별도로 보존됩니다. [원본 복원]으로 언제든 되돌릴 수 있고, 저장할 때마다 직전 버전이 이력(최근 10개)에 남습니다.</div>
  <div id="pglist" class="loading">불러오는 중…</div></div></section>
<section id="t-ticker" style="display:none">
  <style>@keyframes tkmq{from{transform:translateX(0)}to{transform:translateX(-50%)}}</style>
  <div class="panel"><h3>LED 드롭 티커 <span class="tag">저장 즉시 전 페이지 반영</span></h3>
  <div class="hint" style="margin-bottom:10px">한 줄에 한 항목 · <b>**별표 두 개**</b>로 감싸면 흰색 강조 · 항목을 전부 지우고 저장하면 사이트에서 티커가 숨겨집니다. 항목 수가 바뀌어도 흐르는 속도는 일정하게 자동 조절됩니다.</div>
  <div id="tkbox" class="loading">불러오는 중…</div></div></section>
<section id="t-seo" style="display:none">
  <div class="panel"><h3>검색엔진 소유확인·설명 <span class="tag">저장 즉시 전 페이지 반영</span></h3>
  <div class="hint" style="margin-bottom:10px">네이버 서치어드바이저·구글 서치콘솔에서 발급받은 <b>HTML 태그(메타태그) 인증 코드</b>를 붙여넣으세요. 태그 전체를 붙여넣어도 코드만 자동 추출됩니다. 저장 후 각 콘솔에서 [소유확인]을 누르면 됩니다.</div>
  <div id="seobox" class="loading">불러오는 중…</div></div>
  <div class="panel"><h3>검색엔진 등록 절차 <span class="tag">1회 수동 등록</span></h3>
  <div style="line-height:2.1;font-size:13px">
  ① <b>네이버</b> — <a href="https://searchadvisor.naver.com" target="_blank" rel="noopener">서치어드바이저</a>: 사이트 등록(https://mapdal.kr) → 위 네이버 코드 저장 후 소유확인 → [요청 &gt; 사이트맵 제출]에 <span class="mono">https://mapdal.kr/sitemap.xml</span> 제출 → [요청 &gt; 웹 페이지 수집]으로 홈·SHOP·KPOP 수집 요청<br>
  ② <b>구글</b> — <a href="https://search.google.com/search-console" target="_blank" rel="noopener">서치콘솔</a>: URL 접두어 방식으로 등록 → 위 구글 코드 저장 후 확인 → [Sitemaps]에 <span class="mono">sitemap.xml</span> 제출<br>
  ③ <b>다음/카카오</b> — <a href="https://register.search.daum.net" target="_blank" rel="noopener">Daum 검색등록</a>: 사이트 검색 신규등록(URL·설명·품목 입력, 소유확인 불필요) — 네이트에도 함께 노출<br>
  ④ <b>카카오톡 공유 미리보기 갱신</b> — <a href="https://developers.kakao.com/tool/debugger/sharing" target="_blank" rel="noopener">공유 디버거</a>에서 URL 입력 후 [초기화]하면 새 OG 이미지·설명으로 즉시 갱신<br>
  · 확인용: <a href="/robots.txt" target="_blank">robots.txt</a> · <a href="/sitemap.xml" target="_blank">sitemap.xml</a> (전 페이지 + 등록 상품 + K-POP 앨범 전체 수록, 10분 캐시)</div></div></section>
<section id="t-banner" style="display:none">
  <div class="panel"><h3>메인배너 — 홈 히어로 슬라이드 <span class="tag">저장 즉시 홈 반영 · 최대 5개</span></h3>
  <div class="hint" style="margin-bottom:10px">이미지 업로드 시 자동 리사이즈됩니다. 태그 키워드·태그 배경색·앨범명·행사 이름은 배너 이미지 좌하단 캡션으로 표시되고, 이미지가 없는 슬라이드는 기존 텍스트 히어로 디자인으로 노출됩니다.</div>
  <style>.bnf{display:flex;flex-direction:column;font-size:12px;font-weight:700;color:#666;gap:3px}.bnf input{font:inherit;font-weight:400;color:#141414;padding:7px 9px;border:1px solid #ddd;border-radius:5px;background:#fff}</style>
  <datalist id="bntags"><option value="VIDEOCALL"><option value="FANSIGN&amp;PHOTO EVENT"><option value="FANSIGN"><option value="PHOTO EVENT"><option value="LUCKY DRAW"><option value="POP-UP"><option value="NEW DROP"><option value="LIVE"></datalist>
  <div id="bnbox" class="loading">불러오는 중…</div></div></section>
<section id="t-home" style="display:none">
  <div class="panel"><h3>홈 화면 구성 — 히어로 아래 블록 <span class="tag">저장 즉시 홈 반영</span></h3>
  <div class="hint" style="margin-bottom:10px">▲▼로 순서를 바꾸고, [노출] 체크를 끄면 홈에서 숨겨집니다. 히어로 슬라이드 자체는 [메인배너] 탭에서 관리합니다. [기본값 복원]을 누르면 원본 순서·전체 노출로 돌아갑니다.</div>
  <div id="hbbox" class="loading">불러오는 중…</div></div></section>
<section id="t-cust" style="display:none">
  <div class="panel"><h3>통합 고객·계정 <span class="tag">회원 · 비회원 주문 · 포인트 · 동의</span></h3>
  <div class="toolbar grow"><input id="aq" placeholder="고객번호 · 이름 · 이메일 · 전화" onkeydown="if(event.key==='Enter')loadAccounts(1)">
  <select id="ast" onchange="loadAccounts(1)"><option value="">상태 전체</option><option>ACTIVE</option><option>GUEST</option><option>LOCKED</option><option>WITHDRAWN</option><option>MERGED</option></select>
  <select id="apv" onchange="loadAccounts(1)"><option value="">가입방법 전체</option><option value="email">이메일</option><option value="google">Google</option><option value="kakao">카카오</option><option value="apple">Apple</option></select>
  <select id="avf" onchange="loadAccounts(1)"><option value="">인증 전체</option><option value="phone">휴대폰 인증</option><option value="none">인증 없음</option></select>
  <select id="aseg" onchange="loadAccounts(1)"><option value="">구매 전체</option><option value="buyer">구매 고객</option><option value="no_order">주문 없는 가입자</option></select>
  <select id="aissue" onchange="loadAccounts(1)"><option value="">점검 전체</option><option value="duplicate">중복계정 의심</option></select>
  <select id="asup" onchange="loadAccounts(1)"><option value="">마이페이지 업무 전체</option><option value="pending">CS 미처리</option><option value="request">취소·반품·교환</option><option value="inquiry">1:1 미답변</option><option value="pqna">상품 Q&amp;A 미답변</option><option value="restock">재입고 알림 대기</option><option value="liked">좋아요 보유</option><option value="sessions">활성 로그인</option></select>
  <label style="font-size:12px;white-space:nowrap"><input type="checkbox" id="amk" onchange="loadAccounts(1)"> 마케팅 동의</label>
  <button class="btn" onclick="loadAccounts(1)">검색</button><button class="btn ghost" onclick="resetAccounts()">초기화</button></div>
  <div class="hint" style="margin-bottom:10px">개인정보는 기본 마스킹됩니다. 원문 조회는 매니저 이상이 사유를 남긴 경우에만 감사로그와 함께 허용됩니다.</div>
  <div id="aqcards" class="cards" style="grid-template-columns:repeat(5,1fr);margin-bottom:12px"></div>
  <div id="clist" class="loading">통합 고객을 불러오는 중…</div></div></section>
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
const TABS=[['dash','대시보드',0],['orders','주문',0],['products','상품·재고',0],['pages','페이지',2],['ticker','티커',2],['seo','SEO·검색',2],['banner','메인배너',2],['home','홈 화면',2],['cust','고객',0],['notify','알림',0],['cs','문의·요청',0],['admins','관리자',3],['system','시스템',0]];
const LOAD={dash:loadDash,orders:()=>loadOrders(1),products:()=>productMode('catalog'),pages:loadPages,ticker:loadTicker,seo:loadSeo,banner:loadBanner,home:loadHomeBlocks,cust:()=>loadAccounts(1),notify:loadNotify,cs:loadCS,admins:loadAdmins,system:loadSys};
TABS.filter(t=>can(t[2])).forEach(([k,label],i)=>{const b=document.createElement('button');b.textContent=label;if(i===0)b.className='on';
 b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 TABS.forEach(([t])=>{const s=$('#t-'+t);if(s)s.style.display=(t===k?'':'none')});LOAD[k]()};$('#nav').appendChild(b)});
if(!can(2)){['pnew','mergeBtn'].forEach(id=>{const e=document.getElementById(id);if(e)e.style.display='none'})}
if(!can(1)){['csvbtn','tpladd','ccsv','catalogCsv','inventoryCsv'].forEach(id=>{const e=document.getElementById(id);if(e)e.style.display='none'})}

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
function closeM(){$('#mbg').style.display='none';$('#mbox').classList.remove('wide')}
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
async function cancelOrder(oid,refund){if(!confirm(refund?'이니시스 결제취소(환불)를 실행합니다. 계속할까요?':'이 주문을 취소로 표시할까요?'))return;
 const reason=prompt('취소 사유','고객 요청')||'고객 요청';
 try{const r=await api('/admin/api/orders/'+encodeURIComponent(oid)+'/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});
 toast(r.refunded?'환불 완료 · 재고 복원':'취소 처리 완료');closeM();loadOrders(opage)}catch(e){alert(e.message)}}

let PMODE='catalog',catalogPage=1,inventoryPage=1,reviewPage=1;window._inventory={};window._openGroup=null;
const srcLabel=s=>({DIRECT:'직접등록',K2G:'K2G 연동',OWN:'기존상품'})[s]||s;
function productMode(mode){PMODE=mode;['catalog','inventory','review'].forEach(m=>{
 const t=$('#pt-'+m),v=$('#pv-'+m);if(t)t.className='product-tab'+(m===mode?' on':'');if(v)v.className='pview'+(m===mode?' on':'')});
 loadProductSummary();if(mode==='catalog')loadCatalog(catalogPage);else if(mode==='inventory')loadInventory(inventoryPage);else loadReview(reviewPage)}
async function loadProductSummary(){try{const d=await api('/admin/api/catalog/summary');
 $('#reviewCount').textContent=d.review?'('+d.review+')':'';
 $('#productSummary').innerHTML=`<button class="card drill" onclick="summaryDrill('groups')" aria-label="전체 상품그룹 목록 보기"><div class="k">상품그룹</div><div class="v">${d.groups.toLocaleString()}</div><div class="s">SKU ${d.skus.toLocaleString()}개</div></button>
 <button class="card drill ${d.review?'alert':''}" onclick="summaryDrill('review')" aria-label="검토 필요 상품 목록 보기"><div class="k">검토 필요</div><div class="v">${d.review.toLocaleString()}</div><div class="s">메타데이터 확인</div></button>
 <button class="card drill ${d.out?'alert':''}" onclick="summaryDrill('out')" aria-label="품절 상품 목록 보기"><div class="k">품절</div><div class="v">${d.out.toLocaleString()}</div><div class="s">수량관리 SKU</div></button>
 <button class="card drill" onclick="summaryDrill('low')" aria-label="재고 부족 상품 목록 보기"><div class="k">재고 부족</div><div class="v">${d.low.toLocaleString()}</div><div class="s">안전재고 이하</div></button>
 <button class="card drill" onclick="summaryDrill('incoming')" aria-label="입고 예정 상품 목록 보기"><div class="k">입고 예정</div><div class="v">${d.incoming.toLocaleString()}</div><div class="s">총 수량</div></button>
 <button class="card drill" onclick="summaryDrill('external')" aria-label="외부 연동 상품 목록 보기"><div class="k">외부 연동</div><div class="v">${d.external.toLocaleString()}</div><div class="s">수량 미관리</div></button>`}catch(e){$('#productSummary').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function summaryDrill(kind){
 if(kind==='groups'){$('#catalogQ').value='';$('#catalogDept').value='';$('#catalogSource').value='';$('#catalogStatus').value='';$('#catalogIssue').value='';productMode('catalog')}
 else if(kind==='review'){$('#reviewQ').value='';$('#reviewDept').value='';productMode('review')}
 else{$('#inventoryQ').value='';$('#inventoryDept').value='';$('#inventoryFilter').value=(kind==='out'?'tracked_out':kind);$('#inventorySort').value='stock_asc';productMode('inventory')}
 setTimeout(()=>{const v=$('#pv-'+PMODE);if(v)v.scrollIntoView({behavior:'smooth',block:'start'})},80)
}
function reviewChips(g){return (g.review_reasons||[]).map(x=>`<span class="meta-chip warn">${esc(x)}</span>`).join('')}
function variantRows(g){return (g.variants||[]).map(v=>{const inv=v.tracked
 ?`<span class="inv-status ${(v.available||0)<=0?'out':(v.available||0)<=v.reorder_point?'low':'ok'}">가용 ${v.available} / 실재고 ${v.on_hand}</span>`
 :`<span class="inv-status ${v.external_status==='OUT'?'out':'ok'}">외부연동 · ${v.external_status==='OUT'?'판매불가':v.external_status==='AVAILABLE'?'판매가능':'확인필요'}</span>`;
 return `<tr><td class="mono">${esc(v.sku)}</td><td class="variant-name">${esc(v.name)}<div class="group-sub">기존 ID · ${esc(v.legacy_id)}</div></td><td class="right mono">${won(v.list_price||v.price)}</td><td>${inv}</td><td style="white-space:nowrap">${can(2)?`<button class="btn sm" onclick="editDetail('${esc(v.legacy_id)}')">상품편집</button> `:''}<a class="btn sm ghost" style="text-decoration:none" target="_blank" href="/p/${encodeURIComponent(v.legacy_id)}">보기</a>${can(2)?` <button class="btn sm ghost" style="color:#c0392b" onclick="deleteCatalogProduct('${esc(v.legacy_id)}')">삭제</button>`:''}</td></tr>`}).join('')}
function groupCard(g,reviewOnly){const bad=g.review_state!=='READY'||g.confidence<80;const stock=g.external_count
 ?`외부연동 ${g.external_count}${g.available?' · 가용 '+g.available:''}`:`가용 ${g.available}`;
 return `<div class="group-card ${bad?'needs-review':''}" data-gid="${esc(g.id)}">
 <div class="group-head" onclick="if(!event.target.closest('button,a,input'))this.parentElement.classList.toggle('open')">
 ${reviewOnly||!can(2)?'<span></span>':`<input class="gsel" type="checkbox" value="${esc(g.id)}" onclick="event.stopPropagation()">`}
 <div><span class="dept-chip">${esc(g.department)}</span><div class="group-code">${esc(g.code)}</div></div>
 <div><div class="group-title">${esc(g.title)}</div><div class="group-sub">${esc(g.brand_artist||g.product_type)} · SKU ${g.variant_count}개 · ${esc((g.created_at||'').slice(0,10))}</div><div style="margin-top:5px">${reviewChips(g)}</div></div>
 <div class="gsource"><span class="meta-chip">${esc(srcLabel(g.source))}</span><span class="meta-chip ${g.sale_status==='ACTIVE'?'ok':'warn'}">${esc(g.sale_status)}</span></div>
 <div class="gstock">${esc(stock)}</div>
 <div style="display:flex;align-items:center;gap:10px"><div class="quality">${g.confidence}%<div class="quality-bar"><i style="width:${g.confidence}%"></i></div></div>${can(2)?`<button class="btn sm" onclick="openCatalogGroup('${esc(g.id)}')">그룹편집</button>`:''}</div></div>
 <div class="variant-wrap"><table><tr><th>SKU</th><th>상품/옵션</th><th class="right">정가</th><th>재고상태</th><th></th></tr>${variantRows(g)||'<tr><td colspan="5" class="loading">SKU 없음</td></tr>'}</table></div></div>`}
async function loadCatalog(page){catalogPage=page;const q=new URLSearchParams({page,sort:$('#catalogSort').value});
 if($('#catalogQ').value)q.set('query',$('#catalogQ').value);if($('#catalogDept').value)q.set('department',$('#catalogDept').value);if($('#catalogSource').value)q.set('source',$('#catalogSource').value);if($('#catalogStatus').value)q.set('status',$('#catalogStatus').value);if($('#catalogIssue').value)q.set('issue',$('#catalogIssue').value);
 $('#catalogList').innerHTML='<div class="loading">상품그룹을 불러오는 중…</div>';try{const d=await api('/admin/api/catalog/groups?'+q);
 $('#catalogList').innerHTML=`<div class="group-list">${d.rows.map(g=>groupCard(g,false)).join('')||'<div class="empty-state">조건에 맞는 상품이 없습니다.</div>'}</div>${pager(page,d,'loadCatalog')}`
 }catch(e){$('#catalogList').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function resetCatalog(){$('#catalogQ').value='';$('#catalogDept').value='';$('#catalogSource').value='';$('#catalogStatus').value='';$('#catalogIssue').value='';$('#catalogSort').value='created_desc';loadCatalog(1)}
async function loadReview(page){reviewPage=page;const q=new URLSearchParams({page,issue:'review',sort:'quality_asc'});if($('#reviewQ').value)q.set('query',$('#reviewQ').value);if($('#reviewDept').value)q.set('department',$('#reviewDept').value);
 $('#reviewList').innerHTML='<div class="loading">검토 대상을 불러오는 중…</div>';try{const d=await api('/admin/api/catalog/groups?'+q);
 $('#reviewList').innerHTML=`<div class="group-list">${d.rows.map(g=>groupCard(g,true)).join('')||'<div class="empty-state">검토할 상품이 없습니다.</div>'}</div>${pager(page,d,'loadReview')}`
 }catch(e){$('#reviewList').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function mergeSelectedGroups(){const ids=[...document.querySelectorAll('.gsel:checked')].map(x=>x.value);if(ids.length<2)return toast('병합할 그룹을 2개 이상 선택하세요');
 if(!confirm(`선택한 ${ids.length}개 그룹을 첫 번째 그룹으로 병합할까요?\nSKU와 기존 상품 ID는 유지됩니다.`))return;
 try{await api('/admin/api/catalog/groups/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_ids:ids,target_id:ids[0]})});toast('그룹을 병합했습니다');loadCatalog(1);loadProductSummary()}catch(e){toast(e.message)}}
async function openCatalogGroup(id){try{const d=await api('/admin/api/catalog/group?id='+encodeURIComponent(id));window._openGroup=d;const g=d.group,m=g.metadata||{};
 $('#mbox').innerHTML=`<h3>상품그룹 편집 <span class="tag">${esc(g.group_code)}</span></h3><div class="kv">
 <b>그룹명</b><span><input id="geTitle" value="${esc(g.title)}" style="width:100%"></span>
 <b>대분류</b><span><select id="geDept">${d.departments.map(x=>`<option value="${x.value}" ${g.department===x.value?'selected':''}>${esc(x.label)}</option>`).join('')}</select></span>
 <b>카테고리</b><span><select id="geCat"><option value="">미지정</option>${d.categories.map(x=>`<option value="${x.value}" ${g.category===x.value?'selected':''}>${esc(x.label)}</option>`).join('')}</select></span>
 <b>상품유형</b><span><input id="geType" value="${esc(g.product_type||'')}" style="width:100%"></span>
 <b>브랜드/아티스트</b><span><input id="geArtist" value="${esc(g.brand_artist||'')}" style="width:100%"></span>
 <b>컬렉션/앨범</b><span><input id="geCollection" value="${esc(g.collection_name||'')}" style="width:100%"></span>
 <b>대표 이미지</b><span><input id="geImage" value="${esc(g.image||'')}" style="width:100%"></span>
 <b>행사 유형</b><span><select id="geEvent"><option value="">없음/일반</option>${['FANCALL','팬싸인회','럭키드로우','쇼케이스'].map(x=>`<option ${m.event_type===x?'selected':''}>${x}</option>`).join('')}</select></span>
 <b>포장 유형</b><span><select id="gePack">${['단품','랜덤','세트'].map(x=>`<option ${m.pack_type===x?'selected':''}>${x}</option>`).join('')}</select></span>
 <b>판매 상태</b><span><select id="geSale">${['ACTIVE','PAUSED','HIDDEN','SOLD_OUT'].map(x=>`<option ${g.sale_status===x?'selected':''}>${x}</option>`).join('')}</select></span>
 <b>검토 상태</b><span><select id="geReview"><option value="REVIEW" ${g.review_state==='REVIEW'?'selected':''}>확인 필요</option><option value="READY" ${g.review_state==='READY'?'selected':''}>검토 완료</option></select></span></div>
 <div class="hint">자동 분류 원문과 기존 상품 ID는 변경되지 않습니다. SKU도 최초 발급 후 유지됩니다.</div>
 <table style="margin-top:12px"><tr><th></th><th>SKU</th><th>상품/옵션</th><th></th></tr>${d.variants.map(v=>`<tr><td><input class="splitVar" type="checkbox" value="${esc(v.id)}"></td><td class="mono">${esc(v.sku)}</td><td>${esc(v.name)}</td><td>${can(2)?`<button class="btn sm ghost" onclick="editDetail('${esc(v.legacy_id)}')">상품편집</button>`:''}</td></tr>`).join('')}</table>
 <div style="display:flex;gap:8px;justify-content:space-between;margin-top:14px"><span>${d.variants.length>1&&can(2)?'<button class="btn ghost" onclick="splitCatalogGroup()">선택 SKU 새 그룹으로 분리</button>':''}</span><span><button class="btn ghost" onclick="closeM()">닫기</button> ${can(2)?'<button class="btn" onclick="saveCatalogGroup()">저장</button>':''}</span></div>`;$('#mbg').style.display='flex'
 }catch(e){toast(e.message)}}
async function saveCatalogGroup(){const d=window._openGroup,g=d.group,m={...(g.metadata||{}),event_type:$('#geEvent').value,pack_type:$('#gePack').value};const body={id:g.id,title:$('#geTitle').value,department:$('#geDept').value,category:$('#geCat').value,product_type:$('#geType').value,brand_artist:$('#geArtist').value,collection_name:$('#geCollection').value,image:$('#geImage').value,sale_status:$('#geSale').value,review_state:$('#geReview').value,metadata:m};
 try{await api('/admin/api/catalog/group/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('상품그룹을 저장했습니다');closeM();PMODE==='review'?loadReview(reviewPage):loadCatalog(catalogPage);loadProductSummary()}catch(e){toast(e.message)}}
async function splitCatalogGroup(){const ids=[...document.querySelectorAll('.splitVar:checked')].map(x=>x.value);if(!ids.length)return toast('분리할 SKU를 선택하세요');const title=prompt('새 그룹 이름',($('#geTitle').value||'')+' (분리)');if(!title)return;
 try{await api('/admin/api/catalog/group/split',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:window._openGroup.group.id,variant_ids:ids,title})});toast('새 그룹으로 분리했습니다');closeM();loadCatalog(1);loadProductSummary()}catch(e){toast(e.message)}}
async function deleteCatalogProduct(id){if(!confirm('이 상품을 삭제할까요?\n\n기존 주문·문의 이력은 보존되지만 상품과 SKU는 목록에서 제거됩니다.'))return;
 try{await api('/admin/api/products/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});toast('삭제했습니다');loadCatalog(catalogPage);loadProductSummary()}catch(e){toast(e.message)}}
async function loadInventory(page){inventoryPage=page;const q=new URLSearchParams({page,sort:$('#inventorySort').value});if($('#inventoryQ').value)q.set('query',$('#inventoryQ').value);if($('#inventoryDept').value)q.set('department',$('#inventoryDept').value);if($('#inventoryFilter').value)q.set('filter',$('#inventoryFilter').value);
 $('#inventoryList').innerHTML='<div class="loading">재고를 불러오는 중…</div>';try{const d=await api('/admin/api/inventory?'+q);window._inventory={};d.rows.forEach(v=>window._inventory[v.id]=v);
 $('#inventoryList').innerHTML=`<table><tr><th>SKU / 기존ID</th><th>상품그룹 / 상품</th><th>관리방식</th><th class="right">실재고</th><th class="right">예약</th><th class="right">가용</th><th class="right">입고예정</th><th></th></tr>${d.rows.map(v=>{const state=!v.tracked?(v.external_status==='OUT'?'out':'ok'):(v.available<=0?'out':v.available<=v.reorder_point?'low':'ok');return `<tr>
 <td class="mono">${esc(v.sku)}<div class="group-sub">${esc(v.legacy_id)}</div></td><td><b>${esc(v.group_title)}</b><div class="group-sub">${esc(v.name)}</div></td>
 <td>${v.tracked?'<span class="meta-chip">수량관리</span>':'<span class="meta-chip">외부연동</span>'}<div class="inv-status ${state}" style="margin-top:4px">${v.tracked?(state==='out'?'품절':state==='low'?'부족':'정상'):(v.external_status==='OUT'?'판매불가':'판매가능')}</div></td>
 <td class="right mono">${v.tracked?v.on_hand:'-'}</td><td class="right mono">${v.tracked?v.reserved:'-'}</td><td class="right mono"><b>${v.tracked?v.available:'-'}</b></td><td class="right mono">${v.tracked?v.incoming:'-'}</td>
 <td style="white-space:nowrap">${can(1)?`<button class="btn sm" onclick="openInventory('${esc(v.id)}')">${v.tracked?'재고 조정':'상태 변경'}</button> `:''}<button class="btn sm ghost" onclick="inventoryHistory('${esc(v.id)}')">이력</button></td></tr>`}).join('')||'<tr><td colspan="8" class="loading">조건에 맞는 SKU가 없습니다.</td></tr>'}</table>${pager(page,d,'loadInventory')}`
 }catch(e){$('#inventoryList').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function resetInventory(){$('#inventoryQ').value='';$('#inventoryDept').value='';$('#inventoryFilter').value='';$('#inventorySort').value='stock_asc';loadInventory(1)}
function openInventory(id){const v=window._inventory[id];if(!v)return;if(!v.tracked){$('#mbox').innerHTML=`<h3>외부연동 재고 상태</h3><p><b>${esc(v.sku)}</b><br>${esc(v.name)}</p><div class="hint">외부 연동 상품은 수량을 임의로 입력하지 않고 판매 가능 여부만 관리합니다.</div><div class="toolbar" style="margin-top:16px"><button class="btn" onclick="saveExternalInventory('${esc(id)}','AVAILABLE')">판매가능</button><button class="btn red" onclick="saveExternalInventory('${esc(id)}','OUT')">판매불가</button><button class="btn ghost" onclick="closeM()">닫기</button></div>`;$('#mbg').style.display='flex';return}
 $('#mbox').innerHTML=`<h3>재고 조정 <span class="tag">${esc(v.sku)}</span></h3><div class="kv"><b>현재 수량</b><span>실재고 <b>${v.on_hand}</b> · 예약 ${v.reserved} · 가용 <b>${v.available}</b></span>
 <b>작업</b><span><select id="iaKind"><option value="RECEIVE">입고 (+)</option><option value="RETURN">반품입고 (+)</option><option value="DAMAGE">파손/폐기 (-)</option><option value="SAMPLE">샘플사용 (-)</option><option value="COUNT">실사수량으로 맞춤</option><option value="MANUAL">기타 증감</option></select></span>
 <b>수량</b><span><input id="iaQty" type="number" value="1" style="width:120px"></span><b>입고예정</b><span><input id="iaIncoming" type="number" min="0" value="${v.incoming}" style="width:120px"></span>
 <b>안전재고</b><span><input id="iaReorder" type="number" min="0" value="${v.reorder_point}" style="width:120px"></span><b>사유/메모</b><span><input id="iaReason" placeholder="예: 7월 15일 입고, 파손 1개" style="width:100%"></span></div>
 <div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn ghost" onclick="closeM()">취소</button><button class="btn" onclick="saveInventoryAdjustment('${esc(id)}')">반영</button></div>`;$('#mbg').style.display='flex'}
async function saveInventoryAdjustment(id){const kind=$('#iaKind').value;let q=Number($('#iaQty').value||0);if((kind==='DAMAGE'||kind==='SAMPLE')&&q>0)q=-q;const body={variant_id:id,kind,quantity:q,incoming:Number($('#iaIncoming').value||0),reorder_point:Number($('#iaReorder').value||0),reason:$('#iaReason').value};
 try{await api('/admin/api/inventory/adjust',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('재고와 이력을 반영했습니다');closeM();loadInventory(inventoryPage);loadProductSummary()}catch(e){toast(e.message)}}
async function saveExternalInventory(id,status){try{await api('/admin/api/inventory/adjust',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({variant_id:id,external_status:status,reason:'관리자 상태 변경'})});toast('외부재고 상태를 반영했습니다');closeM();loadInventory(inventoryPage)}catch(e){toast(e.message)}}
async function inventoryHistory(id){try{const d=await api('/admin/api/inventory/history?variant_id='+encodeURIComponent(id));$('#mbox').innerHTML=`<h3>재고 변경 이력</h3><table><tr><th>일시</th><th>작업</th><th class="right">증감</th><th>변경</th><th>담당/사유</th></tr>${d.rows.map(x=>`<tr><td class="mono">${esc((x.created_at||'').replace('T',' '))}</td><td>${esc(x.kind)}</td><td class="right mono">${x.quantity>0?'+':''}${x.quantity}</td><td class="mono">${x.before_qty} → ${x.after_qty}</td><td>${esc(x.by_admin)}<div class="group-sub">${esc(x.reason)}</div></td></tr>`).join('')||'<tr><td colspan="5" class="loading">아직 변경 이력이 없습니다.</td></tr>'}</table><div style="text-align:right;margin-top:12px"><button class="btn ghost" onclick="closeM()">닫기</button></div>`;$('#mbg').style.display='flex'}catch(e){toast(e.message)}}

let accountPage=1;
async function loadAccounts(page){accountPage=page;const q=new URLSearchParams({page});
 if($('#aq').value)q.set('query',$('#aq').value);if($('#ast').value)q.set('status',$('#ast').value);if($('#apv').value)q.set('provider',$('#apv').value);if($('#avf').value)q.set('verified',$('#avf').value);if($('#aseg').value)q.set('segment',$('#aseg').value);if($('#aissue').value)q.set('issue',$('#aissue').value);if($('#asup').value)q.set('support',$('#asup').value);if($('#amk').checked)q.set('marketing','1');
 $('#clist').innerHTML='<div class="loading">통합 고객을 불러오는 중…</div>';try{const d=await api('/admin/api/accounts?'+q);
 const Q=d.queues||{};$('#aqcards').innerHTML=[['request','취소·반품',Q.request],['inquiry','1:1 미답변',Q.inquiry],['pqna','Q&A 미답변',Q.pqna],['restock','재입고 대기',Q.restock],['','잠금 계정',Q.locked]].map(x=>`<button class="card" style="text-align:left;cursor:pointer;border:${x[2]?'2px solid #E8332A':'1px solid var(--line)'}" onclick="supportFilter('${x[0]}')"><div class="k">${x[1]}</div><div class="v" style="font-size:20px">${x[2]||0}</div></button>`).join('');
 $('#clist').innerHTML=`<table><tr><th>고객번호 / 고객</th><th>연락처</th><th>계정</th><th>상태</th><th class="right">주문</th><th class="right">구매액</th><th class="right">포인트</th><th>마이페이지</th><th>최근활동</th><th></th></tr>
 ${d.rows.map(c=>`<tr><td><b class="mono">${esc(c.customer_no)}</b><div class="group-sub">${esc(c.name)||'이름 없음'} · ${esc(c.grade)}</div></td>
 <td><span class="mono">${esc(c.phone)||'-'}</span><div class="group-sub">${esc(c.email)||'-'}</div></td><td>${c.providers.map(p=>`<span class="meta-chip">${esc(p)}</span>`).join(' ')||'-'}</td>
 <td><span class="st ${c.status==='ACTIVE'?'PAID':c.status==='LOCKED'?'FAILED':'PENDING'}">${esc(c.status)}</span>${c.marketing?' <span class="meta-chip">마케팅</span>':''}</td>
 <td class="right mono">${c.orders}</td><td class="right mono">${won(c.spend)}</td><td class="right mono"><b>${c.points.toLocaleString()}P</b></td>
 <td style="font-size:11px;line-height:1.7">${c.support.req?'<b style="color:#E8332A">요청 '+c.support.req+'</b> ':''}${c.support.inq?'<b style="color:#E8332A">1:1 '+c.support.inq+'</b> ':''}${c.support.pqna?'<b style="color:#E8332A">Q&A '+c.support.pqna+'</b> ':''}${c.support.restock?'재입고 '+c.support.restock+' ':''}${c.support.likes?'♥ '+c.support.likes+' ':''}${c.support.sessions?'세션 '+c.support.sessions:''}</td>
 <td class="mono">${esc(c.last_order||c.last_login||'-')}</td><td><button class="btn sm ghost" onclick="openAccount('${esc(c.id)}')">통합관리</button></td></tr>`).join('')||'<tr><td colspan="10" class="loading">조건에 맞는 고객이 없습니다.</td></tr>'}</table>${pager(page,d,'loadAccounts')}`
 }catch(e){$('#clist').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function supportFilter(v){$('#asup').value=v;loadAccounts(1)}
function resetAccounts(){$('#aq').value='';$('#ast').value='';$('#apv').value='';$('#avf').value='';$('#aseg').value='';$('#aissue').value='';$('#asup').value='';$('#amk').checked=false;loadAccounts(1)}
async function openAccount(id){try{const d=await api('/admin/api/accounts/'+encodeURIComponent(id));window._account=d;const c=d.customer;$('#mbox').classList.add('wide');
 const tb=(title,head,body)=>`<div style="margin-top:16px"><h4 style="margin:0 0 7px">${title}</h4><table><tr>${head}</tr>${body||'<tr><td colspan="6" class="loading">없음</td></tr>'}</table></div>`;
 $('#mbox').innerHTML=`<h3>${esc(c.name)||'이름 없음'} <span class="tag mono">${esc(c.customer_no)}</span> <span class="st ${c.status==='ACTIVE'?'PAID':'PENDING'}">${esc(c.status)}</span></h3>
 <div class="hint">고객 마이페이지의 주문·요청·문의·좋아요·재입고·배송지·세션을 한 곳에서 조회·처리합니다.</div>
 <div class="cards" style="grid-template-columns:repeat(6,1fr);margin-top:12px">
 ${[['주문',d.orders.length],['미처리 CS',d.requests.filter(x=>x.status==='접수'||x.status==='처리중').length+d.inquiries.filter(x=>x.status!=='답변완료').length+d.pqna.filter(x=>x.status!=='답변완료').length],['좋아요',d.likes.length],['재입고 대기',d.restock.filter(x=>!x.notified).length],['배송지',d.addresses.length],['활성 세션',d.sessions.length]].map(x=>`<div class="card"><div class="k">${x[0]}</div><div class="v" style="font-size:20px">${x[1]}</div></div>`).join('')}</div>
 <div style="margin-top:14px;background:#faf9f5;padding:12px"><b style="font-size:12px">CS 내부메모</b><textarea id="acmemo" rows="2" style="width:100%;margin-top:6px" placeholder="인수인계·응대내용·주의사항">${esc(c.admin_memo||'')}</textarea>${can(1)?`<button class="btn sm" style="margin-top:6px" onclick="saveAccountMemo('${esc(id)}')">메모 저장</button>`:''}</div>
 ${tb('취소·반품·교환 요청','<th>일시</th><th>유형/주문</th><th>사유·메모</th><th>상태</th><th></th>',d.requests.map(x=>`<tr><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td><b>${esc(x.type)}</b><br><a href="#" onclick="openOrder('${esc(x.order_id)}');return false" class="mono">${esc(x.order_id)}</a></td><td>${esc(x.reason)}${x.memo?'<div class="group-sub">'+esc(x.memo)+'</div>':''}</td><td>${esc(x.status)}</td><td>${can(1)?`<button class="btn sm" onclick="accountReq('${esc(x.id)}','${esc(id)}','${esc(x.status)}')">처리</button>`:''}</td></tr>`).join(''))}
 ${tb('1:1 문의','<th>일시</th><th>제목/내용</th><th>상태</th><th>답변</th><th></th>',d.inquiries.map(x=>`<tr><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td><b>${esc(x.title)}</b><div>${esc(x.body)}</div></td><td>${esc(x.status)}</td><td>${esc(x.answer||'-')}</td><td>${can(1)?`<button class="btn sm" onclick="accountAnswer('inq','${esc(x.id)}','${esc(id)}')">${x.answer?'수정':'답변'}</button>`:''}</td></tr>`).join(''))}
 ${tb('상품 Q&A','<th>일시</th><th>상품/문의</th><th>상태</th><th>답변</th><th></th>',d.pqna.map(x=>`<tr><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td><b class="mono">${esc(x.product_id)}</b><div>${esc(x.question)}</div></td><td>${esc(x.status)}</td><td>${esc(x.answer||'-')}</td><td>${can(1)?`<button class="btn sm" onclick="accountAnswer('pqna','${esc(x.id)}','${esc(id)}')">${x.answer?'수정':'답변'}</button>`:''}</td></tr>`).join(''))}
 ${tb('연락처','<th>유형</th><th>값</th><th>인증</th>',d.contacts.map(x=>`<tr><td>${esc(x.kind)}</td><td class="mono">${esc(x.value)}</td><td>${x.verified?'인증':'미인증'}</td></tr>`).join(''))}
 ${tb('연결 계정','<th>방법</th><th>이메일</th><th>최근 로그인</th>',d.identities.map(x=>`<tr><td>${esc(x.provider)}</td><td class="mono">${esc(x.email)}</td><td class="mono">${esc((x.last_login||'-').replace('T',' '))}</td></tr>`).join(''))}
 ${tb('주문·거래증빙','<th>주문번호</th><th>일시</th><th>상태</th><th class="right">금액</th><th></th>',d.orders.map(x=>`<tr><td class="mono"><a href="#" onclick="openOrder('${esc(x.order_id)}');return false">${esc(x.order_id)}</a></td><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td>${esc(x.status)}</td><td class="right mono">${won(x.amount)}</td><td><a class="btn sm ghost" target="_blank" href="/admin/api/orders/${encodeURIComponent(x.order_id)}/receipt">명세서</a></td></tr>`).join(''))}
 ${tb('배송지 (원문은 사유를 남기고 조회)','<th>구분</th><th>받는분</th><th>연락처</th><th>등록일</th><th></th>',d.addresses.map(x=>`<tr><td>${esc(x.label)} ${x.default?'<span class="meta-chip">기본</span>':''}</td><td>${esc(x.rname)}</td><td class="mono">${esc(x.phone)}</td><td class="mono">${esc((x.created||'').slice(0,10))}</td><td>${can(2)?`<button class="btn sm ghost" onclick="accountAction('${esc(id)}','delete_address','${esc(x.id)}','배송지를 삭제할까요?')">삭제</button>`:''}</td></tr>`).join(''))}
 ${tb('좋아요','<th>일시</th><th>상품</th><th class="right">가격</th><th></th>',d.likes.map(x=>`<tr><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td>${esc(x.name)}<div class="group-sub mono">${esc(x.product_id)}</div></td><td class="right mono">${x.price?won(x.price):'-'}</td><td>${can(1)?`<button class="btn sm ghost" onclick="accountAction('${esc(id)}','remove_like','${esc(x.id)}','고객 요청으로 좋아요를 삭제할까요?')">삭제</button>`:''}</td></tr>`).join(''))}
 ${tb('재입고 알림','<th>일시</th><th>상품</th><th>상태</th><th></th>',d.restock.map(x=>`<tr><td class="mono">${esc((x.created||'').replace('T',' '))}</td><td>${esc(x.name)}<div class="group-sub mono">${esc(x.product_id)}</div></td><td>${x.notified?'발송완료':'<b style="color:#E8332A">발송대기</b>'}</td><td>${can(1)?`${x.notified?`<button class="btn sm ghost" onclick="accountAction('${esc(id)}','reset_restock','${esc(x.id)}','다시 발송대기로 바꿀까요?')">대기로</button>`:''} <button class="btn sm ghost" onclick="accountAction('${esc(id)}','cancel_restock','${esc(x.id)}','알림 신청을 해지할까요?')">해지</button>`:''}</td></tr>`).join(''))}
 ${tb('로그인 기기·세션','<th>최근활동</th><th>IP</th><th>기기</th><th>만료</th><th></th>',d.sessions.map(x=>`<tr><td class="mono">${esc((x.last_seen||x.created||'').replace('T',' '))}</td><td class="mono">${esc(x.ip)}</td><td style="max-width:260px">${esc(x.device)}</td><td class="mono">${esc((x.expires||'').replace('T',' '))}</td><td>${can(1)?`<button class="btn sm ghost" onclick="accountAction('${esc(id)}','revoke_session','${esc(x.id)}','이 기기를 로그아웃할까요?')">종료</button>`:''}</td></tr>`).join(''))}
 <div style="margin-top:12px"><b>관심매장</b> <span class="meta-chip">${c.fav_store?'성수 등록':'미등록'}</span>${can(1)?` <button class="btn sm ghost" onclick="accountFav('${esc(id)}',${c.fav_store?0:1})">${c.fav_store?'해제':'등록'}</button>`:''}${can(1)&&d.sessions.length?` <button class="btn sm red" onclick="accountAction('${esc(id)}','revoke_session','','이 고객의 모든 기기를 로그아웃할까요?',true)">모든 기기 종료</button>`:''}</div>
 ${tb('포인트 원장','<th>일시</th><th>유형</th><th class="right">증감</th><th class="right">잔액</th><th>사유</th>',d.points.map(x=>`<tr><td class="mono">${esc((x.created_at||'').replace('T',' '))}</td><td>${esc(x.event_type)}</td><td class="right mono">${x.amount>0?'+':''}${x.amount}</td><td class="right mono">${x.balance_after}</td><td>${esc(x.reason)}</td></tr>`).join(''))}
 ${tb('동의 이력','<th>일시</th><th>항목</th><th>상태</th><th>버전/경로</th>',d.consents.map(x=>`<tr><td class="mono">${esc((x.created_at||'').replace('T',' '))}</td><td>${esc(x.consent_type)}</td><td>${x.granted?'동의':'철회'}</td><td>${esc(x.policy_version)} · ${esc(x.source)}</td></tr>`).join(''))}
 ${d.security.length?tb('보안 이벤트','<th>일시</th><th>이벤트</th><th>IP</th><th>상세</th>',d.security.map(x=>`<tr><td class="mono">${esc((x.created_at||'').replace('T',' '))}</td><td>${esc(x.event_type)}</td><td class="mono">${esc(x.ip)}</td><td>${esc(x.detail)}</td></tr>`).join('')):''}
 <div style="display:flex;justify-content:flex-end;gap:7px;margin-top:16px;flex-wrap:wrap">${can(3)&&c.status!=='MERGED'&&c.status!=='WITHDRAWN'?`<button class="btn ghost" onclick="mergeAccount('${esc(id)}','${esc(c.customer_no)}')">다른 계정으로 병합</button>`:''}${can(2)?`<button class="btn ghost" onclick="revealAccount('${esc(id)}')">개인정보 원문 조회</button>${c.status==='ACTIVE'?`<button class="btn ghost" onclick="adjustAccountPoints('${esc(id)}','${esc(c.customer_no)}')">포인트 조정</button>`:''}<button class="btn ${c.status==='ACTIVE'?'red':'ghost'}" onclick="setAccountStatus('${esc(id)}','${c.status==='ACTIVE'?'LOCKED':'ACTIVE'}')">${c.status==='ACTIVE'?'계정 잠금':'잠금 해제'}</button>`:''}<button class="btn ghost" onclick="closeM()">닫기</button></div>`;
 $('#mbg').style.display='flex'}catch(e){toast(e.message)}}
async function saveAccountMemo(cid){try{await api('/admin/api/accounts/'+encodeURIComponent(cid)+'/mypage-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'save_memo',memo:$('#acmemo').value})});toast('메모를 저장했습니다');loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function accountAction(cid,action,id,msg,all){if(msg&&!confirm(msg))return;try{await api('/admin/api/accounts/'+encodeURIComponent(cid)+'/mypage-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,id,all:!!all})});toast('처리했습니다');openAccount(cid);loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function accountFav(cid,v){try{await api('/admin/api/accounts/'+encodeURIComponent(cid)+'/mypage-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'set_fav_store',value:v})});toast('관심매장을 반영했습니다');openAccount(cid)}catch(e){toast(e.message)}}
async function accountAnswer(kind,qid,cid){const ans=prompt('고객에게 표시할 답변을 입력하세요');if(!ans)return;try{await api('/admin/api/cs/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,id:qid,answer:ans})});toast('답변을 등록했습니다');openAccount(cid);loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function accountReq(qid,cid,cur){const st=prompt('상태: 접수 / 처리중 / 완료 / 거절',cur==='접수'?'처리중':'완료');if(!st)return;const memo=prompt('고객에게 표시할 메모 (선택)','')||'';try{await api('/admin/api/cs/req-update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:qid,status:st,memo})});toast('요청을 처리했습니다');openAccount(cid);loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function revealAccount(id){const reason=prompt('개인정보 원문 조회 사유를 입력하세요');if(!reason)return;try{const d=await api('/admin/api/accounts/'+encodeURIComponent(id)+'/reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});alert(d.contacts.map(x=>x.kind+': '+x.value).join('\n')+'\n\n배송지\n'+d.addresses.map(x=>'['+x.zip+'] '+x.addr1+' '+x.addr2).join('\n'))}catch(e){toast(e.message)}}
async function mergeAccount(source,sourceNo){const target=prompt('병합 후 남길 활성 고객번호를 입력하세요\n예: CUS-2026-XXXXXXXX');if(!target)return;const confirmNo=prompt('되돌릴 수 없습니다. 병합되어 사라질 고객번호를 다시 입력하세요',sourceNo);if(confirmNo!==sourceNo)return toast('고객번호가 일치하지 않습니다');try{await api('/admin/api/accounts/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:source,target_id:target,confirm:confirmNo})});toast('계정과 주문·동의·포인트 이력을 병합했습니다');closeM();loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function setAccountStatus(id,status){if(!confirm(status==='LOCKED'?'계정을 잠그고 모든 세션을 종료할까요?':'계정 잠금을 해제할까요?'))return;try{await api('/admin/api/accounts/'+encodeURIComponent(id)+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});toast('계정 상태를 변경했습니다');closeM();loadAccounts(accountPage)}catch(e){toast(e.message)}}
async function adjustAccountPoints(id,label){const v=prompt(label+' 지급(+) / 차감(-) 포인트','1000');if(!v)return;const reason=prompt('조정 사유 (필수)','CS 보상');if(!reason)return;try{await api('/admin/api/members/points',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({customer_id:id,delta:Number(v),reason})});toast('포인트 원장에 반영했습니다');openAccount(id);loadAccounts(accountPage)}catch(e){toast(e.message)}}

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

// ── 메인배너 ─────────────────────────────────────────────
const BN_COLORS=[['purple','#7C3AED','보라'],['gold','linear-gradient(135deg,#B98A2F,#E7C873)','골드'],['red','#E8332A','레드'],['amber','#FFB000','앰버'],['ink','#141414','잉크'],['blue','#2563EB','블루'],['green','#0E9F6E','그린']];
let BN=null;
function bnCss(c){c=(c||'').trim();const p=BN_COLORS.find(x=>x[0]===c);return p?p[1]:(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(c)?c:'#7C3AED');}
async function loadBanner(){try{BN=await api('/admin/api/banner');renderBanner();}catch(e){$('#bnbox').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function renderBanner(){
 const box=$('#bnbox');box.innerHTML='';
 BN.slides.forEach((s,i)=>{
  const card=document.createElement('div');card.className='panel';card.style.marginBottom='10px';
  card.innerHTML=`<h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">슬라이드 ${i+1}
    <span class="tag">${s.img?'이미지 배너':'텍스트 히어로'}</span>
    <span style="margin-left:auto;display:flex;gap:4px;align-items:center">
     <button class="btn sm ghost" onclick="bnMove(${i},-1)" ${i===0?'disabled':''}>↑</button>
     <button class="btn sm ghost" onclick="bnMove(${i},1)" ${i===BN.slides.length-1?'disabled':''}>↓</button>
     <label style="font-weight:400;font-size:13px;display:flex;align-items:center;gap:4px"><input type="checkbox" ${s.active!==false?'checked':''} onchange="BN.slides[${i}].active=this.checked"> 노출</label>
    </span></h3>
   <div style="display:flex;gap:14px;flex-wrap:wrap">
    <div style="width:230px">
     <div id="bnpv${i}" style="width:230px;height:96px;background:linear-gradient(135deg,#E8332A,#B71F18);${s.img?`background-image:url('${esc(s.img)}');`:''}background-size:cover;background-position:center;border:1px solid #e3e1db;border-radius:6px;position:relative;overflow:hidden">
      <span id="bnchip${i}" style="position:absolute;left:8px;bottom:8px;display:${s.tag_label?'inline-block':'none'};font:700 10px 'IBM Plex Mono',monospace;letter-spacing:.04em;color:#fff;padding:3px 7px;border-radius:4px;background:${bnCss(s.tag_color)}">${esc(s.tag_label||'')}</span>
     </div>
     <button class="btn sm" style="margin-top:6px;width:100%" onclick="$('#bnfile${i}').click()">이미지 업로드 (자동 리사이즈)</button>
     <input type="file" id="bnfile${i}" accept="image/*" style="display:none" onchange="bnUpload(${i},this)">
    </div>
    <div style="flex:1;min-width:300px;display:grid;grid-template-columns:1fr 1fr;gap:8px">
     <label class="bnf">태그 키워드<input list="bntags" value="${esc(s.tag_label||'')}" oninput="bnSet(${i},'tag_label',this.value)" placeholder="예: VIDEOCALL"></label>
     <label class="bnf">태그 배경색
      <span id="bnsw${i}" style="display:flex;gap:4px;align-items:center;margin-top:2px;flex-wrap:wrap">
       ${BN_COLORS.map(c=>`<button type="button" data-c="${c[0]}" title="${c[2]}" onclick="bnSet(${i},'tag_color','${c[0]}')" style="width:22px;height:22px;border-radius:5px;border:2px solid ${s.tag_color===c[0]?'#141414':'#e3e1db'};background:${c[1]};cursor:pointer;padding:0"></button>`).join('')}
       <input type="color" value="${/^#[0-9a-fA-F]{6}$/.test(s.tag_color||'')?s.tag_color:'#7C3AED'}" oninput="bnSet(${i},'tag_color',this.value)" style="width:30px;height:26px;border:1px solid #e3e1db;border-radius:5px;padding:0;background:none;cursor:pointer" title="직접 선택">
      </span></label>
     <label class="bnf">앨범명<input value="${esc(s.album||'')}" oninput="bnSet(${i},'album',this.value)" placeholder="예: JANG HANEUM The 2nd EP [DAYDREAM]"></label>
     <label class="bnf">행사 이름<input value="${esc(s.event||'')}" oninput="bnSet(${i},'event',this.value)" placeholder="예: SPECIAL VIDEO CALL EVENT"></label>
     <label class="bnf" style="grid-column:1/-1">클릭 링크 URL<input value="${esc(s.href||'')}" oninput="bnSet(${i},'href',this.value)" placeholder="/kpop 또는 https://…"></label>
    </div></div>`;
  box.appendChild(card);
 });
 const bar=document.createElement('div');bar.style.cssText='display:flex;gap:10px;align-items:center;flex-wrap:wrap';
 bar.innerHTML=`${BN.slides.length<5?'<button class="btn ghost" onclick="bnAdd()">+ 슬라이드 추가</button>':''}
  <span class="hint">전환 간격 <input id="bniv" type="number" min="1500" max="15000" step="500" value="${BN.interval_ms||3000}" style="width:84px;padding:5px;border:1px solid #ddd;border-radius:5px"> ms</span>
  ${can(2)?'<button class="btn red" style="margin-left:auto" onclick="saveBanner()">저장 (홈 즉시 반영)</button>':''}`;
 box.appendChild(bar);
}
function bnSet(i,k,v){BN.slides[i][k]=v;
 if(k==='tag_label'){const c=$('#bnchip'+i);c.textContent=v;c.style.display=v?'inline-block':'none';}
 if(k==='tag_color'){$('#bnchip'+i).style.background=bnCss(v);
  document.querySelectorAll('#bnsw'+i+' button').forEach(b=>b.style.borderColor=(b.dataset.c===v)?'#141414':'#e3e1db');}}
async function bnUpload(i,inp){const f=inp.files[0];if(!f)return;inp.value='';
 const pv=$('#bnpv'+i);pv.style.opacity=.5;
 try{const u=await uploadFile(f);BN.slides[i].img=u;renderBanner();toast('업로드 완료 — 저장을 눌러 홈에 반영하세요');}
 catch(e){if(e.message!=='세션 만료')toast(e.message);}
 const pv2=$('#bnpv'+i);if(pv2)pv2.style.opacity=1;}
function bnMove(i,d){const s=BN.slides,j=i+d;if(j<0||j>=s.length)return;const t=s[i];s[i]=s[j];s[j]=t;renderBanner();}
function bnAdd(){if(BN.slides.length>=5)return;BN.slides.push({img:'',href:'',tag_label:'',tag_color:'',album:'',event:'',active:true});renderBanner();}
async function saveBanner(){try{BN.interval_ms=parseInt($('#bniv').value)||3000;
 const d=await api('/admin/api/banner',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(BN)});
 toast('저장 완료 — 홈페이지에 '+d.slides+'개 슬라이드 반영');}catch(e){toast(e.message)}}

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

let HB=null;
async function loadHomeBlocks(){try{const d=await api('/admin/api/homeblocks');HB=d.blocks;
 $('#hbbox').innerHTML=`<div id="hblist"></div>
 <div class="toolbar" style="margin-top:12px;align-items:center">
  ${can(2)?'<button class="btn" onclick="saveHB()">저장 (홈에 즉시 반영)</button><button class="btn ghost" onclick="resetHB()">기본값 복원</button>':''}
  <a class="btn ghost" href="/" target="_blank" style="text-decoration:none;margin-left:auto">홈에서 확인</a></div>
 ${d.is_default?'<div class="hint" style="margin-top:8px">아직 저장 이력 없음 — 원본 순서 그대로 노출 중입니다.</div>'
  :`<div class="hint" style="margin-top:8px">마지막 저장 ${esc((d.updated||'').slice(0,16).replace('T',' '))} UTC · ${esc(d.by_admin||'')}</div>`}`;
 renderHB();
}catch(e){$('#hbbox').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
function renderHB(){$('#hblist').innerHTML=HB.map((b,i)=>`
 <div style="display:flex;align-items:center;gap:10px;padding:11px 14px;border:1px solid #e4e1da;border-radius:6px;margin-bottom:8px;background:${b.on?'#fff':'#f4f2ec'}">
  <span class="mono" style="font-size:11px;color:#999;width:18px">${i+1}</span>
  <b style="flex:1;font-size:13.5px;${b.on?'':'color:#999;text-decoration:line-through'}">${esc(b.label)}</b>
  <button class="btn ghost" onclick="hbMove(${i},-1)" ${i===0?'disabled':''} title="위로">▲</button>
  <button class="btn ghost" onclick="hbMove(${i},1)" ${i===HB.length-1?'disabled':''} title="아래로">▼</button>
  <label style="display:flex;gap:6px;align-items:center;font-size:12.5px;cursor:pointer"><input type="checkbox" ${b.on?'checked':''} onchange="HB[${i}].on=this.checked;renderHB()"> 노출</label>
 </div>`).join('')}
function hbMove(i,d){const j=i+d;if(j<0||j>=HB.length)return;const t=HB[i];HB[i]=HB[j];HB[j]=t;renderHB()}
async function saveHB(){try{await api('/admin/api/homeblocks/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({blocks:HB.map(b=>({id:b.id,on:b.on}))})});
 toast('저장 완료 — 홈 새로고침 시 반영됩니다');loadHomeBlocks()}catch(e){toast(e.message)}}
async function resetHB(){if(!confirm('원본 순서·전체 노출로 되돌립니다. 계속할까요?'))return;
 try{await api('/admin/api/homeblocks/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 toast('기본값 복원 완료');loadHomeBlocks()}catch(e){toast(e.message)}}

async function loadSeo(){try{const d=await api('/admin/api/seo');
 $('#seobox').innerHTML=`<div style="display:grid;gap:12px;max-width:720px">
 <label style="font-size:12px;font-weight:700;color:#666">네이버 소유확인 코드 <span style="font-weight:400">(naver-site-verification)</span>
  <input id="seonaver" style="width:100%;margin-top:4px;font:13px 'IBM Plex Mono',monospace;padding:8px 10px;border:1px solid #ddd;border-radius:5px" placeholder="예: 1a2b3c… 또는 <meta …> 태그 전체" value="${esc(d.naver||'')}"></label>
 <label style="font-size:12px;font-weight:700;color:#666">구글 소유확인 코드 <span style="font-weight:400">(google-site-verification)</span>
  <input id="seogoogle" style="width:100%;margin-top:4px;font:13px 'IBM Plex Mono',monospace;padding:8px 10px;border:1px solid #ddd;border-radius:5px" placeholder="예: AbCdEf… 또는 <meta …> 태그 전체" value="${esc(d.google||'')}"></label>
 <label style="font-size:12px;font-weight:700;color:#666">홈 검색 설명문 <span style="font-weight:400">(비우면 기본 설명 · 검색 결과에 표시, 80~160자 권장)</span>
  <textarea id="seodesc" rows="3" style="width:100%;margin-top:4px;font:13px 'IBM Plex Sans KR',sans-serif;padding:8px 10px;border:1px solid #ddd;border-radius:5px">${esc(d.desc||'')}</textarea></label>
 <div><button class="btn red" onclick="saveSeo()">저장 — 전 페이지 반영</button>
 ${d.updated?`<span class="hint" style="display:inline;margin-left:10px">마지막 저장 ${esc((d.updated||'').slice(0,16).replace('T',' '))} UTC · ${esc(d.by_admin||'')}</span>`:''}</div></div>`;
}catch(e){$('#seobox').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function saveSeo(){try{
 await api('/admin/api/seo/save',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({naver:$('#seonaver').value,google:$('#seogoogle').value,desc:$('#seodesc').value})});
 toast('저장 완료 — 소유확인 메타가 전 페이지에 반영되었습니다');loadSeo()}catch(e){toast(e.message)}}

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
 <div class="card ${s.pg_mode.includes('테스트')?'alert':''}"><div class="k">이니시스 결제</div><div class="v" style="font-size:15px">${esc(s.pg_mode)}</div></div>
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

# ═══════════════════ 상품 마스터(그룹 → SKU → 재고) ═══════════════════
# 기존 products 테이블은 결제·정적 페이지 호환용 판매 투영(projection)으로 유지한다.
# 새 관리자 화면은 아래 정규화 테이블을 사용하고, 모든 쓰기는 양쪽을 동기화한다.
CATALOG_DEPARTMENTS = [
    ('KPOP', 'K-POP'), ('KFOOD', 'K-FOOD'), ('KBEAUTY', 'K-BEAUTY'),
    ('KFASHION', 'K-FASHION'), ('LIFESTYLE', 'LIFESTYLE'),
]
_DEPT_LABEL = dict(CATALOG_DEPARTMENTS)
_DEPT_KEYS = set(_DEPT_LABEL)
_DEPT_TO_CAT = {'KPOP': 'album', 'KFOOD': 'kfood', 'KBEAUTY': 'md',
                'KFASHION': 'apparel', 'LIFESTYLE': 'living'}

def _stable_id(prefix, value, size=16):
    return prefix + hashlib.sha1(str(value).encode('utf-8')).hexdigest()[:size]

def _catalog_source(pid):
    pid = str(pid or '')
    return 'K2G' if pid.startswith('k2g::') else ('DIRECT' if pid.startswith('mp::') else 'OWN')

def _catalog_department(category, pid='', name=''):
    cat, low = norm_cat(category), (str(pid) + ' ' + str(name)).lower()
    if cat == 'album' or str(pid).startswith('k2g::'):
        return 'KPOP'
    if cat == 'kfood' or any(x in low for x in ('kimbap', 'tteokbokki', 'bowl-', '김밥', '떡볶이', '식품')):
        return 'KFOOD'
    if cat == 'apparel' or any(x in low for x in ('hoodie', 'ballcap', 'fashion', '후디', '볼캡', '의류')):
        return 'KFASHION'
    if any(x in low for x in ('beauty', 'cosmetic', 'skincare', '뷰티', '화장품')):
        return 'KBEAUTY'
    if cat == 'md' and any(x in low for x in ('album', 'lightstick', 'keyring', 'lp', '응원봉', '앨범')):
        return 'KPOP'
    return 'LIFESTYLE'

def _catalog_norm_key(value):
    return re.sub(r'[^0-9a-z가-힣]+', '', str(value or '').lower())

def _catalog_parse_product(r):
    """레거시 1개 상품을 사람이 읽는 그룹/옵션/메타데이터로 해석한다.
    원문은 metadata.original_name에 항상 남겨 자동 분류가 정보 손실을 만들지 않는다."""
    pid, raw = str(r.get('id') or ''), str(r.get('name') or r.get('id') or '').strip()
    source = _catalog_source(pid)
    category = norm_cat(r.get('category'))
    dept = _catalog_department(category, pid, raw)
    title, option, artist, collection = raw, '', '', ''
    tags = re.findall(r'【([^】]+)】', raw)
    clean = re.sub(r'^\s*(?:【[^】]+】\s*)+', '', raw).strip()
    if source == 'OWN':
        title, sep, option = raw.partition(' — ')
        title, option = title.strip(), option.strip() if sep else ''
        group_key = 'OWN|' + pid.split('::', 1)[0]
    elif source == 'K2G':
        artist, sep, rest = clean.partition(' - ')
        artist = artist.strip()
        rest = rest.strip() if sep else clean
        albums = re.findall(r'\[([^\]]+)\]', rest)
        collection = (albums[-1] if albums else re.sub(r'\([^)]*\)', '', rest)).strip(' :-')
        collection = re.sub(r'\b(?:ver\.?|version)\b.*$', '', collection, flags=re.I).strip(' :-') or rest
        title = ('%s - %s' % (artist, collection)).strip(' -')
        option = ' · '.join(tags + ([rest] if rest and rest != collection else []))
        group_key = 'K2G|%s|%s' % (_catalog_norm_key(artist), _catalog_norm_key(collection))
    else:
        group_key = 'DIRECT|' + pid
    event = ''
    joined = ' '.join(tags) + ' ' + raw
    if re.search(r'영상통화|video\s*call|fancall', joined, re.I): event = 'FANCALL'
    elif re.search(r'대면\s*사인|팬\s*사인|fansign', joined, re.I): event = '팬싸인회'
    elif re.search(r'럭키\s*드로우|lucky\s*draw', joined, re.I): event = '럭키드로우'
    elif re.search(r'쇼케이스|showcase', joined, re.I): event = '쇼케이스'
    pack = '세트' if re.search(r'【세트】|\bset\b|\d+종\s*(?:세트|묶음)', raw, re.I) else \
           ('랜덤' if re.search(r'【랜덤】|\brandom\b', raw, re.I) else '단품')
    event_date = ''
    dm = re.search(r'【\s*(\d{1,2}/\d{1,2})', raw)
    if dm: event_date = dm.group(1)
    product_type = {'KPOP': 'ALBUM', 'KFOOD': 'FOOD', 'KBEAUTY': 'BEAUTY',
                    'KFASHION': 'APPAREL', 'LIFESTYLE': 'LIFESTYLE'}[dept]
    meta = {'original_name': raw, 'event_type': event, 'event_date': event_date,
            'pack_type': pack, 'tags': tags}
    reasons = []
    if not str(r.get('img') or '').strip(): reasons.append('이미지 없음')
    if num(r.get('price')) <= 0: reasons.append('가격 확인')
    if dept == 'KPOP' and not artist: reasons.append('아티스트 확인')
    if not category: reasons.append('카테고리 확인')
    confidence = max(20, 100 - len(reasons) * 15)
    return {'group_key': group_key, 'title': title or raw, 'department': dept,
            'category': category or _DEPT_TO_CAT[dept], 'product_type': product_type,
            'brand_artist': artist, 'collection_name': collection, 'source': source,
            'option_name': option, 'metadata': meta, 'reasons': reasons,
            'confidence': confidence, 'review_state': 'READY' if confidence >= 80 else 'REVIEW'}

def _catalog_migrate_missing():
    """products의 미매핑 행만 그룹/SKU/식별자/재고로 백필한다(재실행 안전)."""
    if not _state.get('pcols') or not _state.get('pname'):
        return 0
    mapped = {r['legacy_product_id'] for r in rows('SELECT legacy_product_id FROM product_variants')}
    sel = ['id', '%s AS name' % _state['pname'], 'stock', 'soldout']
    if _state.get('pprice'): sel.append('%s AS price' % _state['pprice'])
    for col in ('category', 'img', 'created_at'):
        if col in _state['pcols']: sel.append(col)
    products = [r for r in rows('SELECT %s FROM products' % ', '.join(sel)) if r['id'] not in mapped]
    if not products:
        return 0
    known = {r['group_key']: r for r in rows('SELECT id,group_key,group_no,image,metadata,confidence,review_state FROM product_groups')}
    counts = {r['group_id']: num(r['n']) for r in rows('SELECT group_id, COUNT(*) AS n FROM product_variants GROUP BY group_id')}
    mx = num((one('SELECT MAX(group_no) AS n FROM product_groups') or {}).get('n'))
    ops, stamp = [], now_iso()
    for r in products:
        parsed = _catalog_parse_product(r)
        g = known.get(parsed['group_key'])
        if not g:
            mx += 1
            gid = _stable_id('grp_', parsed['group_key'])
            gmeta = dict(parsed['metadata']); gmeta['review_reasons'] = parsed['reasons']
            g = {'id': gid, 'group_key': parsed['group_key'], 'group_no': mx,
                 'image': str(r.get('img') or '')[:2000], 'metadata': json.dumps(gmeta, ensure_ascii=False),
                 'confidence': parsed['confidence'], 'review_state': parsed['review_state']}
            known[parsed['group_key']] = g; counts[gid] = 0
            ops.append(('INSERT INTO product_groups(id,group_no,group_code,group_key,title,department,category,product_type,brand_artist,collection_name,source,sale_status,metadata,confidence,review_state,image,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (gid, mx, 'PG-%06d' % mx, parsed['group_key'], parsed['title'][:300],
                         parsed['department'], parsed['category'], parsed['product_type'],
                         parsed['brand_artist'][:160], parsed['collection_name'][:200], parsed['source'],
                         'ACTIVE', json.dumps(gmeta, ensure_ascii=False), parsed['confidence'],
                         parsed['review_state'], str(r.get('img') or '')[:2000], r.get('created_at') or stamp, stamp)))
        gid, gno = g['id'], num(g['group_no'])
        candidate_image = str(r.get('img') or '').strip()[:2000]
        if candidate_image and not str(g.get('image') or '').strip():
            gm = jload(g.get('metadata'), {}) or {}; reasons = list(gm.get('review_reasons') or [])
            reasons = [x for x in reasons if x not in ('이미지 없음', '대표 이미지 없음')]
            gm['review_reasons'] = reasons
            confidence = min(100, num(g.get('confidence')) + 15)
            review_state = 'READY' if confidence >= 80 else (g.get('review_state') or 'REVIEW')
            ops.append(('UPDATE product_groups SET image=?,metadata=?,confidence=?,review_state=?,updated_at=? WHERE id=?',
                        (candidate_image, json.dumps(gm, ensure_ascii=False), confidence, review_state, stamp, gid)))
            g.update(image=candidate_image, metadata=json.dumps(gm, ensure_ascii=False),
                     confidence=confidence, review_state=review_state)
        counts[gid] = counts.get(gid, 0) + 1
        vid = _stable_id('var_', r['id'])
        sku = '%s-%06d-%02d' % (parsed['department'], gno, counts[gid])
        external = parsed['source'] == 'K2G'
        ops.append(('INSERT INTO product_variants(id,legacy_product_id,group_id,sku,option_name,source,sale_status,stock_mode,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (vid, r['id'], gid, sku, parsed['option_name'][:300], parsed['source'],
                     'SOLD_OUT' if num(r.get('soldout')) else 'ACTIVE', 'EXTERNAL' if external else 'TRACKED',
                     json.dumps(parsed['metadata'], ensure_ascii=False), r.get('created_at') or stamp, stamp)))
        identifiers = [('LEGACY_ID', r['id'])]
        if parsed['source'] == 'K2G': identifiers.append(('K2G_ID', r['id'].split('::', 1)[-1]))
        for kind, value in identifiers:
            ops.append(('INSERT INTO product_identifiers(id,variant_id,kind,value) VALUES(?,?,?,?)',
                        (_stable_id('idn_', kind + '|' + value), vid, kind, value)))
        ops.append(('INSERT INTO inventory_balances(variant_id,location_id,is_tracked,on_hand,reserved,incoming,reorder_point,external_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (vid, 'SEOUL', 0 if external else 1, 0 if external else max(0, num(r.get('stock'))),
                     0, 0, 5, ('OUT' if num(r.get('soldout')) else 'AVAILABLE') if external else '', stamp)))
    runmany(ops)
    try:
        if one('SELECT name FROM catalog_sequences WHERE name=?', ('product_group',)):
            run('UPDATE catalog_sequences SET value=? WHERE name=?', (mx, 'product_group'))
        else:
            run('INSERT INTO catalog_sequences(name,value) VALUES(?,?)', ('product_group', mx))
    except Exception: pass
    return len(products)

def _catalog_migrate_lifestyle():
    """v9에서 생성된 LIVING 그룹을 새 명칭으로 전환한다. 기존 SKU는 식별자이므로 유지한다."""
    run("UPDATE product_groups SET department='LIFESTYLE', "
        "product_type=CASE WHEN product_type='LIVING' THEN 'LIFESTYLE' ELSE product_type END, "
        "updated_at=? WHERE department='LIVING'", (now_iso(),))

def _migrate_lifestyle_page_edits():
    """관리자에서 저장한 HTML 편집본에도 남은 구 명칭을 멱등 치환한다."""
    for row in rows("SELECT path,html FROM page_edits WHERE html LIKE ?", ('%LIVING%',)):
        old = row.get('html') or ''
        new = re.sub(r'\bLIVING\b', 'LIFESTYLE', old)
        if new != old:
            run('UPDATE page_edits SET html=?,updated=?,by_admin=? WHERE path=?',
                (new, now_iso(), '시스템 명칭전환', row['path']))

def _migrate_storefront_header_page_edits():
    """관리자에서 저장한 HTML이 정적 파일을 덮어써도 새 상단 헤더를 유지한다."""
    global_bar = re.compile(
        r'\s*<!-- \uAE00\uB85C\uBC8C \uBC14 -->\s*<div class="global-bar">.*?</div>\s*</div>\s*', re.S)
    global_css = re.compile(
        r'/\* ---------- \uC0C1\uB2E8 \uAE00\uB85C\uBC8C \uBC14 ---------- \*/.*?'
        r'(?=/\* ---------- \uD5E4\uB354 / \uBA54\uAC00\uBA54\uB274 ---------- \*/)', re.S)
    replacements = (
        ('.header-inner{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:64px;max-width:1440px;margin:0 auto}',
         '.header-inner{display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:78px;max-width:1520px;margin:0 auto}'),
        ('.logo{font-family:var(--disp);font-size:26px;',
         '.logo{font-family:var(--disp);font-size:31px;'),
        ('nav.main a.top{font-size:13px;font-weight:600;letter-spacing:.08em;padding:0 14px',
         'nav.main a.top{font-size:14px;font-weight:600;letter-spacing:.08em;padding:0 17px'),
        ('.util{display:flex;gap:18px;font-size:13px;font-weight:600;align-items:center}',
         '.util{display:flex;gap:20px;font-size:14px;font-weight:600;align-items:center}'),
        ('.util a{font-size:13px;font-weight:600}',
         '.util a{font-size:14px;font-weight:600}'),
        ('.util .cart{background:var(--ink);color:#fff;border-radius:20px;padding:6px 14px;font-size:12px}',
         '.util .cart{background:var(--ink);color:#fff;border-radius:22px;padding:8px 17px;font-size:12.5px}'),
        ('position:sticky;top:64px;background:var(--paper);z-index:50}',
         'position:sticky;top:78px;background:var(--paper);z-index:50}'),
    )
    for row in rows("SELECT path,html FROM page_edits"):
        old = row.get('html') or ''
        new = global_bar.sub('\n', old)
        new = global_css.sub('', new)
        for before, after in replacements:
            new = new.replace(before, after)
        if new != old:
            run('UPDATE page_edits SET html=?,updated=?,by_admin=? WHERE path=?',
                (new, now_iso(), '시스템 헤더개편', row['path']))

def _catalog_review_reasons(g, variants=None):
    meta = jload(g.get('metadata'), {}) or {}
    reasons = list(meta.get('review_reasons') or [])
    if not (g.get('title') or '').strip(): reasons.append('상품명 없음')
    if g.get('department') not in _DEPT_KEYS: reasons.append('분류 확인')
    if not (g.get('image') or '').strip(): reasons.append('대표 이미지 없음')
    if variants is not None and not variants: reasons.append('SKU 없음')
    return list(dict.fromkeys(reasons))

def catalog_inventory_from_legacy(pid):
    """결제/취소 등 기존 경로에서 products 재고가 바뀐 뒤 새 재고 뷰를 맞춘다."""
    v = one('SELECT id, stock_mode FROM product_variants WHERE legacy_product_id=?', (pid,))
    if not v:
        _catalog_migrate_missing()
        v = one('SELECT id, stock_mode FROM product_variants WHERE legacy_product_id=?', (pid,))
    r = one('SELECT stock, soldout FROM products WHERE id=?', (pid,))
    if not v or not r: return
    if v.get('stock_mode') == 'EXTERNAL':
        run('UPDATE inventory_balances SET external_status=?, updated_at=? WHERE variant_id=?',
            ('OUT' if num(r.get('soldout')) else 'AVAILABLE', now_iso(), v['id']))
    else:
        run('UPDATE inventory_balances SET on_hand=?, reserved=0, updated_at=? WHERE variant_id=?',
            (max(0, num(r.get('stock'))), now_iso(), v['id']))

def catalog_product_from_legacy(pid):
    """기존 상품 편집 API의 이름·분류·이미지·판매상태를 상품 마스터에 반영한다."""
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    cols = 'id,%s AS name,%s AS price,stock,soldout' % (nm, pr)
    for c in ('category', 'img', 'created_at'):
        if c in _state['pcols']: cols += ',' + c
    r = one('SELECT %s FROM products WHERE id=?' % cols, (pid,))
    if not r: return
    v = one('SELECT * FROM product_variants WHERE legacy_product_id=?', (pid,))
    if not v:
        _catalog_migrate_missing(); v = one('SELECT * FROM product_variants WHERE legacy_product_id=?', (pid,))
    if not v: return
    parsed = _catalog_parse_product(r); stamp = now_iso()
    run('UPDATE product_variants SET option_name=?,sale_status=?,metadata=?,updated_at=? WHERE id=?',
        (parsed['option_name'][:300], 'SOLD_OUT' if num(r.get('soldout')) else 'ACTIVE',
         json.dumps(parsed['metadata'], ensure_ascii=False), stamp, v['id']))
    # 자동 그룹명은 현재 이름으로 갱신하되, 관리자가 병합/분리한 그룹명은 보존한다.
    g = one('SELECT * FROM product_groups WHERE id=?', (v['group_id'],)) or {}
    auto_group = str(g.get('group_key') or '').startswith(('DIRECT|', 'OWN|', 'K2G|'))
    gmeta = dict(parsed['metadata']); gmeta['review_reasons'] = parsed['reasons']
    sets = ['department=?', 'category=?', 'product_type=?', 'image=?',
            'confidence=?', 'review_state=?', 'metadata=?', 'updated_at=?']
    args = [parsed['department'], parsed['category'], parsed['product_type'], str(r.get('img') or '')[:2000],
            parsed['confidence'], parsed['review_state'], json.dumps(gmeta, ensure_ascii=False), stamp]
    if auto_group:
        sets += ['title=?', 'brand_artist=?', 'collection_name=?']
        args += [parsed['title'][:300], parsed['brand_artist'][:160], parsed['collection_name'][:200]]
    run('UPDATE product_groups SET %s WHERE id=?' % ','.join(sets), tuple(args + [v['group_id']]))
    catalog_inventory_from_legacy(pid)

def _catalog_delete_legacy(pid):
    v = one('SELECT id, group_id FROM product_variants WHERE legacy_product_id=?', (pid,))
    if not v: return
    last = num((one('SELECT COUNT(*) AS n FROM product_variants WHERE group_id=?', (v['group_id'],)) or {}).get('n')) <= 1
    ops = [('DELETE FROM product_identifiers WHERE variant_id=?', (v['id'],)),
           ('DELETE FROM inventory_movements WHERE variant_id=?', (v['id'],)),
           ('DELETE FROM inventory_balances WHERE variant_id=?', (v['id'],)),
           ('DELETE FROM product_variants WHERE id=?', (v['id'],))]
    if last: ops.append(('DELETE FROM product_groups WHERE id=?', (v['group_id'],)))
    runmany(ops)

@admin_router.get('/admin/api/products/categories')
def api_product_categories(request: Request):
    a = get_actor(request); need(a, 0)
    return {'categories': [{'value': k, 'label': l} for k, l in PRODUCT_CATEGORIES]}

def _related_id_list(raw):
    """related_ids 저장값을 중복 없는 상품 ID 목록으로 정규화한다."""
    if isinstance(raw, str):
        try: raw = json.loads(raw or '[]')
        except Exception: raw = []
    if not isinstance(raw, list):
        return []
    out = []
    for value in raw:
        value = str(value or '').strip()
        if value and len(value) <= 180 and value not in out:
            out.append(value)
    return out

def _related_clean(raw, pid=''):
    ids = [x for x in _related_id_list(raw) if x != pid][:12]
    if not ids:
        return []
    marks = ','.join(['?'] * len(ids))
    found = {r['id'] for r in rows('SELECT id FROM products WHERE id IN (%s)' % marks, tuple(ids))}
    return [x for x in ids if x in found]

def _related_set(pid, raw):
    """한 상품의 관련상품을 저장하고 상대 상품에도 역방향 연결을 동기화한다."""
    cur = one('SELECT related_ids FROM products WHERE id=?', (pid,))
    if not cur:
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    old_ids = _related_id_list(cur.get('related_ids'))
    new_ids = _related_clean(raw, pid)
    peer_ids = list(dict.fromkeys(old_ids + new_ids))
    peers = {}
    if peer_ids:
        marks = ','.join(['?'] * len(peer_ids))
        peers = {r['id']: _related_id_list(r.get('related_ids')) for r in rows(
            'SELECT id, related_ids FROM products WHERE id IN (%s)' % marks, tuple(peer_ids))}
    # 선택된 상대 상품이 이미 12개로 꽉 찬 경우 한쪽만 연결되는 상태를 만들지 않는다.
    for rid in new_ids:
        rel = peers.get(rid, [])
        if pid not in rel and len(rel) >= 12:
            raise HTTPException(400, '관련상품 한도(12개)가 찬 상품이 있습니다: ' + rid)
    ops = [('UPDATE products SET related_ids=? WHERE id=?',
            (json.dumps(new_ids, ensure_ascii=False, separators=(',', ':')), pid))]
    for rid in peer_ids:
        rel = peers.get(rid, [])
        if rid in new_ids and pid not in rel:
            rel.append(pid)
        if rid not in new_ids:
            rel = [x for x in rel if x != pid]
        ops.append(('UPDATE products SET related_ids=? WHERE id=?',
                    (json.dumps(rel, ensure_ascii=False, separators=(',', ':')), rid)))
    runmany(ops)
    return new_ids

def _related_unlink_deleted(pid, raw):
    ops = []
    for rid in _related_id_list(raw):
        peer = one('SELECT related_ids FROM products WHERE id=?', (rid,))
        if not peer: continue
        rel = [x for x in _related_id_list(peer.get('related_ids')) if x != pid]
        ops.append(('UPDATE products SET related_ids=? WHERE id=?',
                    (json.dumps(rel, ensure_ascii=False, separators=(',', ':')), rid)))
    if ops: runmany(ops)

def _related_admin_item(r):
    img = str(r.get('img') or '').strip()
    if img and str(r['id']).startswith('k2g::') and not img.startswith(('http://', 'https://', '/')):
        img = 'https://www.kpop2gether.com/shopimages/912enter/' + img
    return {'id': r['id'], 'name': r.get('name') or r['id'],
            'img': img, 'price': num(r.get('price')),
            'soldout': bool(num(r.get('soldout')) or num(r.get('stock')) <= 0)}

def _related_items(ids):
    ids = _related_id_list(ids)
    if not ids: return []
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    marks = ','.join(['?'] * len(ids))
    rs = rows('SELECT id, %s AS name, %s AS price, stock, soldout, img FROM products WHERE id IN (%s)'
              % (nm, pr, marks), tuple(ids))
    by_id = {r['id']: _related_admin_item(r) for r in rs}
    return [by_id[x] for x in ids if x in by_id]

@admin_router.get('/admin/api/products/related-options')
def api_related_options(request: Request):
    a = get_actor(request); need(a, 1, '관련상품 조회')
    q = (request.query_params.get('query') or '').strip()
    current = (request.query_params.get('id') or '').strip()
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    where, args = [], []
    if current:
        where.append('id<>?'); args.append(current)
    if q:
        where.append('(id LIKE ? OR %s LIKE ?)' % nm); args += ['%' + q + '%', '%' + q + '%']
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    order = ('CASE WHEN created_at IS NULL THEN 1 ELSE 0 END, created_at DESC, id DESC'
             if 'created_at' in _state['pcols'] else 'id DESC')
    rs = rows('SELECT id, %s AS name, %s AS price, stock, soldout, img FROM products%s ORDER BY %s LIMIT 30'
              % (nm, pr, w, order), tuple(args))
    return {'rows': [_related_admin_item(r) for r in rs]}

# ── 이미지 업로드 (대표 이미지 + 상세페이지 이미지 블록 공용) ──────────────
@admin_router.post('/admin/api/upload')
async def api_upload(request: Request, file: UploadFile = File(...)):
    a = get_actor(request); need(a, 2, '이미지 업로드')
    data = await file.read()
    res = store_image(data, file.content_type)  # 형식/용량 검증 + R2 또는 DB 폴백 저장
    audit(a, '이미지업로드', res['url'][:120],
          '%s · %.0fKB · %s' % (file.content_type, len(data) / 1024, res['stored']))
    return {'ok': True, 'url': res['url'], 'storage': res['stored']}

# ── 메인배너 (홈 히어로 슬라이드) — hero_api 저장소 재사용 ─────────────────
@admin_router.get('/admin/api/banner')
def api_banner_get(request: Request):
    a = get_actor(request); need(a, 2, '메인배너 조회')
    import hero_api
    return hero_api.load_data()

@admin_router.put('/admin/api/banner')
def api_banner_put(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '메인배너 수정')
    import hero_api
    try:
        data = hero_api.HeroData(**body).model_dump()
    except Exception as e:
        raise HTTPException(400, '배너 데이터 형식 오류: %s' % str(e)[:200])
    try:
        hero_api.save_data(data)
    except Exception as e:
        raise HTTPException(502, '배너 저장 실패(DB): %s' % str(e)[:180])
    hero_api._cache['data'] = None  # 5초 캐시 즉시 무효화
    audit(a, '메인배너수정', '%d개 슬라이드' % len(data['slides']), '')
    return {'ok': True, 'slides': len(data['slides'])}

@admin_router.post('/admin/api/products/create')
def api_product_create(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '상품 등록')
    if not _state['pcols'] or not _state['pname']:
        raise HTTPException(400, '상품 테이블이 준비되지 않았습니다')
    name = (body.get('name') or '').strip()
    price = num(body.get('price')); stock = num(body.get('stock'))
    dc = num(body.get('discount_pct'))
    if not name: raise HTTPException(400, '상품명을 입력하세요')
    if price < 0 or stock < 0: raise HTTPException(400, '가격/재고는 0 이상')
    if not 0 <= dc <= 90: raise HTTPException(400, '할인율은 0~90 사이 정수만 가능합니다')
    if dc > 0 and price <= 0: raise HTTPException(400, '할인율을 적용하려면 먼저 정가를 입력하세요')
    pid = 'mp::' + uid()
    cols, vals = ['id', _state['pname'], 'stock', 'soldout'], [pid, name, stock, 1 if stock == 0 else 0]
    if _state['pprice']: cols.append(_state['pprice']); vals.append(disc_price(price, dc) if dc else price)
    if dc and 'list_price' in _state['pcols']:
        cols.append('list_price'); vals.append(price)
    if 'img' in _state['pcols']:
        cols.append('img'); vals.append(_safe_url(body.get('img')))
    if 'descr' in _state['pcols']:
        cols.append('descr'); vals.append((body.get('descr') or '').strip()[:4000])
    if 'category' in _state['pcols']:
        cols.append('category'); vals.append(norm_cat(body.get('category')))
    if 'badge' in _state['pcols']:
        cols.append('badge'); vals.append((body.get('badge') or '').strip()[:30])
    if 'badge_color' in _state['pcols']:
        cols.append('badge_color'); vals.append(badge_color(body.get('badge_color')))
    if 'created_at' in _state['pcols']:
        cols.append('created_at'); vals.append(now_iso())
    if 'related_ids' in _state['pcols']:
        cols.append('related_ids'); vals.append('[]')
    if 'detail_html' in _state['pcols']:
        # detail_blocks(JSON) 우선, 없으면 레거시 detail_html 텍스트 허용
        blocks = body.get('detail_blocks')
        cols.append('detail_html')
        vals.append(clean_blocks(blocks) if blocks is not None else (body.get('detail_html') or '').strip()[:100000])
    if 'gallery' in _state['pcols']:
        cols.append('gallery'); vals.append(_clean_gallery(body.get('gallery')))
    run('INSERT INTO products(%s) VALUES(%s)' % (','.join(cols), ','.join(['?'] * len(vals))), tuple(vals))
    if 'related_ids' in _state['pcols'] and body.get('related_ids') is not None:
        try:
            _related_set(pid, body.get('related_ids'))
        except Exception:
            run('DELETE FROM products WHERE id=?', (pid,))
            raise
    try: catalog_product_from_legacy(pid)
    except Exception: pass
    audit(a, '상품등록', pid, '%s / 정가 %s원%s / 재고 %d / %s' % (
        name, format(price, ','), (' · 할인 %d%% → 판매 ₩%s' % (dc, format(disc_price(price, dc), ','))) if dc else '',
        stock, _CAT_LABEL.get(norm_cat(body.get('category')), '미분류')))
    return {'ok': True, 'id': pid, 'url': '/p/' + pid}

@admin_router.post('/admin/api/products/delete')
def api_product_delete(request: Request, body: dict = Body(...)):
    """상품 삭제. mp::(직접등록)와 k2g::(앨범 카탈로그) 모두 삭제 가능.
    k2g는 삭제 기록(k2g_removed)을 남겨 SHOP·앨범상세의 인라인 카탈로그에서도 즉시 감춘다.
    주문·Q&A 이력은 보존하고, 재입고 알림 대기만 함께 정리한다."""
    a = get_actor(request); need(a, 2, '상품 삭제')
    pid = str(body.get('id') or '').strip()
    if not pid:
        raise HTTPException(400, '상품 ID가 없습니다')
    r = one('SELECT %s AS name%s FROM products WHERE id=?' %
            ((_state['pname'] or 'id'), (', related_ids' if 'related_ids' in _state['pcols'] else '')), (pid,))
    if not r:
        raise HTTPException(404, '상품을 찾을 수 없습니다')
    if 'related_ids' in _state['pcols']:
        _related_unlink_deleted(pid, r.get('related_ids'))
    try: _catalog_delete_legacy(pid)
    except Exception: pass
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
    elif not pid.startswith('mp::'):
        # 정적 백필(own) 상품: 톰스톤 기록 → 시드 재실행·SHOP 정적 카드 노출 모두 차단
        try:
            run('CREATE TABLE IF NOT EXISTS own_removed(id TEXT PRIMARY KEY, name TEXT, created TEXT, by_admin TEXT)')
        except Exception:
            pass
        try:
            run('INSERT INTO own_removed(id, name, created, by_admin) VALUES(?,?,?,?)',
                (pid, str(r.get('name') or '')[:300], now_iso(), a['name']))
        except Exception:
            pass  # 이미 기록됨 — 무시
        _own_rm_cache['set'] = None
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
    for c in ('img', 'descr', 'category', 'detail_html', 'gallery', 'badge', 'badge_color', 'created_at', 'related_ids', 'list_price'):
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
        'list_price': num(r.get('list_price')) or None,
        'discount_pct': derived_pct(r.get('list_price'), r.get('price')),
        'sale_price': num(r.get('price')) if _state['pprice'] else None,
        'stock': num(r.get('stock')), 'soldout': num(r.get('soldout')),
        'img': r.get('img') or '', 'descr': r.get('descr') or '',
        'category': norm_cat(r.get('category')),
        'badge': (r.get('badge') or '').strip(),
        'badge_color': badge_color(r.get('badge_color')),
        'created_at': r.get('created_at') or '',
        'related_ids': _related_id_list(r.get('related_ids')),
        'related_products': _related_items(r.get('related_ids')),
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
        'badge_color': ('badge_color', 7, badge_color),
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
    # 가격(정가) · 할인율 — 단일 규칙(apply_pricing): 판매가는 자동 계산되어 저장
    priced = False
    if (body.get('price') is not None or body.get('discount_pct') is not None) and _state['pprice']:
        b_, p_, s_ = apply_pricing(pid, base=body.get('price'), pct=body.get('discount_pct'))
        log.append('정가 %s원%s' % (format(b_, ','),
                   (' · 할인 %d%% → 판매 ₩%s' % (p_, format(s_, ',')) if p_ else '')))
        priced = True
    if body.get('stock') is not None:
        s = num(body['stock'])
        if s < 0: raise HTTPException(400, '재고는 0 이상')
        sets.append('stock=?'); args.append(s)
        sets.append('soldout=?'); args.append(1 if s == 0 else 0)
        log.append('stock')
    related_changed = 'related_ids' in body and 'related_ids' in _state['pcols']
    if not sets and not priced and not related_changed: raise HTTPException(400, '변경할 값 없음')
    if sets:
        run('UPDATE products SET %s WHERE id=?' % ', '.join(sets), tuple(args + [pid]))
    if related_changed:
        _related_set(pid, body.get('related_ids')); log.append('related_ids')
    try: _k2g_cache_bust()
    except Exception: pass
    try: catalog_product_from_legacy(pid)
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
.rel-search{display:flex;gap:7px}.rel-search input{flex:1;font:inherit;padding:10px 12px;border:1px solid #ccc}
.rel-results{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;max-height:230px;overflow:auto}
.rel-opt,.rel-item{display:grid;grid-template-columns:46px 1fr auto;gap:9px;align-items:center;border:1px solid #e3e1db;background:#fff;padding:7px;text-align:left}
.rel-opt{cursor:pointer;font:inherit}.rel-opt:hover{border-color:var(--black);background:#faf9f6}.rel-opt:disabled{opacity:.45;cursor:not-allowed}
.rel-opt img,.rel-item img{width:46px;height:46px;object-fit:cover;background:#eee}.rel-ph{width:46px;height:46px;background:#eee;display:flex;align-items:center;justify-content:center;font-size:10px;color:#999}
.rel-name{font-size:12px;font-weight:700;line-height:1.35}.rel-id{font:9px 'IBM Plex Mono';color:#999;margin-top:3px;word-break:break-all}
.rel-selected{display:flex;flex-direction:column;gap:6px;margin-top:12px}.rel-item{grid-template-columns:46px 1fr auto}.rel-ctrl{display:flex;gap:3px}
@media(max-width:640px){.rel-results{grid-template-columns:1fr}}
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
  <div class="f"><label>정가(원) *</label><input type="number" id="fp" min="0" placeholder="12900" oninput="fpCalc()"></div>
 </div>
 <div class="row2">
  <div class="f"><label>할인율(%) <span style="font-weight:400;color:#aaa">— 0이면 할인 없음 · 최대 90</span></label>
   <input type="number" id="fdc" min="0" max="90" value="0" oninput="fpCalc()"></div>
  <div class="f"><label>판매가 <span style="font-weight:400;color:#aaa">— 자동 계산 · 실제 결제 금액</span></label>
   <div id="fsale" style="font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600;padding:10px 12px;border:1px solid #e3e1db;background:#faf9f6;min-height:41px">₩0</div></div>
 </div>
 <div class="row2">
  <div class="f"><label id="fslabel">초기 재고 *</label><input type="number" id="fs" min="0" placeholder="100"></div>
  <div class="f"><label>카드 배지 <span style="font-weight:400;color:#aaa">— NEW 오른쪽에 표기</span></label>
   <input type="text" id="fb" list="badgeOpts" maxlength="30" placeholder="비우면 카테고리명이 표기됩니다">
   <datalist id="badgeOpts"><option value="BEST"><option value="NEW"><option value="LIMITED"><option value="EVENT"><option value="GIFT"><option value="성수 한정"><option value="세트"><option value="사인회"><option value="영상통화"><option value="예약판매"></datalist></div>
 </div>
 <div class="f"><label>카드 배지 배경색</label>
  <div style="display:flex;align-items:center;gap:10px"><input type="color" id="fbc" value="#050505" style="width:54px;height:40px;padding:2px;border:1px solid #ccc;background:#fff;cursor:pointer"><span style="font-size:12px;color:#888">배지의 글자색은 흰색으로 고정됩니다.</span></div>
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
<div class="card"><h3>관련 상품</h3>
 <div class="rel-search"><input id="relQ" type="text" placeholder="상품명 또는 상품 ID 검색" onkeydown="if(event.key==='Enter'){event.preventDefault();relSearch()}" oninput="relTyping()"><button class="btn sm" type="button" onclick="relSearch()">검색</button></div>
 <div id="relResults" class="rel-results"></div>
 <div id="relSelected" class="rel-selected"></div>
 <div class="hint">최대 12개 · 선택 순서대로 상세페이지에 표시됩니다. 연결은 상대 상품에도 자동 반영되며 ↑↓ 버튼으로 노출 순서를 바꿀 수 있습니다.</div>
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
let _related=[],_relTimer=0;
function relImg(p){return p.img?`<img src="${esc(p.img)}" alt="">`:'<span class="rel-ph">NO IMG</span>'}
function renderRelated(){
 const host=$('#relSelected');
 if(!_related.length){host.innerHTML='<div class="hint" style="padding:12px;text-align:center;border:1px dashed #ddd">연결된 관련 상품이 없습니다.</div>';return;}
 host.innerHTML=_related.map((p,i)=>`<div class="rel-item">${relImg(p)}<div><div class="rel-name">${esc(p.name)}</div><div class="rel-id">${esc(p.id)}${p.soldout?' · 품절':''}</div></div><div class="rel-ctrl"><button class="btn sm ghost" type="button" onclick="relMove(${i},-1)" ${i===0?'disabled':''}>↑</button><button class="btn sm ghost" type="button" onclick="relMove(${i},1)" ${i===_related.length-1?'disabled':''}>↓</button><button class="btn sm ghost" type="button" onclick="relRemove(${i})">삭제</button></div></div>`).join('');
}
function relMove(i,d){const j=i+d;if(j<0||j>=_related.length)return;const t=_related[i];_related[i]=_related[j];_related[j]=t;renderRelated()}
function relRemove(i){_related.splice(i,1);renderRelated()}
function relAdd(p){if(_related.some(x=>x.id===p.id))return;if(_related.length>=12)return toast('관련 상품은 최대 12개까지 선택할 수 있습니다');_related.push(p);renderRelated();relSearch()}
function relTyping(){clearTimeout(_relTimer);_relTimer=setTimeout(relSearch,350)}
async function relSearch(){
 const q=$('#relQ').value.trim(),url='/admin/api/products/related-options?query='+encodeURIComponent(q)+'&id='+encodeURIComponent(PAGE.id||'');
 try{const d=await api(url),chosen=new Set(_related.map(x=>x.id));
  $('#relResults').innerHTML=d.rows.map((p,i)=>`<button class="rel-opt" type="button" ${chosen.has(p.id)?'disabled':''} onclick="relAdd(window._relFound[${i}])">${relImg(p)}<span><span class="rel-name">${esc(p.name)}</span><span class="rel-id">${esc(p.id)}${p.soldout?' · 품절':''}</span></span><b style="font-size:16px">＋</b></button>`).join('')||'<div class="hint">검색 결과가 없습니다.</div>';window._relFound=d.rows;
 }catch(e){if(e.message!=='세션 만료')toast(e.message)}
}
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

function fpCalc(){
 const base=Math.max(0,Number($('#fp').value)||0);
 let dc=Number($('#fdc').value)||0; dc=Math.min(90,Math.max(0,Math.floor(dc)));
 // 서버 disc_price와 동일: (정가×(100-할인율)+50)//100 → 10원 단위 내림 정렬
 const sale=dc>0?Math.max(0,Math.floor(Math.floor((base*(100-dc)+50)/100)/10)*10):base;
 const w=n=>'₩'+Number(n).toLocaleString('ko-KR');
 $('#fsale').innerHTML=dc>0
  ?w(sale)+' <span style="color:#E8332A;font-family:\'Black Han Sans\'">-'+dc+'%</span>'
  :w(base);
 return {base,dc,sale};
}
async function init(){
 $('#fc').innerHTML=catOptions('');
 if(PAGE.mode==='new'){
 $('#h2title').textContent='신규 상품 등록';$('#ptitle').textContent='상품 등록';
  $('#saveBtn').textContent='등록';renderBlocks();renderRelated();return;
 }
 $('#h2title').textContent='상품 상세 편집';$('#ptitle').textContent='상세 편집';
 $('#fslabel').textContent='재고 *';
 try{
  const d=await api('/admin/api/products/detail?id='+encodeURIComponent(PAGE.id));
  $('#fn').value=d.name;$('#fc').innerHTML=catOptions(d.category);
  // 가격 칸은 항상 '정가' — 할인 중이면 list_price, 아니면 판매가(=정가)
  const _dc=Number(d.discount_pct)||0;
  if(d.price!=null)$('#fp').value=_dc>0?(d.list_price||d.price):d.price;
  $('#fdc').value=_dc;fpCalc();
  $('#fs').value=d.stock;$('#fd').value=d.descr;$('#fb').value=d.badge||'';$('#fbc').value=d.badge_color||'#050505';
  setMainImg(d.img);
  _blocks=Array.isArray(d.detail_blocks)?d.detail_blocks:[];
  _related=Array.isArray(d.related_products)?d.related_products:[];renderBlocks();renderRelated();
  const v=$('#viewBtn');v.href=d.url;v.style.display='';
  $('#stat').textContent=d.soldout?'상태: 품절':'상태: 판매중';
  if(new URLSearchParams(location.search).get('created')==='1'){toast('등록 완료! 상세 내용을 이어서 편집할 수 있습니다.');history.replaceState(null,'','/admin/products/edit?id='+encodeURIComponent(PAGE.id));}
 }catch(e){if(e.message!=='세션 만료'){alert('상품을 불러올 수 없습니다: '+e.message);location.href='/admin/dashboard';}}
}
async function save(){
 const name=$('#fn').value.trim(),cat=$('#fc').value;
 if(!name)return toast('상품명을 입력하세요');
 if(!cat)return toast('카테고리를 선택하세요');
 const pr=fpCalc();
 if(pr.dc>0&&pr.base<=0)return toast('할인율을 적용하려면 먼저 정가를 입력하세요');
 const btn=$('#saveBtn');btn.disabled=true;
 try{
  if(PAGE.mode==='new'){
   const r=await api('/admin/api/products/create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,category:cat,price:pr.base,discount_pct:pr.dc,stock:Number($('#fs').value||0),
     img:$('#fi').value,descr:$('#fd').value,badge:$('#fb').value.trim(),badge_color:$('#fbc').value,detail_blocks:_blocks,related_ids:_related.map(x=>x.id)})});
   location.href='/admin/products/edit?id='+encodeURIComponent(r.id)+'&created=1';return;
  }
  await api('/admin/api/products/detail/update',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({id:PAGE.id,name,category:cat,price:pr.base,discount_pct:pr.dc,stock:Number($('#fs').value||0),
    img:$('#fi').value,descr:$('#fd').value,badge:$('#fb').value.trim(),badge_color:$('#fbc').value,detail_blocks:_blocks,related_ids:_related.map(x=>x.id)})});
  toast('저장되었습니다');
 }catch(e){if(e.message!=='세션 만료')toast(e.message);}
 btn.disabled=false;
}
init();
</script></body></html>'''

# ═══════════════════════════════════════════════════════════════════════
# 카테고리별 상세페이지 메타 (뱃지 · 브랜드라인 · 혜택 아코디언)
#   · album 은 K2G 앨범과 동일한 리치 구성(영상통화·한터차트·4F)
#   · 그 외 카테고리는 성격에 맞는 뱃지/혜택으로 자동 전환
#   · 각 값은 (badges[list of (label, cls)], brand_line, benefits[list of (head, hl, rest, detail)])
# ═══════════════════════════════════════════════════════════════════════
_PDP_META = {
    'album': {
        'badges': [('영상통화', 'dream'), ('한터차트', 'best')],
        'brand': 'KPOP2GETHER × 맵달SEOUL · 4F',
        'benefits': [
            ('차트반영', '한터차트 집계', ' · 4F = 온라인 동시',
             'KPOP2GETHER × 맵달SEOUL 판매분은 한터차트에 집계됩니다'),
            ('가입혜택', '최초 가입 2,000P', '',
             '한 고객당 최초 가입 시 한 번만 지급됩니다'),
            ('맵달드림', '오늘 도착 또는 성수 픽업!', '',
             '서울 당일배송 · 성수 1F/4F 픽업 · 픽업 특전 대상'),
        ],
    },
    'md': {
        'badges': [('공식 굿즈', 'dream'), ('맵달드림', 'best')],
        'brand': 'MAPDAL SEOUL · OFFICIAL MD · 4F',
        'benefits': [
            ('정품보증', '공식 라이선스 굿즈', ' · 정품 보증',
             '맵달SEOUL이 직접 소싱한 공식 상품입니다'),
            ('가입혜택', '최초 가입 2,000P', '',
             '한 고객당 최초 가입 시 한 번만 지급됩니다'),
            ('맵달드림', '오늘 도착 또는 성수 픽업!', '',
             '서울 당일배송 · 성수 1F/4F 픽업 가능'),
        ],
    },
    'kfood': {
        'badges': [('콜드체인', 'dream'), ('오늘 도착', 'best')],
        'brand': 'MAPDAL SEOUL · K-FOOD · 1F MEAL ZIP',
        'benefits': [
            ('신선배송', '콜드체인 포장', ' · 신선도 유지',
             '아이스팩 · 보냉 포장으로 신선하게 배송됩니다'),
            ('가입혜택', '최초 가입 2,000P', '',
             '한 고객당 최초 가입 시 한 번만 지급됩니다'),
            ('맵달드림', '서울 당일배송 · 성수 1F 픽업!', '',
             '성수 1F MEAL ZIP에서 바로 픽업 가능'),
        ],
    },
    'apparel': {
        'badges': [('MAPDAL', 'dream'), ('성수 픽업', 'best')],
        'brand': 'MAPDAL SEOUL · APPAREL',
        'benefits': [
            ('사이즈', '실측 사이즈 안내', ' · 상세 참고',
             '상세 설명의 사이즈 표를 확인해 주세요'),
            ('가입혜택', '최초 가입 2,000P', '',
             '한 고객당 최초 가입 시 한 번만 지급됩니다'),
            ('맵달드림', '오늘 도착 또는 성수 픽업!', '',
             '서울 당일배송 · 성수 픽업 가능'),
        ],
    },
    'living': {
        'badges': [('MAPDAL', 'dream'), ('성수 픽업', 'best')],
        'brand': 'MAPDAL SEOUL · LIFESTYLE & HOME',
        'benefits': [
            ('구성안내', '구성품 상세', ' · 상세 참고',
             '상세 설명에서 구성품을 확인해 주세요'),
            ('가입혜택', '최초 가입 2,000P', '',
             '한 고객당 최초 가입 시 한 번만 지급됩니다'),
            ('맵달드림', '오늘 도착 또는 성수 픽업!', '',
             '서울 당일배송 · 성수 픽업 가능'),
        ],
    },
}
_PDP_META_DEFAULT = {
    'badges': [('MAPDAL', 'dream')],
    'brand': 'MAPDAL SEOUL',
    'benefits': [
        ('가입혜택', '최초 가입 2,000P', '',
         '한 고객당 최초 가입 시 한 번만 지급됩니다'),
        ('맵달드림', '오늘 도착 또는 성수 픽업!', '',
         '서울 당일배송 · 성수 픽업 가능'),
    ],
}

def _pdp_meta_html(cat):
    """카테고리 → (badges_html, brand_text, benefits_html)"""
    m = _PDP_META.get(cat, _PDP_META_DEFAULT)
    bdgs = ''.join('<span class="bdg %s">%s</span>' % (c, b) for b, c in m['badges'])
    bens = ''
    for head, hl, rest, detail in m['benefits']:
        bens += ('<div class="ben-row"><div class="bh"><h6>%s</h6>'
                 '<div class="bv"><span class="hl">%s</span>%s</div>'
                 '<span class="chev">⌄</span></div><div class="bd">%s</div></div>'
                 ) % (head, hl, rest, detail)
    return bdgs, m['brand'], bens

def _related_public_item(item):
    pid = str(item.get('id') or '')
    out = dict(item)
    out['url'] = ('/album-detail?uid=' + urllib.parse.quote(pid[5:], safe='')
                  if pid.startswith('k2g::') else '/p/' + urllib.parse.quote(pid, safe=''))
    return out

@admin_router.get('/api/products/related')
def api_public_related(request: Request):
    """상품 상세페이지 관련상품 위젯용 공개 데이터."""
    try: ensure_ready()
    except Exception: pass
    pid = (request.query_params.get('product_id') or '').strip()
    if not pid or len(pid) > 180 or 'related_ids' not in _state['pcols']:
        return {'rows': []}
    nm, pr = _state['pname'] or 'id', _state['pprice'] or 'price'
    r = one('SELECT id, %s AS name, %s AS price, stock, soldout, img, related_ids FROM products WHERE id=?'
            % (nm, pr), (pid,))
    if not r:
        return {'rows': []}
    related = _related_items(r.get('related_ids'))
    if not related:
        return {'rows': []}
    return {'rows': [_related_public_item(_related_admin_item(r))] +
                    [_related_public_item(x) for x in related]}

_RELATED_WIDGET_SNIPPET = r'''<style id="mpRelatedCss">
.mp-related{margin:18px 0 2px}.mp-rel-title{font-size:13px;font-weight:700;margin-bottom:9px}
.mp-rel-thumbs{display:flex;gap:8px;overflow-x:auto;padding:1px 1px 8px;scrollbar-width:thin}
.mp-rel-thumb{position:relative;flex:0 0 78px;width:78px;height:78px;border:2px solid transparent;border-radius:8px;background:#eee;overflow:hidden;padding:2px;transition:border-color .15s}
.mp-rel-thumb:hover,.mp-rel-thumb:focus,.mp-rel-thumb.current{border-color:#E8332A;outline:none}
.mp-rel-thumb img{width:100%;height:100%;object-fit:cover;border-radius:5px}.mp-rel-ph{display:flex;width:100%;height:100%;align-items:center;justify-content:center;background:#e7e5df;color:#777;font:700 9px 'IBM Plex Mono',monospace}
.mp-rel-thumb.sold:after{content:'SOLD OUT';position:absolute;left:4px;right:4px;bottom:4px;padding:3px 1px;border-radius:2px;background:rgba(20,20,20,.84);color:#fff;text-align:center;font:700 7px 'IBM Plex Sans KR',sans-serif}
.mp-rel-name{position:relative;background:#141414;color:#fff;border-radius:7px;padding:11px 12px;text-align:center;font-size:12.5px;font-weight:700;line-height:1.35;min-height:40px}
.mp-rel-name:before{content:'';position:absolute;left:30px;top:-8px;border-left:8px solid transparent;border-right:8px solid transparent;border-bottom:8px solid #141414}
@media(max-width:640px){.mp-rel-thumb{flex-basis:70px;width:70px;height:70px}.mp-rel-name{font-size:11.5px}}
</style><script id="mpRelatedJs">(function(){
 function pidOf(){try{if(location.pathname.indexOf('/p/')===0)return decodeURIComponent(location.pathname.slice(3));if(location.pathname==='/album-detail'||location.pathname==='/album-detail.html'){var u=new URLSearchParams(location.search).get('uid');return u?'k2g::'+u:''}}catch(e){}return''}
 function eh(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
 var pid=pidOf();if(!pid)return;
 fetch('/api/products/related?product_id='+encodeURIComponent(pid)).then(function(r){return r.json()}).then(function(d){
  if(!d.rows||d.rows.length<2)return;var title=document.querySelector('.buy h1');if(!title)return;
  var box=document.createElement('section');box.className='mp-related';box.setAttribute('aria-label','관련상품');
  var cards=d.rows.map(function(p){var media=p.img?'<img loading="lazy" src="'+eh(p.img)+'" alt="">':'<span class="mp-rel-ph">MAPDAL</span>';return '<a class="mp-rel-thumb'+(p.id===pid?' current':'')+(p.soldout?' sold':'')+'" href="'+eh(p.url)+'" data-name="'+eh(p.name)+'" aria-label="'+eh(p.name)+'">'+media+'</a>'}).join('');
  box.innerHTML='<div class="mp-rel-title">관련상품</div><div class="mp-rel-thumbs">'+cards+'</div><div class="mp-rel-name"></div>';
  title.insertAdjacentElement('afterend',box);var bar=box.querySelector('.mp-rel-name'),base=d.rows[0].name;bar.textContent=base;
  box.querySelectorAll('.mp-rel-thumb').forEach(function(a){a.addEventListener('mouseenter',function(){bar.textContent=a.dataset.name});a.addEventListener('focus',function(){bar.textContent=a.dataset.name});a.addEventListener('mouseleave',function(){bar.textContent=base});a.addEventListener('blur',function(){bar.textContent=base})});
 }).catch(function(){});
})();</script>'''

_PDP_HTML = '''<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>%(name)s — MAPDAL SEOUL</title>
<meta property="og:title" content="%(name)s"><meta property="og:description" content="MAPDAL SEOUL — Shop Seongsu, from Anywhere">%(og)s%(seo)s
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--red:#E8332A;--red-deep:#B71F18;--ink:#141414;--paper:#F7F6F2;--steel:#87867F;--amber:#FFB000;--line:#E2E0D9;
--disp:'Black Han Sans',sans-serif;--body:'IBM Plex Sans KR',sans-serif;--mono:'IBM Plex Mono',monospace}
*{margin:0;padding:0;box-sizing:border-box}html{scroll-behavior:smooth}
body{font-family:var(--body);background:var(--paper);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}img{display:block;max-width:100%%}.mono{font-family:var(--mono)}
header{position:sticky;top:0;z-index:100;background:var(--paper);border-bottom:1px solid var(--line)}
.header-inner{display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:78px;max-width:1520px;margin:0 auto}
.logo{font-family:var(--disp);font-size:31px;letter-spacing:.02em;color:var(--ink);line-height:1}
.logo em{color:var(--red);font-style:normal}
.util{display:flex;gap:20px;font-size:14px;font-weight:600;align-items:center}
.util a{font-size:14px;font-weight:600}.util a:hover{color:var(--red)}
.util .cart{background:var(--ink);color:#fff;border-radius:22px;padding:8px 17px;font-size:12.5px}
.crumb{max-width:1440px;margin:0 auto;padding:18px 48px 0;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--steel)}
.crumb a:hover{color:var(--red)}
.pdp{max-width:1440px;margin:0 auto;padding:28px 48px 56px;display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:48px;align-items:start}
.pdp>div{min-width:0}
.gal-main{border:1px solid var(--line);display:flex;align-items:center;justify-content:center;padding:56px 48px 84px;min-height:560px;position:relative;overflow:hidden;background:radial-gradient(130%% 95%% at 50%% 15%%,#FFF8F2 0%%,#FBE9DF 42%%,#F2D3C4 100%%)}
.gal-main .bg-word{position:absolute;top:6%%;left:50%%;transform:translateX(-50%%);font-family:var(--disp);font-size:clamp(80px,10vw,132px);letter-spacing:.03em;color:rgba(20,20,20,.045);pointer-events:none;user-select:none;white-space:nowrap;z-index:0}
.gal-main img{max-height:420px;width:auto;position:relative;z-index:2;filter:drop-shadow(0 30px 26px rgba(96,32,10,.22))}
.gal-main .ph-word{font-family:var(--disp);font-size:44px;color:rgba(20,20,20,.18);position:relative;z-index:2}
.gal-main::after{content:'';position:absolute;bottom:58px;left:50%%;transform:translateX(-50%%);width:44%%;height:34px;background:radial-gradient(ellipse at center,rgba(60,20,8,.32),rgba(60,20,8,0) 68%%);filter:blur(6px);z-index:1}
.gal-flavor{position:absolute;top:22px;left:22px;font-family:var(--mono);font-size:10px;letter-spacing:.16em;color:var(--red);background:#fff;border:1px solid var(--line);padding:6px 10px;z-index:3}
.gal-thumbs{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.gal-thumbs button{width:84px;height:84px;background:#fff;border:1px solid var(--line);cursor:pointer;display:flex;align-items:center;justify-content:center;padding:10px;transition:border-color .15s}
.gal-thumbs button.on,.gal-thumbs button:hover{border-color:var(--red)}
.gal-thumbs img{max-height:64px;width:auto}
.buy{position:sticky;top:80px;max-height:calc(100vh - 96px);overflow-y:auto;padding-right:8px;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.buy::-webkit-scrollbar{width:5px}.buy::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}
.badges{display:flex;gap:6px;margin-bottom:10px}
.bdg{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;padding:4px 8px}
.bdg.dream{background:var(--red);color:#fff}.bdg.best{background:var(--ink);color:#fff}
.buy .brand{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--red);margin-bottom:8px}
.buy h1{font-size:22px;font-weight:700;line-height:1.4}
.price-block{margin-top:14px}
.price-block .now{display:flex;align-items:baseline;gap:10px;margin-top:2px}
.price-block .pct{font-family:var(--disp);font-size:26px;color:var(--red)}
.price-block .amt{font-family:var(--disp);font-size:30px}
.stock-line{font-size:12.5px;font-weight:600;margin:12px 0 4px}
.stock-line.ok{color:#0a7d38}.stock-line.no{color:var(--red)}
.viewers{font-size:12px;color:var(--red);font-weight:600;margin:8px 0 16px}
.viewers::before{content:'';display:inline-block;width:7px;height:7px;border-radius:50%%;background:var(--red);margin-right:6px;animation:pulse 1.4s infinite}
@keyframes pulse{50%%{opacity:.3}}@media (prefers-reduced-motion:reduce){.viewers::before{animation:none}}
.qty-row{display:flex;align-items:center;justify-content:space-between;margin-top:8px}
.qty-ctl{display:flex;border:1px solid var(--line);background:#fff}
.qty-ctl button{width:34px;height:34px;border:none;background:#fff;cursor:pointer;font-size:16px}
.qty-ctl span{width:44px;text-align:center;font-family:var(--mono);font-size:13px;line-height:34px}
.total-row{display:flex;justify-content:space-between;align-items:baseline;margin-top:14px;padding-top:14px;border-top:2px solid var(--ink)}
.total-row .tl{font-size:13px;font-weight:600}
.total-row .tv{font-family:var(--disp);font-size:28px;color:var(--red)}
.buy-btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}
.buy-btns .btn{justify-content:center;padding:16px;border:none;cursor:pointer;font-family:var(--body);font-weight:700;font-size:14px;display:flex;align-items:center;text-align:center}
.btn.cartb{background:#fff;border:1.5px solid var(--ink);color:var(--ink)}
.btn.red{background:var(--red);color:#fff}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.like-row{display:flex;gap:8px;margin-top:10px}
.like-row button{flex:1;font:700 13px var(--body);padding:12px;border:1px solid var(--ink);background:#fff;cursor:pointer}
.like-row button.rs{border:0;background:var(--amber);color:var(--ink);display:none}
.ben-acc{margin-top:18px;border-top:1px solid var(--line)}
.ben-row{border-bottom:1px solid var(--line)}
.ben-row .bh{display:grid;grid-template-columns:88px 1fr 20px;gap:10px;padding:13px 2px;font-size:12.5px;cursor:pointer;align-items:start}
.ben-row .bh h6{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--steel);padding-top:2px}
.ben-row .bh .bv .hl{color:var(--red);font-weight:700}
.ben-row .chev{color:var(--steel);transition:transform .2s;text-align:center}
.ben-row.open .chev{transform:rotate(180deg)}
.ben-row .bd{display:none;padding:0 2px 14px 98px;font-size:12px;color:var(--steel);line-height:1.7}
.ben-row.open .bd{display:block}
.pdp-tabs{padding:36px 0 8px}
.tab-bar{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--line);position:sticky;top:78px;background:var(--paper);z-index:50}
.tab-bar button{padding:16px;border:none;background:transparent;font-family:var(--body);font-size:14.5px;font-weight:600;cursor:pointer;border-bottom:3px solid transparent;color:var(--steel)}
.tab-bar button.on{border-bottom-color:var(--ink);color:var(--ink)}
.tab-panel{display:none;padding:40px 0 8px}.tab-panel.on{display:block}
.tab-panel .descbody{font-size:14.5px;line-height:1.85;color:#333;white-space:pre-wrap}
.detail-imgs img{max-width:100%%;height:auto;display:block;margin:12px auto;border-radius:2px}
.info-table{width:100%%;border-collapse:collapse;font-size:13.5px}
.info-table th{text-align:left;width:130px;padding:12px 10px;background:#faf9f5;border:1px solid var(--line);font-weight:600;vertical-align:top}
.info-table td{padding:12px 12px;border:1px solid var(--line);color:#444;line-height:1.6;vertical-align:top}
.qna-wrap{max-width:1440px;margin:8px auto 0;padding:0 48px 40px}
.qna-wrap h2{font-family:var(--disp);font-size:22px;font-weight:400;margin-bottom:14px}
.qna-ask{font:700 12.5px var(--body);padding:11px 20px;border:0;background:var(--ink);color:#fff;cursor:pointer;margin-top:12px}
footer{background:var(--ink);color:#8E8D87;margin-top:40px}
.foot-inner{max-width:1440px;margin:0 auto;padding:56px 48px 32px}
footer .logo{color:#fff;margin-bottom:14px}footer .logo em{color:var(--red)}
footer p{font-size:12.5px;line-height:1.8;max-width:340px}
.foot-links{display:flex;gap:22px;flex-wrap:wrap;margin-top:18px;font-size:13px}
.foot-links a{color:#B9B8B1}.foot-links a:hover{color:#fff}
.foot-base{border-top:1px solid #2A2A28;font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;
max-width:1440px;margin:0 auto;padding:20px 48px;color:#5F5E58;line-height:1.9}
.foot-base a{color:var(--amber)}
@media(max-width:1024px){.pdp{grid-template-columns:1fr;gap:28px}.buy{position:static;max-height:none}.header-inner{height:62px;padding:0 12px}.logo{font-size:23px}.tab-bar{top:62px}}
@media(max-width:640px){.crumb{padding:14px 20px 0}.pdp{padding:20px 20px 40px}.gal-main{min-height:380px;padding:36px 20px 60px}.foot-inner,.foot-base{padding-left:20px;padding-right:20px}.qna-wrap{padding:0 20px 40px}}
</style></head><body>
<header><div class="header-inner">
<a class="logo" href="/home">MAPDAL<em>SEOUL</em></a>
<div class="util"><a href="/shop">SHOP</a><a href="/account" id="mpAuth">로그인</a><a class="cart" href="/cart" id="cartBadge">CART</a></div>
</div></header>
<div class="crumb"><a href="/home">HOME</a> &gt; <a href="%(caturl)s">%(catlabel)s</a> &gt; <span style="color:var(--ink)">%(name)s</span></div>
<div class="pdp">
  <div>
    <div class="gal-main" id="galMain"><span class="bg-word">MAPDAL SEOUL</span><span class="gal-flavor">%(flavor)s</span>%(imgtag)s</div>
    %(galhtml)s
    <div class="pdp-tabs">
      <div class="tab-bar">
        <button class="on" data-tab="desc">상품설명</button>
        <button data-tab="info">구매정보</button>
        <button data-tab="qa">배송/교환</button>
      </div>
      <div class="tab-panel on" id="tab-desc">
        <div class="descbody">%(descr)s</div>
        %(detailhtml)s
      </div>
      <div class="tab-panel" id="tab-info">
        <table class="info-table">
          <tr><th>상품명</th><td>%(name)s</td></tr>
          <tr><th>분류</th><td>%(catlabel)s</td></tr>
          <tr><th>판매</th><td>맵달서울성수 · MAPDAL SEOUL (성수)</td></tr>
          %(inforows)s
        </table>
      </div>
      <div class="tab-panel" id="tab-qa">
        <table class="info-table">
          <tr><th>국내배송</th><td>3,000원 (30,000원 이상 무료) · 오후 2시 이전 결제 시 당일 출고</td></tr>
          <tr><th>맵달드림</th><td>서울 당일배송 · 성수 1F/4F 픽업</td></tr>
          <tr><th>교환/반품</th><td>미개봉·미사용에 한해 수령 7일 이내 · 신선식품 및 개봉 상품은 불가</td></tr>
          <tr><th>해외배송</th><td>DDP(관·부가세 포함) 지원 — global@mealzip.kr 문의</td></tr>
        </table>
      </div>
    </div>
  </div>
  <div class="buy">
    <div class="badges">%(badges)s</div>
    <div class="brand">%(brand)s</div>
    <h1>%(name)s</h1>
    <div class="price-block">%(pricehtml)s</div>
    <div class="stock-line %(bcls)s">%(bmsg)s</div>
    <div class="viewers"><span id="vCount">%(viewers)d</span>명이 보고 있어요</div>
    <div class="qty-row"><span style="font-size:13px;font-weight:600">수량</span>
      <div class="qty-ctl"><button id="qm">−</button><span id="qv">1</span><button id="qp">＋</button></div></div>
    <div class="total-row"><span class="tl">총 상품 금액</span><span class="tv" id="pTot">₩%(price_fmt)s</span></div>
    <div class="buy-btns">
      <button class="btn cartb" id="btnCart">장바구니</button>
      <button class="btn red" id="btnBuy">바로구매</button>
    </div>
    <div class="like-row">
      <button id="likeBtn" onclick="toggleLike()">&#9825; 좋아요</button>
      <button id="rsBtn" class="rs" onclick="toggleRestock()">재입고 알림 신청</button>
    </div>
    <div class="ben-acc">%(benefits)s</div>
  </div>
</div>
<div class="qna-wrap">
  <h2>상품 Q&amp;A</h2>
  <div id="qnaList" style="font-size:13px;color:#999;padding:6px 2px">불러오는 중…</div>
  <button class="qna-ask" onclick="askQ()">상품 문의하기</button>
  <div style="font-size:11px;color:#999;margin-top:8px">문의 답변은 마이페이지 &gt; 상품 Q&amp;A 내역에서도 확인할 수 있습니다.</div>
</div>
<footer><div class="foot-inner">
  <div class="logo">MAPDAL<em>SEOUL</em></div>
  <p>Not a store, A stage. 성수동에서 전 세계 팬에게 — Shop Seongsu, from Anywhere.</p>
  <div class="foot-links"><a href="/shop">SHOP</a><a href="/kpop">KPOP</a><a href="/mapdal-seoul">MAPDAL SEOUL</a><a href="/support">SUPPORT</a><a href="/shipping">배송안내</a><a href="/returns">교환/반품</a></div>
</div>
<div class="foot-base"><a href="/terms" style="color:#fff">이용약관</a> · <a href="/privacy">개인정보처리방침</a> &nbsp;&nbsp; © 2026 MEAL ZIP INC. · MAPDAL SEOUL<br>
맵달서울성수 · 대표: 황인범, 김동경 · 서울 성동구 성수이로16길 5 · 사업자등록번호: 394-85-03267 · 통신판매업신고: 제2026-서울성동-0426호 · 고객센터: ceo@mealzip.kr</div>
</footer>
<script>
var PID=%(pidjs)s, PRICE=%(pricejs)d, PNAME=%(namejs)s, PIMG=%(imgjs)s, SOLD=%(soldjs)s, STOCK=%(stockjs)d;
var ST={login:false,liked:false,restock:false};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function fmt(n){return '₩'+Number(n).toLocaleString('ko-KR')}
function swapMain(src){var g=document.getElementById('galMain');var im=g.querySelector('img');if(im){im.src=src}else{g.querySelector('.ph-word')&&(g.querySelector('.ph-word').outerHTML='<img src="'+esc(src)+'" alt="">')}
 g.querySelectorAll('.gal-thumbs button');document.querySelectorAll('.gal-thumbs button').forEach(function(b){b.classList.toggle('on',b.dataset.src===src)})}
// 수량·합계
var q=1;
function updTot(){document.getElementById('qv').textContent=q;document.getElementById('pTot').textContent=fmt(PRICE*q)}
document.getElementById('qp').onclick=function(){q=Math.min(99,q+1);updTot()};
document.getElementById('qm').onclick=function(){q=Math.max(1,q-1);updTot()};
if(SOLD){document.getElementById('btnCart').disabled=true;document.getElementById('btnBuy').disabled=true}
// 장바구니 (localStorage mapdal_cart — 기존 결제 파이프라인과 동일 규격)
var CK='mapdal_cart';
function ldc(){try{return JSON.parse(localStorage.getItem(CK)||'[]')}catch(e){return[]}}
function svc(a){try{localStorage.setItem(CK,JSON.stringify(a))}catch(e){}}
function cbadge(){var c=ldc().reduce(function(a,i){return a+i.q},0);var el=document.getElementById('cartBadge');if(el)el.textContent='CART'+(c?' · '+c:'')}
function addItem(){var items=ldc();var ex=items.find(function(i){return i.id===PID});
 if(ex){ex.q=Math.min(99,ex.q+q)}else{items.push({id:PID,n:PNAME,p:PRICE,q:q,img:PIMG,u:'/p/'+encodeURIComponent(PID)})}
 svc(items);cbadge()}
document.getElementById('btnCart').onclick=function(){if(SOLD)return;addItem();location.href='/cart'};
document.getElementById('btnBuy').onclick=function(){if(SOLD)return;addItem();location.href='/checkout'};
cbadge();
// 뷰어 카운터
var v=%(viewers)d;setInterval(function(){v=Math.max(12,v+Math.floor(Math.random()*9)-4);var el=document.getElementById('vCount');if(el)el.textContent=v},4000);
// 탭·아코디언
document.querySelectorAll('.tab-bar button').forEach(function(b){b.addEventListener('click',function(){
 document.querySelectorAll('.tab-bar button').forEach(function(x){x.classList.toggle('on',x===b)});
 document.querySelectorAll('.tab-panel').forEach(function(p){p.classList.toggle('on',p.id==='tab-'+b.dataset.tab)})})});
document.querySelectorAll('.ben-row .bh').forEach(function(h){h.addEventListener('click',function(){h.parentElement.classList.toggle('open')})});
// 로그인 상태 헤더
fetch('/api/member/me').then(function(r){return r.json()}).catch(function(){return{login:false}}).then(function(d){
 var a=document.getElementById('mpAuth');if(a)a.textContent=(d&&d.login)?('MY · '+(d.name||'회원')):'로그인'});
// 좋아요·재입고
function paint(){var lb=document.getElementById('likeBtn');
 lb.innerHTML=(ST.liked?'&#9829; 좋아요 취소':'&#9825; 좋아요');
 lb.style.background=ST.liked?'#141414':'#fff';lb.style.color=ST.liked?'#FFB000':'#141414';
 var rb=document.getElementById('rsBtn');if(SOLD){rb.style.display='block';rb.textContent=ST.restock?'재입고 알림 신청됨 (해제)':'재입고 알림 신청'}}
fetch('/api/member/pdp-state?product_id='+encodeURIComponent(PID)).then(function(r){return r.json()}).then(function(d){ST=d;paint()}).catch(function(){paint()});
function needLogin(){if(confirm('로그인이 필요합니다. 로그인 페이지로 이동할까요?'))location.href='/account'}
function post(u,b,cb){fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
 .then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.detail||'오류');return j})}).then(cb).catch(function(e){alert(e.message)})}
function toggleLike(){if(!ST.login)return needLogin();post('/api/member/likes',{product_id:PID,on:!ST.liked},function(){ST.liked=!ST.liked;paint()})}
function toggleRestock(){if(!ST.login)return needLogin();post('/api/member/restock',ST.restock?{product_id:PID,off:true}:{product_id:PID},function(j){ST.restock=!!j.on;paint()})}
function askQ(){if(!ST.login)return needLogin();var qq=prompt('상품에 대해 궁금한 점을 남겨주세요');if(!qq)return;
 post('/api/member/pqna',{product_id:PID,question:qq},function(){alert('문의가 접수되었습니다. 답변은 마이페이지에서 확인하세요.')})}
fetch('/api/pqna?product_id='+encodeURIComponent(PID)).then(function(r){return r.json()}).then(function(d){
 var el=document.getElementById('qnaList');
 if(!d.rows.length){el.textContent='아직 등록된 문의가 없습니다.';return}
 el.innerHTML=d.rows.map(function(x){return '<div style="border-bottom:1px solid #eee;padding:10px 2px;color:#141414">'+
 '<div style="font-weight:700">Q. '+esc(x.q)+' <span style="color:#aaa;font-weight:400;font-size:11px">'+esc(x.name)+' · '+esc(x.at)+'</span></div>'+
 '<div style="margin-top:6px;background:#faf9f5;padding:9px;white-space:pre-wrap">A. '+esc(x.a)+'</div></div>'}).join('')}).catch(function(){});
</script>%(relatedsnippet)s</body></html>'''

@admin_router.get('/p/{pid:path}', response_class=HTMLResponse)
def pdp(pid: str):
    try: ensure_ready()
    except Exception: pass
    if not _state['pcols']: raise HTTPException(404)
    sel = 'id, %s AS name, stock, soldout' % (_state['pname'] or 'id')
    if _state['pprice']: sel += ', %s AS price' % _state['pprice']
    for c in ('img', 'descr', 'category', 'detail_html', 'gallery', 'list_price'):
        if c in _state['pcols']: sel += ', ' + c
    r = one('SELECT %s FROM products WHERE id=?' % sel, (pid,))
    if not r:
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>상품을 찾을 수 없습니다</h2><a href="/shop">SHOP으로</a>', status_code=404)
    def h(x): return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    soldout = num(r.get('soldout')) or num(r.get('stock')) <= 0
    img = (r.get('img') or '').strip()
    # 카테고리 칩 → 해당 목록으로 이동 (앨범은 KPOP(음반) 전용관 직행)
    cat = norm_cat(r.get('category'))
    _cu = '/kpop' if cat == 'album' else ('/shop?cat=' + cat)
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
    # 통일 규격: 정가(취소선) 폐기 — 할인율(빨강)·할인가만 표기
    # ── 가격 (리치 price-block: 할인율%+가격) ──
    sale, was = num(r.get('price')), num(r.get('list_price'))
    pct = derived_pct(was, sale)
    if sale <= 0:
        pricehtml = '<div class="now"><span class="amt">가격 문의</span></div>'
    elif pct:
        pricehtml = ('<div class="now"><span class="pct">%d%%</span>'
                     '<span class="amt">₩%s</span></div>' % (pct, format(sale, ',')))
    else:
        pricehtml = '<div class="now"><span class="amt">₩%s</span></div>' % format(sale, ',')
    # SEO — canonical(own→정적 페이지 · k2g→앨범상세 · mp→자기 자신) + Product 스키마
    if pid.startswith('k2g::'):
        canon = '%s/album-detail?uid=%s' % (SITE_ORIGIN, pid[5:])
    elif '::' in pid and not pid.startswith('mp::'):
        canon = SITE_ORIGIN + '/' + pid.split('::')[0].replace('.html', '')
    else:
        canon = SITE_ORIGIN + '/p/' + pid
    sdesc = re.sub(r'\s+', ' ', str(r.get('descr') or '')).strip()[:160] or (
        '%s — MAPDAL SEOUL 성수 공식 온라인 스토어. 국내·해외배송(DDP) 지원.' % str(r.get('name') or '')[:60])
    img_abs = img if img.startswith('http') else ((SITE_ORIGIN + img) if img.startswith('/') else OG_IMAGE_URL)
    prod_ld = {'@context': 'https://schema.org', '@type': 'Product',
               'name': str(r.get('name') or ''), 'image': img_abs, 'description': sdesc,
               'url': canon, 'brand': {'@type': 'Brand', 'name': 'MAPDAL SEOUL'}}
    if sale > 0:
        prod_ld['offers'] = {'@type': 'Offer', 'priceCurrency': 'KRW', 'price': sale,
                             'availability': _seo_avail(soldout), 'url': canon}
    _su, _sn = _seo_section_of(cat)
    seohtml = ('\n<meta name="description" content="%s">' % h(sdesc)
               + '<link rel="canonical" href="%s">' % canon
               + '<meta property="og:url" content="%s">' % canon
               + '<meta property="og:locale" content="ko_KR">'
               + _jsonld(prod_ld)
               + _jsonld(_seo_breadcrumb(_su, _sn, str(r.get('name') or '')[:60], canon)))
    # ── 카테고리별 메타 (뱃지·브랜드라인·혜택 아코디언) ──
    badges_html, brand_line, benefits_html = _pdp_meta_html(cat)
    # 카테고리별 flavor 태그 + 구매정보 탭 추가행
    _FLAVOR = {'album': 'ALBUM', 'md': 'OFFICIAL MD', 'kfood': 'K-FOOD',
               'apparel': 'APPAREL', 'living': 'LIFESTYLE'}
    flavor = _FLAVOR.get(cat, 'MAPDAL')
    if cat == 'album':
        inforows = ('<tr><th>형태</th><td>음반 (CD) — 구성은 상세 참조</td></tr>'
                    '<tr><th>발매/공급</th><td>912엔터테인먼트 (KPOP2GETHER)</td></tr>'
                    '<tr><th>차트 반영</th><td>본 스토어 판매량은 한터차트에 집계됩니다</td></tr>'
                    '<tr><th>랜덤 구성</th><td>버전/포토카드 랜덤 상품은 선택 불가 · 중복 발송 가능</td></tr>')
    elif cat == 'kfood':
        inforows = ('<tr><th>보관</th><td>콜드체인 · 수령 후 냉장/냉동 보관</td></tr>'
                    '<tr><th>배송</th><td>보냉 포장 · 신선 배송</td></tr>'
                    '<tr><th>안내</th><td>상세 설명의 원산지·알레르기 정보 확인</td></tr>')
    elif cat == 'apparel':
        inforows = ('<tr><th>사이즈</th><td>상세 설명의 실측 사이즈 표 참조</td></tr>'
                    '<tr><th>소재/세탁</th><td>상세 설명 참조</td></tr>')
    elif cat == 'living':
        inforows = '<tr><th>구성품</th><td>상세 설명 참조</td></tr>'
    else:
        inforows = '<tr><th>구성</th><td>상세 설명 참조</td></tr>'
    # 뷰어수(상품 id 기반 안정값 60~139)
    try:
        _seed = int(re.sub(r'\D', '', pid)[-4:] or '0')
    except Exception:
        _seed = 0
    viewers = 60 + (_seed % 80)
    _img_og = img if img.startswith('http') else (('https://mapdal.kr' + img) if img.startswith('/') else OG_IMAGE_URL)
    return HTMLResponse(_brand_apply(_PDP_HTML % {
        'name': h(r.get('name')), 'namejs': json.dumps(str(r.get('name') or '')),
        'pricehtml': pricehtml, 'price_fmt': format(sale, ','), 'pricejs': sale,
        'bcls': 'no' if soldout else 'ok',
        'bmsg': '품절 (SOLD OUT)' if soldout else '구매 가능 · 재고 %d' % num(r.get('stock')),
        'stockjs': num(r.get('stock')),
        'descr': h(r.get('descr')) or 'MAPDAL SEOUL 상품입니다.',
        'imgtag': ('<img src="%s" alt="">' % h(img)) if img else '<span class="ph-word">MAPDAL SEOUL</span>',
        'imgjs': json.dumps(img),
        'og': ('<meta property="og:image" content="%s"><meta name="twitter:card" content="summary_large_image">' % h(_img_og)),
        'seo': seohtml,
        'caturl': h(_cu or '/shop'), 'catlabel': h(_cl or 'SHOP'),
        'flavor': flavor, 'badges': badges_html, 'brand': h(brand_line),
        'benefits': benefits_html, 'viewers': viewers,
        'galhtml': galhtml, 'detailhtml': detailhtml, 'inforows': inforows,
        'pidjs': json.dumps(pid), 'soldjs': 'true' if soldout else 'false',
        'relatedsnippet': _RELATED_WIDGET_SNIPPET}))

# ═══════════════════ ⑥ 소셜 회원가입 (Google / Apple) ════════════════════
SIGNUP_BONUS = 2000
TERMS_VERSION = '2026-07-15'
PRIVACY_VERSION = '2026-07-15'

def _burl(request: Request):
    # OAuth redirect URI는 Host 헤더가 아니라 운영 기준 URL을 사용한다.
    return (_genv('SITE_ORIGIN') or _from_app('SITE_ORIGIN', 'https://mapdal.kr')).rstrip('/')

def _mask_email(v):
    s = (v or '').strip()
    if '@' not in s: return s[:2] + ('*' if len(s) > 2 else '')
    a, b = s.split('@', 1)
    return (a[:2] + '*' * max(1, len(a) - 2)) + '@' + b

def _mask_phone(v):
    d = kphone_norm(v)
    if len(d) >= 10: return d[:3] + '-****-' + d[-4:]
    return '*' * len(d)

def account_security(member, event_type, request=None, detail=''):
    if not member: return
    try:
        run('INSERT INTO account_security_events VALUES(?,?,?,?,?,?,?,?)',
            (uid(), member.get('customer_id') or '', member.get('id') or '', event_type,
             ((request.client.host if request and request.client else '') or '-')[:80],
             ((request.headers.get('user-agent') if request else '') or '')[:240],
             str(detail or '')[:300], now_iso()))
    except Exception: pass

def point_apply(customer_id, member_id, event_type, amount, event_key, reason='', order_id='', by_admin='', expires_at=''):
    """포인트 불변원장. 현재 활성 자동정책은 SIGNUP_BONUS 한 종류뿐이다."""
    if not customer_id or not event_key: raise ValueError('customer/event required')
    amount = int(amount or 0)
    with _conn() as c:
        ex = c.execute(_q('SELECT balance_after FROM point_ledger WHERE event_key=?'), (event_key,)).fetchone()
        if ex: return num(dict(ex).get('balance_after'))
        lock = ' FOR UPDATE' if IS_PG else ''
        r = c.execute(_q('SELECT points_balance FROM customer_profiles WHERE id=?' + lock), (customer_id,)).fetchone()
        if not r: raise ValueError('customer not found')
        cur = num(dict(r).get('points_balance'))
        nxt = max(0, cur + amount)
        actual = nxt - cur
        c.execute(_q('UPDATE customer_profiles SET points_balance=?, updated_at=? WHERE id=?'), (nxt, now_iso(), customer_id))
        c.execute(_q('INSERT INTO point_ledger(id,customer_id,member_id,event_type,amount,balance_after,event_key,order_id,reason,expires_at,created_at,by_admin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'),
                  (uid(), customer_id, member_id or '', event_type, actual, nxt, event_key,
                   order_id or '', str(reason or '')[:200], expires_at or '', now_iso(), by_admin or ''))
        c.commit()
    try: run('UPDATE members SET points=? WHERE customer_id=?', (nxt, customer_id))
    except Exception: pass
    return nxt

def consent_record(customer_id, member_id, consent_type, granted, version, source, request=None):
    run('INSERT INTO consent_history VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(), customer_id, member_id, consent_type, version, 1 if granted else 0,
         source, ((request.client.host if request and request.client else '') or '-')[:80], now_iso()))
    if consent_type == 'MARKETING':
        try: run('UPDATE customer_profiles SET marketing_ok=?, updated_at=? WHERE id=?',
                 (1 if granted else 0, now_iso(), customer_id))
        except Exception: pass

def consent_state(customer_id):
    out = {}
    for r in rows('SELECT consent_type, policy_version, granted, created_at FROM consent_history WHERE customer_id=? ORDER BY created_at DESC', (customer_id,)):
        if r['consent_type'] not in out: out[r['consent_type']] = r
    return out

def customer_ensure(member, grant_signup=True):
    """기존 members 행을 보존하면서 단일 고객 프로필·인증수단·연락처를 백필한다."""
    if not member: return None
    cid = member.get('customer_id') or ''
    if cid and one('SELECT id FROM customer_profiles WHERE id=?', (cid,)):
        return cid
    cid = uid()
    cno = 'CUS-%s-%s' % (datetime.datetime.now().strftime('%Y'), cid[:8].upper())
    ts = member.get('created') or now_iso()
    run('INSERT INTO customer_profiles(id,customer_no,name,status,grade,points_balance,marketing_ok,created_at,updated_at,withdrawn_at) VALUES(?,?,?,?,?,0,0,?,?,?)',
        (cid, cno, member.get('name') or '', 'ACTIVE', 'WELCOME', ts, now_iso(), ''))
    run("UPDATE members SET customer_id=?, status=COALESCE(NULLIF(status,''),'ACTIVE'), updated_at=? WHERE id=?",
        (cid, now_iso(), member['id']))
    provider = member.get('provider') or 'email'; sub = member.get('sub') or member.get('email') or member['id']
    try:
        run('INSERT INTO auth_identities(id,customer_id,member_id,provider,provider_sub,email_norm,email_verified,created_at,last_login_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(), cid, member['id'], provider, sub, (member.get('email') or '').strip().lower(),
             num(member.get('email_verified')) or (1 if provider in ('google','kakao','apple') else 0), ts, member.get('last_login_at') or ''))
    except Exception: pass
    email = (member.get('email') or '').strip().lower()
    if email:
        try: run('INSERT INTO customer_contacts VALUES(?,?,?,?,?,?,?,?,?)',
                 (uid(), cid, 'EMAIL', email, email, num(member.get('email_verified')) or (1 if provider in ('google','kakao','apple') else 0), 1, ts, ts if provider in ('google','kakao','apple') else ''))
        except Exception: pass
    phone = kphone_norm(member.get('phone') or '')
    if phone:
        try: run('INSERT INTO customer_contacts VALUES(?,?,?,?,?,?,?,?,?)',
                 (uid(), cid, 'PHONE', phone, phone, num(member.get('phone_verified')), 1, ts, ts if num(member.get('phone_verified')) else ''))
        except Exception: pass
    legacy = num(member.get('points'))
    if legacy > 0:
        point_apply(cid, member['id'], 'LEGACY_BALANCE', legacy, 'legacy:%s' % member['id'], '기존 잔액 이관')
    elif grant_signup:
        point_apply(cid, member['id'], 'SIGNUP_BONUS', SIGNUP_BONUS, 'signup:%s' % cid, '최초 가입 혜택')
    return cid

def guest_customer_ensure(name, phone):
    p=kphone_norm(phone)
    if len(p)<9: return ''
    # 비회원 주문을 전화번호가 같은 회원에게 자동 귀속하지 않는다. 동일 번호는 OTP
    # 인증 전까지 GUEST 그룹 안에서만 재사용한다.
    ex=one("SELECT cc.customer_id FROM customer_contacts cc JOIN customer_profiles c ON c.id=cc.customer_id WHERE cc.kind='PHONE' AND cc.value_norm=? AND c.status='GUEST'",(p,))
    if not ex:
        ex=one("SELECT o.customer_id FROM orders o JOIN customer_profiles c ON c.id=o.customer_id WHERE o.contact_phone_norm=? AND c.status='GUEST' ORDER BY o.created DESC LIMIT 1",(p,))
    if ex: return ex['customer_id']
    cid=uid(); cno='CUS-%s-%s' % (datetime.datetime.now().strftime('%Y'),cid[:8].upper())
    run('INSERT INTO customer_profiles(id,customer_no,name,status,grade,points_balance,marketing_ok,created_at,updated_at,withdrawn_at) VALUES(?,?,?,?,?,0,0,?,?,?)',
        (cid,cno,(name or '')[:40],'GUEST','WELCOME',now_iso(),now_iso(),''))
    try: run('INSERT INTO customer_contacts VALUES(?,?,?,?,?,?,?,?,?)',(uid(),cid,'PHONE',p,p,0,1,now_iso(),''))
    except Exception:
        # 동일 번호가 ACTIVE 계정에 있으면 GUEST 연락처를 만들지 않고 주문의
        # contact_phone_norm에만 보관한다. 인증 후에만 계정으로 병합된다.
        return cid
    return cid

def _account_migrate():
    # 현재 정책은 최초 가입 2,000P 한 종류만 남긴다. 정리 마이그레이션은 한 번만
    # 실행하므로, 향후 관리자가 새 정책을 추가해도 재시작 때 다시 삭제되지 않는다.
    try:
        marker=one("SELECT value FROM site_settings WHERE key='POINT_POLICY_SIGNUP_ONLY_V1'")
        if not marker:
            run("DELETE FROM loyalty_policies WHERE key<>'SIGNUP_BONUS'")
            if one("SELECT key FROM loyalty_policies WHERE key='SIGNUP_BONUS'"):
                run("UPDATE loyalty_policies SET enabled=1,value=?,effective_from=?,updated_at=?,by_admin='SYSTEM' WHERE key='SIGNUP_BONUS'",
                    (str(SIGNUP_BONUS),now_iso(),now_iso()))
            else:
                run('INSERT INTO loyalty_policies VALUES(?,?,?,?,?,?)', ('SIGNUP_BONUS',1,str(SIGNUP_BONUS),now_iso(),now_iso(),'SYSTEM'))
            run("INSERT INTO site_settings(key,value,updated,by_admin) VALUES(?,?,?,?)",('POINT_POLICY_SIGNUP_ONLY_V1','done',now_iso(),'SYSTEM'))
    except Exception: pass
    # /account는 전용 회원 라우트가 서빙한다. CMS에 남은 구형 account.html이
    # 5%·래플·픽업 혜택을 다시 노출하지 않도록 호환 리디렉션으로 멱등 전환한다.
    try:
        legacy_account = one("SELECT html FROM page_edits WHERE path='account.html'")
        if legacy_account:
            shim_path = os.path.join(STATIC_DIR, 'account.html')
            shim = open(shim_path, 'r', encoding='utf-8').read()
            if (legacy_account.get('html') or '') != shim:
                run("UPDATE page_edits SET html=?,updated=?,by_admin='SYSTEM_ACCOUNT_ROUTE' WHERE path='account.html'",
                    (shim, now_iso()))
    except Exception: pass
    for sql in ('CREATE INDEX IF NOT EXISTS idx_members_customer ON members(customer_id)',
                'CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, created)',
                'CREATE INDEX IF NOT EXISTS idx_auth_identities_customer ON auth_identities(customer_id)',
                'CREATE INDEX IF NOT EXISTS idx_point_ledger_customer ON point_ledger(customer_id, created_at)'):
        try: run(sql)
        except Exception: pass
    for m in rows('SELECT * FROM members WHERE customer_id IS NULL OR customer_id=\'\''):
        customer_ensure(m, True)
    for table in ('member_addresses','member_likes','member_restock','member_requests','member_inquiries','member_pqna'):
        try: run("UPDATE %s SET customer_id=(SELECT customer_id FROM members WHERE members.id=%s.member_id) WHERE customer_id IS NULL OR customer_id=''" % (table,table))
        except Exception: pass
    # 기존 비회원 주문은 게스트 고객으로만 묶고, 회원에게는 OTP 확인 전 자동 귀속하지 않는다.
    try:
        for o in rows("SELECT order_id,buyer FROM orders WHERE customer_id IS NULL OR customer_id='' ORDER BY created"):
            b=jload(o.get('buyer'),{}); ph=kphone_norm(b.get('phone') or '')
            cid=guest_customer_ensure(b.get('name') or '',ph)
            if not cid: continue
            run('UPDATE orders SET customer_id=?,contact_phone_norm=? WHERE order_id=?',(cid,ph,o['order_id']))
            try: run('INSERT INTO account_order_links VALUES(?,?,?,?,?,?)',(o['order_id'],cid,'','LEGACY_GUEST',now_iso(),''))
            except Exception: pass
    except Exception: pass
    # 환불계좌는 상시 보관하지 않는다. 주문 전인 현 단계에서 기존 평문 값을 안전하게 제거한다.
    try: run("UPDATE members SET bank='',acct='',acct_name='' WHERE COALESCE(bank,'')<>'' OR COALESCE(acct,'')<>'' OR COALESCE(acct_name,'')<>''")
    except Exception: pass
    # 관리자 CMS에 저장된 과거 화면도 구매적립/래플 추가적립 문구를 남기지 않는다.
    try:
        for r in rows('SELECT path,html FROM page_edits'):
            old=r.get('html') or ''; new=old
            new=new.replace('맵달APP 첫 구매 2,000P</span> · 포인트 <b>5% 적립</b>','최초 가입 2,000P</span>')
            new=new.replace('맵달APP 첫 구매 2,000P</span> · 5% 적립','최초 가입 2,000P</span>')
            new=new.replace('맵달APP 가입 즉시 2,000P 지급 · 구매 금액의 5% 적립 · 래플 응모 이력 보유 시 추가 2% (자동 적용)','한 고객당 최초 가입 시 한 번만 지급됩니다')
            new=new.replace('맵달APP 가입 즉시 2,000P 지급 · 구매 금액의 5% 적립 · 래플 응모 이력 보유 시 추가 2%','한 고객당 최초 가입 시 한 번만 지급됩니다')
            new=new.replace('맵달APP 가입 즉시 2,000P 지급 · 구매 금액의 5% 적립','한 고객당 최초 가입 시 한 번만 지급됩니다')
            new=new.replace('래플 응모 이력 보유 시 추가 2% 적립','한 고객당 최초 가입 시 한 번만 지급됩니다')
            new=new.replace('공고일: 2026년 7월 7일 · 시행일: 2026년 7월 7일','공고일: 2026년 7월 15일 · 시행일: 2026년 7월 15일')
            new=new.replace('시행일: 2026년 7월 7일','시행일: 2026년 7월 15일')
            new=new.replace('이 약관은 2026년 7월 7일부터 시행합니다.','이 약관은 2026년 7월 15일부터 시행합니다.')
            new=new.replace('이메일 또는 카카오·Google 계정 연동','이메일 또는 카카오·Google·Apple 계정 연동')
            new=new.replace('토스페이먼츠 주식회사</td><td>전자결제(결제 승인·취소) 처리','케이지이니시스</td><td>전자결제(결제 승인·취소) 처리')
            new=new.replace('대금 결제는 토스페이먼츠를 통한','대금 결제는 케이지이니시스를 통한')
            new=new.replace('현재 결제 시 사용 기능은 준비 중입니다. 포인트는 현금으로 환급되지 않으며 회원 탈퇴 시 소멸합니다.','현재 자동 지급 정책은 고객당 최초 가입 시 2,000P 1회 제공뿐이며, 구매·래플 적립과 결제 사용은 운영하지 않습니다. 포인트는 현금으로 환급되지 않으며 회원 탈퇴 시 소멸합니다.')
            new=new.replace('<tr><td>회원가입(필수)</td><td>이름, 성별, 연령대, 생년월일, 휴대폰 번호, 이메일 주소, 비밀번호(이메일 가입 시, 일방향 암호화 저장)</td><td>회원 식별·관리, 주문내역 연동, 본인 확인, 연령·성별 기반 상품 추천 및 통계, 생일 혜택 제공</td><td>회원가입 화면, 카카오·Google 계정 연동(동의 항목에 한함)</td></tr>',
                            '<tr><td>회원가입(필수)</td><td>이름, 이메일 주소, 비밀번호(이메일 가입 시, 일방향 암호화 저장), 이용약관·개인정보 동의 이력</td><td>회원 식별·관리, 로그인, 고객 문의 처리, 최초 가입 혜택 제공</td><td>회원가입 화면, 카카오·Google·Apple 계정 연동(동의 항목에 한함)</td></tr>')
            new=new.replace('<tr><td>선택</td><td>배송지 정보(수령인, 주소, 연락처), 환불계좌(은행·계좌번호·예금주), 마케팅 수신 동의 여부</td><td>배송지 자동 입력 편의, 환불 처리, 이벤트·혜택 안내</td><td>마이페이지, 카카오 배송지 연동(동의 시)</td></tr>',
                            '<tr><td>회원정보(선택)</td><td>성별, 생년월일, 휴대폰 번호, 배송지 정보(수령인, 주소, 연락처), 마케팅 수신 동의 여부</td><td>휴대폰 본인확인·계정복구·기존 주문 연결, 배송지 자동 입력, 이벤트·신상품 안내</td><td>회원가입 화면, 마이페이지, 카카오 배송지 연동(동의 시)</td></tr>')
            if new!=old: run('UPDATE page_edits SET html=?,updated=?,by_admin=? WHERE path=?',(new,now_iso(),'SYSTEM_POLICY',r['path']))
    except Exception: pass

def member_session_make(mid, request=None):
    sid = secrets.token_urlsafe(24)
    exp = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat(timespec='seconds')
    try: run('DELETE FROM member_sessions WHERE expires < ?', (now_iso(),))
    except Exception: pass
    run('INSERT INTO member_sessions(id,member_id,created,expires,ip,user_agent,last_seen) VALUES(?,?,?,?,?,?,?)',
        (hashlib.sha256(sid.encode()).hexdigest(), mid, now_iso(), exp,
         ((request.client.host if request and request.client else '') or '-')[:80],
         ((request.headers.get('user-agent') if request else '') or '')[:240], now_iso()))
    return sid

def member_of(request: Request):
    sid = request.cookies.get('mp_member') or ''
    if not sid: return None
    s = one('SELECT * FROM member_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
    if not s or (s.get('expires') or '') <= now_iso(): return None
    m = one('SELECT * FROM members WHERE id=?', (s['member_id'],))
    if not m or (m.get('status') or 'ACTIVE') != 'ACTIVE': return None
    if not m.get('customer_id'):
        try:
            customer_ensure(m, True); m = one('SELECT * FROM members WHERE id=?', (s['member_id'],))
        except Exception: pass
    if m and m.get('customer_id'):
        profile = one('SELECT status FROM customer_profiles WHERE id=?', (m['customer_id'],)) or {}
        if (profile.get('status') or 'ACTIVE') != 'ACTIVE': return None
    try: run('UPDATE member_sessions SET last_seen=? WHERE id=?', (now_iso(), s['id']))
    except Exception: pass
    return m

def kphone_norm(p):
    d = digits(p)
    if d.startswith('82'): d = '0' + d[2:]
    return d

def member_upsert(provider, sub, email, name):
    ident = one('SELECT member_id FROM auth_identities WHERE provider=? AND provider_sub=?', (provider, sub))
    row = one('SELECT * FROM members WHERE id=?', (ident['member_id'],)) if ident else one('SELECT * FROM members WHERE provider=? AND sub=?', (provider, sub))
    if row:
        run('UPDATE members SET email=COALESCE(NULLIF(?, \'\'), email), name=COALESCE(NULLIF(?, \'\'), name), email_verified=1, last_login_at=?, updated_at=? WHERE id=?',
            (email or '', name or '', now_iso(), now_iso(), row['id']))
        cid = customer_ensure(one('SELECT * FROM members WHERE id=?', (row['id'],)), True)
        try: run('UPDATE auth_identities SET last_login_at=?, email_norm=?, email_verified=1 WHERE member_id=?',
                 (now_iso(), (email or '').strip().lower(), row['id']))
        except Exception: pass
        return row['id'], False
    mid = uid()
    run('INSERT INTO members(id,provider,sub,email,name,created,email_verified,last_login_at,status,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)',
        (mid, provider, sub, email or '', name or '', now_iso(), now_iso(), 'ACTIVE', now_iso()))
    customer_ensure(one('SELECT * FROM members WHERE id=?', (mid,)), True)
    return mid, True

def oauth_flow_start(request, state, provider):
    m=member_of(request); action='LINK' if request.query_params.get('link')=='1' and m else 'LOGIN'
    exp=(datetime.datetime.utcnow()+datetime.timedelta(minutes=10)).isoformat(timespec='seconds')
    try: run('DELETE FROM oauth_flows WHERE expires_at<? OR used=1',(now_iso(),))
    except Exception: pass
    run('INSERT INTO oauth_flows VALUES(?,?,?,?,?,?,0)',(state,m['id'] if action=='LINK' else '',provider,action,now_iso(),exp))

def oauth_member_finish(state, provider, sub, email, name):
    flow=one('SELECT * FROM oauth_flows WHERE state=? AND provider=? AND used=0',(state,provider))
    if not flow or (flow.get('expires_at') or '')<=now_iso(): raise HTTPException(400,'인증 연결 요청이 만료되었습니다')
    run('UPDATE oauth_flows SET used=1 WHERE state=?',(state,))
    if flow.get('action')!='LINK':
        mid,is_new=member_upsert(provider,sub,email,name); return mid,is_new,False
    current=one('SELECT * FROM members WHERE id=?',(flow.get('member_id'),))
    if not current or not current.get('customer_id'): raise HTTPException(400,'연결할 계정 세션이 만료되었습니다')
    ex=one('SELECT customer_id,member_id FROM auth_identities WHERE provider=? AND provider_sub=?',(provider,sub))
    if ex and ex.get('customer_id')!=current.get('customer_id'):
        raise HTTPException(409,'이 로그인 방법은 이미 다른 계정에 연결되어 있습니다')
    if not ex:
        alias=uid()
        run('INSERT INTO members(id,provider,sub,email,name,created,customer_id,email_verified,last_login_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (alias,provider,sub,email or '',name or current.get('name') or '',now_iso(),current['customer_id'],1,now_iso(),'ACTIVE',now_iso()))
        run('INSERT INTO auth_identities(id,customer_id,member_id,provider,provider_sub,email_norm,email_verified,created_at,last_login_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(),current['customer_id'],alias,provider,sub,(email or '').strip().lower(),1,now_iso(),now_iso()))
    return current['id'],False,True

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
    oauth_flow_start(request,state,'google')
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
        mid, is_new, linked = oauth_member_finish(p.get('state'), 'google', str(ui.get('sub', '')), ui.get('email', ''), ui.get('name', ''))
        sid = member_session_make(mid, request)
        account_security(one('SELECT * FROM members WHERE id=?', (mid,)), 'IDENTITY_LINKED' if linked else ('SIGNUP' if is_new else 'LOGIN_SUCCESS'), request, 'google')
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
    oauth_flow_start(request,state,'kakao')
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
        mid, is_new, linked = oauth_member_finish(p.get('state'), 'kakao', str(ui.get('id', '')), acct.get('email', '') or '', rname)
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
                    km=one('SELECT customer_id FROM members WHERE id=?',(mid,)) or {}
                    run('INSERT INTO member_addresses(id,member_id,label,rname,phone,zip,addr1,addr2,is_default,created,customer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (uid(), mid, (ad.get('name') or '기본')[:20], (ad.get('receiver_name') or rname)[:30],
                         digits(ad.get('receiver_phone_number1') or kp), str(ad.get('zone_number') or '')[:10],
                         (ad.get('base_address') or '')[:120], (ad.get('detail_address') or '')[:80],
                         1, now_iso(), km.get('customer_id') or ''))
        except Exception:
            pass
        sid = member_session_make(mid, request)
        account_security(one('SELECT * FROM members WHERE id=?', (mid,)), 'IDENTITY_LINKED' if linked else ('SIGNUP' if is_new else 'LOGIN_SUCCESS'), request, 'kakao')
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
    oauth_flow_start(request,state,'apple')
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
        mid, is_new, linked = oauth_member_finish(str(form.get('state')), 'apple', str(claims.get('sub', '')), claims.get('email', ''), name)
        sid = member_session_make(mid, request)
        account_security(one('SELECT * FROM members WHERE id=?', (mid,)), 'IDENTITY_LINKED' if linked else ('SIGNUP' if is_new else 'LOGIN_SUCCESS'), request, 'apple')
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
<div class="r"><a href="/shop">SHOP</a><a href="/cart">장바구니</a><a href="#" onclick="logout()">로그아웃</a></div></header>
<main>
<aside>
 <h1>마이페이지</h1>
 <div class="grp">쇼핑 활동</div>
 <a data-p="orders" class="on" href="#orders">주문/배송 조회</a>
 <a data-p="requests" href="#requests">취소/반품/교환 내역</a>
 <a data-p="receipts" href="#receipts">거래증빙서류 확인</a>
 <a data-p="points" href="#points">포인트 내역</a>
 <a href="/cart">장바구니</a>
 <a data-p="likes" href="#likes">좋아요</a>
 <a data-p="restock" href="#restock">재입고 알림</a>
 <hr><div class="grp">마이 정보</div>
 <a data-p="profile" href="#profile">회원정보 수정</a>
 <a data-p="addr" href="#addr">배송지 관리</a>
 <a data-p="consents" href="#consents">약관·마케팅 동의</a>
 <a data-p="security" href="#security">로그인·보안</a>
 <a data-p="store" href="#store">관심 매장 관리</a>
 <a data-p="withdraw" href="#withdraw">회원탈퇴</a>
 <hr><div class="grp">문의</div>
 <a data-p="inq" href="#inq">1:1 문의내역</a>
 <a data-p="pqna" href="#pqna">상품 Q&amp;A 내역</a>
</aside>
<section>
 <div class="banner"><span class="hi" id="hi"></span><span class="gr" id="gr"></span>
  <div class="sp"><span><a href="#points" style="color:inherit;text-decoration:none">포인트<b id="pt">0P</b></a></span>
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
  return '<div class="st'+(c?' on':'')+'" onclick="location.hash=\'#orders\'" style="cursor:pointer"><b>'+c+'</b><span>'+n+'</span></div>'}).join('');
 if(OV.consent_required){location.hash='#consents'} route()}
const PANES={orders,requests:reqPane,receipts,points:pointsPane,likes:likesPane,restock:restockPane,profile,addr:addrPane,consents:consentPane,security:securityPane,store:storePane,withdraw:withdrawPane,inq:inqPane,pqna:pqnaPane};
function route(){const p=(location.hash||'#orders').slice(1);
 document.querySelectorAll('aside a[data-p]').forEach(a=>a.className=a.dataset.p===p?'on':'');
 (PANES[p]||orders)()}
window.addEventListener('hashchange',route);
async function orders(){
 const d=await api('/api/member/orders?range=3m');
 $('#pane').innerHTML='<div class="panel"><h3>주문/배송 조회 <small style="color:#888;font-weight:400">(최근 3개월)</small></h3>'+
 (d.rows.length?'<table><tr><th>주문번호/일시</th><th>상품</th><th class="r">금액</th><th>상태</th><th></th></tr>'+
 d.rows.map(o=>'<tr><td class="mono" style="font-size:11.5px">'+esc(o.order_id)+'<br><span style="color:#999">'+esc(o.created)+'</span></td>'+
 '<td>'+esc(o.label)+'</td><td class="r mono">'+won(o.amount)+'</td>'+
 '<td><span class="tagst s'+o.step+'">'+esc(o.status_kr)+'</span>'+(o.tracking?'<br><span class="mono" style="font-size:11px;color:#1a5fb4">'+esc(o.tracking)+'</span>':'')+'</td>'+
 '<td><button class="b ghost" onclick="orderDetail(\''+esc(o.order_id)+'\')">상세</button></td></tr>').join('')+'</table>':'<div class="empty">연결된 최근 주문이 없습니다</div>')+'</div>'+
 '<div class="panel"><h3>기존·비회원 주문 연결</h3><div class="hint">주문번호와 주문 당시 휴대폰으로 받은 인증번호를 확인한 후에만 이 계정으로 연결됩니다.</div><div class="row2"><div><label>주문번호</label><input id="coid" placeholder="MD-2026..."></div><div><label>주문 당시 휴대폰</label><input id="cph" placeholder="010-0000-0000"></div></div><div style="margin-top:10px"><button class="b" onclick="claimSend()">인증번호 받기</button></div><div id="claimVerify" style="display:none"><label>인증번호 6자리</label><div style="display:flex;gap:8px"><input id="ccode" maxlength="6"><button class="b red" onclick="claimVerify()" style="white-space:nowrap">주문 연결</button></div></div></div><div id="odetail"></div>'}
let CLAIM_ID='';async function claimSend(){try{const d=await post('/api/member/orders/claim/send',{order_id:$('#coid').value,phone:$('#cph').value});CLAIM_ID=d.claim_id;$('#claimVerify').style.display='block';toast(d.dry?'테스트 모드: 관리자 알림 로그에서 인증번호를 확인하세요':'인증번호를 발송했습니다')}catch(e){toast(e.message)}}
async function claimVerify(){try{await post('/api/member/orders/claim/verify',{claim_id:CLAIM_ID,code:$('#ccode').value});toast('주문이 안전하게 연결되었습니다');orders()}catch(e){toast(e.message)}}
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

async function receipts(){
 const d=await api('/api/member/orders?range=all');const rs=d.rows.filter(o=>o.paid||o.receipt);
 $('#pane').innerHTML='<div class="panel"><h3>거래증빙서류</h3>'+
 (rs.length?'<table><tr><th>주문번호</th><th>일시</th><th class="r">금액</th><th>증빙</th></tr>'+
 rs.map(o=>'<tr><td class="mono" style="font-size:11.5px">'+esc(o.order_id)+'</td><td class="mono">'+esc(o.created)+'</td><td class="r mono">'+won(o.amount)+'</td>'+
 '<td>'+(o.receipt?'<a class="b ghost" target="_blank" href="'+esc(o.receipt)+'">토스 영수증</a> ':'')+
 '<a class="b ghost" target="_blank" href="/api/member/receipt/'+encodeURIComponent(o.order_id)+'">거래명세서</a></td></tr>').join('')+'</table>':'<div class="empty">결제 완료된 주문이 없습니다</div>')+
 '<div class="hint">세금계산서·현금영수증은 결제 시 신청 내역에 따라 토스 영수증에서 확인됩니다.</div></div>'}

async function pointsPane(){const d=await api('/api/member/points');
 $('#pane').innerHTML='<div class="panel"><h3>포인트 <span class="tagst s5">'+d.balance.toLocaleString()+'P</span></h3><div class="hint">현재 자동 지급 정책은 <b>최초 가입 2,000P</b>뿐입니다. 구매 적립과 래플 추가 적립은 운영하지 않습니다.</div>'+
 (d.rows.length?'<table style="margin-top:14px"><tr><th>일시</th><th>구분</th><th>사유</th><th class="r">증감</th><th class="r">잔액</th></tr>'+d.rows.map(x=>'<tr><td class="mono">'+esc(x.created)+'</td><td>'+esc(x.type)+'</td><td>'+esc(x.reason)+'</td><td class="r mono" style="color:'+(x.amount>=0?'#0a7d38':'var(--red)')+'">'+(x.amount>0?'+':'')+x.amount.toLocaleString()+'P</td><td class="r mono">'+x.balance.toLocaleString()+'P</td></tr>').join('')+'</table>':'<div class="empty">포인트 이력이 없습니다</div>')+'</div>'}

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
 '<div class="hint">인증된 번호는 재입고·배송 알림과 본인 확인에 사용됩니다. 기존 주문은 주문번호와 별도 인증번호로 직접 연결합니다.</div>'+
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
 toast('휴대폰 인증이 완료되었습니다');OV=await api('/api/member/overview');boot()}catch(e){toast(e.message)}}

async function addrPane(){const d=await api('/api/member/addresses');
 $('#pane').innerHTML='<div class="panel"><h3>배송지 관리</h3>'+
 (d.rows.length?'<table><tr><th>배송지명</th><th>받는분</th><th>주소</th><th></th></tr>'+
 d.rows.map(a=>'<tr><td><b>'+esc(a.label)+'</b>'+(a.is_default?' <span class="tagst s5">기본</span>':'')+'</td><td>'+esc(a.rname)+'<br><span class="mono" style="font-size:11px">'+esc(a.phone)+'</span></td>'+
 '<td style="font-size:12.5px">['+esc(a.zip)+'] '+esc(a.addr1)+' '+esc(a.addr2)+'</td>'+
 '<td class="r" style="white-space:nowrap">'+(!a.is_default?'<button class="b ghost" onclick="addrAct(\''+a.id+'\',\'default\')">기본설정</button> ':'')+'<button class="b ghost" onclick="addrAct(\''+a.id+'\',\'delete\')">삭제</button></td></tr>').join('')+'</table>':'<div class="empty">등록된 배송지가 없습니다</div>')+
 '<h3 style="margin-top:18px">새 배송지 추가</h3>'+
 '<div class="row2"><div><label>배송지명</label><input id="al" placeholder="집 / 회사"></div><div><label>받는분 *</label><input id="an"></div>'+
 '<div><label>연락처 *</label><input id="ap" placeholder="010-0000-0000"></div><div><label>우편번호 *</label><input id="az"></div></div>'+
 '<label>주소 *</label><input id="a1"><label>상세주소</label><input id="a2">'+
 '<div style="margin-top:12px"><button class="b" onclick="addrAdd()">배송지 추가</button></div><div class="hint">환불계좌는 계정에 상시 저장하지 않습니다. 필요한 환불 건에서만 안전하게 별도 확인합니다.</div></div>'}
async function addrAdd(){try{await post('/api/member/addresses',{label:$('#al').value,rname:$('#an').value,phone:$('#ap').value,zip:$('#az').value,addr1:$('#a1').value,addr2:$('#a2').value});toast('추가되었습니다');addrPane()}catch(e){toast(e.message)}}
async function addrAct(id,act){if(act==='delete'&&!confirm('이 배송지를 삭제할까요?'))return;
 await post('/api/member/addresses',{act,id});addrPane()}

async function consentPane(){const d=await api('/api/member/consents');
 $('#pane').innerHTML='<div class="panel"><h3>약관·개인정보 동의</h3><div class="qa"><div class="q">이용약관 <span class="tagst '+(d.terms?'s2':'s3')+'">'+(d.terms?'동의 완료':'동의 필요')+'</span></div><div class="hint">버전 '+esc(d.terms_version)+' · <a href="/terms" target="_blank">내용 보기</a></div></div><div class="qa"><div class="q">개인정보 수집·이용 <span class="tagst '+(d.privacy?'s2':'s3')+'">'+(d.privacy?'동의 완료':'동의 필요')+'</span></div><div class="hint">버전 '+esc(d.privacy_version)+' · <a href="/privacy" target="_blank">내용 보기</a></div></div>'+
 ((!d.terms||!d.privacy)?'<label style="font-size:13px"><input id="reqConsent" type="checkbox" style="width:auto"> 필수 이용약관과 개인정보 처리 내용을 확인하고 동의합니다.</label><button class="b red" onclick="saveRequiredConsent()">필수 동의 완료</button>':'')+
 '<hr style="border:0;border-top:1px solid var(--line);margin:18px 0"><h3>선택 동의</h3><label style="font-size:13px"><input id="mkConsent" type="checkbox" style="width:auto" '+(d.marketing?'checked':'')+'> 이벤트·신상품·혜택 마케팅 알림 수신</label><div class="hint">선택 동의이며 언제든 철회할 수 있습니다.</div><button class="b ghost" style="margin-top:10px" onclick="saveMarketing()">마케팅 동의 저장</button></div>'}
async function saveRequiredConsent(){if(!$('#reqConsent').checked)return toast('필수 내용을 확인하고 동의해 주세요');try{await post('/api/member/consents',{accept_required:true});OV=await api('/api/member/overview');toast('필수 동의를 저장했습니다');consentPane()}catch(e){toast(e.message)}}
async function saveMarketing(){try{await post('/api/member/consents',{marketing:$('#mkConsent').checked});OV.marketing_ok=$('#mkConsent').checked?1:0;toast('선택 동의를 저장했습니다')}catch(e){toast(e.message)}}

async function securityPane(){const d=await api('/api/member/sessions');
 $('#pane').innerHTML='<div class="panel"><h3>연결된 로그인 방법</h3><div>'+OV.identity_providers.map(x=>'<span class="tagst s2" style="margin-right:6px">'+esc(x)+'</span>').join('')+'</div><div style="display:flex;gap:7px;margin-top:12px;flex-wrap:wrap">'+[['google','Google'],['kakao','카카오'],['apple','Apple']].filter(x=>!OV.identity_providers.includes(x[0])).map(x=>'<a class="b ghost" href="/auth/'+x[0]+'?link=1">'+x[1]+' 연결</a>').join('')+'</div><div class="hint">다른 로그인 수단은 해당 서비스에서 본인을 다시 인증한 후 연결됩니다. 동일 이메일만으로 자동 병합하지 않습니다.</div></div><div class="panel"><h3>로그인 기기</h3>'+
 (d.rows.length?'<table><tr><th>기기</th><th>접속 IP</th><th>시작</th><th>만료</th><th></th></tr>'+d.rows.map(x=>'<tr><td style="max-width:280px">'+esc(x.device)+(x.current?' <span class="tagst s5">현재</span>':'')+'</td><td class="mono">'+esc(x.ip)+'</td><td class="mono">'+esc(x.created)+'</td><td class="mono">'+esc(x.expires)+'</td><td>'+(x.current?'':'<button class="b ghost" onclick="revokeSession(\''+esc(x.id)+'\')">종료</button>')+'</td></tr>').join('')+'</table>':'<div class="empty">활성 세션이 없습니다</div>')+'<button class="b red" style="margin-top:12px" onclick="revokeOthers()">다른 기기 모두 로그아웃</button></div>'}
async function revokeSession(id){await post('/api/member/sessions/revoke',{id});toast('세션을 종료했습니다');securityPane()}
async function revokeOthers(){if(!confirm('현재 기기를 제외한 모든 로그인을 종료할까요?'))return;await post('/api/member/sessions/revoke',{all:true});toast('다른 기기 로그인을 종료했습니다');securityPane()}

async function storePane(){const on=OV.fav_store;
 $('#pane').innerHTML='<div class="panel"><h3>관심 매장 관리</h3>'+
 '<table><tr><td><b>맵달SEOUL 성수 플래그십</b><br><span class="hint">서울 성동구 성수이로16길 5 · 매일 11:00–21:00 · 825평 K-컬처 복합공간</span></td>'+
 '<td class="r" style="white-space:nowrap">'+(on?'<span class="tagst s2">관심 매장</span> <button class="b ghost" onclick="favStore(0)">해제</button>':'<button class="b red" onclick="favStore(1)">관심 매장 등록</button>')+'</td></tr></table>'+
 '<div class="hint">관심 매장으로 등록하면 오프라인 드롭·팬미팅·시식 이벤트 소식을 우선 안내해 드립니다.</div></div>'}
async function favStore(v){await post('/api/member/profile',{fav_store:v});OV.fav_store=v;toast(v?'관심 매장으로 등록했습니다':'해제되었습니다');storePane()}

async function withdrawPane(){const m=OV;
 $('#pane').innerHTML='<div class="panel"><h3>회원탈퇴</h3>'+
 '<div class="hint">탈퇴 시 로그인 정보·세션·좋아요·재입고 알림·배송지·마케팅 정보는 삭제 또는 비식별 처리되어 복구할 수 없습니다.<br>주문·결제 및 분쟁 처리 기록은 관계 법령에 따른 기간 동안 일반 회원 데이터와 분리해 보관됩니다.</div>'+
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
        return HTMLResponse(_brand_apply(_MYPAGE_HTML.replace('__MDATA__', json.dumps(mdata, ensure_ascii=False))))
    g_on = bool(_genv('GOOGLE_CLIENT_ID'))
    a_on = all(_apple_conf().values())
    k_on = bool(_genv('KAKAO_CLIENT_ID'))
    social = ('<a class="sbtn kakao%s" href="/auth/kakao" style="background:#FEE500;color:#191919;border-color:#FEE500;font-weight:800">TALK · 카카오로 3초만에 시작하기%s</a>'
              % ('' if k_on else ' off', '' if k_on else ' (준비 중)')
              + '<a class="sbtn%s" href="/auth/google">G · Google 계정으로 계속하기%s</a>'
              '<a class="sbtn apple%s" href="/auth/apple">&#63743; · Apple 계정으로 계속하기%s</a>'
              % ('' if g_on else ' off', '' if g_on else ' (준비 중)',
                 '' if a_on else ' off', '' if a_on else ' (준비 중)'))
    return HTMLResponse(_brand_apply('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>로그인 — MAPDAL SEOUL</title>' + _ACCOUNT_CSS + _ACCOUNT_FORM_CSS +
        '</head><body><div class="box"><h1>MAPDAL<span>SEOUL</span></h1><div class="sub">SIGN IN / SIGN UP</div>'
        '<div style="background:#fff3df;border-left:4px solid #FFB000;padding:9px 11px;font-size:12px;margin-bottom:14px"><b>최초 가입 2,000P</b> · 한 고객당 한 번만 지급</div>'
        + social +
        '<div class="div"><span>또는 이메일로</span></div>'
        '<div class="tabs"><button id="tbL" class="on" onclick="mode(0)">로그인</button><button id="tbS" onclick="mode(1)">회원가입</button></div>'
        '<div id="fL"><label>이메일</label><input id="le" type="email" autocomplete="email">'
        '<label>비밀번호</label><input id="lp" type="password" autocomplete="current-password">'
        '<button class="go" onclick="doLogin()">로그인</button><button class="out" style="border:0;background:none;width:100%;cursor:pointer" onclick="resetMode()">비밀번호 재설정</button></div>'
        '<div id="fS" style="display:none"><label>이름 *</label><input id="sn" autocomplete="name">'
        '<label>성별 (선택)</label><div style="display:flex;gap:16px;padding:4px 2px"><label style="margin:0;font-weight:400;font-size:13px"><input type="radio" name="sg" value="F" style="width:auto"> 여성</label><label style="margin:0;font-weight:400;font-size:13px"><input type="radio" name="sg" value="M" style="width:auto"> 남성</label></div>'
        '<label>휴대폰 번호 (선택 · 주문연결/계정복구 시 인증)</label><input id="sph" placeholder="010-0000-0000" autocomplete="tel">'
        '<label>생년월일 (선택)</label><input id="sbi" type="date">'
        '<label>이메일 *</label><input id="se" type="email" autocomplete="email">'
        '<label>비밀번호 (8자 이상)</label><input id="sp" type="password" autocomplete="new-password">'
        '<label>비밀번호 확인</label><input id="sp2" type="password" autocomplete="new-password">'
        '<label style="font-weight:400;font-size:12px"><input id="sterms" type="checkbox" style="width:auto"> <a href="/terms" target="_blank">이용약관</a> 동의 (필수)</label>'
        '<label style="font-weight:400;font-size:12px"><input id="sprivacy" type="checkbox" style="width:auto"> <a href="/privacy" target="_blank">개인정보 수집·이용</a> 동의 (필수)</label>'
        '<label style="font-weight:400;font-size:12px"><input id="smarketing" type="checkbox" style="width:auto"> 이벤트·신상품 마케팅 알림 동의 (선택)</label>'
        '<button class="go" onclick="doSignup()">가입하기</button></div>'
        '<div id="fR" style="display:none"><h3 style="font-size:16px;margin:8px 0">비밀번호 재설정</h3><div class="sub" style="margin-bottom:10px">가입 후 인증한 휴대폰으로 본인을 확인합니다.</div><label>이메일</label><input id="re" type="email"><label>인증된 휴대폰</label><input id="rp" placeholder="010-0000-0000"><button class="go" onclick="resetSend()">인증번호 받기</button><div id="rverify" style="display:none"><label>인증번호</label><input id="rc" maxlength="6"><label>새 비밀번호 (8자 이상)</label><input id="rnw" type="password"><button class="go" onclick="resetVerify()">비밀번호 변경</button></div><button class="out" style="border:0;background:none;width:100%;cursor:pointer" onclick="mode(0)">로그인으로 돌아가기</button></div>'
        '<div class="err" id="err"></div>'
        '<a class="out" href="/">홈으로 돌아가기</a>'
        '<div class="foot">SHOP SEONGSU, FROM ANYWHERE</div></div>'
        '<script>'
        'const E=document.getElementById("err");function show(m){E.textContent=m;E.style.display="block"}'
        'function mode(i){document.getElementById("fR").style.display="none";document.getElementById("fL").style.display=i?"none":"";document.getElementById("fS").style.display=i?"":"none";'
        'document.getElementById("tbL").className=i?"":"on";document.getElementById("tbS").className=i?"on":"";E.style.display="none"}'
        'function resetMode(){document.getElementById("fL").style.display="none";document.getElementById("fS").style.display="none";document.getElementById("fR").style.display="";E.style.display="none"}'
        'async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        'if(!r.ok){let m="오류";try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}'
        'async function doLogin(){try{await post("/api/member/login",{email:document.getElementById("le").value,password:document.getElementById("lp").value});location.reload()}catch(e){show(e.message)}}'
        'async function doSignup(){const p=document.getElementById("sp").value;'
        'if(p!==document.getElementById("sp2").value)return show("비밀번호가 서로 다릅니다");'
        'const g=(document.querySelector(\'input[name=sg]:checked\')||{}).value;'
        'try{await post("/api/member/signup",{name:document.getElementById("sn").value,gender:g,phone:document.getElementById("sph").value,birth:document.getElementById("sbi").value,email:document.getElementById("se").value,password:p,terms:document.getElementById("sterms").checked,privacy:document.getElementById("sprivacy").checked,marketing:document.getElementById("smarketing").checked});location.reload()}catch(e){show(e.message)}}'
        'let RID="";async function resetSend(){try{const r=await post("/api/member/password-reset/send",{email:document.getElementById("re").value,phone:document.getElementById("rp").value});RID=r.reset_id;document.getElementById("rverify").style.display="";show(r.dry?"테스트 모드: 관리자 알림 로그에서 인증번호를 확인하세요":"인증번호를 발송했습니다")}catch(e){show(e.message)}}'
        'async function resetVerify(){try{await post("/api/member/password-reset/verify",{reset_id:RID,code:document.getElementById("rc").value,password:document.getElementById("rnw").value});alert("비밀번호가 변경되었습니다. 다시 로그인해 주세요.");mode(0)}catch(e){show(e.message)}}'
        '</script></body></html>'))

@admin_router.get('/admin/api/members')
def api_members(request: Request):
    a = get_actor(request); need(a, 0)
    rs = rows('SELECT m.*,c.customer_no,c.points_balance,c.status AS customer_status FROM members m LEFT JOIN customer_profiles c ON c.id=m.customer_id ORDER BY m.created DESC LIMIT 300')
    total = num((one('SELECT COUNT(*) AS c FROM members') or {}).get('c'))
    return {'total': total, 'rows': [{'id': r['id'], 'provider': r.get('provider'), 'email': _mask_email(r.get('email') or ''),
            'name': r.get('name') or '', 'created': (r.get('created') or '')[:16].replace('T', ' '),
            'phone': _mask_phone(r.get('phone') or ''), 'verified': num(r.get('phone_verified')),
            'points': num(r.get('points_balance')), 'customer_no': r.get('customer_no') or '',
            'status': r.get('customer_status') or r.get('status') or 'ACTIVE',
            'gender': r.get('gender') or '', 'birth': ((r.get('birth') or '')[:4] if len(r.get('birth') or '')==10 else (r.get('birth') or ''))} for r in rs]}

@admin_router.get('/admin/api/accounts')
def api_accounts(request: Request):
    a = get_actor(request); need(a, 0)
    p=request.query_params; where=[]; args=[]
    q=(p.get('query') or '').strip()
    if q:
        kw='%'+q+'%'; where.append("(c.customer_no LIKE ? OR c.name LIKE ? OR EXISTS(SELECT 1 FROM members m WHERE m.customer_id=c.id AND (m.email LIKE ? OR m.phone LIKE ?)))")
        args += [kw,kw,kw,kw]
    if p.get('status'): where.append('c.status=?'); args.append(p['status'])
    if p.get('provider'):
        where.append('EXISTS(SELECT 1 FROM auth_identities ai WHERE ai.customer_id=c.id AND ai.provider=?)'); args.append(p['provider'])
    if p.get('verified')=='phone': where.append("EXISTS(SELECT 1 FROM customer_contacts cc WHERE cc.customer_id=c.id AND cc.kind='PHONE' AND cc.verified=1)")
    if p.get('verified')=='none': where.append('NOT EXISTS(SELECT 1 FROM customer_contacts cc WHERE cc.customer_id=c.id AND cc.verified=1)')
    if p.get('marketing')=='1': where.append('c.marketing_ok=1')
    if p.get('segment')=='no_order': where.append('NOT EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.id)')
    if p.get('segment')=='buyer': where.append('EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.id)')
    if p.get('issue')=='duplicate':
        where.append("EXISTS(SELECT 1 FROM members m1 JOIN members m2 ON lower(m1.email)=lower(m2.email) AND m1.id<>m2.id WHERE m1.customer_id=c.id AND COALESCE(m1.email,'')<>'')")
    support=p.get('support') or ''
    support_where={
        'request': "EXISTS(SELECT 1 FROM member_requests x WHERE x.customer_id=c.id AND x.status IN ('접수','처리중'))",
        'inquiry': "EXISTS(SELECT 1 FROM member_inquiries x WHERE x.customer_id=c.id AND x.status<>'답변완료')",
        'pqna': "EXISTS(SELECT 1 FROM member_pqna x WHERE x.customer_id=c.id AND x.status<>'답변완료')",
        'restock': "EXISTS(SELECT 1 FROM member_restock x WHERE x.customer_id=c.id AND x.notified=0)",
        'liked': "EXISTS(SELECT 1 FROM member_likes x WHERE x.customer_id=c.id)",
        'sessions': "EXISTS(SELECT 1 FROM member_sessions s JOIN members m ON m.id=s.member_id WHERE m.customer_id=c.id AND s.expires>?)",
    }
    if support=='pending':
        where.append("(EXISTS(SELECT 1 FROM member_requests x WHERE x.customer_id=c.id AND x.status IN ('접수','처리중')) OR EXISTS(SELECT 1 FROM member_inquiries x WHERE x.customer_id=c.id AND x.status<>'답변완료') OR EXISTS(SELECT 1 FROM member_pqna x WHERE x.customer_id=c.id AND x.status<>'답변완료'))")
    elif support in support_where:
        where.append(support_where[support])
        if support=='sessions': args.append(now_iso())
    w=(' WHERE '+' AND '.join(where)) if where else ''
    page=max(1,int(p.get('page',1) or 1)); size=25
    total=num((one('SELECT COUNT(*) AS c FROM customer_profiles c'+w,tuple(args)) or {}).get('c'))
    rs=rows('SELECT c.* FROM customer_profiles c%s ORDER BY c.updated_at DESC,c.created_at DESC LIMIT %d OFFSET %d' % (w,size,(page-1)*size),tuple(args))
    out=[]
    for c in rs:
        ids=rows('SELECT provider,email_norm,last_login_at FROM auth_identities WHERE customer_id=? ORDER BY created_at',(c['id'],))
        phone=one("SELECT value FROM customer_contacts WHERE customer_id=? AND kind='PHONE' AND is_primary=1",(c['id'],)) or {}
        email=one("SELECT value FROM customer_contacts WHERE customer_id=? AND kind='EMAIL' AND is_primary=1",(c['id'],)) or {}
        stats=one("SELECT COUNT(*) AS cnt,COALESCE(SUM(CASE WHEN status='PAID' THEN amount ELSE 0 END),0) AS spend,MAX(created) AS last_order FROM orders WHERE customer_id=?",(c['id'],)) or {}
        mp=one("SELECT "
               "(SELECT COUNT(*) FROM member_requests WHERE customer_id=? AND status IN ('접수','처리중')) AS req,"
               "(SELECT COUNT(*) FROM member_inquiries WHERE customer_id=? AND status<>'답변완료') AS inq,"
               "(SELECT COUNT(*) FROM member_pqna WHERE customer_id=? AND status<>'답변완료') AS pqna,"
               "(SELECT COUNT(*) FROM member_restock WHERE customer_id=? AND notified=0) AS restock,"
               "(SELECT COUNT(*) FROM member_likes WHERE customer_id=?) AS likes,"
               "(SELECT COUNT(*) FROM member_sessions s JOIN members m ON m.id=s.member_id WHERE m.customer_id=? AND s.expires>?) AS sessions",
               (c['id'],c['id'],c['id'],c['id'],c['id'],c['id'],now_iso())) or {}
        out.append({'id':c['id'],'customer_no':c.get('customer_no') or '','name':c.get('name') or '',
                    'status':c.get('status') or 'ACTIVE','grade':c.get('grade') or 'WELCOME',
                    'phone':_mask_phone(phone.get('value') or ''),'email':_mask_email(email.get('value') or ''),
                    'providers':[x['provider'] for x in ids],'last_login':max([x.get('last_login_at') or '' for x in ids] or ['']),
                    'orders':num(stats.get('cnt')),'spend':num(stats.get('spend')),'last_order':(stats.get('last_order') or '')[:10],
                    'points':num(c.get('points_balance')),'marketing':num(c.get('marketing_ok')),
                    'support':{k:num(mp.get(k)) for k in ('req','inq','pqna','restock','likes','sessions')}})
    queues={
        'request':num((one("SELECT COUNT(*) AS c FROM member_requests WHERE status IN ('접수','처리중')") or {}).get('c')),
        'inquiry':num((one("SELECT COUNT(*) AS c FROM member_inquiries WHERE status<>'답변완료'") or {}).get('c')),
        'pqna':num((one("SELECT COUNT(*) AS c FROM member_pqna WHERE status<>'답변완료'") or {}).get('c')),
        'restock':num((one("SELECT COUNT(*) AS c FROM member_restock WHERE notified=0") or {}).get('c')),
        'locked':num((one("SELECT COUNT(*) AS c FROM customer_profiles WHERE status='LOCKED'") or {}).get('c')),
    }
    return {'total':total,'page':page,'size':size,'rows':out,'queues':queues}

@admin_router.get('/admin/api/accounts/{cid}')
def api_account_detail(cid: str, request: Request):
    a=get_actor(request); need(a,0)
    c=one('SELECT * FROM customer_profiles WHERE id=?',(cid,))
    if not c: raise HTTPException(404,'not found')
    contacts=rows('SELECT kind,value,verified,is_primary,verified_at FROM customer_contacts WHERE customer_id=? ORDER BY kind,is_primary DESC',(cid,))
    identities=rows('SELECT provider,email_norm,email_verified,created_at,last_login_at FROM auth_identities WHERE customer_id=? ORDER BY created_at',(cid,))
    os=rows('SELECT order_id,created,status,amount,ship_method FROM orders WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    pts=rows('SELECT event_type,amount,balance_after,reason,created_at,by_admin FROM point_ledger WHERE customer_id=? ORDER BY created_at DESC LIMIT 100',(cid,))
    cons=rows('SELECT consent_type,policy_version,granted,source,created_at FROM consent_history WHERE customer_id=? ORDER BY created_at DESC LIMIT 100',(cid,))
    sec=rows('SELECT event_type,ip,detail,created_at FROM account_security_events WHERE customer_id=? ORDER BY created_at DESC LIMIT 100',(cid,)) if RANK.get(a['role'],0)>=2 else []
    addresses=rows('SELECT id,label,rname,phone,is_default,created FROM member_addresses WHERE customer_id=? ORDER BY is_default DESC,created DESC LIMIT 50',(cid,))
    likes=rows('SELECT id,product_id,page,pname,pprice,created FROM member_likes WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    restock=rows('SELECT id,product_id,phone,created,notified FROM member_restock WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    requests=rows('SELECT id,order_id,rtype,reason,created,status,admin_memo,updated FROM member_requests WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    inquiries=rows('SELECT id,order_id,title,body,created,status,answer,answered_at,answered_by FROM member_inquiries WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    pqna=rows('SELECT id,product_id,question,created,status,answer,answered_at,answered_by FROM member_pqna WHERE customer_id=? ORDER BY created DESC LIMIT 100',(cid,))
    sessions=rows('SELECT s.id,s.created,s.expires,s.ip,s.user_agent,s.last_seen FROM member_sessions s JOIN members m ON m.id=s.member_id WHERE m.customer_id=? ORDER BY s.last_seen DESC,s.created DESC',(cid,))
    members=rows('SELECT id,provider,email,name,phone,phone_verified,fav_store,status,last_login_at FROM members WHERE customer_id=? ORDER BY created',(cid,))
    def product_label(pid, fallback=''):
        if fallback: return fallback
        try:
            nm=_state['pname'] or 'id'; p=one('SELECT %s AS name FROM products WHERE id=?' % nm,(pid,)) or {}
            return p.get('name') or pid
        except Exception: return pid
    def mask_ip(v):
        s=str(v or '')
        if ':' in s: return s.split(':',1)[0]+':****'
        p=s.split('.'); return '.'.join(p[:2]+['***','***']) if len(p)==4 else s
    req_kr={'cancel':'취소','return':'반품','exchange':'교환'}
    return {'customer':{'id':c['id'],'customer_no':c.get('customer_no'),'name':c.get('name') or '',
                        'status':c.get('status'),'grade':c.get('grade'),'points':num(c.get('points_balance')),
                        'marketing':num(c.get('marketing_ok')),'created':c.get('created_at'),'withdrawn':c.get('withdrawn_at') or '',
                        'admin_memo':c.get('admin_memo') or '',
                        'fav_store':1 if any(num(x.get('fav_store')) for x in members) else 0},
            'contacts':[{'kind':x['kind'],'value':_mask_phone(x['value']) if x['kind']=='PHONE' else _mask_email(x['value']),
                         'verified':num(x['verified']),'primary':num(x['is_primary']),'verified_at':x.get('verified_at') or ''} for x in contacts],
            'identities':[{'provider':x['provider'],'email':_mask_email(x.get('email_norm') or ''),'verified':num(x.get('email_verified')),
                           'created':x.get('created_at') or '','last_login':x.get('last_login_at') or ''} for x in identities],
            'orders':os,'points':pts,'consents':cons,'security':sec,
            'addresses':[{'id':x['id'],'label':x.get('label') or '배송지','rname':((x.get('rname') or '')[:1]+'**') if x.get('rname') else '',
                          'phone':_mask_phone(x.get('phone') or ''),'default':num(x.get('is_default')),'created':x.get('created') or ''} for x in addresses],
            'likes':[{'id':x['id'],'product_id':x.get('product_id') or '','name':product_label(x.get('product_id') or '',x.get('pname') or ''),
                      'page':x.get('page') or '','price':num(x.get('pprice')),'created':x.get('created') or ''} for x in likes],
            'restock':[{'id':x['id'],'product_id':x.get('product_id') or '','name':product_label(x.get('product_id') or ''),
                        'phone':_mask_phone(x.get('phone') or ''),'notified':num(x.get('notified')),'created':x.get('created') or ''} for x in restock],
            'requests':[{'id':x['id'],'order_id':x.get('order_id') or '','type':req_kr.get(x.get('rtype'),x.get('rtype')),
                         'reason':x.get('reason') or '','status':x.get('status') or '','memo':x.get('admin_memo') or '',
                         'created':x.get('created') or '','updated':x.get('updated') or ''} for x in requests],
            'inquiries':[dict(x) for x in inquiries], 'pqna':[dict(x) for x in pqna],
            'sessions':[{'id':x['id'],'created':x.get('created') or '','expires':x.get('expires') or '',
                         'last_seen':x.get('last_seen') or '','ip':mask_ip(x.get('ip')),
                         'device':str(x.get('user_agent') or '')[:100]} for x in sessions],
            'members':[{'id':x['id'],'provider':x.get('provider') or '','email':_mask_email(x.get('email') or ''),
                        'name':x.get('name') or '','phone':_mask_phone(x.get('phone') or ''),'verified':num(x.get('phone_verified')),
                        'status':x.get('status') or '','last_login':x.get('last_login_at') or ''} for x in members]}

@admin_router.post('/admin/api/accounts/{cid}/reveal')
def api_account_reveal(cid: str, request: Request, body: dict=Body(...)):
    a=get_actor(request); need(a,2,'개인정보 원문 조회')
    reason=(body.get('reason') or '').strip()[:200]
    if not reason: raise HTTPException(400,'조회 사유를 입력하세요')
    c=one('SELECT customer_no FROM customer_profiles WHERE id=?',(cid,))
    if not c: raise HTTPException(404,'not found')
    audit(a,'개인정보조회',c.get('customer_no') or cid,reason)
    return {'contacts':rows('SELECT kind,value,verified FROM customer_contacts WHERE customer_id=? ORDER BY kind,is_primary DESC',(cid,)),
            'addresses':rows('SELECT label,rname,phone,zip,addr1,addr2,is_default FROM member_addresses WHERE customer_id=? ORDER BY is_default DESC,created DESC',(cid,))}

@admin_router.post('/admin/api/accounts/{cid}/status')
def api_account_status(cid: str, request: Request, body: dict=Body(...)):
    a=get_actor(request); need(a,2,'계정 상태 변경'); st=body.get('status')
    if st not in ('ACTIVE','LOCKED'): raise HTTPException(400,'상태 값 오류')
    c=one('SELECT customer_no FROM customer_profiles WHERE id=?',(cid,))
    if not c: raise HTTPException(404,'not found')
    run('UPDATE customer_profiles SET status=?,updated_at=? WHERE id=?',(st,now_iso(),cid))
    run('UPDATE members SET status=?,updated_at=? WHERE customer_id=?',(st,now_iso(),cid))
    if st=='LOCKED': run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)',(cid,))
    audit(a,'계정상태',c.get('customer_no') or cid,st)
    return {'ok':True,'status':st}

@admin_router.post('/admin/api/accounts/{cid}/mypage-action')
def api_account_mypage_action(cid: str, request: Request, body: dict=Body(...)):
    """고객 마이페이지 지원 작업. 법적 동의이력·포인트원장은 별도 API로만 처리한다."""
    a=get_actor(request); need(a,1,'마이페이지 고객지원')
    c=one('SELECT customer_no,status FROM customer_profiles WHERE id=?',(cid,))
    if not c: raise HTTPException(404,'고객을 찾을 수 없습니다')
    action=(body.get('action') or '').strip(); rid=(body.get('id') or '').strip()
    label=''; detail=''
    if action=='save_memo':
        memo=(body.get('memo') or '').strip()[:1000]
        run('UPDATE customer_profiles SET admin_memo=?,updated_at=? WHERE id=?',(memo,now_iso(),cid))
        label='고객메모'; detail=memo[:120]
    elif action=='revoke_session':
        if body.get('all'):
            n=run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)',(cid,)); detail='전체'
        else:
            n=run('DELETE FROM member_sessions WHERE id=? AND member_id IN (SELECT id FROM members WHERE customer_id=?)',(rid,cid)); detail=rid[:12]
        label='세션종료'; detail+='·%d개' % num(n)
    elif action in ('remove_like','cancel_restock','reset_restock','delete_address'):
        table={'remove_like':'member_likes','cancel_restock':'member_restock','reset_restock':'member_restock','delete_address':'member_addresses'}[action]
        if action=='delete_address': need(a,2,'고객 배송지 삭제')
        if action=='reset_restock':
            n=run('UPDATE member_restock SET notified=0 WHERE id=? AND customer_id=?',(rid,cid)); label='재입고알림 대기로 복원'
        else:
            n=run('DELETE FROM %s WHERE id=? AND customer_id=?' % table,(rid,cid))
            label={'remove_like':'좋아요 삭제','cancel_restock':'재입고알림 해지','delete_address':'배송지 삭제'}[action]
        if not n: raise HTTPException(404,'대상을 찾을 수 없습니다')
        detail=rid
    elif action=='set_fav_store':
        v=1 if body.get('value') else 0
        run('UPDATE members SET fav_store=?,updated_at=? WHERE customer_id=?',(v,now_iso(),cid))
        label='관심매장'; detail='등록' if v else '해제'
    else:
        raise HTTPException(400,'지원하지 않는 작업입니다')
    audit(a,label,c.get('customer_no') or cid,detail)
    return {'ok':True,'action':action}

@admin_router.post('/admin/api/accounts/merge')
def api_accounts_merge(request: Request, body: dict=Body(...)):
    a=get_actor(request); need(a,3,'고객 계정 병합')
    target=(body.get('target_id') or '').strip(); source=(body.get('source_id') or '').strip()
    if target and not one('SELECT id FROM customer_profiles WHERE id=?',(target,)):
        tr=one('SELECT id FROM customer_profiles WHERE customer_no=?',(target,)); target=tr['id'] if tr else ''
    if not target or not source or target==source: raise HTTPException(400,'병합 대상 확인')
    tc=one('SELECT * FROM customer_profiles WHERE id=?',(target,)); sc=one('SELECT * FROM customer_profiles WHERE id=?',(source,))
    if not tc or not sc: raise HTTPException(404,'고객을 찾을 수 없습니다')
    if tc.get('status')!='ACTIVE' or sc.get('status') in ('MERGED','WITHDRAWN'):
        raise HTTPException(400,'활성 고객으로만 병합할 수 있습니다')
    if body.get('confirm') != sc.get('customer_no'): raise HTTPException(400,'원본 고객번호를 정확히 입력하세요')
    # 한 트랜잭션으로 모든 직접 참조를 이동한다. 동일 연락처는 대상 쪽 한 건만 보존한다.
    with _conn() as c:
        tb=c.execute(_q("SELECT COALESCE(SUM(amount),0) AS b FROM point_ledger WHERE customer_id=? AND event_type='SIGNUP_BONUS'"),(target,)).fetchone()
        sb=c.execute(_q("SELECT COALESCE(SUM(amount),0) AS b FROM point_ledger WHERE customer_id=? AND event_type='SIGNUP_BONUS'"),(source,)).fetchone()
        target_bonus=num(dict(tb).get('b')) if tb else 0; source_bonus=num(dict(sb).get('b')) if sb else 0
        c.execute(_q("DELETE FROM customer_contacts WHERE customer_id=? AND EXISTS(SELECT 1 FROM customer_contacts t WHERE t.customer_id=? AND t.kind=customer_contacts.kind AND t.value_norm=customer_contacts.value_norm)"),(source,target))
        for table,col in (('members','customer_id'),('auth_identities','customer_id'),('customer_contacts','customer_id'),
                          ('consent_history','customer_id'),('point_ledger','customer_id'),('account_order_links','customer_id'),
                          ('account_security_events','customer_id'),('orders','customer_id'),('order_claims','customer_id'),
                          ('member_addresses','customer_id'),('member_likes','customer_id'),('member_restock','customer_id'),
                          ('member_requests','customer_id'),('member_inquiries','customer_id'),('member_pqna','customer_id')):
            c.execute(_q('UPDATE %s SET %s=? WHERE %s=?' % (table,col,col)),(target,source))
        br=c.execute(_q('SELECT COALESCE(SUM(amount),0) AS b FROM point_ledger WHERE customer_id=?'),(target,)).fetchone()
        bal=num(dict(br).get('b')) if br else 0
        # 중복 가입 계정을 병합해도 최초 가입 혜택은 고객당 한 번만 남긴다.
        reversal=(source_bonus if target_bonus>0 else max(0,source_bonus-SIGNUP_BONUS))
        if reversal:
            nxt=max(0,bal-reversal); actual=nxt-bal; bal=nxt
            c.execute(_q('INSERT INTO point_ledger(id,customer_id,member_id,event_type,amount,balance_after,event_key,order_id,reason,expires_at,created_at,by_admin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'),
                      (uid(),target,'','MERGE_SIGNUP_REVERSAL',actual,nxt,'merge-signup:%s:%s' % (source,target),'','중복 계정 가입 혜택 회수','',now_iso(),a.get('name') or ''))
        c.execute(_q('UPDATE customer_profiles SET points_balance=?,updated_at=? WHERE id=?'),(bal,now_iso(),target))
        c.execute(_q('UPDATE members SET points=? WHERE customer_id=?'),(bal,target))
        c.execute(_q('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)'),(target,))
        c.execute(_q("UPDATE customer_profiles SET status='MERGED',points_balance=0,updated_at=? WHERE id=?"),(now_iso(),source))
        c.commit()
    audit(a,'고객병합',sc.get('customer_no'),'%s로 병합' % tc.get('customer_no'))
    return {'ok':True,'target_id':target}

# ═══════════════ 이메일 회원가입/로그인 + 헤더 로그인 버튼 ═══════════════
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

@admin_router.get('/api/member/me')
def api_member_me(request: Request):
    try: ensure_ready()
    except Exception: pass
    m = member_of(request)
    if not m: return {'login': False}
    cs = consent_state(m.get('customer_id') or '')
    required = not (cs.get('TERMS') and num(cs['TERMS'].get('granted')) and cs['TERMS'].get('policy_version') == TERMS_VERSION
                    and cs.get('PRIVACY') and num(cs['PRIVACY'].get('granted')) and cs['PRIVACY'].get('policy_version') == PRIVACY_VERSION)
    return {'login': True, 'name': m.get('name') or '회원', 'email': m.get('email') or '',
            'provider': m.get('provider'), 'consent_required': required}

@admin_router.post('/api/member/password-reset/send')
def api_member_password_reset_send(request: Request, body: dict=Body(...)):
    email=(body.get('email') or '').strip().lower(); phone=kphone_norm(body.get('phone') or '')
    ip=(request.client.host if request.client else '') or '-'; key='pr:'+ip; guard(key); fail_hit(key)
    m=one("SELECT * FROM members WHERE provider='email' AND lower(email)=? AND phone=? AND phone_verified=1 AND status='ACTIVE'",(email,phone))
    if not m: raise HTTPException(400,'이메일과 인증된 휴대폰 정보를 확인해 주세요')
    rid=uid(); code=str(secrets.randbelow(900000)+100000); ch=hashlib.sha256((rid+':'+code).encode()).hexdigest()
    exp=(datetime.datetime.utcnow()+datetime.timedelta(minutes=5)).isoformat(timespec='seconds')
    run('UPDATE password_resets SET used=1 WHERE member_id=? AND used=0',(m['id'],))
    run('INSERT INTO password_resets VALUES(?,?,?,?,?,?,?,?)',(rid,m['id'],phone,ch,0,now_iso(),exp,0))
    ok,dry=system_sms(phone,'[맵달SEOUL] 비밀번호 재설정 인증번호는 [%s] 입니다. 5분 내에 입력해 주세요.' % code,'비밀번호재설정')
    if not ok: raise HTTPException(400,'문자 발송에 실패했습니다')
    return {'ok':True,'reset_id':rid,'dry':dry}

@admin_router.post('/api/member/password-reset/verify')
def api_member_password_reset_verify(request: Request, body: dict=Body(...)):
    rid=(body.get('reset_id') or '').strip(); code=(body.get('code') or '').strip(); new=body.get('password') or ''
    if len(new)<8: raise HTTPException(400,'새 비밀번호는 8자 이상이어야 합니다')
    r=one('SELECT * FROM password_resets WHERE id=? AND used=0',(rid,))
    if not r or (r.get('expires_at') or '')<=now_iso(): raise HTTPException(400,'재설정 요청이 만료되었습니다')
    if num(r.get('attempts'))>=5: raise HTTPException(429,'인증 시도 횟수를 초과했습니다')
    if not hmac.compare_digest(r.get('code_hash') or '',hashlib.sha256((rid+':'+code).encode()).hexdigest()):
        run('UPDATE password_resets SET attempts=attempts+1 WHERE id=?',(rid,)); raise HTTPException(400,'인증번호가 올바르지 않습니다')
    m=one('SELECT * FROM members WHERE id=?',(r['member_id'],))
    if not m: raise HTTPException(404,'계정을 찾을 수 없습니다')
    run('UPDATE members SET pw=?,updated_at=? WHERE id=?',(pw_hash(new),now_iso(),m['id']))
    run('UPDATE password_resets SET used=1 WHERE id=?',(rid,)); run('DELETE FROM member_sessions WHERE member_id=?',(m['id'],))
    account_security(m,'PASSWORD_RESET',request,'all sessions revoked')
    fail_clear('pr:'+(((request.client.host if request.client else '') or '-')))
    return {'ok':True}

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
    if gender and gender not in ('F', 'M'): raise HTTPException(400, '성별 값을 확인해 주세요')
    if phone and len(phone) < 10: raise HTTPException(400, '휴대폰 번호를 확인해 주세요')
    if not _EMAIL_RE.fullmatch(email): raise HTTPException(400, '이메일 형식을 확인하세요')
    if len(pw) < 8: raise HTTPException(400, '비밀번호는 8자 이상이어야 합니다')
    if not body.get('terms') or not body.get('privacy'):
        raise HTTPException(400, '이용약관과 개인정보 처리 동의가 필요합니다')
    if one("SELECT id FROM members WHERE provider='email' AND email=?", (email,)):
        raise HTTPException(400, '이미 가입된 이메일입니다 — 로그인해 주세요')
    mid = uid()
    try:
        run('INSERT INTO members(id,provider,sub,email,name,created,pw,gender,phone,phone_verified,birth,email_verified,last_login_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,?,0,?,?,?)',
            (mid, 'email', email, email, name, now_iso(), pw_hash(pw), gender, phone, birth, now_iso(), 'ACTIVE', now_iso()))
    except Exception:
        if one("SELECT id FROM members WHERE provider='email' AND email=?", (email,)):
            raise HTTPException(400, '이미 가입된 이메일입니다 — 로그인해 주세요')
        raise
    m = one('SELECT * FROM members WHERE id=?', (mid,))
    cid = customer_ensure(m, True)
    consent_record(cid, mid, 'TERMS', True, TERMS_VERSION, 'EMAIL_SIGNUP', request)
    consent_record(cid, mid, 'PRIVACY', True, PRIVACY_VERSION, 'EMAIL_SIGNUP', request)
    consent_record(cid, mid, 'MARKETING', bool(body.get('marketing')), TERMS_VERSION, 'EMAIL_SIGNUP', request)
    account_security(one('SELECT * FROM members WHERE id=?', (mid,)), 'SIGNUP', request, 'email')
    sid = member_session_make(mid, request)
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
    run('UPDATE members SET last_login_at=?, updated_at=? WHERE id=?', (now_iso(), now_iso(), row['id']))
    try: run('UPDATE auth_identities SET last_login_at=? WHERE member_id=?', (now_iso(), row['id']))
    except Exception: pass
    row = one('SELECT * FROM members WHERE id=?', (row['id'],))
    account_security(row, 'LOGIN_SUCCESS', request, 'email')
    sid = member_session_make(row['id'], request)
    resp = JSONResponse({'ok': True, 'name': row.get('name') or '회원'})
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    return resp

@admin_router.post('/api/member/logout')
def api_member_logout(request: Request):
    m = member_of(request)
    sid = request.cookies.get('mp_member') or ''
    if sid:
        try: run('DELETE FROM member_sessions WHERE id=?', (hashlib.sha256(sid.encode()).hexdigest(),))
        except Exception: pass
    account_security(m, 'LOGOUT', request)
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
<div class="meta">맵달서울성수(이하 "회사")는 「개인정보 보호법」 제30조에 따라 정보주체의 개인정보를 보호하고 관련 고충을 신속하게 처리하기 위하여 다음과 같이 개인정보처리방침을 수립·공개합니다.<br>공고일: 2026년 7월 15일 · 시행일: 2026년 7월 15일</div>

<h2>제1조 (개인정보의 처리 목적 및 수집 항목)</h2>
<p>회사는 다음 목적을 위해 개인정보를 처리하며, 목적이 변경되는 경우 별도 동의를 받습니다.</p>
<table><tr><th>구분</th><th>수집 항목</th><th>처리 목적</th><th>수집 방법</th></tr>
<tr><td>회원가입(필수)</td><td>이름, 이메일 주소, 비밀번호(이메일 가입 시, 일방향 암호화 저장), 이용약관·개인정보 동의 이력</td><td>회원 식별·관리, 로그인, 고객 문의 처리, 최초 가입 혜택 제공</td><td>회원가입 화면, 카카오·Google·Apple 계정 연동(동의 항목에 한함)</td></tr>
<tr><td>회원정보(선택)</td><td>성별, 생년월일, 휴대폰 번호, 배송지 정보(수령인, 주소, 연락처), 마케팅 수신 동의 여부</td><td>휴대폰 본인확인·계정복구·기존 주문 연결, 배송지 자동 입력, 이벤트·신상품 안내</td><td>회원가입 화면, 마이페이지, 카카오 배송지 연동(동의 시)</td></tr>
<tr><td>주문/결제</td><td>주문자·수령인 정보(이름, 연락처, 주소), 주문·결제 내역</td><td>계약 이행(상품 배송), 결제·환불 처리, 고객 상담</td><td>주문서 작성 화면</td></tr>
<tr><td>자동 수집</td><td>접속 IP, 접속 일시, 브라우저·기기 정보, 로그인 세션 쿠키</td><td>서비스 제공, 계정 보안, 부정 이용 방지</td><td>서비스 이용 과정에서 자동 생성</td></tr></table>
<p>※ 회사는 주민등록번호, CI(연계정보) 등 고유식별정보를 수집하지 않습니다. 만 14세 미만 아동의 회원가입은 받지 않습니다.</p>

<h2>제2조 (개인정보의 보유 및 이용 기간)</h2>
<ul><li>회원 정보: 회원 탈퇴 시까지 (탈퇴 즉시 파기)</li>
<li>다만 관계 법령에 따라 다음 기간 동안 보존합니다 — 계약·청약철회 기록 5년, 대금결제·재화공급 기록 5년, 소비자 불만·분쟁처리 기록 3년(전자상거래법), 접속 기록 3개월(통신비밀보호법)</li></ul>

<h2>제3조 (개인정보 처리의 위탁)</h2>
<table><tr><th>수탁자</th><th>위탁 업무</th></tr>
<tr><td>케이지이니시스</td><td>전자결제(결제 승인·취소) 처리</td></tr>
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
<div class="meta">시행일: 2026년 7월 15일</div>

<h2>제1조 (목적)</h2>
<p>이 약관은 맵달서울성수(이하 "회사")가 운영하는 MAPDAL SEOUL 온라인 몰(mapdal.kr, 이하 "몰")에서 제공하는 전자상거래 서비스의 이용 조건 및 절차, 회사와 이용자의 권리·의무를 규정함을 목적으로 합니다.</p>

<h2>제2조 (정의)</h2>
<ol><li>"회원"이란 몰에 개인정보를 제공하여 가입한 자로서 몰의 서비스를 계속 이용할 수 있는 자를 말합니다.</li>
<li>"드롭(DROP)"이란 회사가 지정한 일시에 한정 수량으로 판매를 개시하는 방식을, "래플(RAFFLE)"이란 응모자 중 추첨을 통해 구매 자격을 부여하는 방식을 말합니다.</li></ol>

<h2>제3조 (약관의 명시와 개정)</h2>
<p>회사는 이 약관과 상호, 대표자, 주소, 사업자등록번호, 통신판매업 신고번호, 연락처 등을 몰의 초기 화면(하단)에 게시합니다. 회사는 관련 법령을 위배하지 않는 범위에서 약관을 개정할 수 있으며, 개정 시 적용일자 7일 전(회원에게 불리한 변경은 30일 전)부터 공지합니다.</p>

<h2>제4조 (회원가입 및 탈퇴)</h2>
<ol><li>회원가입은 이메일 또는 카카오·Google·Apple 계정 연동으로 신청하며, 회사가 승낙함으로써 성립합니다.</li>
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
<p>대금 결제는 케이지이니시스를 통한 신용·체크카드, 계좌이체, 간편결제 등 몰이 제공하는 방법으로 할 수 있습니다. 회사는 카드번호 등 결제수단 정보를 직접 저장하지 않으며, 결제 금액은 서버에서 재검증됩니다.</p>

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
<p>현재 포인트 자동 지급 정책은 고객당 최초 가입 시 2,000P 1회 제공뿐입니다. 구매 금액 적립, 래플 추가 적립 및 결제 시 사용은 운영하지 않습니다. 향후 정책을 신설하거나 변경하는 경우 적용일과 기준을 별도로 공지합니다. 포인트는 현금으로 환급되지 않으며 회원 탈퇴 시 소멸합니다.</p>

<h2>제11조 (회사와 이용자의 의무)</h2>
<p>회사는 법령과 이 약관에 따라 지속적이고 안정적으로 서비스를 제공하며, 이용자의 개인정보를 개인정보처리방침에 따라 보호합니다. 이용자는 타인의 정보 도용, 몰 운영 방해, 지식재산권 침해 행위를 하여서는 안 됩니다.</p>

<h2>제12조 (면책 및 분쟁 해결)</h2>
<p>회사는 천재지변 등 불가항력으로 인한 서비스 장애에 대해 책임을 지지 않습니다. 회사는 이용자의 불만 및 분쟁을 신속히 처리하며, 처리가 곤란한 경우 공정거래위원회 또는 시·도 소비자분쟁조정기구의 조정에 따를 수 있습니다. 회사와 이용자 간 소송은 민사소송법상의 관할법원에 제기합니다.</p>

<h2>부칙</h2>
<p>이 약관은 2026년 7월 15일부터 시행합니다.</p>
</main></body></html>'''

FOOTER_SNIPPET_TPL = '''<footer id="mpFooter" style="background:#141414;color:#fff;font:12px/1.9 'IBM Plex Sans KR',sans-serif;margin:0;padding:0;border-top:1px solid rgba(255,255,255,.09)">
<div style="max-width:1440px;margin:0 auto;padding:26px 48px 40px">
<div style="margin-bottom:10px"><a href="/terms" style="color:#fff;text-decoration:none;margin-right:16px">이용약관</a><a href="/privacy" style="color:#FFB000;font-weight:800;text-decoration:none">개인정보처리방침</a></div>
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
 .header-inner{padding:0 12px;height:62px}
 .logo{font-size:23px;white-space:nowrap}
 .util{gap:10px;min-width:0}
 .util a{white-space:nowrap;font-size:12px}
 .util a.cart{padding:5px 10px;font-size:11px}
 .tab-bar{top:62px!important}
}
@media(max-width:400px){
 .logo{font-size:20px}
 .util{gap:8px}
 .util a{font-size:11px}
 .util a.cart{padding:4px 8px}
 .header-inner{padding:0 10px;height:58px}
 .tab-bar{top:58px!important}
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
/* ── 모바일 히어로: 2.4:1 배너 측면 크롭 제거 → 원본 비율 노출 + 캡션 패널 분리 ── */
@media(max-width:1024px){
 .mzh .mzh-track{height:auto!important}
 .mzh .mzh-slide{height:auto!important;display:flex!important;flex-direction:column;background:#141414}
 .mzh .mzh-slide.is-img::after{display:none}
 .mzh .mzh-img{position:static!important;width:100%!important;height:auto!important;max-height:62vh!important;object-fit:contain!important;background:#141414;flex:0 0 auto}
 .mzh .mzh-cap{position:static;flex:1;display:flex;flex-direction:column;justify-content:center;align-items:flex-start;background:#141414;padding:16px 20px 50px}
 .mzh .mzh-slide .hero-inner{flex:1;min-height:0}
 .mzh-progress{background:rgba(255,255,255,.14)}
 #mpCatBar{-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 34px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 34px),transparent)}
 #mpCatBar.mp-end{-webkit-mask-image:none;mask-image:none}
}
@media(max-width:680px){
 .mzh .mzh-cap{padding:14px 20px 48px}
 .mzh .mzh-cap-tag{font-size:11px;padding:5px 10px;margin-bottom:8px}
 .mzh .mzh-cap-album{font-size:17px;line-height:1.35}
 .mzh .mzh-cap-event{font-size:12.5px;margin-top:6px}
 .mzh .mzh-slide .hero-inner{padding:44px 20px 60px}
 .mzh .mzh-slide h1{font-size:clamp(30px,8.4vw,38px)}
 .mzh .hollow{-webkit-text-stroke:1.5px #fff}
 .mzh .mzh-slide .hero-inner p{font-size:13.5px;line-height:1.65;margin:10px 0 6px}
 .mzh .cta-row{margin-top:16px;gap:10px}
 .mzh .cta-row .btn{padding:12px 18px;font-size:12.5px}
 .mzh .mzh-slide .eyebrow{font-size:11px;letter-spacing:.14em}
 .mzh-dots{bottom:14px;gap:8px}
 .mzh-dots button{width:22px}
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
  {label:'NEW / DROPS',href:'/new-drops',red:true},
  {label:'SHOP',href:'/shop',red:false},
  {label:'MAPDAL SEOUL',href:'/mapdal-seoul',red:false},
  {label:'SUPPORT',href:'/support',red:false}];
 var cur=(location.pathname.split('/').pop()||'').toLowerCase();
 function base(h){return (h||'').split(/[?#]/)[0].split('/').pop().toLowerCase()}
 var h='';
 for(var j=0;j<items.length;j++){var t=items[j],on=cur&&base(t.href)===cur;
  h+='<a class="'+(t.red?'red ':'')+(on?'on':'')+'" href="'+esc(t.href)+'">'+esc(t.label)+'</a>'}
 var bar=document.createElement('nav');bar.id='mpCatBar';
 bar.setAttribute('aria-label','\uce74\ud14c\uace0\ub9ac');
 bar.innerHTML=h;
 header.appendChild(bar);
 /* 스크롤 끝 도달 시 우측 페이드 힌트 제거 */
 function mpEdge(){bar.classList.toggle('mp-end',bar.scrollLeft+bar.clientWidth>=bar.scrollWidth-6)}
 bar.addEventListener('scroll',mpEdge,{passive:true});
 window.addEventListener('resize',mpEdge);mpEdge();
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


# ─────────────────────────────────────────────────────────────
# 홈 화면 블록 (히어로 하부 섹션) — 관리자에서 순서/노출 제어
#   site_settings key='home_blocks' : [{"id","on"}] 순서 배열
#   저장 이력이 없으면 원본 정적 파일 순서 그대로 (제로 리스크)
# ─────────────────────────────────────────────────────────────
HOME_BLOCKS = [
    ('collections', '컬렉션 — 세계관으로 쇼핑하기'),
    ('space',       '공간 소개 — 하나의 건물, 완성되는 팬 경험 (층별 안내)'),
    ('kfood',       'K-FOOD — 분식을 배송합니다 (떡볶이·김밥·BOWL)'),
    ('journal',     '저널 & LIVE'),
    ('trust',       '신뢰 스트립 — 전 세계 배송·DDP·차트 집계 USP'),
]
HOME_BLOCK_LABELS = dict(HOME_BLOCKS)

def homeblocks_conf():
    """저장 순서/노출 설정. 레지스트리 기준으로 정규화(누락분은 노출 상태로 뒤에 붙음)."""
    try:
        r = one('SELECT value, updated, by_admin FROM site_settings WHERE key=?', ('home_blocks',))
    except Exception:
        r = None
    saved = jload(r.get('value'), []) if r else []
    order, seen = [], set()
    for it in (saved if isinstance(saved, list) else []):
        bid = str((it or {}).get('id', ''))
        if bid in HOME_BLOCK_LABELS and bid not in seen:
            order.append({'id': bid, 'label': HOME_BLOCK_LABELS[bid],
                          'on': bool((it or {}).get('on', True))})
            seen.add(bid)
    for bid, lb in HOME_BLOCKS:
        if bid not in seen:
            order.append({'id': bid, 'label': lb, 'on': True})
    return {'blocks': order, 'updated': (r.get('updated') or '') if r else '',
            'by_admin': (r.get('by_admin') or '') if r else '', 'is_default': not r}

def _hb_span_with_comment(html, s, e):
    """블록 시작 직전의 안내 주석(<!-- … -->)까지 스팬에 포함해 같이 이동."""
    j = s
    while j > 0 and html[j-1] in ' \t\r\n':
        j -= 1
    if html.endswith('-->', 0, j):
        k = html.rfind('<!--', 0, j)
        if k >= 0 and (j - k) <= 120:
            s = k
    return (s, e)

def _homeblocks_found(html):
    """홈 HTML에서 각 블록의 (start, end) 스팬 탐지. 못 찾은 블록은 생략(내성)."""
    found = {}

    def _divspan(marker):
        s = html.find(marker)
        if s < 0:
            return None
        depth = 0
        for mm in re.finditer(r'<div\b|</div>', html[s:]):
            depth += 1 if mm.group(0) != '</div>' else -1
            if depth == 0:
                return (s, s + mm.end())
        return None

    i = html.find('세계관으로 쇼핑하기')
    if i > 0:
        s = html.rfind('<section', 0, i)
        e = html.find('</section>', i)
        if s >= 0 and e > 0:
            found['collections'] = _hb_span_with_comment(html, s, e + len('</section>'))
    for bid, marker in (('kfood', '<section id="kfood"'), ('journal', '<section id="journal"')):
        s = html.find(marker)
        if s >= 0:
            e = html.find('</section>', s)
            if e > 0:
                found[bid] = _hb_span_with_comment(html, s, e + len('</section>'))
    for bid, marker in (('space', '<div class="floor-sec" id="space">'), ('trust', '<div class="trust">')):
        sp = _divspan(marker)
        if sp:
            found[bid] = _hb_span_with_comment(html, *sp)
    return found

def _homeblocks_apply(html, path=''):
    """홈(index)의 히어로 하부 블록을 관리자 설정 순서/노출로 재배치 (멱등·fail-open)."""
    try:
        if not isinstance(html, str) or 'mzHero' not in html:
            return html                       # 홈이 아니면 통과
        conf = homeblocks_conf()
        if conf['is_default']:
            return html                       # 저장 이력 없음 → 원본 그대로
        found = _homeblocks_found(html)
        if not found:
            return html
        pieces = {bid: html[s:e] for bid, (s, e) in found.items()}
        first = min(s for s, _ in found.values())
        out = html
        for _bid, (s, e) in sorted(found.items(), key=lambda kv: -kv[1][0]):
            out = out[:s] + out[e:]           # 뒤에서부터 제거 (인덱스 보존)
        seq = [b['id'] for b in conf['blocks'] if b['on'] and b['id'] in pieces]
        tail = out[first:]                    # 블록 사이 공백 잔여 소거 (멱등 보장)
        k = 0
        while k < len(tail) and tail[k] in ' \t\r\n':
            k += 1
        blob = '\n\n'.join(pieces[b] for b in seq)
        out = out[:first] + blob + ('\n\n' if blob else '') + tail[k:]
        return out
    except Exception:
        return html

@admin_router.get('/admin/api/homeblocks')
def api_homeblocks_get(request: Request):
    get_actor(request)
    return homeblocks_conf()

@admin_router.post('/admin/api/homeblocks/save')
def api_homeblocks_save(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, '홈 화면 관리')
    raw = body.get('blocks')
    if not isinstance(raw, list):
        raise HTTPException(400, 'blocks는 목록이어야 합니다')
    order, seen = [], set()
    for it in raw:
        bid = str((it or {}).get('id', ''))
        if bid in HOME_BLOCK_LABELS and bid not in seen:
            order.append({'id': bid, 'on': bool((it or {}).get('on', True))})
            seen.add(bid)
    for bid, _lb in HOME_BLOCKS:
        if bid not in seen:
            order.append({'id': bid, 'on': True})
    _setting_put('home_blocks', order, a['name'])
    audit(a, '홈블록저장', '', ' → '.join(('%s%s' % (b['id'], '' if b['on'] else '(숨김)')) for b in order))
    return {'ok': True}

@admin_router.post('/admin/api/homeblocks/reset')
def api_homeblocks_reset(request: Request):
    a = get_actor(request); need(a, 2, '홈 화면 관리')
    run("DELETE FROM site_settings WHERE key='home_blocks'")
    audit(a, '홈블록복원', '', '원본 순서·전체 노출')
    return {'ok': True}

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

CARD_CSS_SNIPPET = r"""<style id="mpCardCss">/* mpCardCss v20260715.6 */
/* KPOP·SHOP 카드 정보영역 공통 규격: 아티스트/브랜드 → 상품명 → 가격 → 상태 */
#shopGrid .col-card .col-body,
#shopGrid .k2g-card .k2g-body{padding:10px 12px 14px;display:flex;flex-direction:column;align-items:flex-start;min-height:116px;font-family:var(--body),'Noto Sans KR',sans-serif}
#shopGrid .card-artist{display:block;color:#718895;font-size:11.5px;font-weight:500;line-height:1.3;margin-bottom:4px}
#shopGrid .col-card .col-body h3{
  font-family:var(--body),'Noto Sans KR',sans-serif;font-size:12.5px;font-weight:700;line-height:1.38;letter-spacing:-.025em;min-height:35px;margin:0;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
#shopGrid .k2g-card .k2g-body h4{font-family:var(--body),'Noto Sans KR',sans-serif;font-size:12.5px;font-weight:700;line-height:1.38;letter-spacing:-.025em;min-height:35px;margin:0}
#shopGrid .col-card .price-row{display:block;margin-top:7px}
#shopGrid .col-card .k2g-price,#shopGrid .k2g-card .k2g-price{text-align:left;margin-top:7px}

#shopGrid .col-card .k2g-price .now{display:flex;gap:6px;align-items:baseline}
#shopGrid .k2g-price .pct{font-family:var(--body),'Noto Sans KR',sans-serif;font-size:15px;font-weight:700;color:#E8332A}
#shopGrid .k2g-price .amt,#shopGrid .col-card .price{font-family:var(--body),'Noto Sans KR',sans-serif;font-size:15px;font-weight:700;letter-spacing:-.02em}
#shopGrid .card-badges{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:7px}
#shopGrid .card-state{display:inline-flex;align-items:center;margin:0;padding:3px 6px;border-radius:3px;background:#050505;color:#fff;font-family:var(--body),'Noto Sans KR',sans-serif;font-size:8.5px;font-weight:700;line-height:1.25;letter-spacing:.02em}
#shopGrid .card-state.sold{background:#777}
#shopGrid .card-state.card-label{background:var(--badge-bg,#050505)}
</style>"""

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
_MP_CAT_TAG = {'album': 'ALBUM', 'md': 'MD', 'kfood': 'K-FOOD', 'apparel': 'APPAREL', 'living': 'LIFESTYLE'}
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
        if 'list_price' in _state['pcols']:
            sel += ', list_price'
        for c in ('img', 'category', 'badge', 'badge_color', 'created_at'):
            if c in _state['pcols']:
                sel += ', ' + c
        rs = rows('SELECT %s FROM products WHERE id LIKE ? ORDER BY id' % sel, ('mp::%',))
    except Exception:
        return ''
    if not rs:
        return ''
    def h(x):
        return str(x or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    def artist_of(name):
        clean = re.sub(r'^(?:\s*【[^】]+】)+\s*', '', str(name or '')).strip()
        lead = (clean.split(' - ', 1)[0] or clean).strip()
        par = re.search(r'\(([^)]*[가-힣][^)]*)\)', lead)
        if par:
            return par.group(1).strip()
        ko = re.search(r'[가-힣][가-힣&·\s]*', lead)
        return (ko.group(0).strip() if ko else (lead.split()[0] if lead else 'KPOP'))
    cards = []
    for i, r in enumerate(rs):
        name = str(r.get('name') or r['id'])
        cat = norm_cat(r.get('category'))
        soldout = bool(num(r.get('soldout')) or num(r.get('stock')) <= 0)
        badge = str(r.get('badge') or '').strip()
        label = badge or _MP_CAT_TAG.get(cat, 'MAPDAL')
        color = badge_color(r.get('badge_color'))
        gray = ';filter:grayscale(.85);opacity:.75' if soldout else ''
        img = (r.get('img') or '').strip()
        # 커버는 K2G 카드(.k2g-cover{aspect-ratio:1/1})와 동일한 1:1 정방형.
        # 인라인 height:auto가 정적 CSS(데스크톱 340px/모바일 190px)를 무효화하고
        # aspect-ratio가 폭 기준 정사각형을 강제 → center/cover로 중앙 크롭(=object-fit:cover 동일).
        _SQ = 'height:auto;aspect-ratio:1/1'
        if _MP_IMG_OK.fullmatch(img):
            cover = ('<div class="col-cover" style="background:#EDECE7 url(\'%s\') center/cover no-repeat;%s%s">'
                     '</div>') % (h(img), _SQ, gray)
        else:
            cover = ('<div class="col-cover" style="background:%s;%s%s">'
                     '<span class="big" style="font-size:44px">%s</span></div>'
                     ) % (_MP_COVERS[i % len(_MP_COVERS)], _SQ, gray, h(name[:2]))
        sale, was = num(r.get('price')), num(r.get('list_price'))
        pct = derived_pct(was, sale)
        if pct:
            # 통일 규격: 정가(취소선) 폐기 — 할인율(빨강)+할인가만 표기
            pr_html = ('<span class="k2g-price" style="margin-top:0">'
                       '<span class="now"><span class="pct">%d%%</span>'
                       '<span class="amt">₩%s</span></span></span>'
                       % (pct, format(sale, ',')))
        else:
            pr_html = '<span class="price">₩%s</span>' % format(sale, ',')
        states = []
        if is_new_product(r.get('created_at')):
            states.append('<span class="card-state new">NEW</span>')
        if label:
            states.append('<span class="card-state card-label" style="--badge-bg:%s">%s</span>' % (color, h(label)))
        if soldout:
            states.append('<span class="card-state add sold">SOLD OUT</span>')
        badges_html = '<div class="card-badges">%s</div>' % ''.join(states)
        cards.append('<a class="col-card" data-cat="%s" href="/p/%s">%s'
                     '<div class="col-body"><span class="card-artist">%s</span><h3>%s</h3>'
                     '<div class="price-row">%s</div>%s'
                     '</div></a>'
                     % (h(cat), h(r['id']), cover, h(artist_of(name)), h(name), pr_html, badges_html))
    return '<!-- mpShopDyn -->' + ''.join(cards) + '<!-- /mpShopDyn -->'

_own_rm_cache = {'t': 0.0, 'set': None}

def _own_removed_pages():
    """삭제된 정적(own) 상품의 페이지 슬러그 집합 (product-… 형태, 30초 캐시)."""
    if _own_rm_cache['set'] is not None and time.time() - _own_rm_cache['t'] < 30:
        return _own_rm_cache['set']
    try:
        s = {str(r['id']).split('::')[0].replace('.html', '')
             for r in rows('SELECT id FROM own_removed')}
    except Exception:
        s = _own_rm_cache['set'] or set()
    _own_rm_cache.update(t=time.time(), set=s)
    return s

_CARD_RE_CACHE = {}

def _hide_removed_static_cards(html):
    """삭제된 own 상품의 정적 col-card를 목록 페이지 서빙 시 제거 (멱등)."""
    pages = _own_removed_pages()
    if not pages or 'col-card' not in html:
        return html
    for pg in pages:
        rx = _CARD_RE_CACHE.get(pg)
        if rx is None:
            rx = re.compile(
                r'<(?:a|div)[^>]*class="[^"]*col-card[^"]*"[^>]*href="/?(?:%s)(?:\.html)?[?#"][^\x00]*?</(?:a|div)>'
                % re.escape(pg), re.S)
            # 위 패턴이 과탐지될 수 있어, 안전한 앵커 기반 2차 패턴을 기본 사용
            rx = re.compile(
                r'<a class="col-card"[^>]*href="/?%s(?:\.html)?"[\s\S]*?</a>' % re.escape(pg))
            _CARD_RE_CACHE[pg] = rx
        html = rx.sub('', html, count=1)
    return html

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
    # 정적 배열의 uid 집합과 DB k2g 행 수 비교 — 신규 항목이 있으면 백필 계속
    static_uids = {str(r[0]) for r in arr if isinstance(r, list) and len(r) >= 5}
    db_count = one("SELECT COUNT(*) AS n FROM products WHERE id LIKE ?", ('k2g::%',))
    db_n = db_count['n'] if db_count else 0
    if db_n > 0 and db_n >= len(static_uids):
        if one("SELECT 1 FROM products WHERE id LIKE ? AND sort_order IS NULL LIMIT 1", ('k2g::%',)) is None:
            return
    removed = _k2g_removed_set()
    nm, pr = _state['pname'] or 'name', _state['pprice'] or 'price'
    existing = {r['id']: r for r in rows(
        "SELECT id, img, list_price, sort_order, soldout, created_at FROM products WHERE id LIKE ?", ('k2g::%',))}
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
            cols = ['id', nm, pr, 'stock', 'soldout', 'img', 'category', 'list_price', 'sort_order']
            vals = [pid, name[:300], sale, 0, sold, img[:300], 'album', was, i]
            if 'created_at' in _state['pcols']:
                cols.append('created_at'); vals.append(now_iso())
            ops.append(('INSERT INTO products(%s) VALUES(%s)' %
                        (','.join(cols), ','.join(['?'] * len(vals))), tuple(vals)))
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
    """k2g:: 상품 → [uid, img, name, 정가, 판매가, 품절, NEW, 배지, 배지색]의
    JSON 문자열. 60초 캐시 + 쓰기 API가 즉시 무효화. <script> 내 삽입 안전 처리."""
    if _k2g_cat_cache['body'] is not None and time.time() - _k2g_cat_cache['t'] < 60:
        return _k2g_cat_cache['body']
    ensure_ready()
    if not _state['pcols'] or not _state['pname'] or not _state['pprice']:
        return None
    rs = rows("SELECT id, %s AS name, %s AS price, list_price, img, soldout, created_at, badge, badge_color FROM products "
              "WHERE id LIKE ? ORDER BY COALESCE(sort_order, 999999999), id"
              % (_state['pname'], _state['pprice']), ('k2g::%',))
    if not rs:
        return '[]' if _k2g_removed_set() else None   # 전부 삭제한 상태면 빈 카탈로그, 미백필이면 정적 폴백
    out = []
    for r in rs:
        sale, was = num(r.get('price')), num(r.get('list_price'))
        out.append([r['id'][5:], str(r.get('img') or ''), str(r.get('name') or ''),
                    was if was > sale else 0, sale, 1 if num(r.get('soldout')) else 0,
                    1 if is_new_product(r.get('created_at')) else 0,
                    str(r.get('badge') or '').strip(), badge_color(r.get('badge_color'))])
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
# ══════════════════════════════════════════════════════════════════════════
# 홈페이지 수정의견 반영 (2026-07 · 맵달_홈페이지_의견.pdf)
#   Q1 금액 폰트: 앨범상세 가격 Black Han Sans(--disp) → 고딕(--body) 700 (얇고 깔끔)
#   Q2 배송정보 : (제외 — 해외배송 불가 문구는 미표기, 글로벌 DDP 메시징 유지)
#   Q3+Q5 상단바: 데스크톱 nav a.top 폰트 13px/600/.08em → 14px/700/.02em (가독성·확대)
#   Q4 목록도구 : 정렬(신상품/낮은가격/높은가격/가나다) + 필터(품절제외·행사상품) +
#                Total 아이템 수 — /kpop(앨범 전용관)은 전체, /shop(굿즈 등)은 정렬·개수
#   Q7 공간소개 : mapdal-seoul 6F 'VIP 전용 주차장' 층·문구 삭제 (일반 고객 비노출)
#   ※ 정적 HTML 무수정 원칙 — 전량 서빙 시점 문자열 치환(멱등). _kpop_apply 직후 실행.
# ══════════════════════════════════════════════════════════════════════════
# 홈페이지 수정의견 반영 (2026-07 · 맵달_홈페이지_의견.pdf)
#   Q1 금액 폰트: 앨범상세 가격 Black Han Sans(--disp) → 고딕(--body) 700 (얇고 깔끔)
#   Q2 배송정보 : 앨범상세 [배송/교환] 탭에 '해외배송 불가' 명시
#   Q3+Q5 상단바: 데스크톱 nav a.top 폰트 13px/600/.08em → 14px/700/.02em (가독성·확대)
#   Q4 목록도구 : 정렬(신상품/낮은가격/높은가격/가나다) + 필터(품절제외·행사상품) +
#                Total 아이템 수 — /kpop(앨범 전용관)은 전체, /shop(굿즈 등)은 정렬·개수
#   Q7 공간소개 : mapdal-seoul 6F 'VIP 전용 주차장' 층·문구 삭제 (일반 고객 비노출)
#   ※ 정적 HTML 무수정 원칙 — 전량 서빙 시점 문자열 치환(멱등). _kpop_apply 직후 실행.
# ══════════════════════════════════════════════════════════════════════════

# [Q4] 목록 도구모음(정렬·필터·개수) — CSS+HTML+JS. shop.html/kpop 공통 로직.
#   · 기존 sync()/renderBatch()/VIEW 파이프라인에 훅(원본 함수 재정의 없이 확장).
#   · window.__mpApplyExtra: 원본 sync()가 매 호출 끝에 부르도록 sync 본문에 1줄 삽입.
_FB_TOOLBAR_CSS = (
    '<style id="mpListTools">'
    '#mpTools{display:flex;align-items:center;gap:14px 18px;flex-wrap:wrap;'
    'margin:2px 0 18px;padding-bottom:14px;border-bottom:1px solid var(--line)}'
    '#mpTools .cnt{font-family:var(--mono);font-size:12px;letter-spacing:.04em;'
    'color:var(--steel)}#mpTools .cnt b{color:var(--ink)}'
    '#mpTools .spring{flex:1 1 auto}'
    '#mpTools .chk{display:inline-flex;align-items:center;gap:6px;cursor:pointer;'
    'font-size:12.5px;color:var(--ink);user-select:none}'
    '#mpTools .chk input{width:15px;height:15px;accent-color:var(--red);cursor:pointer}'
    '#mpTools .event-filters{display:inline-flex;align-items:center;gap:12px;flex-wrap:wrap}'
    '#mpTools select{font-family:var(--body);font-size:12.5px;color:var(--ink);'
    'padding:7px 30px 7px 12px;border:1px solid var(--line);border-radius:6px;'
    'background:#fff url("data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' '
    'width=\'12\' height=\'12\' viewBox=\'0 0 12 12\'><path d=\'M2 4l4 4 4-4\' stroke=\'%23141414\' '
    'stroke-width=\'1.6\' fill=\'none\'/></svg>") no-repeat right 10px center;'
    '-webkit-appearance:none;appearance:none;cursor:pointer}'
    '@media(max-width:640px){#mpTools{gap:10px 14px}#mpTools .spring{display:none}'
    '#mpTools .cnt{width:100%;order:-1}}'
    '</style>'
)

# 도구모음 마크업 — filter-bar 다음, shopGrid 앞에 삽입. data-mode로 페이지 구분.
def _fb_toolbar_html(mode):
    # 품절제외·행사유형 체크박스는 앨범(=행사/품절 상태 존재) 목록에만 노출.
    chks = ''
    if mode == 'kpop':
        chks = (
            '<label class="chk"><input type="checkbox" id="mpFhide">품절 제외</label>'
            '<span class="event-filters" role="group" aria-label="행사 유형">'
            '<label class="chk"><input type="checkbox" id="mpFcall">FANCALL</label>'
            '<label class="chk"><input type="checkbox" id="mpFsign">팬싸인회</label>'
            '<label class="chk"><input type="checkbox" id="mpFlucky">럭키드로우</label>'
            '</span>'
        )
    return (
        '<div id="mpTools" data-mode="' + mode + '">'
        '<span class="cnt">Total <b id="mpCnt">0</b> items</span>'
        + chks +
        '<span class="spring"></span>'
        '<select id="mpSort" aria-label="정렬">'
        '<option value="new">신상품순</option>'
        '<option value="asc">낮은가격순</option>'
        '<option value="desc">높은가격순</option>'
        '<option value="name">가나다순</option>'
        '</select>'
        '</div>'
    )

# [Q4] JS — 원본 sync() 말미에서 호출되는 확장 훅. 원본 전역(F,Q,K2G,VIEW,ptr,
#   albumEligible,renderBatch,K2G_FIRST 등)을 그대로 사용. 앨범은 VIEW 정렬/필터 후
#   재렌더, 자체상품(col-card)은 DOM 정렬. 개수는 표시중 항목 합산.
_FB_TOOLS_JS = r"""<script id="mpListToolsJs">/* mpListTools v20260715.3 */(function(){
  var T=document.getElementById('mpTools'); if(!T) return;
  var MODE=T.dataset.mode, grid=document.getElementById('shopGrid');
  // 최초 카탈로그 순번을 별도 보존한다. 품절 플래그(r[5])가 바뀌어도
  // 신상품순 위치와 다른 정렬의 동순위 위치는 절대 달라지지 않는다.
  var originalOrder={};
  if(typeof K2G!=='undefined') K2G.forEach(function(r,i){originalOrder[String(r[0])]=i;});
  function opos(r){var n=originalOrder[String(r[0])];return typeof n==='number'?n:999999999;}
  function pnum(el){ // col-card 가격 텍스트(₩4,500~)→숫자
    var t=(el.querySelector('.price')||{}).textContent||''; 
    var m=t.replace(/[^0-9]/g,''); return m?parseInt(m,10):0; }
  function cname(el){ return ((el.querySelector('h3,h4')||{}).textContent||'').trim(); }
  // SHOP의 기존 정적 카드도 KPOP과 같은 정보 구조로 보정한다.
  function decorateCards(){
    [].slice.call(grid.querySelectorAll('.col-card')).forEach(function(c){
      var body=c.querySelector('.col-body'); if(!body) return;
      var title=(body.querySelector('h3')||{}).textContent||'';
      var tagEl=c.querySelector('.tag'),tagText=((tagEl||{}).textContent||'').trim();
      if(!body.querySelector('.card-artist')){
        var tag=tagText.replace(/\s*·.*$/,'').trim();
        var clean=title.replace(/^(?:\s*【[^】]+】)+\s*/,'').trim();
        var lead=(clean.split(' - ')[0]||clean).trim(), par=lead.match(/\(([^)]+)\)/);
        var ko=lead.match(/[가-힣][가-힣&·\s]*/);
        var artist=(par&&/[가-힣]/.test(par[1]))?par[1].trim():(ko?ko[0].trim():(lead.split(/\s+/)[0]||tag));
        var label=(tag&&!/^ALBUM$/i.test(tag))?tag:(artist||tag||'MAPDAL');
        var el=document.createElement('span'); el.className='card-artist'; el.textContent=label;
        body.insertBefore(el,body.querySelector('h3'));
      }
      if(!body.querySelector('.card-badges')){
        var row=document.createElement('div');row.className='card-badges';
        var add=body.querySelector('.add');
        var sold=(add&&/품절|SOLD OUT/i.test(add.textContent||''))||/품절|SOLD OUT/i.test(tagText);
        if(add&&add.parentNode)add.parentNode.removeChild(add);
        var label=tagText.replace(/\s*·?\s*(?:품절|SOLD OUT)\s*/ig,'').trim();
        if(label){
          var lb=document.createElement('span');lb.className='card-state card-label';lb.textContent=label;row.appendChild(lb);
        }
        if(sold){var so=document.createElement('span');so.className='card-state add sold';so.textContent='SOLD OUT';row.appendChild(so);}
        if(row.childNodes.length)body.appendChild(row);
      }
      if(tagEl&&tagEl.parentNode)tagEl.parentNode.removeChild(tagEl);
    });
  }
  // 상품명에 명시된 행사 유형만 분류한다. "Lucky Man", "Drawstring" 같은
  // 일반 앨범명은 럭키드로우로 오인하지 않도록 결합어 패턴을 사용한다.
  function eventTypes(name){
    var n=String(name||''), types=[];
    if(/럭키\s*드로우|럭드|LUCKY[\s_-]*DRAW/i.test(n)) types.push('luckydraw');
    if(/대면\s*사인회|팬\s*사인회|팬싸인회|팬싸|FAN[\s_-]*SIGN/i.test(n)) types.push('fansign');
    if(/영상\s*통화|영통|FAN[\s_-]*CALL|FANCALL/i.test(n)) types.push('fancall');
    return types;
  }
  function selectedTypes(){
    var selected=[];
    if((document.getElementById('mpFcall')||{}).checked) selected.push('fancall');
    if((document.getElementById('mpFsign')||{}).checked) selected.push('fansign');
    if((document.getElementById('mpFlucky')||{}).checked) selected.push('luckydraw');
    return selected;
  }
  // 원본 VIEW(앨범 데이터셋)에 정렬/필터 적용본을 만들어 되돌려줌
  window.__mpBuildView=function(base){
    var v=base.slice(), s=(document.getElementById('mpSort')||{}).value||'new';
    var hide=(document.getElementById('mpFhide')||{}).checked;
    var selected=selectedTypes();
    if(hide) v=v.filter(function(r){return !r[5];});          // r[5]=품절
    // 미선택은 전체, 복수 선택은 행사 유형 합집합(OR)
    if(selected.length) v=v.filter(function(r){
      var types=eventTypes(r[2]);
      return selected.some(function(type){return types.indexOf(type)!==-1;});
    });
    if(s==='asc')  v.sort(function(a,b){return ((a[4]||0)-(b[4]||0))||(opos(a)-opos(b));});
    else if(s==='desc') v.sort(function(a,b){return ((b[4]||0)-(a[4]||0))||(opos(a)-opos(b));});
    else if(s==='name') v.sort(function(a,b){return (a[2]||'').localeCompare(b[2]||'','ko')||(opos(a)-opos(b));});
    else v.sort(function(a,b){return opos(a)-opos(b);}); // 'new': 품절과 무관한 원본 순서
    return v;
  };
  // 자체상품 카드 정렬(표시중인 것만) — grid 내 col-card 재배치
  function sortCards(){
    var s=(document.getElementById('mpSort')||{}).value||'new';
    var cards=[].slice.call(grid.querySelectorAll('.col-card'));
    if(!cards.length) return;
    // DB 자체상품도 K2G와 같은 행사/품절 규칙으로 표시 상태를 계산한다.
    if(MODE==='kpop'){
      var selected=selectedTypes(), hide=(document.getElementById('mpFhide')||{}).checked;
      cards.forEach(function(c){
        var text=(c.textContent||''), okF=(typeof F==='undefined'||F==='all'||c.dataset.cat===F);
        var okQ=(typeof Q==='undefined'||!Q||text.toLowerCase().includes(Q));
        var types=eventTypes(text);
        var okE=!selected.length||selected.some(function(type){return types.indexOf(type)!==-1;});
        var sold=/품절|SOLD OUT/i.test(((c.querySelector('.add')||{}).textContent||''));
        c.style.display=okF&&okQ&&okE&&(!hide||!sold)?'':'none';
      });
    }
    var vis=cards.filter(function(c){return c.style.display!=='none';});
    if(s==='new'){ // 원래 문서순 복원
      vis.sort(function(a,b){return (+a.dataset.mpseq||0)-(+b.dataset.mpseq||0);});
    } else if(s==='asc'){ vis.sort(function(a,b){return pnum(a)-pnum(b);}); }
    else if(s==='desc'){ vis.sort(function(a,b){return pnum(b)-pnum(a);}); }
    else if(s==='name'){ vis.sort(function(a,b){return cname(a).localeCompare(cname(b),'ko');}); }
    // 직접 등록 상품은 K2G 제휴 상품보다 앞에 유지한다. 더보기 영역(lmWrap)은
    // grid 바깥에 있으므로 insertBefore 기준으로 사용할 수 없다.
    var firstK2g=grid.querySelector('.k2g-card');
    vis.forEach(function(c){ grid.insertBefore(c, firstK2g||null); });
  }
  function count(){
    var n=0;
    grid.querySelectorAll('.col-card').forEach(function(c){ if(c.style.display!=='none') n++; });
    // 앨범: 로드된 DOM이 아니라 전체 VIEW 길이로 집계(더보기 방식이므로)
    if(typeof VIEW!=='undefined' && typeof albumEligible==='function' && albumEligible())
      n += VIEW.length;
    var el=document.getElementById('mpCnt'); if(el) el.textContent=n.toLocaleString('ko-KR');
  }
  // 기존 SHOP 카드의 마크업 통일 후 원문서순 기록(정렬 후 복원용)
  decorateCards();
  [].slice.call(grid.querySelectorAll('.col-card')).forEach(function(c,i){ c.dataset.mpseq=i; });
  // 원본 sync() 말미 훅: 앨범 재렌더는 sync가 담당하므로 여기선 카드정렬+개수만.
  window.__mpApplyExtra=function(){ sortCards(); count(); };
  // 컨트롤 이벤트 → 원본 sync() 재호출(앨범 VIEW 재구성 포함)
  T.addEventListener('change', function(){ if(typeof sync==='function') sync(); else window.__mpApplyExtra(); });
  // 더보기 클릭 후에도 개수·정렬 반영
  var lb=document.getElementById('lmBtn');
  if(lb) lb.addEventListener('click', function(){ setTimeout(window.__mpApplyExtra,0); });
  window.__mpApplyExtra();
})();</script>"""


def _feedback_apply(html):
    """홈페이지 수정의견 5건 서빙 시점 반영 (멱등)."""
    if not isinstance(html, str) or '</html>' not in html:
        return html

    # ── [Q3+Q5] 데스크톱 상단 nav 폰트: 가독성↑·소폭 확대 (전 페이지 동일 룰) ──
    html = html.replace(
        'a.top{font-size:13px;font-weight:600;letter-spacing:.08em;',
        'a.top{font-size:14px;font-weight:700;letter-spacing:.02em;', 1)

    # ── [Q1] 앨범상세 가격 폰트: Black Han Sans → 고딕(--body) 700 ──
    html = html.replace(
        '.price-block .amt{font-family:var(--disp);font-size:30px}',
        '.price-block .amt{font-family:var(--body);font-weight:700;font-size:29px;letter-spacing:-.01em}', 1)
    html = html.replace(
        '.price-block .pct{font-family:var(--disp);font-size:26px;color:var(--red)}',
        '.price-block .pct{font-family:var(--body);font-weight:700;font-size:24px;color:var(--red)}', 1)

    # ── [Q2] 앨범상세 [배송/교환] 탭: '해외배송 불가' 명시 (국내배송 행 확장) ──
    _q2_src = ('<tr><th>국내배송</th><td>3,000원 (30,000원 이상 무료) · '
               '오후 2시 이전 결제 시 당일 출고</td></tr>')
    if _q2_src in html and '해외배송' not in html:
        html = html.replace(
            _q2_src,
            _q2_src + '<tr><th>해외배송</th><td>현재 <b>해외배송은 제공하지 않습니다</b> '
            '(국내 배송지만 가능)</td></tr>', 1)

    # ── [Q7] mapdal-seoul 6F 'VIP 전용 주차장' 삭제 (층 카드 + 방문안내 문구) ──
    _q7_floor = ('<div class="floor rf" role="listitem" data-t="VIP 전용 주차장" '
                 'data-n="6F" data-d="아티스트·VIP 동선 분리, 보안·의전 대응을 위한 전용 주차 공간.">'
                 '\n          <span class="no">6F</span><span class="nm">VIP 전용 주차장</span>'
                 '<span class="en">VIP PARKING</span>\n        </div>')
    if _q7_floor in html:
        html = html.replace(_q7_floor, '', 1)
        # 방문 안내 하단 6F 주차 문구도 함께 제거
        html = html.replace(
            '<p style="font-size:13px;color:var(--steel);margin-top:6px">'
            '6F VIP 전용 주차 — 동선 분리·보안·의전 대응</p>', '', 1)

    # ── [Q4] 목록 도구모음: shop.html / kpop 에만 (shopGrid+filter-bar 보유 페이지) ──
    if 'id="shopGrid"' in html and 'id="mpTools"' not in html:
        _is_kpop = ('KPOP(음반) — MAPDAL SEOUL' in html) or ('<h1>KPOP(음반)</h1>' in html) \
                   or ('<!--MP_KPOP-->' in html)
        _mode = 'kpop' if _is_kpop else 'shop'
        # CSS (head 주입) + 도구모음 마크업(filter-bar 다음 or shopGrid 앞) + JS
        if 'id="mpListTools"' not in html:
            _h = html.lower().find('</head>')
            if _h >= 0:
                html = html[:_h] + _FB_TOOLBAR_CSS + html[_h:]
        _tb = _fb_toolbar_html(_mode)
        # shopGrid 여는 <div> 바로 앞에 삽입 (들여쓰기 편차 무관하게 grid 태그 기준)
        _gi = html.find('<div class="journal-grid" id="shopGrid"')
        if _gi == -1:
            _k = html.find('id="shopGrid"')
            _gi = html.rfind('<div', 0, _k) if _k >= 0 else -1
        if _gi >= 0:
            html = html[:_gi] + _tb + '\n      ' + html[_gi:]
        # 원본 sync() 말미에 확장 훅 1줄 삽입 (멱등)
        if 'window.__mpApplyExtra' in _FB_TOOLS_JS and 'mpSyncHook' not in html:
            html = html.replace(
                'if(albumEligible())renderBatch(K2G_FIRST);else updateLM();}',
                'if(albumEligible())renderBatch(K2G_FIRST);else updateLM();'
                '/*mpSyncHook*/if(window.__mpApplyExtra)window.__mpApplyExtra();}', 1)
            # 앨범 VIEW 구성 지점: sync가 VIEW=... 로 좁힌 뒤 정렬/필터 적용
            html = html.replace(
                'VIEW=Q?K2G.filter(r=>r[2].toLowerCase().includes(Q)):K2G;',
                'VIEW=Q?K2G.filter(r=>r[2].toLowerCase().includes(Q)):K2G;'
                'if(window.__mpBuildView)VIEW=window.__mpBuildView(VIEW);', 1)
        # JS 주입 (body 말미는 _inject_auth가 처리하나, 여기선 즉시 삽입해 순서 보장)
        if 'id="mpListToolsJs"' not in html:
            _b = html.lower().rfind('</body>')
            html = (html[:_b] + _FB_TOOLS_JS + html[_b:]) if _b >= 0 else (html + _FB_TOOLS_JS)

    return html


# ══════════════════════════════════════════════════════════════════════════
# 체크아웃 정비 (2026-07 · 해외배송 제외 + 결제수단 정합화)
#   · 해외 배송(DDP) 토글·안내문·해외 주소폼 제거 → 국내 배송 전용
#   · 결제수단: 오해 소지 있던 4-라디오(선택 무시됨) → 실제 동작하는 항목만.
#     토스 v2 '통합결제창'(method:CARD)은 카드+간편결제(카카오·네이버·토스페이)를
#     한 창에서 처리하므로, 신용카드/간편결제를 1개 항목으로 통합. 해외카드 항목 제거.
#   · 결제 실행부: 주문생성→INIStdPay.pay()→서버 /inicis/return 승인 (app.py).
#     라이브 전환은 INICIS_MID/INICIS_SIGNKEY/INICIS_INIAPI 실계약값 교체(운영 액션)뿐.
#   ※ 정적 HTML 무수정 — 서빙 시점 문자열 치환(멱등). _feedback_apply와 동일 파이프라인.
# ══════════════════════════════════════════════════════════════════════════

def _checkout_apply(html):
    """체크아웃(checkout.html)만 정비 — 해외배송 제거 + 결제수단 정합화 (멱등)."""
    if not isinstance(html, str) or 'id="tIntl"' not in html:
        return html   # 체크아웃 페이지가 아니면 통과 (tIntl 토글은 checkout 고유)

    # ── [1] 배송지: DDP 안내문 제거 ──
    html = html.replace(
        '<div class="sub">해외 배송 선택 시 관세·세금이 선지불(DDP)로 합산되어 수령 시 추가 비용이 없습니다.</div>',
        '<div class="sub">국내 전 지역 배송 · 30,000원 이상 무료배송 · 오후 2시 이전 결제 시 당일 출고.</div>', 1)

    # ── [2] 배송지: 국내/해외(DDP) 토글 버튼 제거 ──
    html = html.replace(
        '<div class="toggle-2"><button class="on" id="tDom">국내 배송</button><button id="tIntl">해외 배송 (DDP)</button></div>',
        '', 1)

    # ── [3] 결제수단: 카드+간편결제 통합(실제 통합결제창과 일치) · 해외카드 제거 ──
    #   기존 라디오는 value·핸들러가 없어 선택이 무시됐음 → 실제 동작 항목만 노출.
    html = html.replace(
        '<label class="radio-item on"><input type="radio" name="pay" checked><span class="rd"><b>신용/체크카드</b><small>국내 전 카드사</small></span></label>',
        '<label class="radio-item on"><input type="radio" name="pay" value="CARD" checked>'
        '<span class="rd"><b>신용·체크카드 · 간편결제</b>'
        '<small>카카오페이 · 네이버페이 · 페이코 · 삼성페이 · 국내 전 카드사</small></span></label>', 1)
    # 중복된 간편결제 라디오 제거 (위 통합 항목에 포함)
    html = html.replace(
        '<label class="radio-item"><input type="radio" name="pay"><span class="rd"><b>카카오페이 · 네이버페이 · 토스페이</b></span></label>',
        '', 1)
    # 해외 결제 라디오 제거 (해외배송 종료)
    html = html.replace(
        '<label class="radio-item"><input type="radio" name="pay"><span class="rd"><b>PayPal · Alipay · 해외 카드</b><small>해외 배송 주문 권장</small></span></label>',
        '', 1)

    # ── [4] intl JS 무력화: 토글 버튼이 사라졌으므로 null 참조 방지 + 항상 국내 ──
    if '/*mpNoIntl*/' not in html:
        html = html.replace(
            "const dom=document.getElementById('tDom'),intlB=document.getElementById('tIntl');",
            "/*mpNoIntl*/const dom=document.getElementById('tDom'),intlB=document.getElementById('tIntl');"
            "intl=false;", 1)
        # 토글 클릭 리스너: 버튼이 없으면(=제거됨) 건너뜀
        html = html.replace(
            "dom.addEventListener('click',()=>setIntl(false));",
            "if(dom)dom.addEventListener('click',()=>setIntl(false));", 1)
        html = html.replace(
            "intlB.addEventListener('click',()=>setIntl(true));",
            "if(intlB)intlB.addEventListener('click',()=>setIntl(true));", 1)

    # ── [5] 결제 SDK: 토스 → KG이니시스 INIStdPay ──
    html = html.replace(
        '<script src="https://js.tosspayments.com/v2/standard"></script>',
        '<script src="https://stdpay.inicis.com/stdjs/INIStdPay.js" charset="UTF-8"></script>', 1)

    # ── [5-1] 결제 안내 문구: 토스페이먼츠 → KG이니시스 브랜딩 ──
    html = html.replace(
        '토스페이먼츠 안전결제 · 카드/간편결제 지원',
        'KG이니시스 안전결제 · 카드 · 계좌이체 · 간편결제 지원', 1)

    # ── [5-2] 결제 모드 문구: 라이브 MID면 '테스트 모드' 경고 제거 (멱등) ──
    _test_line = ('<br><span style="color:var(--red)">현재 테스트 모드 — '
                  '실제 과금되지 않습니다 (라이브 키 전환 시 실결제)</span>')
    if inicis_mid() != 'INIpayTest':
        html = html.replace(_test_line, '', 1)

    # ── [6] 결제 실행부: 토스 requestPayment → INIStdPay 폼 POST ──
    #   /api/orders 응답의 od.inicis(서버 서명 파라미터)로 히든폼 생성 후 INIStdPay.pay().
    #   이후 인증→승인은 서버 /inicis/return 이 처리하고 /order-complete 로 리다이렉트.
    #   ★ 팝업 차단 방지: 주문 생성을 동기 XHR로 처리해 INIStdPay.pay()가 클릭 제스처
    #     안에서 동기 호출되도록 함. (await fetch 뒤 호출하면 브라우저가 팝업을 차단함)
    _toss_handler = (
        "document.getElementById('payBtn').addEventListener('click',async()=>{\n"
        "  if(!items.length)return;\n"
        "  if(!document.getElementById('ag1').checked){alert('필수 약관에 동의해 주세요.');return;}\n"
        "  const buyer={\n"
        "    name:document.getElementById('bName').value.trim(),\n"
        "    phone:document.getElementById('bPhone').value.trim(),\n"
        "    zip:document.getElementById('zip').value.trim(),\n"
        "    addr1:document.getElementById('addr1').value.trim(),\n"
        "    addr2:document.getElementById('addr2').value.trim()};\n"
        "  const btn=document.getElementById('payBtn');btn.disabled=true;btn.textContent='주문 생성중…';\n"
        "  try{\n"
        "    const res=await fetch('/api/orders',{method:'POST',headers:{'Content-Type':'application/json'},\n"
        "      body:JSON.stringify({items:items.map(i=>({id:i.id,q:i.q})),buyer,shipMethod:shipMethod(),intl})});\n"
        "    const od=await res.json();\n"
        "    if(!res.ok)throw new Error(od.detail||'주문 생성 실패');\n"
        "    const toss=TossPayments(CFG.clientKey);\n"
        "    const payment=toss.payment({customerKey:TossPayments.ANONYMOUS});\n"
        "    await payment.requestPayment({\n"
        "      method:'CARD',\n"
        "      amount:{currency:'KRW',value:od.amount},\n"
        "      orderId:od.orderId,orderName:od.orderName,\n"
        "      customerName:buyer.name||'맵달 고객',\n"
        "      successUrl:location.origin+'/order-complete',\n"
        "      failUrl:location.origin+'/checkout?fail=1',\n"
        "      card:{useEscrow:false,flowMode:'DEFAULT',useCardPoint:false,useAppCardOnly:false}\n"
        "    });")
    _ini_handler = (
        "document.getElementById('payBtn').addEventListener('click',function(){\n"
        "  if(!items.length)return;\n"
        "  if(!document.getElementById('ag1').checked){alert('필수 약관에 동의해 주세요.');return;}\n"
        "  const buyer={\n"
        "    name:document.getElementById('bName').value.trim(),\n"
        "    phone:document.getElementById('bPhone').value.trim(),\n"
        "    zip:document.getElementById('zip').value.trim(),\n"
        "    addr1:document.getElementById('addr1').value.trim(),\n"
        "    addr2:document.getElementById('addr2').value.trim(),\n"
        "    memo:(typeof mpMemo==='function'?mpMemo():'')};\n"
        "  const btn=document.getElementById('payBtn');btn.disabled=true;btn.textContent='주문 생성중…';\n"
        "  try{\n"
        "    // 동기 XHR: 응답을 받은 뒤에도 클릭 제스처가 유지되어 결제창이 팝업 차단되지 않음\n"
        "    const xhr=new XMLHttpRequest();\n"
        "    xhr.open('POST','/api/orders',false);\n"
        "    xhr.setRequestHeader('Content-Type','application/json');\n"
        "    xhr.send(JSON.stringify({items:items.map(i=>({id:i.id,q:i.q})),buyer,shipMethod:shipMethod(),intl}));\n"
        "    const od=JSON.parse(xhr.responseText||'{}');\n"
        "    if(xhr.status<200||xhr.status>=300)throw new Error(od.detail||'주문 생성 실패');\n"
        "    if(!od.inicis)throw new Error('결제 파라미터 생성 실패');\n"
        "    var f=document.getElementById('mpIniForm');if(f)f.remove();\n"
        "    f=document.createElement('form');f.id='mpIniForm';f.method='POST';f.acceptCharset='UTF-8';\n"
        "    Object.keys(od.inicis).forEach(function(k){var inp=document.createElement('input');\n"
        "      inp.type='hidden';inp.name=k;inp.value=od.inicis[k];f.appendChild(inp);});\n"
        "    document.body.appendChild(f);\n"
        "    if(typeof INIStdPay==='undefined')throw new Error('결제 모듈 로딩 실패 — 새로고침 후 다시 시도해 주세요');\n"
        "    INIStdPay.pay('mpIniForm');   // 클릭 제스처 내 동기 호출 → 팝업 차단 회피")
    if _toss_handler in html:
        html = html.replace(_toss_handler, _ini_handler, 1)

    # ── [8] 배송 방법 정리: 맵달드림(당일)·성수 1F 픽업 제거 → 일반배송만 (멱등) ──
    html = html.replace(
        '<label class="radio-item"><input type="radio" name="ship" value="dream">'
        '<span class="rd"><b>맵달드림 — 서울 당일배송</b><small>21시 이전 주문 당일 처리</small></span>'
        '<span class="rp">무료 (3만 이상)</span></label>', '', 1)
    html = html.replace(
        '<label class="radio-item"><input type="radio" name="ship" value="pickup">'
        '<span class="rd"><b>성수 1F 픽업</b><small>1시간 내 준비 · 현장 특전 동봉</small></span>'
        '<span class="rp">무료 + 특전</span></label>', '', 1)

    # ── [9] 결제 금액 요약: 상품별 수량조절(−/+ 1~99)·삭제(✕) — localStorage 동기화 (멱등) ──
    if 'function mpQty(' not in html:
        html = html.replace(
            "function renderSum(){",
            "function mpSaveCart(){try{localStorage.setItem(CK,JSON.stringify(items))}catch(e){}\n"
            "  var _c=items.reduce(function(a,i){return a+(i.q||0)},0);\n"
            "  var _el=document.querySelector('.util .cart');if(_el)_el.textContent='CART \u00b7 '+_c;}\n"
            "function mpQty(ix,d){var it=items[ix];if(!it)return;"
            "it.q=Math.max(1,Math.min(99,(it.q||1)+d));mpSaveCart();renderSum();}\n"
            "function mpRm(ix){if(!items[ix])return;items.splice(ix,1);mpSaveCart();renderSum();}\n"
            "function renderSum(){", 1)
        _old_render = (
            "  document.getElementById('ckItems').innerHTML=items.length\n"
            "    ? items.map(i=>`${i.n.replace(/</g,'&lt;').slice(0,34)}${i.n.length>34?'…':''} × ${i.q}`).join('<br>')\n"
            "    : '장바구니가 비어 있습니다 — <a href=\"/shop\" style=\"text-decoration:underline\">쇼핑하러 가기</a>';")
        _new_render = (
            "  document.getElementById('ckItems').innerHTML=items.length\n"
            "    ? items.map(function(i,ix){var nm=String(i.n).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\"/g,'&quot;');\n"
            "      return '<div style=\"display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px dashed var(--line)\">'\n"
            "      +'<span style=\"flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap\" title=\"'+nm+'\">'+nm+'</span>'\n"
            "      +'<span style=\"display:inline-flex;border:1px solid var(--line);background:#fff;flex:none\">'\n"
            "      +'<button type=\"button\" onclick=\"mpQty('+ix+',-1)\" aria-label=\"수량 줄이기\" style=\"width:22px;height:22px;border:none;background:#fff;cursor:pointer;line-height:1\">\u2212</button>'\n"
            "      +'<span style=\"width:26px;text-align:center;line-height:22px;font-family:var(--mono)\">'+i.q+'</span>'\n"
            "      +'<button type=\"button\" onclick=\"mpQty('+ix+',1)\" aria-label=\"수량 늘리기\" style=\"width:22px;height:22px;border:none;background:#fff;cursor:pointer;line-height:1\">+</button></span>'\n"
            "      +'<span style=\"font-family:var(--mono);min-width:64px;text-align:right;flex:none\">'+fmt(i.p*i.q)+'</span>'\n"
            "      +'<button type=\"button\" onclick=\"mpRm('+ix+')\" aria-label=\"삭제\" title=\"삭제\" style=\"border:none;background:none;color:var(--steel);cursor:pointer;font-size:14px;padding:0 2px;flex:none\">\u2715</button>'\n"
            "      +'</div>';}).join('')\n"
            "    : '장바구니가 비어 있습니다 — <a href=\"/shop\" style=\"text-decoration:underline\">쇼핑하러 가기</a>';")
        if _old_render in html:
            html = html.replace(_old_render, _new_render, 1)

    # ── [10] 배송 메모: 프리셋 선택 + 공동현관 비밀번호/직접 입력 (멱등) ──
    if 'mpMemoSel' not in html:
        html = html.replace(
            '<input class="f-input" id="addr2" placeholder="상세 주소 · 공동현관 비밀번호 등 배송 메모">',
            '<input class="f-input" id="addr2" placeholder="상세 주소 (동·호수 등)">\n'
            '          <select class="f-input" id="mpMemoSel" style="cursor:pointer">\n'
            '            <option value="" selected>배송 메모 선택 (선택사항)</option>\n'
            '            <option>문 앞에 놓아주세요</option>\n'
            '            <option>경비실(관리실)에 맡겨주세요</option>\n'
            '            <option>택배함에 넣어주세요</option>\n'
            '            <option>부재 시 연락 주세요</option>\n'
            '            <option>배송 전에 미리 연락 주세요</option>\n'
            '            <option value="__door">공동현관 비밀번호 입력…</option>\n'
            '            <option value="__custom">직접 입력…</option>\n'
            '          </select>\n'
            '          <input class="f-input" id="mpMemoTxt" maxlength="80" style="display:none" autocomplete="off">', 1)
        html = html.replace(
            "// ── 결제 실행 ──",
            "// ── 배송 메모 (mpMemoSel) ──\n"
            "function mpMemo(){var s=document.getElementById('mpMemoSel'),t=document.getElementById('mpMemoTxt');\n"
            "  if(!s)return'';var v=s.value;\n"
            "  if(v==='__door'){var x=t&&t.value.trim();return x?('공동현관 비밀번호: '+x):'';}\n"
            "  if(v==='__custom')return (t&&t.value.trim())||'';\n"
            "  return v;}\n"
            "(function(){var s=document.getElementById('mpMemoSel'),t=document.getElementById('mpMemoTxt');\n"
            "  if(!s||!t)return;\n"
            "  s.addEventListener('change',function(){var v=s.value;\n"
            "    if(v==='__door'){t.style.display='block';t.placeholder='공동현관 비밀번호 입력 (예: #1234*)';t.value='';t.focus();}\n"
            "    else if(v==='__custom'){t.style.display='block';t.placeholder='배송 메모를 직접 입력해 주세요 (80자 이내)';t.value='';t.focus();}\n"
            "    else{t.style.display='none';t.value='';}});\n"
            "})();\n"
            "// ── 결제 실행 ──", 1)

    # ── [7] 실패 시 실제 사유(msg) 표시 — 서버가 /checkout?fail=1&msg=... 로 전달 ──
    html = html.replace(
        "if(new URLSearchParams(location.search).get('fail'))\n"
        "  setTimeout(()=>alert('결제가 완료되지 않았습니다. 다시 시도해 주세요.'),300);",
        "(function(){var _q=new URLSearchParams(location.search);\n"
        "  if(_q.get('fail')){var _m=_q.get('msg');\n"
        "    setTimeout(()=>alert(_m?('결제가 완료되지 않았습니다.\\n\\n사유: '+_m):'결제가 완료되지 않았습니다. 다시 시도해 주세요.'),300);}\n"
        "})();", 1)

    return html


def _order_complete_apply(html):
    """order-complete.html: 토스 confirm 호출 → 이니시스 서버승인 결과(oid) 표시 (멱등)."""
    if not isinstance(html, str) or "const pk=q.get('paymentKey')" not in html:
        return html   # order-complete 페이지가 아니면 통과
    # 토스 결제결과 확인 IIFE 전체 → oid 기반 주문조회로 교체
    _toss_iife_start = "(async function(){\n  const q=new URLSearchParams(location.search);\n  const pk=q.get('paymentKey'),oid=q.get('orderId'),amt=q.get('amount');"
    _new_iife_start = (
        "(async function(){\n"
        "  const q=new URLSearchParams(location.search);\n"
        "  const oid=q.get('oid')||q.get('orderId');")
    html = html.replace(_toss_iife_start, _new_iife_start, 1)
    # 본문: pk/amt 기반 confirm 호출부 → 서버가 이미 승인한 주문을 조회해 표시
    _toss_body = (
        "  if(pk&&oid&&amt){\n"
        "    title.textContent='결제를 확인하고 있습니다…';\n"
        "    try{\n"
        "      const r=await fetch('/api/payments/confirm',{method:'POST',headers:{'Content-Type':'application/json'},\n"
        "        body:JSON.stringify({paymentKey:pk,orderId:oid,amount:+amt})});\n"
        "      const d=await r.json();\n"
        "      if(!r.ok)throw new Error(d.detail||'승인 실패');\n"
        "      title.textContent='주문이 완료되었습니다';\n"
        "      desc.innerHTML='결제 금액 <b>₩'+(+amt).toLocaleString('ko-KR')+'</b> · '+(d.method||'카드')+' 결제가 승인되었습니다.'+(d.receipt?' <a href=\"'+d.receipt+'\" target=\"_blank\" style=\"text-decoration:underline\">매출전표 보기</a>':'');\n"
        "      ono.textContent='ORDER NO. '+oid;\n"
        "      try{localStorage.removeItem('mapdal_cart');}catch(e){}\n"
        "    }catch(e){")
    _new_body = (
        "  if(oid){\n"
        "    title.textContent='주문을 확인하고 있습니다…';\n"
        "    try{\n"
        "      const r=await fetch('/api/orders/'+encodeURIComponent(oid));\n"
        "      const d=await r.json();\n"
        "      if(!r.ok)throw new Error(d.detail||'주문 조회 실패');\n"
        "      if(d.status!=='PAID')throw new Error('결제가 완료되지 않았습니다');\n"
        "      title.textContent='주문이 완료되었습니다';\n"
        "      desc.innerHTML='결제 금액 <b>₩'+(+d.amount).toLocaleString('ko-KR')+'</b> · 결제가 정상 승인되었습니다.';\n"
        "      ono.textContent='ORDER NO. '+oid;\n"
        "      try{localStorage.removeItem('mapdal_cart');}catch(e){}\n"
        "    }catch(e){")
    html = html.replace(_toss_body, _new_body, 1)
    return html


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
    r'<div>\s*<a class="top(?:[^"]*)" href="(?:(?:\./)?shop\.html|/shop)"[^>]*>\s*SHOP\s*</a>\s*</div>')
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
    if 'class="top" href="/kpop"' not in html and 'class="top active-page" href="/kpop"' not in html:
        html = _KPOP_NAV_RE.sub(
            lambda m: '<div><a class="top" href="/kpop">KPOP(음반)</a></div>' + m.group(0),
            html, count=1)

    # ── [전역 2] 구형 앨범 딥링크·라벨 재작성 ─────────────────────────
    html = html.replace('shop.html?cat=album', '/kpop')
    html = html.replace('/shop?cat=album', '/kpop')
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
        html = html.replace('class="top active-page" href="/shop"',
                            'class="top" href="/shop"', 1)
        html = html.replace('class="top" href="/kpop"',
                            'class="top active-page" href="/kpop"', 1)
        html = html.replace(_KPOP_MARK, '', 1)           # 모드 마커 제거

    elif is_adet:
        # 앨범 상세: 활성 메뉴를 KPOP(음반)으로 이관 (크럼은 전역 2에서 처리됨)
        html = html.replace('class="top active-page" href="/shop"',
                            'class="top" href="/shop"', 1)
        html = html.replace('class="top" href="/kpop"',
                            'class="top active-page" href="/kpop"', 1)

    return html

# 모바일 mpCatBar 폴백 배열에도 KPOP(음반) 선행 삽입 (nav.main 없는 페이지 대비)
MOBNAV_SNIPPET = MOBNAV_SNIPPET.replace(
    "{label:'SHOP',href:'/shop',red:false},",
    "{label:'KPOP(음반)',href:'/kpop',red:false},{label:'SHOP',href:'/shop',red:false},", 1)

# ═══════ OG(오픈그래프) 메타 전역 주입 — 카톡/문자/슬랙 링크 썸네일 ═══════
#  · 정적/편집본 HTML 전체에 og:image 등 부재 시 <title> 직후 자동 삽입
#  · 페이지별 <title>/<meta description>을 og:title/og:description으로 승계
#  · 이미지 파일: static/og-image.png (1200×630) → /og-image.png 로 서빙
import html as _pyhtml

OG_SITE = 'MAPDAL SEOUL'
OG_TITLE_DEFAULT = 'MAPDAL SEOUL — Shop Seongsu, from Anywhere'
HOME_TITLE = '맵달SEOUL | K-POP 음반·굿즈·K-FOOD'   # 홈 <title> — 네이버 40자 권장 충족
OG_DESC_DEFAULT = '성수에서 전 세계로 — K-POP 음반 · MD · K-FOOD · 어패럴. K-컬처 플래그십 맵달SEOUL 공식 스토어.'
OG_IMAGE_URL = 'https://mapdal.kr/og-image.png'

# OG 이미지를 코드에 내장(base64) → 앱이 직접 서빙.
#  static 파일 업로드 누락/재배포 소실과 무관하게 항상 200 보장.
_OG_PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAABLAAAAJ2CAMAAAB4notuAAAAwFBMVEX39vL29PD08+/18e3x7Ojr5eH/2dbj3Nj50s7S0c7/sAD3wr35rSPbvbm0tLHyopzupAbzmZPjkTign529jSOUlJGQkI2HhoSHhn/ufG+2fwSMf2F/f3l8e3Z2dXDuaWLsXFTsUkrqRz/pQDfoNy6HXwZubmpoZ2RgYFxaWldYV1ZwTwhUU1FfShpKSkdQQiNDQkFBQD85OTfoMyo6MyI0LiEuKBokJCQhICAiHRIbGxsXFhUUFBQXFAwTEQ0NDQ088i+WAAA/BUlEQVR42u2di3LauhZAIUCAMgNhmDzoJEACDSSlnARKoUB7/v+vrt/ItmzLRnbCuWvN3Hta6oe0bS2kLdmU/gUAOBNKhAAAEBYAAMICAIQFAICwAAAQFgAgLAAAhAUAgLAAAGEBACAsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAwGcU1u9vV1+uvn43/vT9i82V+fEf6+Nfxp++fvnyw/jP1Ze/xv//tT7+7d/x6xfz//9++fJb1xmF4119+ZVTkL59+Wqe5qv1H5EfX6++fP0ZWZBvX7545Ra3sGtz9e1PoGJH3I/NI/nrdWUH2RdgX4S/mX//YlyDb8KhhTPKIywWVQiwQlF14VXsWF/rjHa9xPgZhTPusB/u9onH9bZ2jvfrpKsuXhohUMGYKZ9GqHko2N4FczcRPxavR373/vkK6/eVHZ+vf32hMi6oxU9LWF89YdkfX/327ZhOWCpnLFBYX82CBD63+JFNWFa00grrl72bL8D+CMcI60vQuMcdxaIKAVYoqiaOFQsIy+B7UFh2yFWF5W3tHO/q1ylXXbg0QqAkMVM8jVBzSbC/+jcRP0ZYSdfv6uff39+vjCbyXbjxv3+5+vHn11czbOY1++0Iy4jmd/Pjr74d0wlL5YzFCeu7LQexuX/58u33b6OUf2IK8t3ziygs47O/P8x7/nuo1+bb69/g4Yyi2MYQAuyPsCcs8TDWH/5YZ4yIsLC1EGCFouqLsaNCUVjGGX/bn/uF9fXff76oC8vd2jzen59XivWIuOpCGIRABWOmfhqh5sJV8l8w4ap7lyP6HkFYQov79/dvf6i8FvLL/DIy/2IJy/3Y61ZZO6YTlsoZCxPWP/a3Z/gr+N+vV7+yCMv+KK2wvn75af9dCLA/wtHCMg/1PSrCvqJ6AVYoqia8igWF9e/PkLCujI3Nm01NWMet7eP9Uv6+lF51IQy+OzEYM9XTCDUXrpL/gnmbCJcDYSXG9euf8DX763wHmCE07gnz3rKEdfXlH2tk798xrbCSz1iUsH4H27p3vzp1LEZYv63B5bd/fQH2RzidsL7+CZ1UDHBhwjpWTNLD+hYU1j9fjP7LN1VheVt7UfhxwlU/hkEIlCxmiqcRah4lrOMmCEtdWD/MofO3H3/8I+nfjnzMeFrfZP9YwvojDD6EHdMJS+WMRQnrqyR1I9zNJwwJpckl9+NvgcOZe/6wRgvi6M4XYQeJsP6GhoTCjsetxQArFFUPXsUkOaxvf4PCMsd5P5WF5W0tNvfMV/14aYRAhWOmfBqh5sHxt3vBjpv4h4TH64GwZJG1MrRXP3yh+uUX1j/GZ6awfnt3+G9xx2hhfQvlCtTOmE1YX458V7p1Tf7x8p9f3K5AZmFFJFcThWUe5Y95DF+AfRGOEJZcNscdj1v/8je+pKJqifCxYhJhXf0TEpZZVImwZHeRsLVnkm8qO0Zc9eOlEQL1SyospdMINY8K9nETWdIdYUXx55/vX6+++FPggR6WEbnfIWEdd4wW1pU0hZp8xqKEdfXVmh3XKayr73//TTkk/Hv1xckE+gMsRjhiSChf1nDcMbKHlVRULRE+Viw0JPxj5739wvp79V0mLNldJGwd3fWR7Rhx1Y9hSN/DkpxGrHnEsgZhE+FyMCRUk9bV8cKHM0o/jav83cphOV9NV56arB295vRHqYelcEbheHkOCb/8NDz8LS6HFVWQUCb7j/3ZP3aF0wnrx/FLNRTgQISlOayYCMfksOKLqoUf0t6C1/J/+OJ3Zc7K/U3Rw3K3jk4uRfSwZFf9lByW5DQ/fH2mr7Ib4IfY8fIuB8JK4Of3f2XpV99MyU8zPyjMElrfDcKO36xPf375oumMvuPlvqwh2C8UZgmjC+KWXNzC+syXRVUUlre06q8YYDFQ6YQl7PhvzCxhbFF1fSW4FZMJ67svfldXzj6qOSx36/SzhJKrrnmWUKx5hLCETYTLgbASJjOM75pff39/My9D5Kooe/Wo2ViM7b7//W385bd/x6t/zE+/ajqj73gFLBwNzu9467CiCyIsbvK2+O7k4n6lFZY7aWQ0vmOAfYFKJSxxx39j1mHFFlULQsUkQ0L77F78ThLW33TrsMJXXW0dlvJpxJpHCEvYRLgcCCtxNsPLA0euO/9p91//WsMMsxN7ZcnG2/HPlbc2XMsZfcdz/hxaOKNLWL9C5T6udI8uiHtbiVt8d5Z7xq10F29G93C/nLG0aSUhwGKgIle6f02IsLCRf9V2UlF1IFZMqK+YVhbid4Kw0q90D111MTtx+kp3X83lwhI3ES5HYKV7Xvf+GeewfpnZ2a8//o15su+nNUixvt1/f726Mtex/BZ3ND91/6jjjL7j5S0sMwMReDbHe5YwuiDenSdsYX/2++rYhtSE9e3YBfIFWAhUOmGJl0bYyPdcXFJRNQbYrlhQWFfWsgYhfqcJK+WzhKGrLoby9GcJfTWXC0vcRLgcCAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgCcv7BWAABnAsICAIQFAICwAABhAQB8emEdAADOBIQFAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAICwCAEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYALmxgv8GCAsQFiAshAUICxAWAMJCWAgLEBYgLIQFCAsQFgDCQlgICxAWICwAhAUICwBhISyEBQgLEBYAwgKEBYCwEBbCAoQFCAvgkwrradjvdlqtRq1arVRKlWq1dnnZaDRarU633x+OHiefpwnbRW00LmvVSrlcqVQv7WIOnxAWwoL/vLDG/U6rWkqi1rCc8LHieoovarXV6T8hrE8krF5JiWnE7m213Z8TStEspeEh7lDXqQ5VKldr9Wa7d/eyTiii+nHLF9Vq3Tho+/p+uthkvC4aI5JQ9HfNwnrqNsppyl5pdPqjseRAndKJNBJKOhl2GkrH6Y4SjhR7mIpsj1bsGcfKYRj9vwlL0Tg9+d77mo4GddikusNLbY3CEr7w29NNDsetta+ny9SXRWdEChXWsFXOEqWy0dua6BZWK7akj92q+qEuu2OEdU7Casr3fle83rfxhZiluxGr+zyEZTab9myfx3FL9d58n+qy6IxIgcJ6bGWPUUW7sLqxYk1bvM4jwvoEwlIdesg7HwPFva+1jEs9FjkJy1TLYJ/LcUv16/cUl0VnRAoT1qRbPiFADe1Dwr5esVY6Y4R1NsKandI/ixpRZkvYlEp3+QnL6Ey+5nNcY+C2yCmFFR+RooT11DopOi3twopqzJNORrFW+wjro4WlmISK6COp7h2fY1mnbvl5CsvQ6y6f46orS2tEChLWU+202HS1CyuiQ/TYOMGqY4T1scK6UO13yHZe6kkKT1N3zne5CqvUXOYkrFL5eqdyVbRGpBhhPV3qHsCdKqyavKD9yikHvRwhrI8U1k65pcmSWM+lU3SXOWFTKs3zFVap9p6TsOQyzDcihQhr0jg1MCPdwpJPEvZPPGp1hLA+UFgb5Qv1ekq7qscWop76rrnNWVgSY+kSVqn6mnxZtEakEGGdPH4LD+BOPWQnD1/JjYWwihKW8qCudHNKu6rpKYNKj02TWOqbvIRVupjpuypKfdgChDU8/StCuwP7ufhKaiyEVZSwFqe0CPV2dRFXhucMbX6bt7BKzV1ewiqVpwlXRW9EChDW6QNCyQDuVGENw+UclXVcv8sxwvooYc1PaREpUsPb09euJg5QNYvlJjdhlS4WWlbzKkakAGFp6Lh0tAsr/AzguKbn+rUQ1kcJK8WK6vkpqeG1hpUVItf5C+timZuwwgPOPCNSgLAuTw9JX7ewKmktkYIuwvogYaUYfNyekhpeak3YxKVs9ImlmZ+wSs39obCI5C+soYaIDHULq5FLAstx0BPC+hhh3atfpPYpqxtjxkCDLHdMeZO/sPx9Sr3CKg0OhUUkf2F1NATkSfdRQ+O2yaW+y9dBWB8jrJsU3yq77MPJuGVC7Ux3zKwAYbVzFFZtU1hEchfWpHJ6PKraNRhaOd/Vef1GCOtDhJVmheLihH2jW9O+mumG6RUgLF+NNQsr5vFK3RHJXVg6RoQN7cIKJsXGVZ2Xr4WwPr2wgg/YpnlAN/oNfm86Ekw5iaWXo7AulkVFJHdh6ei6dLQLa5RbBsviEWF9hLDSjD7aWRfJG9zryKIpzTvqFEttl5+wpCtxc4lI7sLSMfnW1S6sSZqZvBMNi7CKElaaXlLgLXEzLa0zY8Im+qXNWsXymqOwaruCIpK7sHSMtYa6hXWZw7jV1xomCOuTCyuQxErVgiNXCe2y3u29IoR1naOwoobJ2iOSt7CetI+wdAirlcNMZmSODGEVJaxUT9neZ3dd7/RngwLUixBWM09htQuKSN7C0tF3qUx0CyuQFJtUdV++FsL6AGFVM7ewbVlD4zwcbjPfMMsChCW8GVq/sC42xUQkb2HpyGY3tK/uCkwSjrRfPnFMiLAKEtY+3SUSk1ivJS3Came+YZ6LENY8R2FFJJ20RyRvYSVNElYbLfunU5/G48lkNRk/PY2GfeunSytRA7jThTVMOZNZNgo5ehpP1Ae7I4RVuLA26W6C92xLTkvRc+7bi8x3ZC9L63xzRL3dLOcPveT+5UO64zpZqO3m7eWuXclUA/0RyVtYSWKJ+4Gsp5EhrkZV+gM3Wltq0hxh5yl1dq6LsAoXVsqn1h6yprAi8yvz7F+htROE5ZrlRt0BaY7rfBvcXmSogf6I5C2s1gnCcs3wlLOwJgkJjG6G6YQWwipcWG+Z08QpOwLV0x8NUkzZpBNL0sLZ5gnCSl75sS4kInkLq3G6sDL03NK11IQUVmOSQVhVhFW4sOaZv8JT7lnea1hWEWCgQVhJXcz6ScJKykfNCokIwkqcGOhmWrDxiLCKFtYs81d42rks+Rv8Nqe8AbKtQVhJfrjYnySshPjeFBKRvIV1eQbCSsizjTIJq4+wihbWc+av8HZm1Ym8JMx6xZ6lttcgrKR6bE4S1ia1YHKICMJK7AWOMwmri7CKFtZD1nmoXdo3irxnaErLhH7cWwHCWp4krEMl7VREDhHJW1i1MxBWwnzwJJOwWgiraGGlHdjVMy/HXqRPYdWTMmX3BQjr7TRhxT9JUN4VEZGPFlar0+0PR0+TDxTWOL6ElWwPHTUQVtHCSr0a0u1w3KXd8SX9iKmdNBfZLkBYizyFFR4o5xGRjxaWN6vWsNT1OCleWKNchFVFWEULK/UvDD9nXY49TZ+THiR1OKq7/IU1z1VY8yIi8tE5rFADbnS6w0mhwurnIqyjVxBWQcJK7R0niZX+pZiD9L5cJra1xbkLa1BERD56WUOUtZKkpbGldvMR1iPC+uzCqmdMYYVeV5rcnGvJPY7bcxfWbRER+eiV7tH9wVassz6/sIYIq2BhpV+laC/OTv9SzJvUDwa1lXI6553Dui4iIh/9LGGsg7tPeRy3mu5QWYXVR1gFC6ue+laYZuuZSR/MjV8F9pCsVNnvs5+VsHpFROSj39aQIIvOU/7CaiGs/4awatnEs0+/Xzt1CutdYZt57sJ6z1VY7SIi8tnfh1XpTs5UWF2EVayw9umfA7GeBn4vaRFWbGt2Xr41TTvQ1Cysda7CahYRkbyFNSqdyuUwZ2E18hFWB2EVK6xthnvBfFYlw08TNzOlsJI2auYurF2RwsonInkLa1w+2Vjlbr7CqiGs/4Sw1hnuhVmmFJasIcVr716lyUt+n12vsKqHXIVVLyIiuf9qjo4f0OpM8hRWFWH9J4SVYWhnTWylT2HJHptrK2W747d6OVVYyl2gXIRVKyIiZ/FDqqXW5PyE1UJYxQoryw+0NFO/pzTQVfESaLHa89ZsD1KtC9D9Pqx2kcLKKSK5C2ukQ1jh337WKKwKwvpPCOslS7phkyWFJSSD1Lp3bbXNmicKK+nZpOt8hVUtIiK5Cyv1wzkJiwT0C6uMsP4TwppmuRle0j+B6CbrfcS/2uZWrdsRPqzed7o/5CusiyIikr+wtIwJS5XHvIQ1KSGs/4SwMnWVbjIsNy1JXkyglrBJ2m6WSVj73WY5H/SSc3GLfIVVLiIi+QtrrOdXSlsIC2HFEv+SmJq8PdcjOlj1cqomHf/89MVxBBn/GFAvXetMyzZfYVWKiEj+wtLUxQr8liDCQlgB4kdEN6leoFxLaJzzVAn/puqG9VyFVT/kK6xqEREpQFiThpZwt/LKYSGs/4aw4pNRt6lmA9sJT1LPUvXuhAXbu2qqoaZWYfVyFlatiIgUIKzVqKIj3OUnZgkRVgzxyZD7VMmq5wRhPac691x5y2mewprlLKx6EREpQlgnP1Bo00VYCCuzsAappgOXCcJ68J86/lcsfC8duE2VstEpLHHZeO7Cyi0ihQhL0+pRFo4irBiS+kQplj3Uk/QXeLXcXDVhk7RpPUdhtQ85C6tZRESKEdaqX9bwDTHOR1g8S/j/IKxpmmcNe0nCCqzAvlXfOP53F4IpG53CmhYprNwiUpCwVkMNixuG5/m2hpZmYU0QlpT4L56XNG8kfU4SVi+NK9MwyE1Y1W3ewmoXEZGihLV6ap1c9O55vg8LYRUjrIukNK96EmuZtLH/hVgJfYSs4za9wro+5C2sXhERKUxYRifr1OUNnfN846huYa0QloyE12Etkn7xIJA3SSOsV31aCfw+uz5hVTe5C+u6iIgUKKzVqn9aL6t1nu901yysMsKSEv97BmYj3KRIYSUsQ23mlml6z+nI14fchXVXREQKFdZq9dg9oZt1eVbCGuYkrArCkrJMHOUpJ1aeE7PG9ZxSWMEFE9oafn2bv7Cei4hIwcIyndVvZUzAV87qZ75GOQmrirCkLJKFpdr8l4mPuPkWdW/KGptnOx9hzQ75C2tRRESKF5YtrW7rMn2lJrm01Lx/+bmjV1iXCEtK/HIe6z0lim/MsrpP8e9+8L1JZaaxdbo/zqBZWOkWpGYU1rqIiHyMsOxnDB+H/W6n1WrUVNU1zqWljnIRVmWVk7AaCEtKQiPZqn/xW637Oflwag8xpmWRg7CauwKEdbEvIiIfKCzxFTSPI1NelQ8R1lMuwmqoDTnTC6uFsKQkvA5rr55asVZYTpM7bHkkbHypa13Cqq8PWoRVUZ2IyDEin0NYamvOcxLWpJJJWI+qU5oIqxBhxSed7Hc13Sg1kLVCh01Yf73W2jr9KRstwqqH3niQTVgb1YLnGZGzElY+OayEpe6VbCPJjlqOrJK6QB2EJeVGIUuutDyorpISE4YpU73Ns7LTLKzm+qBHWAkOvy0kImclrJxaaiuTsIbxZe2rbVhJ/Rr8LsKS0lOwkNIC7J7KpOM8rxSW/9UrGoTV3h40CSvhVw9fC4nIOQkrp2UNCesaooTVUVzVED92lB19rGhChKUurKZ6csV+SPhNeZlAXXPzvNUprOpAGqwswkqa+tsUEpH8hTUZTTQJq5ZTSx1mEdY4vrDliZp/KqnXWSCsLN//TZVxozg7n7AO9Vl1wWqGIZw+YZV7m4MmYW3vL1RG0rlHJH9hjUqVRqf/pEFYjZxa6lMWYXXUy1pJd/SEV0qPEJaUpso9P1duHQmt7t4777Pu5im+3e4kYVV7kc03hbB22837y307cbF3r5iI5C8sp79Qa3X6o/FJwmrl1VIvUwvrMempyI5iDr2S+rUWY4SVQVjOTNMuOYnVU5oSu8ktYSMmg04QVrU92EYH61p7oYVflc81IqcUvaokLF+bqrVanW5/+Pj4NDbHTJPJePz0ZC7C6nc7jWoKCXRODkJL8WB+pUzGT6N+N/kh7qFiUr/iO/bjsN9KWIxWzeu3OM5dWHWlqfG2Ygor6eUPx2eJa9qb580JrbNcrdWb7ZvnxT42WPqFVd0VE5H8hdXSVuxuXsIaao+w7+2oXZ1HbiEsOVUlYd0mxsXJ+uwVB0DaEza+lE22BZ7J6BdW+1BMRPIXlj7fDvMS1risO8It/b/BIelmIiwh1aImmIVy26iotc+B/uYp/FrE+QhrdigmIrkLa6yv2OO8hKWxFyjpDCatiU9HH2FlWYbtDuF2SXmHntrApn1QHmOe0vjPRlj1/aGYiOQurJG2QjdWuQmrrzm+lXGa3+VJxSPCkrJUzIK0VZtGXakjtq/l0Dyvz09Yx+f9co5I7sLS54JOfsKaVPXGt5VXGi+oGYTl8hZfW2/t4Z1aCitp1rGudtpTUzbnIizhBcw5RyR3YXW0FXqYn7D05sUDRdV69BbCkpOwwspbN7VQbRlNpXv/Po/meXyz1LkI61b1IfSTI5K7sLT1LqqTHIX1VNEZ3Ua6FztkT2EhLNUHR7wnVPZVxbFHvLDK+/wSNsJPCJ6JsMQXMOcckdyFpW2w1VnlKCyNHcFwByvxpw8zJ8cQlury6mfFBjVTbXhblRx+Paq0U8XM/5kIS8iJ5x2RvIX1pC0oo1yFNdaYxWrll8jrrBCWnAfVb+jYIYswf54grKXKALMXVdqEN0bVz0tY4gur8o5I3sLStiazscpVWBrzTOXwEzHjSi7WRlhHblX7AG+Kyd0EYb2rnHWQcV2+937AsxBWc6t+HU6OSN7C0uaBfs7CmmhLtnXzC0NrhbCyNcH5QSmJJUyfJzwRt1C3moye2gj2HIRV9b3QNO+I5C0sXbmhxiRnYa2eNA0KW7K36WgacY4QVsb7faHWpGbKB7Se9U14IWA1+oG+gdrI6QyEVRWfTM4/InkLS1e6ebjKW1iaEk3Vp/x6mq0Vwooi4Zv9TSnbJT4CcqOQFJur53YCvMfvWTsbYVUXaVaXnB6RvIWlKXnTWuUvLC1SqUS80kXHiLM2RlhZhbVUahnNlMmYBKndRBc3YXWFW95PL6ya31f5RyRnYWlagRTutuQhLA3Gqgwjp0tPHhSWhyuEFUlTWVgxTwkKKaykBZB3Cid9yS7YwXkIq71OdxlOj0jOwtI0SThcFSKsk40V7SsjFKd2NrsrhHXIOsm0UWoZL+rrJMy+wrasftKUs5rtcxBW5SF43PwjkrOw9MyO9VcFCWvVP8kql7Gv+DzRWN0VwoohoQO7VUnviimspJWoZhL4JX6T+gmPEtX2n15Y5V749w7zj0jOwtKyVqC7KkxYq9HlCUdMeAP0ScbqrhBWDPuEr3ZxdmqpksJKWnrdTm47vbgCJ/3i2NsnF1a5vUx/XA0RyVlYGiYJq/1VgcJajTtlneX0Z/Qyh6M2XCGsOBJeh1VRGj7epHg4sZ2csBmcknS7/9TCqt8us1RKQ0TyFdbk9EnC1tOqUGEp/MCE9AunM1b4iYdJN5sNI/tuCCup1+QIX2nN1muKEUozUZIJv+ByrZKy+ZTCal4vsn1t6IhIvsI6eZKwMcxtQWorZuyWVlmVzqPqT4pl6MC1Rnmsy/1vCes9TfIkIj1V9v3MTPJ7aBL6YNX434FI2nv3GYVVa1/PNtnrpCEi+QprPOy2sr+AsNrJpaUmC8swbSfFKoTL7nilTqpDGy5sDVV/kuj/WVjKr7mK6Y75N3pLVGBPbVorY29k8UmEdVGp1uvNdvv6frrYnPa4gY6I5P8jFKsnw1qN1GPDy85wkk9LVRGWMXobdpRU2+ik/vU/49BVVVv1412IsAAykNxIn4Z9w1tKDbXW6g6fVp+Ax34nzrTVluIvWstmI+MPbYxPLo0oTAqvMsIChCUOEs1fTe12Oq1Wo9G4rFWrlVK5UqlUa5eNhvMjq5PVZ2LyaJa3ZZa1Ujb6PEZRnYI+aTq0d+yycPCPigLCAoQFZwPCAoQFCAthAcIChAWAsBAWwgKEBQgLYQHCAoQFgLAQFsIChAUICwBhAcICQFgIC2EBwgKEBYCwAGEBICyEhbAAYQHCAkBYgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAQFiEAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgAgLAAAhAUAgLAAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgAgLAAAhAUAgLAAAGEBACAsAACEBQAICwAAYQEAICwAQFhBtrPrZr12Ua23r2db4fPXksWFuO3C/qy0DRxi2mvWKhe1Znuw9v+LcxCPcq15vTihNEFuU5ajLh6zKa3MYXHbrletErzuUpTw1j5aL3zWZsIVkNetFhXA3uteUqrEqrcDezyXwgUOsPRO+54unO6/N30lfXA+fXBjJRSp5/zb9fGjtnDq5Jsx9gZJjKLS7QWfQFjr66pwgaq9ZXphrXvCIcrtRWJbbL9nLk3cHaVUjtsEYe2fm2IJbjbKJSxGWPYB58FSKVS9vPTtsq8nC+suquUmhtORTWkq/PvOOWN9d5g7fwrtLMbK3ry6z0FYoSgirDMR1nM1cIkqt/uUwhoED9HbJd0I1VnW0sTcUWrlqK5jhfXeDGpjplrCAoVVKj/4DqBW9ba0gxUnrGMwmhEljgrnWzncxXo4Smzr/LP3dbBxD3ixC3zUPuQlLDGKCOs8hHUv6/9s0whr35N8dW2SboTyPGNpIu8o5XL04oQ1q8bftHElLFJYvq6LatV9XSyvgxUjrKVwwGVEiaPC6ZbpOdTBagrberfBi3dAr3c4lxhHr7CEKCKssxDWVHqV2mmE1ZN2trcJN0KpvslWmsg7Srkc5bdoYc0uZEe5UythscKqLtNfgrY02D2lr7P7iBJHhXN54Y3/3G5gSZBUL3DUm/CJ7kWn5SQsL4oI6xyEta1JL+IihbDkTfjYCKLuFMmtoFSaqMOkKEc7Uljv1fjuYHwJixXW8TzqVRe6WPumgrDE4XEzqsRR4bx2/n0Q6GC1xQFpL5BgF49nf1Te5iksrwAI6xyE5X6DNqfr3XY5a1+I7U9JWBu3Cdfu37e75TFh/So/yH55W3F22J9emiOq5bCYRQjLa8TV68Vmv327d0dNbi8hvoQnC+tC4d/2ywfHqpVt+qq3ZX3FSGGtndrb/1lHKTYinJuqP3hOB6v8Lo423cjsqwFPezn35iGdsC4OmaIYf3vB5xBWMzB6MDPOXvtTEpb7Ndp2Rnj7m8AXcvggD8FcRfbSHFEth3+UEhDWc+Agh63zvV9bqJSwEGEJrnlJX3WviyV0sKKF9eDMOvh7SsrhvPcWMYg5M/dsNbH/dHgLZ8s2vu31CiscRYR1BsLal33XzGqhx/anIqyt8z3V3IUSKvOog+zKsqRIptIct1Qth//U/hbmNinhIFtzi8r1RqmERQnL7Y3cZKh6WzKMjBSWreve5iI8x6gQTm8MWNsK3wberGLb97Vld796Ypp+7kuLaxeWP4oI6xyEtQ5PAG3fDmmE9RzuLbl5nuhvxrq8nWQojYdyOZxWs5G1sLlkjeTyonxcaZVQwqKE5Ra7l6HqThdL7GBFCmtTdoxhb3yxiRKWPJzHot0L3wY3gWgNBMfWX8Xy3PuCrV1Y/igirHMQ1lK+ijmFsNqSBumMSOz1fjF3Svv00gT6AgrlKPvuUn8L68nKNXhTLmHRwmqnq7q406ykIKyBu1TqNrhAQSmcRy2aXaxnsbclfD30hC+x9kasSs+3tDQ3YbUR1tkIa1OSD86U7xG3W30r22SRUlgZShPs3ieXw1kQ7mR+/S2sHnv+5BJ+iLDUq94uH7tYTTGb3osdETa9w7XDpYoLp6DKW89dx5WaztLRphDZB+cSbISjtREWwvJwhg7VeUZhLUvBpM7xTnQ6++pDwgylCXZ9kstxfS3epr4Wti4F1jKmjVfROazrlFVvezW3O1jl51hhOakrYwy3r/km1NTCKXb/qptpcFGWu7W9sH3m+LV3rItTh4e8hOWLIsI6D2F5i1+a9/N1BmHNpGMkXwYlOul+d3ppXNTLcb2+EOb8m7J1PO9Z41WUsGbiKu0UVV94WSy73r1FrLCej/5uhxbXJ4fTwp38u2kGVkAcR+DWkNvyXmXnjEJvhDttkZewZqXEpD58NmHNfanT5vV0c4hKrAaw75EH6WMb9fhnwCKXNWQozfyQshzXbjuxnnHztbCB9CApSqh34WjUWGjtPjdY3qStutvFenHGcfHCsreumj2gaWhMmBzOgOLDcZgKmbGm869vx/M8HE+vdjPG3SBJUUzYGz6JsAL3k/mc/0saYd1IW0tTvDtDK/bu3YWjOw2lmR9SluP6sBSGS74WditdBlTyrZqMLWGuworaIE3VX50ulpsdihXWtnI8ixOL6i4srMhwOoP1i0gBLI9n31XcwWfV6Wq5HbD2QauwUoYZYX02Ya3Dz5o0l+r3yLVya1F66CF9aZw76jpFq3WlY85XNWMqIxVWbAmLFdbFe/qq+3Ltb/HCmopZ8mYwUZYczoP/0kimWWpeGRfe0dte57vpXwWRk7Au3hHWOQnrsGyGH2Fb6BFWPfYgtY2O0qgIqx5oYYvjAuzUwootYbHCeshQ9Zn/+LHCaovHvQlumRxON3VfLUlfFnEcc+7d4d/GO5FxrN2FX5E5CevhgLDOSliH3X0t6kUKefaw5K+XSV2aLD0stynW9xmEFVfCIoV1cX/IUvWmzx5xwnIW0Nd9ybvj85/J4XS5i1zvdeeVvO0V9tWt+1tJWOGQl7C8KCKssxGW0QRfe80L2RPsOnNYASozPaXJkMM65lWm/hZ2oySsmBIWKKy2t5w1XdX9r7GKE9bM92+7aqAFJ4fTM5/7BHl1LZ/CmDlzBL1j1OvuHGXzkKew2m8HhHV+wrJz4S8PPe/rt6L6Rg/1KarAnfKupzTBiUeVqbJjr6Tpb2EPisKKLGGuyxqiZttSVt0t88UyQVhtpVfWxITTw31A5ybUiSs7n6+FhfRNp2N1LR+DnrisIXrOkmUNZyUsm7em78UkOtdhHYciST9CkaY0ge6AymIkq6U4//LSTK5MUyosaQnv81+HdR1aEpKy6u7CjOtDvLB28jeD1fcSYcnDecxiRS1j8dayzwTluktHm4GngXSuw7qWlAhhnaGwvHV+94r3SOwy64dTb4Tk0njJeuVy2OuanSVXTV8Le5eOA+KEFSjhINz+Z/KnkDILy12n2c5c9bbwuHKMsF4iRl8LibDk4VQQlvu04LWQH3t2el3VQL9Rp7DCUURY5ykst3neKN4j2Z4l1Fcab4SmXA67hblPtpXFyridinvZ4GqtUMLw8kq3KWsTlvfmmPesVV8IBY4RVi9CWNcyYUnDqSAsJ2CbphAjW8Dtpf9dfppXuvfC3VKEdSbC2soyF9eK90i2tzXoK01wy+RyXMtSGv7K+OyyvQgIK6aEi5Lv/QLC4ONGm7DeQkua0la9fXxnQrSwdrUIYdVlwpKHM1lYTu/wtSIa1zpzdRaxtF6PsMJRRFhnIqz3+qukx6A6JPQSqsJ0y7ae/Sn4tKUJJXYTy3Etyylvfd0hXzpoFki6x5VwE85/130vZtn06he13vIUYbkFP65pSlv197LXiYwW1jxyRu5dOZzJwnJSbW3fSLwtfHafk7DCUURY5yGsZc23wsB9zmKmKizJ6y6v/amgNDdC6tIcm6hqOdwW5ntkxH0JeSV0kG3TL6zYEoZfQ/Hia+XOwSS/F5RCWC/B+brUVW9770yIFlYv/C9139BTIZwKwhI8dxF8sXJgF73CCkURYZ2FsJbWV1zPbUG7tu/mSfVOd/c1w7eBOeMUN0L60oTGXonluJYlabaBz7zf9Nu0/csa4kvo7l9232qwrPvGUYPIh5LSvK3BUejFOmvVl+XBIUFYzvtkSq9hhzXVw5ksrHvJGoOFILFdXsIKRxFhnYGwlu77nXov691uPW2WEkdzp/5qzsmliXgkUbUcXgvb1MItbO3O5Vu/mrNZ3NT867ASSugFp9ybmz+6c1v1r0DqlaJS8GmE9VyKqolq1b2fqo4U1jz0sPOxU7JUDmeysObhbL77TE4wL6fnZ76io8jPfH1+Ye3qEY/NvKsLS/VH8S50lSbqjlIsh3eDeisuxco8SFYXHoWVVEL/7/gJy/qX/gHQacJy8+HVzSFr1Q9JwrqWTT9UJRnF+HAmCmt7lNP0EA7idX7CCkURYZ1BD2tRU5m61vLLzxe6ShN5R6mV41omyK0sqeJwJ/SwEkp4OLxLfzj6/qCzh+W9BufmkLXqScLa++cK/LFppghnkrAEOS1Do3v/6/40CysURYR1Djmsd1mfobdPJay9pLk0N1mSmUqlibyj1MohtNqZpIXt2qGXSog5rPgSyrtopbb77wM9wnLff3D8PYfUVU8Q1kL6PNJA/FQtnInC6gWXS/gOtMlRWMEoIqxzENZhe30ReoB9f0glLONODj7G0dsesghLqTQxd5RKOa5lc1TCZvubsniE6mLra7uxJbSGZxfRPtvWdSTdj638PnvV44V1LX2gaC2+kUUxnEnCmkpelbWWSEy/sIJRRFhnISyj09AWm1hFXCWkKqzDslcRexQL1baYpTRxd5RCOcQW9l6WVWYhdLKa74dtoLMRU0KnDD5lNcWJtrX9IzOvJwrLXUwhvrM1ZdXjhRUhVvFnZpTDGS+sZei9VMfT+8ulXViBKCKsMxGWcUNNe8169aJWD76jPE1PzThG7eKi2mw/LD+yNFrKsbxt1ysXtWZvnqmE64Hx75WycYD23Vvg39rBuTeNnWVtlwDgMwsLCsP65awecQCEBWfAtfsmKgCEBZ+ctzIdLEBYcCYDQjObXN8SCEBY8Omx1nhV3wkEICz49Ozb4k+CASAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQlY3p3d7f7/7qUS6PKb4HPnu/ung+zu7v7qB0WJ5xMfd/7u7uX2GK8ZTxV5IGNz6aH14gzAsIqkv3y5fnh/u7+4fl1uUdYysIy/uKyySKshbH9Msoi6zvTHQgLzk5Yg+MN+GL88X4T+lebh9n6eFPOBvf3g9kyfitn24djw7t7cHZ5dQS1M/77qiIs8wwPru52xo1vtGzhxA/Pi80p9RwIZbzbyisZV3GF8xvFXjwbO79sfA1fDE6Bwlo6gRdLZ1RtupZ6JXgoQVgbMXLmRuFi7o1P5iFhbb2dZkFh3d/5eaDhIyxJQ34zb473wyGyJTv34cb78HkTvZVwfwdu6azC8trJ4i4orDuvTWSrZ1hY4UomVTzh/If1vW/D3IU1jxPWIvjB1qnKG8KC8xDWWtbqBmHbrIX7yemnSLZyb1azfzVdbveH/XY59W6+bMIa7I8drLCwzBs/az1DwpJUMrHi8eff3PuVnigsh/eswpq6O0r23Q8C18na2mKNsOAchLWz1LKPauZb844emNuZHw2Wu93S+sNOvpV4Pw98R9tkF5bTLVrc+YRlnni7uEtqzrH1HAQSM7JKxlVc4fxmFefbndm9u9+q5LCmQg9rKe29xg9AzWa/F3pq4r4v4gjb1dDLbv1giiOVsDypvkXmsOTCOkZ+FpvDekBYCEvWVq3uzy56ILVxxiaLYwt+cO/N8FYKwnJJISyri7V7CAvLbif3u6z1DApLVsn4iied3wzE1B2pLQoQ1psbo7Cw9lb4F6/3041gMLP0C6uDGfLK3O55RQrr1RHam7SYW+8TubBsEBbCSiOseTjh7m+RO6fL/3BMAL253fXwVgpDwqCw3BYdIyzrxIs7mbCsdvmWtZ5BYckqGV/xpPO/uXu75nIb/vJ5YKjiYfD8plVYW7ODtXgxlBQS1sb+xArp684LwLMzXF6GvRIcXb5JOnPbSGGtvcBJhPUgEdYDQ0KEldCQl5KEu1RFG2Eebet2p6KFFZN0Ty2sB6uLZSrwXiKseUIWKbaeA0liJlDJhIonnd/thNgRujtmuN+szqU9agoKSxxcp8th7a2ImqFaBvbdvNiJo/XCShXdv1kjQyf4G2u7kFcenNHlTCh3IL3/vNtECevdrrr9TwFh7e8QFsJKLyxrkDOPS1W7gz3z7vPuN7f1Rw8JUyxrSBTWwjqb2SjmEmG9JdzZsfUMCEtWyYSKJ51/5o6q9gFhmcfd7u1W7naGPGHtd7vN8n0+fdikFJallpeHO2H8ae27nh2VsrX/PFja12EqptB9Xlm74ZYLy/oSmN7fTbdyr77YppcKa20fbYawEFYaYc1CnolKui/EG+jeuTmjk+4HceHoi7dwNFPS3VxVMDCVM1hLhGU2/Lus9RRm+wYHeSXjK554/mmUsMwGvtnZAgwKK2H6Lbp/NbNn3zZW05/tjvvaB3VzV9b0gdkv3MYK68WaL9hGCOs4eWr15kLF3N/bgZMKa25fj5CwcBTCimnIz2L/Zn4XSBL5ms082G7nh7hlDTEDpFhhvd7fv4aEZTrB3PI9Uli7+CNE1jMgLFklIyuudn5fD2sg5ILMumy2dtiihbVII6ydtevzzlldNRCEtTVk8Hy8QPuFPQMR28OyPpy6mb9gDmtpxuLBybKJbhS7q8+z6U6WwzKzX+ZsJcJCWGmEdXcnrBqIFtbioCKs4926u5OzS5wlXAbFZwlr78wVHhSEJT1CZD31Cyt4fl8OS0y6mzrYbOyORjjpfv8weJ4tlpt9SFiSuLpdW3sBhjVA2z7fPWzFfZfP/u+TzXQZyGHNg15xzWlPkQaEZU2B3K+djpZxUumSL+dBoJCwZk4cERbCSies6cCb7Y8QlvPQTeTISPJoTnZhhZY1DbwEmtnuFYaE0iNE1nMQboUnDgmD5/fNEorLGrausLYJDz8HiBGW2Y8abN3e1kYlYS/MEr4HvGJl0q3B40wirJknZmP8KUu1WQvPHuyNgsJaWGtBBklJ98zPfMN/VViD3dJ1R1hYb8EOflzu2Tc00dzDcsZ1B6mwAklveQ8rqp76k+7B8/vWYW2Ehm8Gab22p+GCwtrFtNw4YRn9pm0oLx7b6oV1WBu/V9Z2mmt9J18/tp86E5FmAlPiRnt5xdL+mggIa26HEWEhrJTCMtMTU2k+OqSipNn9bKjlsOzWsJQLK7isQHaEyHrmsKwheH5hpfuz2FMxc1pL04bz500aYamwWUwH93cPg9dl0jfIcaX7s78jZOXtzaX5cyH373soYLqOc+PUXu87tQaFPmHZmbbXA8JCWGmFtXAb6XOisJLWT2oSlqwctiHckUuWhaOR9cx/4ajwLOH95iisN99shV5h7YV+7GAZL6xjit8/crPHa0s3F/WwTfl6mYWzuxX1pSgse7nLzBtYhla6v8tXBgLCsm/AuWRJoERF8U+oxOX073yq2It/URbW0ppqW5/waI60nvk/mnOc/ndGUMvjQlZvMiA2h7VOKaz9NDR3GyMs920Ni0NYWHOv2zmQC0vMkvuF9ea+d8u00VoU1uJOeMMNwkJYqYVlNcNgmwurKPbh59yFdZgfZMJK8fCztJ4FPPzsvg9rvj0IwjIscG/NBL6aU4EhYd2f0MOyMtqLzf6ws5+J2h9ibWC/D2t5OAReqjDwlvMurEx+OmEZXw9O9O6Dz/zMhOMgLISVWlj2t/1LkrD8b1lZH7QIS6mUG19nI7zqYpq9noPo1ZBeJaMqrnh+2dAp8RXJpwjrWXhieRpcHhdrA59XtlPX7ftZ1Fsm4tYhvDpleAvOEu6mxxIdhRXTCQSE5W+r0zvhHo8UlvAeu8HmoCCsgXhj5yes+T57PcMVCFcyquKK5y9cWA/CMouFbLJXTVgq5VZdOKXwimSEhbCUhbUR3pJ3iFOR7E3BHyqsh+f55pR6yiqQ7hXJ803KuKsKa5r1wj4LITuhh4Ww4FMJK2c0Ceu/R+7CsnJYb1YOaxZ6B9inFRYgrI8WljyHFVr5OENYiUPCNA16/xzzhKdeYQ0Ui4mwENZnZxoprAHCkg6jg2/Oyyqsw/4luA5LUVihYiSVO1UxIx89QlgI6+PZPEcJazn4vxZWpuilbNDuSveX0A9C6l0zcGIxAWEBACAsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAgLAAAhAUAgLAAAGEBACAsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgBASmGtAADOBIQFAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAOD/VVjdUqkj/YdayaIau2uXywmAsBAWACAsdWGZPCEsAISFsAAAYSEsOCNal5d94z/jy8uG9ffRpf3BavV4adHoPHp/aVl/Dmy36l62oo4+7DQuG63+2LyRL13s3Z66LePYI/OP/ctLpyyd+MNFYpTm0TnS0F9w34kC/7IamcWz/8m3nVjW/uWRjvm3iV3UbrBOwYMjLIQFeQirZbf0hmOfy8uOKCzjHx6FvwzdO8/bLsYwXfcIo5CwRo3jX7QJa3hpesRXcN+J/P/iucgq0KOw3SnCcg+OsBAW5CKsy9Fq0nKF1bo0/jh2hGXYaTJsmBp5tLYaGX2Qp1Vgu2jDGBrpPE7GQ6t1Px1lZ/boGpetx8lTxxKNLmE9GUWdBAruO5HvXyy9PZl1ujTqZESgNZo8dqy/+MsqlikkrON2zsFHjcvOGQirVWpMOlXjm+SxVXEqMem3LiuVS7fHuWqUSsNJt1GptpxKTvqNarXVPworuEcKYXVKpZpxRS7tecXJatiolFo0R0gWlqmJofH/DWcwNHJa4aPz367ZRi1hWf9s32/CdtGG6TtKG/eDjdv8J9N8k4Z5PE3CGht1GQcL7juR71/cEw2t/tHQrqDht/5JwnIPfgbC6humuBwbWipVrDF7w5ZHqdw9Cqtlf2T5aeL8pesKK7SHsrAmhq+sPrErrK75/wgLFITVb1yOO5d9W1h9o2Xa3vAaoKUdR1irjtNyhe2iDeNrun4JuMfpmqdVFdawYx2u35cLq+MMxnwF953I9y9GkR6dgvWPZx09rk4UlqvpTy+s4dhQVmc4NuTRt/o85a7RF+1WSqWRK6xWx/yk7G5QqvbHRpe05QgrtIeqsEz1uV311cRQ1bCEsEBRWMPupdEeH21hdYyGaLhrEtHD8hwkbBdtGHPMNZYLy5Wd1boVhTW2R3x9J8EeFFbfPYGv4L4TSTtBdk+xJQ7kTuxhNc5iSFgzXFE2JfHo9nr6/gxVw/2D8YlRpbHhpaHb07K7XME9FIVl+cq7MUxhdVqjMW0R1HpY1oSfLayJM8s2kuawhKGcsF20YczM2GWr239yJOBg7uQOLS37qPawHk1j9YMycaTTv3T38xXcdyJ/DuvY2TMc1AgISyhrQFgO3eB2Z5fDMv6/agpr7E+EDy09OUNC+5vCUMqT+fnlKiwocQ81YZm+ah39NKFvBamEZfyvMbaFNbQ6TXb3IThLOPLavX+7GMOYuW6rcU8CjXsieGSknsMyjNWS+coUViO4HsMquP9Esom8rn1ASzNdeyows7Ccgz+dnbBE/4xCwjIfuRmarmnJhTVKJayO4auq0J8yhfVIQwR1YfWN1mcLqysoI7gOS+xhidvFZsnHo36ncRlOZGfqYdlrD/orqbAuu8ZYbLwKFjzYwwouleo6BwwKK9OQ0Dp4V7+vihHWY7dRrVjJpKCwGmYSq+NpyhNWcA8lYdmJe7+wJjREUBfWuPHoCKvh9hKejjkZtzWKOSxxu8RpPXP2TkcOy+ngdCZSYXXMHFdXTCaFT/QYFNGkc9kY+rbrniKsoTTBdibC6ldKLorCCu2hJqyKmcUfIizIKqyVteyxYXdUxIWQYWHZk26+7ZLXIVitPH6WULBAzOGMDfsNmbFG7nquUVBY0llCr3EZKn0MbneSsCatHDJYhQhrXDX+8zhOMSQM76EkrOrI+v8nhAVZhbVyhNV15v1aTp49JCxnHZZvu2jDPLaejrKIWYc1ctZAJfnPfvBGZixbWObiz0mg4NJ1WEJK7Om43cir0gmzhENpju0MhOVlzkeqSffwHlJhDS8rjZEgLHdZwwRhwYnCOo6LguMn/0p333aRhjH80eg/TZ76lwkr3SfiX2KWNbSsIzw2Go9yYTlrQH0Fl6x091pSw5oN8LZrjCZP3caJwlrZ0jxjYfUFYcUuawjvIRVWTZwFdBaOPlWPeXuEBRmF9eRmtEfOqoVQRtn+yL+d98RgSDSPLfcfxrHPEnpzb86kY8ThVk/OOqtRxJDQHNg1gpmqwLOEQ9FCHl4ZWl3/LGE/SVihIfRIPi3w+YeEho+6T/YyUE9YsQtHw3vIhDXxpbjcR3PMlaJ9hAUnCctbo20NoCTCct7W4N8uxjCTfst6W8NkFZbA4/HlCN5bHSbxworGFdajmXwPDvx8b2uIFNZq2Go0OuOThWVL8/yEZfaT7Am8htMlSnw0J7RHrXSkFdfDspNZx2cJLYa0RoD/BIUsaxi2qmXz2ebHVsUTVvDh54rv4efgHlJhSXJYKy+NhbAAEJYWGjgEABAWACAshAUACAsAAGEBAMJCWADwfyYsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgCAw/8AwbqgtC4mB6MAAAAASUVORK5CYII='
)
_OG_PNG_BYTES = base64.b64decode(_OG_PNG_B64)

@admin_router.api_route('/og-image.png', methods=['GET', 'HEAD'])
def og_image(request: Request):
    # 카카오/페이스북 등 일부 스크래퍼는 이미지 GET 전에 HEAD로 존재·크기를 확인한다.
    #  .get()은 HEAD를 자동 포함하지 않아 HEAD가 404 → 썸네일 누락되므로 명시 등록.
    hdr = {'Cache-Control': 'public, max-age=86400',
           'Content-Length': str(len(_OG_PNG_BYTES))}
    if request.method == 'HEAD':
        return Response(b'', media_type='image/png', headers=hdr)  # 헤더만, 바디 없음
    return Response(_OG_PNG_BYTES, media_type='image/png', headers=hdr)

_OG_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_OG_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_OG_HEAD_RE = re.compile(r'<head[^>]*>', re.I)

def _og_esc(s):
    return (_pyhtml.unescape(str(s or '')).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))

def _inject_og(html):
    if '<head' not in html[:4000].lower():
        return html                                  # HTML 문서가 아니면 통과
    parts = []
    if 'og:title' not in html:
        m = _OG_TITLE_RE.search(html)
        t = _og_esc(re.sub(r'\s+', ' ', m.group(1)).strip()) if m else ''
        t = t or OG_TITLE_DEFAULT
        dm = _OG_DESC_RE.search(html)
        dsc = _og_esc(dm.group(1).strip()) if dm and dm.group(1).strip() else OG_DESC_DEFAULT
        parts += ['<meta property="og:type" content="website">',
                  '<meta property="og:site_name" content="%s">' % _og_esc(OG_SITE),
                  '<meta property="og:title" content="%s">' % t,
                  '<meta property="og:description" content="%s">' % dsc]
    if 'og:image' not in html:
        parts += ['<meta property="og:image" content="%s">' % OG_IMAGE_URL,
                  '<meta property="og:image:width" content="1200">',
                  '<meta property="og:image:height" content="630">']
    if 'twitter:card' not in html:
        parts += ['<meta name="twitter:card" content="summary_large_image">']
    if not parts:
        return html
    block = '\n' + '\n'.join(parts)
    i = html.lower().find('</title>')                # 문서 앞부분 삽입 — 크롤러 부분 읽기 대응
    if i >= 0:
        j = i + len('</title>')
        return html[:j] + block + html[j:]
    hm = _OG_HEAD_RE.search(html)
    return (html[:hm.end()] + block + html[hm.end():]) if hm else html

# ═══════════════════ ⑨ SEO — 네이버·구글·다음(카카오) 검색엔진 최적화 ═══════════
# 파이프라인(_inject_auth) 안에서 경로별로 canonical·og:url·description·robots·
# JSON-LD(Organization/Product/Breadcrumb)를 서버 사이드 주입하고, /robots.txt·
# /sitemap.xml(전 상품 + K2G 앨범 4,900여 종)을 동적 생성한다. 소유확인 메타
# (naver/google-site-verification)는 관리자 [SEO] 탭에서 저장 → 전 페이지 반영.
SITE_ORIGIN = 'https://mapdal.kr'
_K2G_IMG_BASE = 'https://www.kpop2gether.com/shopimages/912enter/'

# 검색 노출 제외(결제·계정·검색결과·관리 화면) — 클린 경로 기준
_SEO_NOINDEX = {'/cart', '/checkout', '/order-complete', '/account', '/search',
                '/hero-admin', '/admin-hero'}
# /home 과 동일 문서를 서빙하는 별칭 파일 → canonical 을 /home 으로 통일
_SEO_HOME_ALIAS_FILES = {'mapdal_home_mockup_v1.html', 'index.html'}

# 주요 페이지 meta description (150자 내외 · 미지정 페이지는 사이트 기본 설명)
_SEO_PAGE_DESC = {
    '/home': '성수동 K-컬처 플래그십 맵달SEOUL 공식몰. K-POP 음반·굿즈, 컵떡볶이·김밥 K-FOOD, 국내외 배송(DDP).',
    '/shop': 'MAPDAL SEOUL 공식 SHOP — 굿즈/MD · K-FOOD · 어패럴 · 리빙/홈 전 카테고리. 성수 플래그십에서 전 세계로, 3만원 이상 무료배송.',
    '/kpop': 'K-POP 최신 음반·앨범 온라인 구매 — 팬사인회·영상통화 이벤트 응모와 특전까지. KPOP2GETHER×맵달SEOUL 공식 앨범 스토어, 판매량 차트 집계 반영.',
    '/kfood': '맵달 K-FOOD — 컵떡볶이, 김밥 6종, BOWL 6종. 성수 매장의 맛을 콜드체인 배송으로 집앞까지. MAPDAL SEOUL 공식몰.',
    '/new-drops': '이번 주 신상 드롭 — 새로 나온 K-POP 앨범·굿즈·K-FOOD를 한눈에. MAPDAL SEOUL NEW/DROPS.',
    '/bestsellers': '지금 가장 많이 팔리는 맵달 베스트셀러 — 앨범·굿즈·K-FOOD 인기 상품 모음.',
    '/collections': '맵달 컬렉션 아카이브 — 스타라이트, 네온서울, 래빗클럽 등 시즌 컬렉션 모음.',
    '/collection-starlight': '스타라이트 컬렉션 — 한정반 앨범과 연계 굿즈. MAPDAL SEOUL.',
    '/collection-neon-seoul': '네온서울 컬렉션 — 바이닐 LP와 시티팝 무드 굿즈. MAPDAL SEOUL.',
    '/collection-rabbit-club': '래빗클럽 컬렉션 — 티셔츠·캐릭터 굿즈 라인. MAPDAL SEOUL.',
    '/collection-glow-seoul': '글로우 서울 컬렉션 — MAPDAL SEOUL 시즌 한정 라인.',
    '/collection-han-river': '한강 컬렉션 — 피크닉 매트 등 리빙 굿즈 라인. MAPDAL SEOUL.',
    '/collection-sports-day': '스포츠 데이 컬렉션 — 볼캡·타월 등 응원 굿즈. MAPDAL SEOUL.',
    '/collection-archive': '지난 시즌 컬렉션 아카이브 — MAPDAL SEOUL.',
    '/journal': '맵달 저널 — 성수 플래그십 소식, 드롭 비하인드, K-컬처 스토리.',
    '/mapdal-seoul': '맵달SEOUL 성수 플래그십 — 서울 성동구 성수이로16길 5, 825평 K-컬처 복합공간. 미디어홀·팬덤홀·KPOP2GETHER 앨범 스토어, 매일 11:00–21:00.',
    '/gift-sets': '맵달 기프트 세트 — 선물하기 좋은 굿즈·K-FOOD 패키지 모음. MAPDAL SEOUL.',
    '/seongsu-limited': '성수 한정 — 맵달SEOUL 플래그십에서만 만나는 리미티드 에디션.',
    '/support': 'MAPDAL SEOUL 고객센터 — 주문·배송·교환/반품 안내와 1:1 문의.',
    '/shipping': '배송 안내 — 국내 배송·해외 배송(DDP) 조건, 3만원 이상 무료배송, 콜드체인 K-FOOD 배송 정책.',
    '/returns': '교환/반품 안내 — 신청 방법, 가능 기간, 환불 절차 안내. MAPDAL SEOUL.',
    '/partnership': '파트너십·입점 문의 — K-culture IP 이벤트·커머스 협업 제안. MAPDAL SEOUL.',
    '/ir': 'IR·뉴스룸 — 맵달서울성수 투자 정보와 보도자료.',
    '/album-detail': 'K-POP 앨범 상세 — KPOP2GETHER×맵달SEOUL 공식 앨범 스토어.',
}

def seo_conf():
    """관리자 저장값(site_settings key='seo'). 없으면 빈 값 — 소유확인 메타 미출력."""
    try:
        r = one('SELECT value, updated, by_admin FROM site_settings WHERE key=?', ('seo',))
    except Exception:
        r = None
    d = (jload(r.get('value'), {}) if r else {}) or {}
    return {'naver': str(d.get('naver') or '').strip()[:120],
            'google': str(d.get('google') or '').strip()[:120],
            'desc': str(d.get('desc') or '').strip()[:300],
            'updated': (r or {}).get('updated') or '', 'by_admin': (r or {}).get('by_admin') or ''}

@admin_router.get('/admin/api/seo')
def api_seo_get(request: Request):
    get_actor(request)
    c = seo_conf()
    c['origin'] = SITE_ORIGIN
    return c

@admin_router.post('/admin/api/seo/save')
def api_seo_save(request: Request, body: dict = Body(...)):
    a = get_actor(request); need(a, 2, 'SEO 설정')
    def _code(v):  # 소유확인 코드: 태그 전체를 붙여넣어도 content 값만 추출
        s = str(v or '').strip()
        m = re.search(r'content=["\']([^"\']+)["\']', s)
        if m: s = m.group(1)
        return re.sub(r'[^A-Za-z0-9_-]', '', s)[:120]
    conf = {'naver': _code(body.get('naver')), 'google': _code(body.get('google')),
            'desc': re.sub(r'\s+', ' ', str(body.get('desc') or '')).strip()[:300]}
    _setting_put('seo', conf, a['name'])
    _sitemap_cache['xml'] = None
    audit(a, 'SEO저장', '', 'naver:%s · google:%s' % ('설정' if conf['naver'] else '-', '설정' if conf['google'] else '-'))
    return {'ok': True}

def _jsonld(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return '<script type="application/ld+json">%s</script>' % s

def _seo_avail(soldout):
    return 'https://schema.org/OutOfStock' if soldout else 'https://schema.org/InStock'

def _seo_breadcrumb(section_url, section_name, name, url):
    items = [{'@type': 'ListItem', 'position': 1, 'name': 'MAPDAL SEOUL', 'item': SITE_ORIGIN + '/home'}]
    if section_url:
        items.append({'@type': 'ListItem', 'position': 2, 'name': section_name, 'item': SITE_ORIGIN + section_url})
    items.append({'@type': 'ListItem', 'position': len(items) + 1, 'name': name, 'item': url})
    return {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': items}

def _seo_section_of(cat):
    if cat == 'album': return '/kpop', 'KPOP(음반)'
    if cat == 'kfood': return '/kfood', 'K-FOOD'
    return '/shop', 'SHOP'

def _seo_home_ld():
    org = {'@context': 'https://schema.org', '@type': 'Organization',
           'name': 'MAPDAL SEOUL', 'alternateName': '맵달서울성수',
           'url': SITE_ORIGIN + '/', 'logo': OG_IMAGE_URL,
           'address': {'@type': 'PostalAddress', 'streetAddress': '성수이로16길 5',
                       'addressLocality': '성동구', 'addressRegion': '서울', 'addressCountry': 'KR'}}
    site = {'@context': 'https://schema.org', '@type': 'WebSite',
            'name': 'MAPDAL SEOUL', 'url': SITE_ORIGIN + '/', 'inLanguage': 'ko'}
    return _jsonld(org) + _jsonld(site)

def _seo_own_product_ld(page, canonical):
    """정적 상품 페이지(product-*.html) → DB의 page::옵션 행들로 Product 스키마 생성."""
    try:
        if not _state['pcols'] or not _state['pname'] or not _state['pprice']:
            return ''
        extra = (', img' if 'img' in _state['pcols'] else '') + \
                (', category' if 'category' in _state['pcols'] else '')
        # DB의 own ID는 'product-x.html::opt' 형식, 클린 URL 경로는 '.html' 없음 → 양쪽 매칭
        rs = rows('SELECT %s AS name, %s AS price, soldout, stock%s FROM products '
                  'WHERE id LIKE ? OR id LIKE ?'
                  % (_state['pname'], _state['pprice'], extra),
                  (page + '::%', page + '.html::%'))
    except Exception:
        return ''
    prices = [num(r.get('price')) for r in rs if num(r.get('price')) > 0]
    if not rs or not prices:
        return ''
    base = str(rs[0].get('name') or '').split(' — ')[0].strip() or page
    instock = any(not num(r.get('soldout')) for r in rs)
    img = next((str(r.get('img') or '') for r in rs if str(r.get('img') or '').strip()), '')
    if img and img.startswith('/'): img = SITE_ORIGIN + img
    prod = {'@context': 'https://schema.org', '@type': 'Product', 'name': base,
            'image': img or OG_IMAGE_URL, 'url': canonical,
            'brand': {'@type': 'Brand', 'name': 'MAPDAL SEOUL'},
            'offers': {'@type': 'AggregateOffer', 'priceCurrency': 'KRW',
                       'lowPrice': min(prices), 'highPrice': max(prices),
                       'offerCount': len(prices), 'availability': _seo_avail(not instock)}}
    cat = ''
    try: cat = norm_cat(rs[0].get('category'))
    except Exception: pass
    su, sn = _seo_section_of(cat if cat else ('kfood' if page.startswith(('product-kimbap', 'product-bowl', 'product-tteokbokki')) else ''))
    return _jsonld(prod) + _jsonld(_seo_breadcrumb(su, sn, base, canonical))

def _seo_album_block(uid):
    """/album-detail?uid=… 전용: 제목·설명·canonical·og·Product 스키마 (앨범별)."""
    try:
        r = one('SELECT %s AS name, %s AS price, list_price, img, soldout FROM products WHERE id=?'
                % (_state['pname'], _state['pprice']), ('k2g::' + uid,))
    except Exception:
        r = None
    if not r:
        return None
    name = re.sub(r'\s+', ' ', str(r.get('name') or '')).strip()
    if not name:
        return None
    price = num(r.get('price'))
    img = str(r.get('img') or '').strip()
    img_url = (_K2G_IMG_BASE + img) if img and not img.startswith('http') else (img or OG_IMAGE_URL)
    canonical = '%s/album-detail?uid=%s' % (SITE_ORIGIN, uid)
    desc = '%s — 정품 K-POP 앨범. %sKPOP2GETHER×맵달SEOUL 공식 스토어, 판매량 차트 집계 반영.' % (
        name[:80], ('판매가 ₩%s. ' % format(price, ',')) if price > 0 else '')
    prod = {'@context': 'https://schema.org', '@type': 'Product', 'name': name,
            'image': img_url, 'url': canonical, 'category': 'K-POP Album',
            'brand': {'@type': 'Brand', 'name': 'KPOP2GETHER'}}
    if price > 0:
        prod['offers'] = {'@type': 'Offer', 'priceCurrency': 'KRW', 'price': price,
                          'availability': _seo_avail(num(r.get('soldout'))), 'url': canonical}
    parts = ['<meta name="description" content="%s">' % _og_esc(desc),
             '<link rel="canonical" href="%s">' % canonical,
             '<meta property="og:url" content="%s">' % canonical,
             '<meta property="og:image" content="%s">' % _og_esc(img_url),
             _jsonld(prod),
             _jsonld(_seo_breadcrumb('/kpop', 'KPOP(음반)', name[:60], canonical))]
    return {'title': name, 'block': '\n'.join(parts)}

def _seo_insert_after_title(html, block):
    i = html.lower().find('</title>')
    if i >= 0:
        j = i + len('</title>')
        return html[:j] + '\n' + block + html[j:]
    hm = _OG_HEAD_RE.search(html)
    return (html[:hm.end()] + '\n' + block + html[hm.end():]) if hm else html

def _seo_apply(html, path='', uid=None):
    """경로 기반 SEO 주입 — _inject_og 이전에 실행되어 제목/설명이 og에도 반영된다."""
    if not path or 'mpSeo' in html or '<head' not in html[:4000].lower():
        return html
    parts = ['<!--mpSeo-->']
    # ① 결제·계정 등 — 색인 제외
    if path in _SEO_NOINDEX:
        return _seo_insert_after_title(html, parts[0] + '<meta name="robots" content="noindex,nofollow">')
    conf = seo_conf()
    if conf['naver']:
        parts.append('<meta name="naver-site-verification" content="%s">' % conf['naver'])
    if conf['google']:
        parts.append('<meta name="google-site-verification" content="%s">' % conf['google'])
    # ② 앨범 상세(?uid=) — 앨범별 제목·설명·스키마
    if path == '/album-detail' and uid:
        ab = _seo_album_block(uid)
        if ab:
            html = _OG_TITLE_RE.sub('<title>%s — MAPDAL SEOUL</title>' % _og_esc(ab['title'][:120]), html, count=1)
            parts.append(ab['block'])
            if 'og:locale' not in html:
                parts.append('<meta property="og:locale" content="ko_KR">')
            return _seo_insert_after_title(html, '\n'.join(parts))
    canonical = SITE_ORIGIN + path
    # ③ 설명 — 페이지별 사전 → 홈 설명(관리자 설정) → 사이트 기본
    if 'name="description"' not in html:
        desc = _SEO_PAGE_DESC.get(path) or ''
        if path == '/home' and conf['desc']:
            desc = conf['desc']
        parts.append('<meta name="description" content="%s">' % _og_esc(desc or OG_DESC_DEFAULT))
    if 'rel="canonical"' not in html:
        parts.append('<link rel="canonical" href="%s">' % canonical)
    if 'og:url' not in html:
        parts.append('<meta property="og:url" content="%s">' % canonical)
    if 'og:locale' not in html:
        parts.append('<meta property="og:locale" content="ko_KR">')
    # ④ 구조화 데이터
    if path == '/home':
        html = _OG_TITLE_RE.sub('<title>%s</title>' % _og_esc(HOME_TITLE), html, count=1)
        parts.append(_seo_home_ld())
    elif path.startswith('/product-'):
        parts.append(_seo_own_product_ld(path[1:], canonical))
    return _seo_insert_after_title(html, '\n'.join(p for p in parts if p))

# ── robots.txt · sitemap.xml (반드시 catch-all 라우트보다 먼저 등록) ──────
_ROBOTS_TXT = '''User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /auth/
Disallow: /cart
Disallow: /checkout
Disallow: /order-complete
Disallow: /account
Disallow: /search
Disallow: /hero-admin

Sitemap: %s/sitemap.xml
''' % SITE_ORIGIN

@admin_router.api_route('/robots.txt', methods=['GET', 'HEAD'])
def robots_txt(request: Request):
    hdr = {'Cache-Control': 'public, max-age=3600'}
    # HEAD도 GET과 동일 응답을 반환 — Starlette가 HEAD 시 본문만 제거하고
    # Content-Length는 유지한다. (빈 본문 반환 시 크롤러가 '내용 없음'으로 오판)
    return Response(_ROBOTS_TXT, media_type='text/plain; charset=utf-8', headers=hdr)

_sitemap_cache = {'t': 0.0, 'xml': None}

def _sitemap_xml():
    if _sitemap_cache['xml'] is not None and time.time() - _sitemap_cache['t'] < 600:
        return _sitemap_cache['xml']
    locs = [SITE_ORIGIN + '/home']
    removed = set()
    try: removed = _own_removed_pages()
    except Exception: pass
    try:
        files = sorted(f for f in os.listdir(STATIC_DIR) if _PAGE_RE.fullmatch(f))
    except Exception:
        files = []
    for f in files:
        if f in _SEO_HOME_ALIAS_FILES:
            continue
        slug = f[:-5]
        if ('/' + slug) in _SEO_NOINDEX or slug in removed:
            continue
        locs.append(SITE_ORIGIN + '/' + slug)
    locs.append(SITE_ORIGIN + '/kpop')
    try:
        ensure_ready()
        for r in rows("SELECT id FROM products WHERE id LIKE ? ORDER BY id", ('mp::%',)):
            locs.append(SITE_ORIGIN + '/p/' + str(r['id']))
        for r in rows("SELECT id FROM products WHERE id LIKE ? ORDER BY COALESCE(sort_order,999999999), id", ('k2g::%',)):
            locs.append(SITE_ORIGIN + '/album-detail?uid=' + str(r['id'])[5:])
    except Exception:
        pass                                          # DB 미준비 시 페이지만이라도 제공 (fail-open)
    esc = lambda u: u.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + ''.join('<url><loc>%s</loc></url>\n' % esc(u) for u in locs)
           + '</urlset>\n')
    _sitemap_cache.update(t=time.time(), xml=xml)
    return xml

@admin_router.api_route('/sitemap.xml', methods=['GET', 'HEAD'])
def sitemap_xml(request: Request):
    hdr = {'Cache-Control': 'public, max-age=600'}
    # HEAD도 GET과 동일 응답 — Starlette가 HEAD 시 본문만 제거하고 Content-Length는
    # 실제 크기로 유지한다. 빈 본문(0바이트) 반환 시 구글이 '가져올 수 없음' 처리.
    return Response(_sitemap_xml(), media_type='application/xml; charset=utf-8', headers=hdr)

_BRAND_CSS_LINK = '<link id="mpBrandCss" rel="stylesheet" href="/brand-system.css">'
_BRAND_JS_TAG = '<script id="mpBrandJs" defer src="/brand-system.js"></script>'

def _brand_apply(html):
    """Attach the shared wordmark and white-canvas brand system once per document."""
    if '<head' not in html[:4000].lower():
        return html
    if 'mpBrandCss' not in html:
        i = html.lower().find('</head>')
        html = (html[:i] + _BRAND_CSS_LINK + html[i:]) if i >= 0 else (_BRAND_CSS_LINK + html)
    if 'mpBrandJs' not in html:
        i = html.lower().rfind('</body>')
        html = (html[:i] + _BRAND_JS_TAG + html[i:]) if i >= 0 else (html + _BRAND_JS_TAG)
    return html

def _inject_auth(html, path='', uid=None):
    html = _serve_k2g_from_db(html)
    html = _inject_shop_products(html)
    html = _hide_removed_static_cards(html)
    html = _kpop_apply(html)
    html = _feedback_apply(html)
    html = _checkout_apply(html)
    html = _order_complete_apply(html)
    html = _homeblocks_apply(html, path)
    html, patched = _patch_legacy_footer(html)
    html = _seo_apply(html, path, uid)
    html = _inject_og(html)
    html = _brand_apply(html)
    add = ''
    if 'mpAuthJs' not in html: add += AUTH_SNIPPET
    if 'mpLikeJs' not in html: add += LIKE_SNIPPET
    if 'mpMobNav' not in html: add += MOBNAV_SNIPPET
    if 'mpTickerJs' not in html: add += TICKER_SNIPPET
    if 'mpCardCss' not in html: add += CARD_CSS_SNIPPET
    if 'mpRelatedJs' not in html: add += _RELATED_WIDGET_SNIPPET
    if 'mpFooter' not in html: add += footer_snippet()
    if not add: return html
    i = html.lower().rfind('</body>')
    return (html[:i] + add + html[i:]) if i >= 0 else (html + add)

# ── 관리자: 문의/상품Q&A/취소·반품·교환 요청 처리 + 포인트 ──
@admin_router.get('/admin/api/cs')
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
    m = one('SELECT * FROM members WHERE id=?', (body.get('id'),)) if body.get('id') else one('SELECT * FROM members WHERE customer_id=? ORDER BY created LIMIT 1', (body.get('customer_id'),))
    if not m: raise HTTPException(404, 'not found')
    delta = num(body.get('delta'))
    if not delta: raise HTTPException(400, '지급/차감 포인트를 입력하세요')
    cid = m.get('customer_id') or customer_ensure(m, True)
    reason = (body.get('reason') or '').strip()[:120]
    if not reason: raise HTTPException(400, '포인트 조정 사유를 입력하세요')
    nv = point_apply(cid, m['id'], 'ADMIN_ADJUST', delta, 'admin:%s' % uid(), reason,
                     by_admin=a.get('name') or '')
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
    cid = m.get('customer_id') or ''
    if not cid: return None, ()
    return '(customer_id=? OR order_id IN (SELECT order_id FROM account_order_links WHERE customer_id=?))', (cid, cid)

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
    cp = one('SELECT * FROM customer_profiles WHERE id=?', (m.get('customer_id') or '',)) or {}
    grade = cp.get('grade') or 'WELCOME'
    counters = {str(i): 0 for i in range(1, 6)}
    w, args = member_orders_where(m)
    linked = bool(w)
    if w:
        d30 = (kst_today() - datetime.timedelta(days=30)).isoformat()
        for r in rows('SELECT status, fulfill FROM orders WHERE created>=? AND ' + w, (d30,) + args):
            st, _ = order_step(r.get('status'), r.get('fulfill'))
            if st: counters[str(st)] += 1
    likes = num((one('SELECT COUNT(*) AS c FROM member_likes WHERE customer_id=?', (m.get('customer_id') or '',)) or {}).get('c'))
    cs = consent_state(m.get('customer_id') or '')
    consent_required = not (cs.get('TERMS') and num(cs['TERMS'].get('granted')) and cs['TERMS'].get('policy_version') == TERMS_VERSION
                            and cs.get('PRIVACY') and num(cs['PRIVACY'].get('granted')) and cs['PRIVACY'].get('policy_version') == PRIVACY_VERSION)
    identity_providers=[r['provider'] for r in rows('SELECT provider FROM auth_identities WHERE customer_id=? ORDER BY created_at',(m.get('customer_id') or '',))]
    return {'name': m.get('name') or '회원', 'email': m.get('email') or '',
            'provider': {'google': 'Google', 'apple': 'Apple', 'email': '이메일', 'kakao': '카카오'}.get(m.get('provider'), m.get('provider')),
            'has_pw': m.get('provider') == 'email', 'phone': m.get('phone') or '',
            'phone_verified': num(m.get('phone_verified')), 'points': num(cp.get('points_balance')),
            'grade': grade, 'linked': linked, 'counters': counters, 'likes': likes,
            'fav_store': num(m.get('fav_store')),
            'gender': m.get('gender') or '', 'birth': m.get('birth') or '', 'age_range': m.get('age_range') or '',
            'customer_no': cp.get('customer_no') or '', 'status': cp.get('status') or 'ACTIVE',
            'consent_required': consent_required, 'marketing_ok': num(cp.get('marketing_ok')),
            'identity_providers': identity_providers}

@admin_router.get('/api/member/points')
def api_m_points(request: Request):
    m = member_required(request); cid = m.get('customer_id') or ''
    cp = one('SELECT points_balance FROM customer_profiles WHERE id=?', (cid,)) or {}
    rs = rows('SELECT event_type,amount,balance_after,reason,expires_at,created_at FROM point_ledger WHERE customer_id=? ORDER BY created_at DESC LIMIT 200', (cid,))
    names = {'SIGNUP_BONUS':'최초 가입 혜택','LEGACY_BALANCE':'기존 포인트 이관','ADMIN_ADJUST':'관리자 조정'}
    return {'balance': num(cp.get('points_balance')), 'rows': [
        {'type': names.get(r['event_type'], r['event_type']), 'amount': num(r['amount']),
         'balance': num(r['balance_after']), 'reason': r.get('reason') or '',
         'expires': r.get('expires_at') or '', 'created': (r.get('created_at') or '')[:16].replace('T',' ')} for r in rs]}

@admin_router.get('/api/member/consents')
def api_m_consents(request: Request):
    m = member_required(request); st = consent_state(m.get('customer_id') or '')
    return {'terms_version': TERMS_VERSION, 'privacy_version': PRIVACY_VERSION,
            'terms': bool(st.get('TERMS') and num(st['TERMS'].get('granted')) and st['TERMS'].get('policy_version') == TERMS_VERSION),
            'privacy': bool(st.get('PRIVACY') and num(st['PRIVACY'].get('granted')) and st['PRIVACY'].get('policy_version') == PRIVACY_VERSION),
            'marketing': bool(st.get('MARKETING') and num(st['MARKETING'].get('granted')))}

@admin_router.post('/api/member/consents')
def api_m_consents_save(request: Request, body: dict = Body(...)):
    m = member_required(request); cid = m.get('customer_id') or ''
    if body.get('accept_required'):
        consent_record(cid, m['id'], 'TERMS', True, TERMS_VERSION, 'ACCOUNT', request)
        consent_record(cid, m['id'], 'PRIVACY', True, PRIVACY_VERSION, 'ACCOUNT', request)
    if 'marketing' in body:
        consent_record(cid, m['id'], 'MARKETING', bool(body.get('marketing')), TERMS_VERSION, 'ACCOUNT', request)
    account_security(m, 'CONSENT_UPDATE', request)
    return {'ok': True}

@admin_router.get('/api/member/sessions')
def api_m_sessions(request: Request):
    m=member_required(request); cur=hashlib.sha256((request.cookies.get('mp_member') or '').encode()).hexdigest()
    rs=rows('SELECT id,created,expires,ip,user_agent,last_seen FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?) AND expires>? ORDER BY created DESC',(m.get('customer_id') or '',now_iso()))
    return {'rows':[{'id':r['id'],'current':r['id']==cur,'created':(r.get('created') or '')[:16].replace('T',' '),
                     'expires':(r.get('expires') or '')[:16].replace('T',' '),'ip':r.get('ip') or '',
                     'device':(r.get('user_agent') or '알 수 없는 기기')[:120]} for r in rs]}

@admin_router.post('/api/member/sessions/revoke')
def api_m_sessions_revoke(request: Request, body: dict=Body(...)):
    m=member_required(request); cur=hashlib.sha256((request.cookies.get('mp_member') or '').encode()).hexdigest()
    if body.get('all'):
        run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?) AND id<>?',(m.get('customer_id') or '',cur)); detail='other sessions'
    else:
        sid=(body.get('id') or '').strip();
        if sid==cur: raise HTTPException(400,'현재 세션은 로그아웃 버튼을 이용하세요')
        run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?) AND id=?',(m.get('customer_id') or '',sid)); detail='one session'
    account_security(m,'SESSION_REVOKED',request,detail)
    return {'ok':True}

@admin_router.post('/api/member/orders/claim/send')
def api_m_claim_send(request: Request, body: dict = Body(...)):
    m = member_required(request); oid = (body.get('order_id') or '').strip()[:60]
    r = one('SELECT order_id,buyer,customer_id,contact_phone_norm FROM orders WHERE order_id=?', (oid,))
    if not r: raise HTTPException(404, '주문을 찾을 수 없습니다')
    if r.get('customer_id') and r.get('customer_id') != m.get('customer_id'):
        owner=one('SELECT status FROM customer_profiles WHERE id=?',(r.get('customer_id'),)) or {}
        if owner.get('status')!='GUEST': raise HTTPException(409, '이미 다른 계정에 연결된 주문입니다')
    b = jload(r.get('buyer'), {}); phone = kphone_norm(r.get('contact_phone_norm') or b.get('phone') or '')
    entered = kphone_norm(body.get('phone') or '')
    if len(phone) < 9 or not hmac.compare_digest(phone, entered):
        raise HTTPException(400, '주문번호와 주문 당시 연락처를 확인해 주세요')
    ip = (request.client.host if request.client else '') or '-'; key='oc:'+m['id']+':'+ip; guard(key); fail_hit(key)
    code = str(secrets.randbelow(900000) + 100000); cid = uid()
    exp = (datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).isoformat(timespec='seconds')
    ch = hashlib.sha256((cid + ':' + code).encode()).hexdigest()
    run('INSERT INTO order_claims VALUES(?,?,?,?,?,?,?,?,?,?)',
        (cid, oid, m.get('customer_id') or '', m['id'], phone, ch, 0, now_iso(), exp, 0))
    ok, dry = system_sms(phone, '[맵달SEOUL] 주문 연결 인증번호는 [%s] 입니다. 5분 내에 입력해 주세요.' % code, '주문연결', oid)
    if not ok: raise HTTPException(400, '문자 발송에 실패했습니다')
    return {'ok': True, 'claim_id': cid, 'dry': dry}

@admin_router.post('/api/member/orders/claim/verify')
def api_m_claim_verify(request: Request, body: dict = Body(...)):
    m = member_required(request); cid=(body.get('claim_id') or '').strip(); code=(body.get('code') or '').strip()
    c = one('SELECT * FROM order_claims WHERE id=? AND member_id=? AND used=0', (cid, m['id']))
    if not c or (c.get('expires_at') or '') <= now_iso(): raise HTTPException(400, '인증 요청이 만료되었습니다')
    if num(c.get('attempts')) >= 5: raise HTTPException(429, '인증 시도 횟수를 초과했습니다')
    if not hmac.compare_digest(c.get('code_hash') or '', hashlib.sha256((cid + ':' + code).encode()).hexdigest()):
        run('UPDATE order_claims SET attempts=attempts+1 WHERE id=?', (cid,)); raise HTTPException(400, '인증번호가 올바르지 않습니다')
    r = one('SELECT customer_id FROM orders WHERE order_id=?', (c['order_id'],))
    if r and r.get('customer_id') and r.get('customer_id') != m.get('customer_id'):
        owner=one('SELECT status FROM customer_profiles WHERE id=?',(r.get('customer_id'),)) or {}
        if owner.get('status')!='GUEST': raise HTTPException(409, '이미 다른 계정에 연결된 주문입니다')
    run('UPDATE orders SET customer_id=?, member_id=?, contact_phone_norm=? WHERE order_id=?',
        (m.get('customer_id'), m['id'], c['phone_norm'], c['order_id']))
    try: run('INSERT INTO account_order_links VALUES(?,?,?,?,?,?)',
             (c['order_id'], m.get('customer_id'), m['id'], 'ORDER_PHONE_OTP', now_iso(), now_iso()))
    except Exception: run('UPDATE account_order_links SET customer_id=?,member_id=?,link_source=?,linked_at=?,verified_at=? WHERE order_id=?',
                          (m.get('customer_id'),m['id'],'ORDER_PHONE_OTP',now_iso(),now_iso(),c['order_id']))
    run('UPDATE order_claims SET used=1 WHERE id=?', (cid,)); fail_clear('oc:'+m['id']+':'+(((request.client.host if request.client else '') or '-')))
    account_security(m, 'ORDER_CLAIM', request, c['order_id'])
    return {'ok': True, 'order_id': c['order_id']}

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
    open_req = one("SELECT rtype, status FROM member_requests WHERE order_id=? AND customer_id=? AND status IN ('접수','처리중')", (oid, m.get('customer_id') or ''))
    return {'order_id': oid, 'created': (r.get('created') or '')[:19].replace('T', ' '), 'step': st, 'status_kr': kr,
            'amount': num(r.get('amount')), 'ship_method': r.get('ship_method') or '',
            'tracking': r.get('tracking') or '', 'receipt': r.get('receipt_url') or '',
            'addr': ('[%s] %s %s' % (b.get('zip', ''), b.get('addr1', ''), b.get('addr2', ''))
                     + ((' · 메모: ' + str(b.get('memo'))) if b.get('memo') else '')),
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
    if one("SELECT id FROM member_requests WHERE order_id=? AND customer_id=? AND status IN ('접수','처리중')", (oid, m.get('customer_id') or '')):
        raise HTTPException(400, '이미 처리 중인 요청이 있습니다')
    run('INSERT INTO member_requests(id,member_id,order_id,rtype,reason,created,status,admin_memo,updated,customer_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], oid, rtype, reason, now_iso(), '접수', '', now_iso(), m.get('customer_id') or ''))
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
                         for r in rows('SELECT * FROM member_requests WHERE customer_id=? ORDER BY created DESC LIMIT 50', (m.get('customer_id') or '',))],
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
    vid = uid(); ch = hashlib.sha256((vid + ':' + code).encode()).hexdigest()
    run('UPDATE phone_verifications SET used=1 WHERE member_id=? AND used=0', (m['id'],))
    run('INSERT INTO phone_verifications(id,member_id,phone,code,created,expires,used,code_hash,attempts) VALUES(?,?,?,?,?,?,0,?,0)',
        (vid, m['id'], d, '', now_iso(), exp, ch))
    ok, dry = system_sms(d, '[맵달SEOUL] 휴대폰 인증번호는 [%s] 입니다. 5분 내에 입력해 주세요.' % code, '휴대폰인증')
    if not ok: raise HTTPException(400, '문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    return {'ok': True, 'dry': dry}

@admin_router.post('/api/member/phone/verify')
def api_m_phone_verify(request: Request, body: dict = Body(...)):
    m = member_required(request)
    code = (body.get('code') or '').strip()
    v = one('SELECT * FROM phone_verifications WHERE member_id=? AND used=0 ORDER BY created DESC LIMIT 1', (m['id'],))
    if not v or (v.get('expires') or '') <= now_iso():
        raise HTTPException(400, '인증번호가 올바르지 않거나 만료되었습니다')
    if num(v.get('attempts')) >= 5: raise HTTPException(429, '인증 시도 횟수를 초과했습니다')
    expected = v.get('code_hash') or ''
    valid = (hmac.compare_digest(expected, hashlib.sha256((v['id'] + ':' + code).encode()).hexdigest())
             if expected else hmac.compare_digest(v.get('code') or '', code))
    if not valid:
        run('UPDATE phone_verifications SET attempts=attempts+1 WHERE id=?', (v['id'],))
        raise HTTPException(400, '인증번호가 올바르지 않거나 만료되었습니다')
    conflict = one("SELECT customer_id FROM customer_contacts WHERE kind='PHONE' AND value_norm=? AND customer_id<>? AND verified=1",
                   (v['phone'], m.get('customer_id') or ''))
    if conflict: raise HTTPException(409, '이미 다른 계정에서 인증된 휴대폰 번호입니다. 고객센터에 문의해 주세요')
    cid=m.get('customer_id') or ''
    guests = rows("SELECT cc.customer_id FROM customer_contacts cc JOIN customer_profiles c ON c.id=cc.customer_id WHERE cc.kind='PHONE' AND cc.value_norm=? AND cc.customer_id<>? AND c.status='GUEST' UNION SELECT o.customer_id FROM orders o JOIN customer_profiles c ON c.id=o.customer_id WHERE o.contact_phone_norm=? AND o.customer_id<>? AND c.status='GUEST'",
                  (v['phone'],cid,v['phone'],cid))
    for guest in guests:
        gid=guest['customer_id']
        run('UPDATE orders SET customer_id=?,member_id=? WHERE customer_id=?',(cid,m['id'],gid))
        run('UPDATE account_order_links SET customer_id=?,member_id=?,link_source=?,verified_at=? WHERE customer_id=?',(cid,m['id'],'VERIFIED_PHONE_MERGE',now_iso(),gid))
        run('DELETE FROM customer_contacts WHERE customer_id=?',(gid,))
        run("UPDATE customer_profiles SET status='MERGED',updated_at=? WHERE id=?",(now_iso(),gid))
    # 다른 계정에 입력만 되고 인증되지 않은 번호는 실제 소유자의 인증을 우선한다.
    displaced=rows("SELECT DISTINCT customer_id FROM customer_contacts WHERE kind='PHONE' AND value_norm=? AND customer_id<>? AND verified=0",(v['phone'],cid))
    for x in displaced:
        run("DELETE FROM customer_contacts WHERE kind='PHONE' AND value_norm=? AND customer_id=?",(v['phone'],x['customer_id']))
        run("UPDATE members SET phone='',phone_verified=0,updated_at=? WHERE customer_id=?",(now_iso(),x['customer_id']))
        try: run('INSERT INTO account_security_events VALUES(?,?,?,?,?,?,?,?)',(uid(),x['customer_id'],'','UNVERIFIED_PHONE_REMOVED','-','',_mask_phone(v['phone']),now_iso()))
        except Exception: pass
    run('UPDATE phone_verifications SET used=1 WHERE id=?', (v['id'],))
    run('UPDATE members SET phone=?, phone_verified=1 WHERE id=?', (v['phone'], m['id']))
    ex = one("SELECT id FROM customer_contacts WHERE customer_id=? AND kind='PHONE'", (m.get('customer_id') or '',))
    if ex:
        run('UPDATE customer_contacts SET value=?,value_norm=?,verified=1,is_primary=1,verified_at=? WHERE id=?',
            (v['phone'],v['phone'],now_iso(),ex['id']))
    else:
        run('INSERT INTO customer_contacts VALUES(?,?,?,?,?,?,?,?,?)',
            (uid(),m.get('customer_id') or '','PHONE',v['phone'],v['phone'],1,1,now_iso(),now_iso()))
    fail_clear('pv:' + m['id'] + ':' + (((request.client.host if request.client else '') or '-')))
    account_security(m, 'PHONE_VERIFIED', request, _mask_phone(v['phone']))
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
    if any(k in body for k in ('bank','acct','acct_name')):
        raise HTTPException(400, '환불계좌는 계정에 저장하지 않습니다. 환불 요청 시 안전하게 별도 확인합니다')
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
    run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)', (m.get('customer_id') or '',))
    sid = member_session_make(m['id'], request)
    account_security(m, 'PASSWORD_CHANGED', request, 'all sessions revoked')
    resp = JSONResponse({'ok': True})
    resp.set_cookie('mp_member', sid, httponly=True, secure=True, samesite='lax', max_age=2592000)
    return resp

@admin_router.get('/api/member/addresses')
def api_m_addr_list(request: Request):
    m = member_required(request)
    return {'rows': rows('SELECT * FROM member_addresses WHERE customer_id=? ORDER BY is_default DESC, created DESC', (m.get('customer_id') or '',))}

@admin_router.post('/api/member/addresses')
def api_m_addr_save(request: Request, body: dict = Body(...)):
    m = member_required(request)
    act = body.get('act', 'add')
    if act == 'delete':
        run('DELETE FROM member_addresses WHERE id=? AND customer_id=?', (body.get('id'), m.get('customer_id') or '')); return {'ok': True}
    if act == 'default':
        run('UPDATE member_addresses SET is_default=0 WHERE customer_id=?', (m.get('customer_id') or '',))
        run('UPDATE member_addresses SET is_default=1 WHERE id=? AND customer_id=?', (body.get('id'), m.get('customer_id') or '')); return {'ok': True}
    for k in ('rname', 'phone', 'zip', 'addr1'):
        if not (body.get(k) or '').strip(): raise HTTPException(400, '받는분/연락처/우편번호/주소를 입력하세요')
    first = not one('SELECT id FROM member_addresses WHERE customer_id=? LIMIT 1', (m.get('customer_id') or '',))
    run('INSERT INTO member_addresses(id,member_id,label,rname,phone,zip,addr1,addr2,is_default,created,customer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], (body.get('label') or '기본')[:20], body['rname'][:30], digits(body['phone']),
         body['zip'][:10], body['addr1'][:120], (body.get('addr2') or '')[:80], 1 if first else 0, now_iso(), m.get('customer_id') or ''))
    return {'ok': True}

@admin_router.post('/api/member/likes')
def api_m_like(request: Request, body: dict = Body(...)):
    m = member_required(request)
    pid = body.get('product_id') or ''
    if not one('SELECT id FROM products WHERE id=?', (pid,)): raise HTTPException(404, '상품 없음')
    ex = one('SELECT id FROM member_likes WHERE customer_id=? AND product_id=?', (m.get('customer_id') or '', pid))
    if body.get('on'):
        if not ex: run('INSERT INTO member_likes(id,member_id,product_id,created,customer_id) VALUES(?,?,?,?,?)', (uid(), m['id'], pid, now_iso(), m.get('customer_id') or ''))
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
    rs = rows('SELECT %s FROM member_likes l LEFT JOIN products p ON p.id=l.product_id WHERE l.customer_id=? ORDER BY l.created DESC LIMIT 200' % sel, (m.get('customer_id') or '',))
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
        run('DELETE FROM member_restock WHERE customer_id=? AND product_id=? AND notified=0', (m.get('customer_id') or '', pid)); return {'ok': True, 'on': False}
    if one('SELECT id FROM member_restock WHERE customer_id=? AND product_id=? AND notified=0', (m.get('customer_id') or '', pid)):
        return {'ok': True, 'on': True}
    run('INSERT INTO member_restock(id,member_id,product_id,phone,created,notified,customer_id) VALUES(?,?,?,?,?,0,?)', (uid(), m['id'], pid, digits(m.get('phone')), now_iso(), m.get('customer_id') or ''))
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
    n = run('DELETE FROM member_likes WHERE id=? AND customer_id=?', (body.get('rid'), m.get('customer_id') or ''))
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
        ex = one('SELECT id FROM member_likes WHERE customer_id=? AND product_id=?', (m.get('customer_id') or '', pid))
        if on and not ex:
            run('INSERT INTO member_likes(id,member_id,product_id,created,page,pname,pprice,pimg,customer_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(), m['id'], pid, now_iso(), href, pname, pprice, pimg, m.get('customer_id') or ''))
        elif not on and ex:
            run('DELETE FROM member_likes WHERE id=?', (ex['id'],))
    else:
        ex = one('SELECT id FROM member_likes WHERE customer_id=? AND page=?', (m.get('customer_id') or '', href))
        if on and not ex:
            run('INSERT INTO member_likes(id,member_id,product_id,created,page,pname,pprice,pimg,customer_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(), m['id'], None, now_iso(), href, pname, pprice, pimg, m.get('customer_id') or ''))
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
    mine = rows('SELECT page FROM member_likes WHERE customer_id=? AND page IS NOT NULL', (m.get('customer_id') or '',))
    have = {r['page'] for r in mine if r.get('page')}
    for p in pages:
        if p in have: liked.add(p)
    return {'login': True, 'liked': sorted(liked)}

@admin_router.get('/api/member/restock')
def api_m_restock_list(request: Request):
    m = member_required(request)
    nm = _state['pname'] or 'id'
    rs = rows('SELECT r.id AS rid, r.notified, r.created, p.id, p.%s AS name, p.soldout, p.stock FROM member_restock r JOIN products p ON p.id=r.product_id WHERE r.customer_id=? ORDER BY r.created DESC LIMIT 100' % nm, (m.get('customer_id') or '',))
    return {'rows': [{'rid': r['rid'], 'id': r['id'], 'name': r.get('name') or r['id'],
                      'notified': num(r['notified']), 'soldout': num(r.get('soldout')) or num(r.get('stock')) <= 0,
                      'created': (r['created'] or '')[:10]} for r in rs]}

@admin_router.post('/api/member/inquiries')
def api_m_inq_create(request: Request, body: dict = Body(...)):
    m = member_required(request)
    title = (body.get('title') or '').strip()[:80]; bd = (body.get('body') or '').strip()[:2000]
    if not title or not bd: raise HTTPException(400, '제목과 내용을 입력하세요')
    run('INSERT INTO member_inquiries(id,member_id,order_id,title,body,created,status,answer,answered_at,answered_by,customer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], (body.get('order_id') or '')[:40], title, bd, now_iso(), '접수', '', '', '', m.get('customer_id') or ''))
    return {'ok': True}

@admin_router.get('/api/member/inquiries')
def api_m_inq_list(request: Request):
    m = member_required(request)
    return {'rows': [{'id': r['id'], 'title': r['title'], 'body': r['body'], 'order_id': r.get('order_id') or '',
                      'created': (r['created'] or '')[:16].replace('T', ' '), 'status': r['status'],
                      'answer': r.get('answer') or '', 'answered_at': (r.get('answered_at') or '')[:16].replace('T', ' ')}
                     for r in rows('SELECT * FROM member_inquiries WHERE customer_id=? ORDER BY created DESC LIMIT 50', (m.get('customer_id') or '',))]}

@admin_router.post('/api/member/pqna')
def api_m_pqna_create(request: Request, body: dict = Body(...)):
    m = member_required(request)
    pid = body.get('product_id') or ''; q = (body.get('question') or '').strip()[:1000]
    if not q: raise HTTPException(400, '문의 내용을 입력하세요')
    if not one('SELECT id FROM products WHERE id=?', (pid,)): raise HTTPException(404, '상품 없음')
    run('INSERT INTO member_pqna(id,member_id,product_id,question,created,status,answer,answered_at,answered_by,customer_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (uid(), m['id'], pid, q, now_iso(), '접수', '', '', '', m.get('customer_id') or ''))
    return {'ok': True}

@admin_router.get('/api/member/pqna')
def api_m_pqna_list(request: Request):
    m = member_required(request)
    nm = _state['pname'] or 'id'
    rs = rows('SELECT q.*, p.%s AS pname FROM member_pqna q LEFT JOIN products p ON p.id=q.product_id WHERE q.customer_id=? ORDER BY q.created DESC LIMIT 50' % nm, (m.get('customer_id') or '',))
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
            'liked': bool(one('SELECT id FROM member_likes WHERE customer_id=? AND product_id=?', (m.get('customer_id') or '', pid))),
            'restock': bool(one('SELECT id FROM member_restock WHERE customer_id=? AND product_id=? AND notified=0', (m.get('customer_id') or '', pid)))}

@admin_router.post('/api/member/withdraw')
def api_m_withdraw(request: Request, body: dict = Body(...)):
    m = member_required(request)
    if m.get('provider') == 'email':
        if not pw_verify(body.get('password') or '', m.get('pw') or ''):
            raise HTTPException(403, '비밀번호가 올바르지 않습니다')
    elif (body.get('confirm') or '') != '탈퇴':
        raise HTTPException(400, "'탈퇴' 를 정확히 입력해 주세요")
    account_security(m, 'WITHDRAW', request)
    cid=m.get('customer_id') or ''
    try: consent_record(cid,m['id'],'MARKETING',False,TERMS_VERSION,'WITHDRAW',request)
    except Exception: pass
    try:
        bal=num((one('SELECT points_balance FROM customer_profiles WHERE id=?',(cid,)) or {}).get('points_balance'))
        if bal: point_apply(cid,m['id'],'WITHDRAWAL_EXPIRY',-bal,'withdraw:%s' % cid,'회원탈퇴로 포인트 소멸')
    except Exception: pass
    try: run('DELETE FROM member_sessions WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)',(cid,))
    except Exception: pass
    for t in ('member_likes','member_restock','member_addresses'):
        try: run('DELETE FROM %s WHERE customer_id=?' % t,(cid,))
        except Exception: pass
    for t in ('phone_verifications','order_claims','password_resets'):
        try: run('DELETE FROM %s WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)' % t,(cid,))
        except Exception: pass
    try: run('DELETE FROM oauth_flows WHERE member_id IN (SELECT id FROM members WHERE customer_id=?)',(cid,))
    except Exception: pass
    try: run('DELETE FROM customer_contacts WHERE customer_id=?', (m.get('customer_id') or '',))
    except Exception: pass
    try: run("UPDATE auth_identities SET provider_sub='withdrawn:'||id,email_norm='',email_verified=0 WHERE customer_id=?", (cid,))
    except Exception: pass
    run("UPDATE members SET status='WITHDRAWN',sub='withdrawn:'||id,name='탈퇴회원',email='',email_verified=0,phone='',phone_verified=0,pw='',bank='',acct='',acct_name='',ci='',birth='',gender='',age_range='',withdrawn_at=?,updated_at=? WHERE customer_id=?",
        (now_iso(),now_iso(),cid))
    run("UPDATE customer_profiles SET status='WITHDRAWN',name='',points_balance=0,marketing_ok=0,withdrawn_at=?,updated_at=? WHERE id=?",
        (now_iso(),now_iso(),m.get('customer_id') or ''))
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
    return HTMLResponse(_inject_auth(_KPOP_MARK + html, '/kpop'), headers={'Cache-Control': 'no-cache'})

# ═══════ 정적 서빙 대체 (편집본 우선 · 반드시 모듈 마지막 라우트) ═══════
import mimetypes

@admin_router.get('/{spath:path}')
def serve_site(spath: str, request: Request):
    if not spath or spath.startswith(('admin', 'api/', 'auth/', 'p/')):
        raise HTTPException(404)
    name = os.path.basename(spath)
    seo_path, seo_uid = '', None                     # SEO: 클린 경로 (+앨범 uid)
    if name.endswith('.html') and '/' not in spath:
        seo_path = '/home' if name in _SEO_HOME_ALIAS_FILES else '/' + name[:-5]
        if seo_path == '/album-detail':
            u = (request.query_params.get('uid') or '').strip()
            if re.fullmatch(r'[A-Za-z0-9_-]{1,40}', u):
                seo_uid = u
    if name.endswith('.html') and _PAGE_RE.fullmatch(name) and '/' not in spath:
        try:
            ensure_ready()
            ov = one('SELECT html FROM page_edits WHERE path=?', (name,))
            if ov: return HTMLResponse(_inject_auth(ov['html'], seo_path, seo_uid), headers={'Cache-Control': 'no-cache'})
        except Exception:
            pass
    fp = os.path.realpath(os.path.join(STATIC_DIR, spath))
    root = os.path.realpath(STATIC_DIR)
    if not fp.startswith(root + os.sep) or not os.path.isfile(fp):
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>페이지를 찾을 수 없습니다</h2><a href="/">MAPDAL SEOUL 홈으로</a>', status_code=404)
    mt = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
    data = open(fp, 'rb').read()
    if mt == 'text/html':
        return HTMLResponse(_inject_auth(data.decode('utf-8', errors='replace'), seo_path, seo_uid), headers={'Cache-Control': 'no-cache'})
    return Response(data, media_type=mt)
