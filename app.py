"""
MAPDAL SEOUL — 라이브 커머스 백엔드 v2 (확장형)
FastAPI + PostgreSQL(운영) / SQLite(로컬 폴백) + 토스페이먼츠
- DATABASE_URL 환경변수가 있으면 PostgreSQL, 없으면 SQLite로 자동 전환
- 동시 주문 안전: 트랜잭션 + 행 잠금(FOR UPDATE), 재고 원자적 차감
- 결제 승인 멱등 처리 (중복 승인 방지)
"""
import os, re, json, secrets, datetime, base64, hashlib, threading
import urllib.request, urllib.error, urllib.parse
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
IS_PG = DATABASE_URL.startswith('postgresql')

# KG이니시스 INIStdPay — 계약 완료 시 아래 3개 환경변수를 실계약 값으로 교체
#   INICIS_MID     : 상점아이디 (상점관리자 발급)
#   INICIS_SIGNKEY : 웹결제 Sign Key (상점정보>계약정보>KEY정보>웹결제 Sign Key)
#   INICIS_INIAPI  : INIAPI Key (취소/환불용 · 상점정보>계약정보>부가정보>INIAPI key)
# 기본값은 이니시스 공식 테스트 상점 값 (INIpayTest) — 실결제 발생 안 함.
INICIS_MID     = os.getenv('INICIS_MID', 'INIpayTest')
INICIS_SIGNKEY = os.getenv('INICIS_SIGNKEY', 'SU5JTElURV9UUklQTEVERVNfS0VZU1RS')
INICIS_INIAPI  = os.getenv('INICIS_INIAPI', 'ItEQKi3rY7uvDS8l')
# 모바일 결제 — PC(웹표준)와 별개 모듈이며 파라미터 규격이 완전히 다르다.
#   INICIS_MOBILE_HASHKEY : 모바일 금액위변조 Hash Key
#     (상점정보>계약정보>KEY정보>모바일 금액위변조 Hash Key)
#   미설정 시 P_CHKFAKE(위변조 검증)를 생략하고 결제는 정상 진행된다.
#   운영에서는 반드시 설정할 것 — 금액 위변조 공격 방어에 필요.
INICIS_MOBILE_HASHKEY = os.getenv('INICIS_MOBILE_HASHKEY', '')
# PC 통합결제창 노출 수단 — 운영 스위치.
#   가상계좌(VBank)가 상점 계약에 없거나 장애가 확인되면 Render 환경변수
#   INICIS_GOPAYMETHOD=Card:DirectBank:HPP 로 코드 재배포 없이 즉시 내릴 수 있다.
#   (2026-07-27 무통장입금 계좌 미안내 CS — 대응 레버)
INICIS_GOPAYMETHOD = (os.getenv('INICIS_GOPAYMETHOD', '') or 'Card:DirectBank:VBank:HPP').strip()
# 결제 returnUrl/closeUrl 도메인 — 이니시스는 요청페이지와 도메인 일치를 검증(V023).
#   Cloudflare/Render 프록시 뒤에서는 req.base_url이 실제 도메인과 달라질 수 있으므로
#   SITE_ORIGIN 환경변수로 실도메인을 고정하는 것이 가장 안전. 미설정 시 헤더로 추론.
SITE_ORIGIN    = os.getenv('SITE_ORIGIN', 'https://mapdal.kr').rstrip('/')

# ── GA4 서버사이드 계측 (Measurement Protocol) ──────────────────────────
#   클라이언트 purchase(주문완료 페이지 도달 시 전송)는 결제창 이탈·인앱 전환·
#   창 닫힘 시 유실된다(업계 통상 5~15%). PAID 확정 시점에 서버가 동일
#   transaction_id + client_id 로 재전송하면 GA4 가 transaction_id 기준으로
#   중복을 제거하므로 유실분만 정확히 보전된다.
#   GA4_ID · GA4_API_SECRET 두 환경변수가 모두 있을 때만 동작(미설정 = 완전 무변화).
#   GA4_API_SECRET: GA4 관리 > 데이터 스트림 > Measurement Protocol API 비밀번호 생성.
GA4_ID         = ''.join(ch for ch in os.getenv('GA4_ID', '') if ch.isalnum() or ch in '-_')
GA4_API_SECRET = os.getenv('GA4_API_SECRET', '').strip()
_GA_COLS_OK    = None         # ga_* 컬럼 존재 여부 (최초 1회 판정 후 캐시 — _has_vbank_cols 와 동일 패턴)

# ── 시각 기준 ──────────────────────────────────────────────────────────
#  운영 서버(Render 싱가포르)의 시스템 시계는 UTC 다. now() 를 그대로 저장하면
#  주문 일시가 한국시간보다 9시간 이르게 찍힌다. 저장용 시각은 KST 로 통일한다.
#  ※ 이니시스 서명용 timestamp(epoch ms)는 절대시각이므로 변환하지 않는다.
KST = datetime.timezone(datetime.timedelta(hours=9))
def kst_naive():
    return datetime.datetime.now(KST).replace(tzinfo=None)
def kst_iso():
    return kst_naive().isoformat(timespec='seconds')

# ── 가상계좌 입금기한 (KST) ─────────────────────────────────────────────
#   정책: 채번 당일 23:59 마감. PC(acceptmethod vbank(YYYYMMDDhhmm))와
#   모바일(P_VBANK_DT + P_VBANK_TM)에 같은 값을 넣어 두 모듈의 기한을 일치시킨다.
#   ※ 이니시스 규격: vbank 는 시·분까지 지정 시 YYYYMMDDhhmm, 모바일은 날짜(8)와
#     시간(hhmm, 4)을 분리해 받는다. 미지정 시 PC=결제창에서 고객 선택, 모바일=+10일.
#   ※ 심야 채번 보호: 마감까지 남은 시간이 VBANK_DUE_GRACE(기본 30분) 미만이면
#     입금할 시간 자체가 없어 미입금 취소만 늘어나므로 익일 같은 시각으로 넘긴다.
#     환경변수로 재배포 없이 조정 — 무조건 당일 마감을 원하면 VBANK_DUE_GRACE=0.
#   · VBANK_DUE_HHMM  : 마감 시각 hhmm (기본 '2359')
#   · VBANK_DUE_DAYS  : 기준일 오프셋 (기본 0=당일 · 종전 3일 정책 복귀 시 3)
#   · VBANK_DUE_GRACE : 최소 보장 입금시간(분, 기본 30)
def _env_int(name: str, default: int) -> int:
    try:
        v = (os.getenv(name) or '').strip()
        return int(v) if v else default
    except Exception:
        return default

VBANK_DUE_HHMM  = re.sub(r'\D', '', os.getenv('VBANK_DUE_HHMM', '2359') or '')[:4] or '2359'
VBANK_DUE_DAYS  = max(0, min(30, _env_int('VBANK_DUE_DAYS', 0)))
VBANK_DUE_GRACE = max(0, min(720, _env_int('VBANK_DUE_GRACE', 30)))

def _vbank_due() -> str:
    """가상계좌 입금기한 YYYYMMDDhhmm (KST). PC·모바일 공통 소스."""
    now = kst_naive()
    try:
        hh, mm = int(VBANK_DUE_HHMM[:2]), int(VBANK_DUE_HHMM[2:4])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        hh, mm = 23, 59
    due = (now + datetime.timedelta(days=VBANK_DUE_DAYS)).replace(
        hour=hh, minute=mm, second=0, microsecond=0)
    while due <= now + datetime.timedelta(minutes=VBANK_DUE_GRACE):
        due += datetime.timedelta(days=1)
    return due.strftime('%Y%m%d%H%M')

ADMIN_TOKEN     = os.getenv('ADMIN_TOKEN', 'mapdal-admin-2026')
FREE_SHIP_OVER, SHIP_FEE = 30000, 3000
# ── NEW/DROPS 배송·적립 특칙 ─────────────────────────────────────────────
#   드롭 상품(mpd:: 프리픽스)은 한정수량·개별출고 특성상 금액과 무관하게
#   배송비 3,000원 정액이며 무료배송 기준을 적용하지 않는다. 적립도 없다.
#   장바구니에 드롭 상품이 1개라도 있으면 주문 전체를 드롭 정책으로 본다.
DROP_PREFIX = 'mpd::'
DROP_SHIP_FEE = 3000
POINT_RATE_BP = 100          # 일반 상품 구매 적립률 1% (basis point/10000)


# ── DB 계층 (PG/SQLite 이중 지원) ───────────────────────────────
POOL = None
DB_READY = False
if IS_PG:
    import psycopg
    from psycopg_pool import ConnectionPool
    from psycopg.rows import dict_row
else:
    import sqlite3
    SQLITE_PATH = os.path.join(BASE, 'mapdal.db')

def _connect_pg_with_retry(max_attempts=30, delay=5):
    """DB 기동 지연·SSL 요구를 모두 커버: 변형 DSN을 교차 시도하며 실제 오류를 로그로 남김"""
    global POOL
    base = DATABASE_URL
    variants = [base]
    if 'sslmode=' not in base:
        variants.append(base + ('&' if '?' in base else '?') + 'sslmode=require')
    import time
    for attempt in range(1, max_attempts + 1):
        dsn = variants[(attempt - 1) % len(variants)]
        try:
            conn = psycopg.connect(dsn, connect_timeout=8)
            conn.close()
            POOL = ConnectionPool(dsn, min_size=1, max_size=10,
                                  kwargs={'row_factory': dict_row}, open=True)
            print(f'[db] PostgreSQL 연결 성공 (attempt {attempt}, sslmode={"require" if "sslmode=require" in dsn else "default"})', flush=True)
            return
        except Exception as e:
            print(f'[db] 연결 시도 {attempt}/{max_attempts} 실패: {type(e).__name__}: {e}', flush=True)
            time.sleep(delay)
    raise RuntimeError('PostgreSQL 연결 실패 — 위 로그의 오류를 확인하세요')

def _adapt(sql: str) -> str:
    return sql.replace('?', '%s') if IS_PG else sql

class Cx:
    """커밋/롤백을 컨텍스트로 관리하는 얇은 래퍼"""
    def __init__(self, conn): self.conn = conn
    def exec(self, sql, params=()):
        if IS_PG:
            return self.conn.execute(_adapt(sql), params)
        cur = self.conn.execute(sql, params)
        return cur
    def one(self, sql, params=()):
        r = self.exec(sql, params).fetchone()
        return dict(r) if r is not None else None
    def all(self, sql, params=()):
        return [dict(r) for r in self.exec(sql, params).fetchall()]

@contextmanager
def db():
    if IS_PG:
        if POOL is None:
            raise HTTPException(503, '데이터베이스 연결 준비중입니다')
        with POOL.connection() as conn:   # 블록 정상 종료 시 commit, 예외 시 rollback
            yield Cx(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')   # 단일 파일 쓰기 직렬화
        try:
            yield Cx(conn); conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

LOCK = ' FOR UPDATE' if IS_PG else ''    # PG 행 잠금 / SQLite는 BEGIN IMMEDIATE로 대체

def seed():
    ddl = '''
    CREATE TABLE IF NOT EXISTS products(
      id TEXT PRIMARY KEY, name TEXT, price INTEGER,
      soldout INTEGER DEFAULT 0, kind TEXT, stock INTEGER);
    CREATE TABLE IF NOT EXISTS orders(
      order_id TEXT PRIMARY KEY, created TEXT, status TEXT, amount INTEGER,
      buyer TEXT, items TEXT, ship_method TEXT,
      payment_key TEXT, pay_method TEXT, receipt_url TEXT,
      customer_id TEXT, member_id TEXT, contact_phone_norm TEXT);
    CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created);
    '''
    with db() as c:
        for stmt in ddl.strip().split(';'):
            if stmt.strip(): c.exec(stmt)

    # ── 컬럼 마이그레이션은 반드시 _migrate_columns() 로만 수행한다 ──
    #   (1) 시드 블록은 상품이 이미 있으면 `return` 으로 빠져나가므로 같은
    #       트랜잭션에 섞으면 커밋 전에 롤백된다.
    #   (2) PostgreSQL 은 트랜잭션 안에서 문장 하나가 실패하면 트랜잭션 전체가
    #       '중단(aborted)' 상태가 되어 이후 문장이 전부 무시된다 — 한 블록에서
    #       전 컬럼을 돌리던 종전 방식은 이미 존재하는 첫 컬럼(customer_id)에서
    #       중단돼 vbank_* 가 영영 생성되지 않았다(2026-07-27 운영 재현 확정).
    _migrate_columns()

    # own_removed 조회는 테이블이 없을 수 있어 반드시 별도 트랜잭션으로 격리한다.
    # (본 블록 안에서 실패하면 PG 트랜잭션이 중단돼 상품 INSERT까지 전부 무효가 된다)
    removed = set()
    try:
        with db() as c:
            removed = {x['id'] for x in c.all('SELECT id FROM own_removed')}
    except Exception:
        pass

    with db() as c:
        n = c.one('SELECT COUNT(*) AS n FROM products')['n']
        if n: return
        own = json.load(open(os.path.join(BASE, 'data', 'own_products.json')))
        rows = []
        for page, opts in own['opts'].items():
            bn = own['names'].get(page, page)
            for k, v in opts.items():
                rows.append((f'{page}::{k}', f"{bn} — {v['name']}", int(v['price']), 0, 'own', None))
        for it in json.load(open(os.path.join(BASE, 'data', 'k2g_catalog.json'))):
            price = int(it['p'].replace(',', '')) if it['p'] else 0
            rows.append((f"k2g::{it['u']}", it['n'], price, int(it['s']), 'k2g', None))
        ins = ('INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING'
               if IS_PG else 'INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)')
        for r in rows:
            if r[0] in removed: continue
            c.exec(ins, r)
        print(f'[seed] products: {len(rows)} ({"PostgreSQL" if IS_PG else "SQLite"})')

from contextlib import asynccontextmanager
import threading

_VB_COLS_OK = None      # 가상계좌 컬럼 존재 여부 (최초 1회 판정 후 캐시)

def _has_vbank_cols() -> bool:
    """마이그레이션 이전 DB 에는 vbank_* 컬럼이 없다.
       매 조회마다 예외를 내면 PG 트랜잭션이 오염되므로 한 번만 판정해 캐시한다."""
    global _VB_COLS_OK
    if _VB_COLS_OK is None:
        try:
            with db() as c:
                c.exec('SELECT vbank_num FROM orders WHERE 1=0')
            _VB_COLS_OK = True
        except Exception:
            _VB_COLS_OK = False
    return _VB_COLS_OK

_SHIP_COLS_OK = None    # fulfill/tracking/courier 컬럼 존재 여부 (최초 1회 판정 후 캐시)

def _has_ship_cols() -> bool:
    """orders.fulfill/tracking/courier 컬럼 존재 여부 — _has_vbank_cols 와 동일 패턴."""
    global _SHIP_COLS_OK
    if _SHIP_COLS_OK is None:
        try:
            with db() as c:
                c.exec('SELECT fulfill, tracking, courier FROM orders WHERE 1=0')
            _SHIP_COLS_OK = True
        except Exception:
            _SHIP_COLS_OK = False
    return _SHIP_COLS_OK

def _has_ga_cols() -> bool:
    """orders.ga_* 컬럼 존재 여부 — 매 조회 예외는 PG 트랜잭션을 오염시키므로 1회 판정 후 캐시."""
    global _GA_COLS_OK
    if _GA_COLS_OK is None:
        try:
            with db() as c:
                c.exec('SELECT ga_cid, ga_sid, ga_mp_sent FROM orders WHERE 1=0')
            _GA_COLS_OK = True
        except Exception:
            _GA_COLS_OK = False
            print('[ga4] orders.ga_* 컬럼 미가용 — cid 저장 없이 동작(서버 purchase 백업 비활성)', flush=True)
    return _GA_COLS_OK

# orders 확장 컬럼 단일 목록 — seed()·_migrate_columns()·자가치유가 전부 이것만 본다.
_ORDER_EXTRA_COLS = ('customer_id', 'member_id', 'contact_phone_norm',
                     'vbank_num', 'vbank_name', 'vbank_holder', 'vbank_due', 'paid_at',
                     'ga_cid', 'ga_sid', 'ga_mp_sent', 'pay_log',
                     'client_ip', 'country', 'geo')      # 구매 국가(접속 국가) 2026-09-01 — admin_v2 가 기록

def _add_order_col(col: str) -> bool:
    """컬럼 1개 = 트랜잭션 1개. 반드시 이 단위를 유지한다.

    PostgreSQL 은 트랜잭션 내부에서 문장 하나가 실패하면 트랜잭션이 '중단
    (aborted)' 상태가 되어, except 로 삼켜도 이후 모든 문장이
    InFailedSqlTransaction 으로 무시된다. 종전처럼 with db() 한 블록에서
    전 컬럼을 ALTER 하면 이미 존재하는 첫 컬럼(customer_id)에서 중단돼
    나머지(vbank_* 등)가 전부 누락된다 — '[db] 컬럼 마이그레이션 완료' 로그가
    찍히면서도 실제로는 아무것도 안 된다(2026-07-27 운영 PG 재현으로 확정).
    SQLite 는 이런 중단 개념이 없어 로컬 테스트로는 잡히지 않는다."""
    try:
        with db() as c:
            c.exec('ALTER TABLE orders ADD COLUMN %s TEXT' % col)
        return True                                   # 신규 추가됨
    except Exception:
        return False                                  # 이미 존재(정상) 또는 DB 미준비

def _migrate_columns():
    """컬럼 추가만 따로 수행한다. seed() 가 시드 데이터 문제로 실패해도
       마이그레이션은 반드시 완료되어야 한다(누락 시 주문 조회·승인이 500)."""
    global _VB_COLS_OK, _GA_COLS_OK
    added = [col for col in _ORDER_EXTRA_COLS if _add_order_col(col)]
    try:
        with db() as c:
            c.exec('CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, created)')
    except Exception:
        pass
    _VB_COLS_OK = None                    # 판정 캐시 무효화 → 방금 생긴 컬럼을
    _GA_COLS_OK = None                    # 재기동 없이 즉시 사용한다.
    print('[db] 컬럼 마이그레이션 완료 (신규 %s)' % (', '.join(added) if added else '없음'),
          flush=True)

def _init_db():
    global DB_READY
    try:
        if IS_PG:
            _connect_pg_with_retry()
        _migrate_columns()          # seed 보다 먼저, 독립적으로 (캐시 재판정 포함)
        seed()
        DB_READY = True
        print('[db] 준비 완료', flush=True)
    except Exception as e:
        print(f'[db] 초기화 실패: {e}', flush=True)

@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_init_db, daemon=True).start()  # 포트 바인딩을 막지 않음
    yield

app = FastAPI(title='MAPDAL SEOUL API v2', lifespan=lifespan)

@app.middleware('http')
async def account_security_headers(req: Request, call_next):
    # 브라우저 교차 사이트 상태변경 요청을 차단한다. PG/OAuth 공급자 콜백은 예외다.
    # 이니시스 결제 콜백은 외부(PG 서버·결제창)에서 cross-site 로 들어오므로 CSRF 검사 제외.
    _PG_CALLBACKS = ('/inicis/return', '/inicis/mobile-return', '/inicis/mobile-noti',
                     '/inicis/vbank-noti', '/auth/apple/callback')
    if req.method in ('POST','PUT','PATCH','DELETE') and req.url.path not in _PG_CALLBACKS:
        origin=(req.headers.get('origin') or '').rstrip('/')
        fetch_site=(req.headers.get('sec-fetch-site') or '').lower()
        if (origin and origin != SITE_ORIGIN) or fetch_site=='cross-site':
            return JSONResponse({'detail':'허용되지 않은 요청 출처입니다'},status_code=403)
    resp=await call_next(req)
    resp.headers.setdefault('X-Content-Type-Options','nosniff')
    resp.headers.setdefault('X-Frame-Options','DENY')
    resp.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy','camera=(), microphone=(), geolocation=()')
    if req.url.path.startswith('/api/member') or req.url.path.startswith('/admin') or req.url.path=='/account':
        resp.headers.setdefault('Cache-Control','no-store')
    return resp

# ── 클린 URL: .html 숨김 · 홈은 /home ─────────────────────────────────────
_STATIC_DIR = os.path.join(BASE, 'static')
_HOME_FILE = 'mapdal_home_mockup_v1.html'
_DYNAMIC_CLEAN_ROUTES = {'/account'}

@app.middleware('http')
async def clean_urls(request, call_next):
    if request.method in ('GET', 'HEAD'):
        p = request.url.path
        if p == '/home':
            # 클린 주소 → 실제 홈 파일을 내부 매핑 (주소창은 /home 유지)
            request.scope['path'] = '/' + _HOME_FILE
        elif p in _DYNAMIC_CLEAN_ROUTES:
            # 동명 HTML이 있어도 회원·인증 전용 라우트를 우선한다.
            # /account.html은 과거 편집본 호환용 리디렉션 파일일 뿐이다.
            pass
        elif p.endswith('.html'):
            # 구식 .html 주소 → 클린 주소로 영구 이동 (주소창 정리)
            name = p.lstrip('/')
            tgt = '/home' if name in (_HOME_FILE, 'index.html') else p[:-5]
            q = ('?' + request.url.query) if request.url.query else ''
            return RedirectResponse(tgt + q, status_code=301)
        elif p != '/' and '.' not in p.rsplit('/', 1)[-1]:
            # 확장자 없는 경로 → 동명 html 파일이 있으면 내부 매핑 (API·관리자 경로는 파일이 없어 통과)
            cand = p.strip('/') + '.html'
            full = os.path.normpath(os.path.join(_STATIC_DIR, cand))
            if full.startswith(_STATIC_DIR + os.sep) and os.path.isfile(full):
                request.scope['path'] = '/' + cand
    return await call_next(request)

# ── API ─────────────────────────────────────────────────────────
# KG이니시스 INIStdPay(표준결제창) 연동
#   흐름: [1] /api/orders 주문생성 + STEP1 서명파라미터 반환
#         [2] checkout.html이 INIStdPay.pay() 로 결제창 호출
#         [3] KG → /inicis/return (STEP2 인증결과 POST) 수신
#         [4] 서버가 authUrl 로 STEP3 승인요청 → 0000 이면 PAID → /order-complete 리다이렉트
#   서명(SHA-256, NVP·알파벳순·&연결·공백/후행& 제외) — KG 공식 테스트벡터로 검증됨.
def _ini_hash(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def _req_origin(req: Request) -> str:
    """결제 return/close URL용 origin — 프록시 뒤 실도메인 우선순위로 결정.
       1) SITE_ORIGIN 환경변수(권장·고정)  2) X-Forwarded-Proto/Host 헤더  3) req.base_url."""
    if SITE_ORIGIN:
        return SITE_ORIGIN
    h = req.headers
    host = h.get('x-forwarded-host') or h.get('host')
    proto = (h.get('x-forwarded-proto') or '').split(',')[0].strip() or 'https'
    if host:
        return f'{proto}://{host}'.rstrip('/')
    return str(req.base_url).rstrip('/')

def _ini_signature(params: dict) -> str:
    """대상 필드를 알파벳순 정렬 → key=value & 연결(후행& 없음) → SHA-256 hex."""
    plain = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    return _ini_hash(plain)

def _ini_idc_host_ok(idc_name: str, url: str) -> bool:
    """STEP2에서 받은 idc_name(fc/ks/stg)과 authUrl 호스트 접두가 일치하는지 검증(보안필수)."""
    try:
        host = urllib.parse.urlparse(url).hostname or ''
    except Exception:
        return False
    return bool(idc_name) and host.startswith(idc_name) and host.endswith('inicis.com')

def _is_mobile_ua(req: Request) -> bool:
    """이니시스는 PC/모바일 모듈이 분리되어 있고, 모바일 기기에서 PC모듈(INIStdPay.js)을
       호출하면 'Dev. Error — PC로 결제 진행을 부탁드립니다' 얼럿이 뜬다.
       기기 구분 기준은 운영체제(윈도우 / 안드로이드·iOS)이며 태블릿도 모바일로 처리한다."""
    ua = (req.headers.get('user-agent') or '').lower()
    if not ua:
        return False
    # iPadOS 13+ 는 데스크톱 사파리로 위장하므로 터치 힌트를 함께 본다.
    if 'ipad' in ua or 'iphone' in ua or 'ipod' in ua:
        return True
    if 'android' in ua:
        return True
    for k in ('mobile', 'windows phone', 'iemobile', 'opera mini', 'silk'):
        if k in ua:
            return True
    return False

def _ini_mobile_req_url_ok(url: str) -> bool:
    """STEP2에서 받은 P_REQ_URL 이 이니시스 도메인인지 검증(보안필수).
       임의 URL로 승인요청이 나가면 인증정보가 외부로 유출된다."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    return p.scheme == 'https' and (p.hostname or '').endswith('inicis.com')

# ── 가상계좌: 은행코드(금융결제원 표준) → 은행명 ─────────────────────────
#   이니시스 응답이 은행명 필드 없이 코드(VACT_BankCode / P_VACT_BANK_CODE)만
#   내려주는 연동 변형이 있어, 코드→이름 변환을 서버가 보장한다.
#   이름을 못 구해도 계좌번호·예금주는 반드시 안내한다(은행명은 보조 정보).
_VBANK_BANK_NAMES = {
    '02': '산업은행', '03': 'IBK기업은행', '04': 'KB국민은행', '05': '하나은행',
    '06': '수협은행', '07': '수협은행', '11': 'NH농협은행', '12': '지역농·축협',
    '20': '우리은행', '23': 'SC제일은행', '26': '신한은행', '27': '씨티은행',
    '31': 'iM뱅크(대구)', '32': '부산은행', '34': '광주은행', '35': '제주은행',
    '37': '전북은행', '39': '경남은행', '45': '새마을금고', '48': '신협',
    '50': '저축은행', '64': '산림조합', '71': '우체국', '81': '하나은행',
    '88': '신한은행', '89': '케이뱅크', '90': '카카오뱅크', '92': '토스뱅크',
}

def _vbank_bank_name(code, fallback: str = '') -> str:
    c = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not c:
        return fallback
    return _VBANK_BANK_NAMES.get(c.zfill(2)[-2:], fallback) or fallback

def _vbank_finalize(oid: str, tid: str, vnum: str, vbank: str, vname: str, vdate: str):
    """가상계좌 채번 확정 — 입금대기 전환 + 계좌 저장 + 입금안내 발송 (PC·모바일 공용).

    계좌정보가 고객에게 닿는 3개 채널(① 주문완료 화면 ② 마이페이지 주문내역
    ③ SMS/알림톡)이 전부 여기서 저장한 vbank_* 를 읽는다. 따라서 채번이
    성공했는데 이 함수가 호출되지 않거나 필드가 비면 '무통장입금을 선택했는데
    계좌를 못 받았다'는 CS 가 재발한다. 채번 = 승인 아님(WAITING_DEPOSIT),
    실제 입금은 /inicis/vbank-noti(PC)·/inicis/mobile-noti(모바일)에서 PAID 전환."""
    _pay_log(oid, 'VBANK_ISSUED',
             (('%s %s' % (vbank, vnum)).strip() + ' · 입금대기') if vnum
             else '채번 응답에 계좌번호 없음 — 이니시스 상점관리자에서 확인 필요')
    def _upd_full():
        with db() as c:
            c.exec("UPDATE orders SET status='WAITING_DEPOSIT', payment_key=?, pay_method=?, "
                   "vbank_num=?, vbank_name=?, vbank_holder=?, vbank_due=? "
                   "WHERE order_id=? AND status<>'PAID'",
                   (tid, 'VBank', vnum, vbank, vname, vdate, oid))
    try:
        _upd_full()
    except Exception:
        # 컬럼 미생성 DB — 즉석 마이그레이션 후 1회 재시도(자가치유).
        # 계좌가 저장되지 않으면 3개 채널(완료화면·마이페이지·SMS) 전부가
        # 빈 값을 안내하게 되므로, 여기서 포기하기 전에 반드시 복구를 시도한다.
        try:
            _migrate_columns()
            _upd_full()
        except Exception:
            # 그래도 실패하면 계좌 상세는 못 남겨도 '입금대기' 는 반드시 기록한다.
            with db() as c:
                c.exec("UPDATE orders SET status='WAITING_DEPOSIT', payment_key=?, pay_method=? "
                       "WHERE order_id=? AND status<>'PAID'", (tid, 'VBank', oid))
    try:
        import admin_v2 as _av; _av.order_notify_async(oid, 'deposit_wait')
    except Exception:
        pass
    return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

def _ini_mobile_params(order_id: str, amount: int, order_name: str,
                       buyer: dict, origin: str, pay_sel: str = '') -> dict:
    """모바일 결제요청 파라미터 (https://mobile.inicis.com/smart/payment/ 로 POST).
       PC와 달리 P_ 접두 필드를 쓰고 서명 대신 P_CHKFAKE(Hash) 로 위변조를 검증한다.
       pay_sel: 체크아웃에서 사전선택한 수단(Card/DirectBank/VBank/HPP). 모바일 규격은
       요청당 1개 수단(P_INI_PAYMENT)만 받으므로, 사전선택이 곧 모바일에서
       계좌이체·가상계좌를 여는 유일한 경로다(종전엔 CARD 고정이라 카드만 가능했다)."""
    # PC(gopaymethod) 코드 → 모바일(P_INI_PAYMENT) 코드. 미선택(구버전 캐시)은 CARD 유지.
    _MOBILE_PM = {'Card': 'CARD', 'DirectBank': 'BANK', 'VBank': 'VBANK', 'HPP': 'MOBILE'}
    p = {
        'P_INI_PAYMENT': _MOBILE_PM.get(pay_sel, 'CARD'),   # 지불수단: CARD/BANK/VBANK/MOBILE
        'P_MID'   : INICIS_MID,
        'P_OID'   : order_id,
        'P_AMT'   : str(amount),
        'P_GOODS' : order_name,
        'P_UNAME' : (buyer.get('name') or '맵달 고객')[:30],
        'P_MOBILE': (buyer.get('phone') or ''),
        'P_EMAIL' : (buyer.get('email') or ''),
        'P_NEXT_URL': origin + '/inicis/mobile-return',   # 인증/승인 결과 수신(https 필수)
        'P_NOTI_URL': origin + '/inicis/mobile-noti',     # 백단 결과 통보(1trs·가상계좌)
        'P_CHARSET' : 'utf8',
        'P_NOTI'    : order_id,             # 그대로 되돌아오는 상점 전달값
        'P_HPP_METHOD': '2',                # 휴대폰결제 상품유형 — 컨텐츠=1, 실물=2 (맵달=실물)
    }
    if pay_sel == 'VBank':
        # 가상계좌 입금기한 — PC(acceptmethod vbank(YYYYMMDDhhmm))와 같은 _vbank_due() 값.
        #   모바일 규격은 날짜(P_VBANK_DT, YYYYMMDD)와 시각(P_VBANK_TM, hhmm)이 분리돼 있다.
        #   미지정 시 +10일로 자동설정되므로 두 필드를 모두 넣어야 당일 마감이 실제로 걸린다.
        #   입금통보는 P_NOTI_URL(/inicis/mobile-noti).
        _due = _vbank_due()
        p['P_VBANK_DT'] = _due[:8]
        p['P_VBANK_TM'] = _due[8:12]
    # 금액 위변조 방지 해시 (Hash Key 미설정 시 생략 — 결제는 정상 진행)
    if INICIS_MOBILE_HASHKEY:
        p['P_CHKFAKE'] = _ini_hash(
            f"{INICIS_MID}{order_id}{amount}{INICIS_MOBILE_HASHKEY}")
    return p

@app.get('/api/config')
def config():
    return {'pg': 'inicis', 'mid': INICIS_MID,
            'freeShipOver': FREE_SHIP_OVER, 'shipFee': SHIP_FEE,
            'dropPrefix': DROP_PREFIX, 'dropShipFee': DROP_SHIP_FEE,
            'pointRateBp': POINT_RATE_BP,
            # 체크아웃 결제수단 사전선택 라디오 노출 목록 (2026-08-07).
            #   INICIS_GOPAYMETHOD 환경변수를 그대로 반영 — 수단을 내리면(예: VBank 제거)
            #   재배포 없이 체크아웃 라디오에서도 함께 사라진다(mpPayVbJs가 필터링).
            'payMethods': [m.strip() for m in INICIS_GOPAYMETHOD.split(':') if m.strip()]}

def _product_id_candidates(pid: str):
    """장바구니가 보낸 상품 ID를 DB 저장 형태로 정규화한 후보 목록을 만든다.
    클린 URL(.html 숨김) 정책 때문에 상품 페이지는 슬러그에서 .html이 빠진
    'product-x::opt' 형태로 담지만, 시드는 원본 파일명 기준 'product-x.html::opt'로
    저장한다. 두 형태를 모두 시도해 어느 쪽으로 담겼든 정상 조회되게 한다.
    (k2g::uid 등 이미 올바른 ID는 원본이 먼저 매칭되고, 존재하지 않는 .html 변형은
     조회에 실패해도 무해하므로 오매칭 위험이 없다.)"""
    cands = [pid]
    if '::' in pid:
        left, right = pid.split('::', 1)
        if left and not left.endswith('.html'):
            alt = left + '.html::' + right
            if alt not in cands:
                cands.append(alt)
    return cands

# ── 주문자 이메일 — 회원구매·비회원구매 모두 필수 (2026-08-03 요청) ──────────
#   수신처 용도: 주문확인 / 입금안내(가상계좌) / 배송안내 메일 + 이니시스 영수증 메일
#   (buyeremail·P_EMAIL 파라미터). 클라이언트(mpCkValidate)와 동일 규칙을 서버에서
#   재검증한다 — 캐시된 구버전 페이지·직접 API 호출로 우회되는 것을 막는다.
_EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$')

def _require_buyer_email(buyer: dict, member: dict = None) -> str:
    """이메일 필수 검증 + 소문자 정규화.

    로그인 회원이 구버전(캐시된) 체크아웃으로 값을 못 보낸 경우에만 계정 이메일로
    보완한다 — 이미 검증된 값이 계정에 있으므로 결제 실패로 매출을 잃지 않으면서
    '모든 주문에 이메일이 남는다'는 요구는 그대로 충족한다. 비회원은 예외 없음.
    """
    e = str(buyer.get('email') or '').strip().lower()
    if not e and member:
        e = str(member.get('email') or '').strip().lower()
    if not e:
        raise HTTPException(400, '이메일 주소를 입력해 주세요 — 주문확인·입금안내·배송안내를 보내드립니다')
    if len(e) > 60 or not _EMAIL_RE.match(e):
        raise HTTPException(400, '올바른 이메일 주소를 입력해 주세요 (예: name@example.com)')
    return e

@app.post('/api/orders')
async def create_order(req: Request):
    body = await req.json()
    items, buyer = body.get('items') or [], body.get('buyer') or {}
    ship = body.get('shipMethod', 'standard')
    if not items: raise HTTPException(400, '장바구니가 비어 있습니다')
    if body.get('intl'): raise HTTPException(400, '현재 국내 배송만 지원합니다')
    for f in ('name', 'phone'):
        if not buyer.get(f): raise HTTPException(400, '받는 분 이름/연락처를 입력해 주세요')
    if ship != 'pickup' and not buyer.get('addr1'):
        raise HTTPException(400, '배송 주소를 입력해 주세요')

    # 로그인 주문은 생성 시점부터 고객/계정에 귀속한다. 전화번호 문자열 역검색은 사용하지 않는다.
    member_id = customer_id = ''
    member_row = None
    try:
        import admin_v2
        admin_v2.ensure_ready()
        member = admin_v2.member_of(req)
        if member and (member.get('status') or 'ACTIVE') == 'ACTIVE':
            member_id = member.get('id') or ''
            customer_id = member.get('customer_id') or ''
            member_row = member
    except Exception:
        pass
    # 이메일 필수 — 회원/비회원 동일. 정규화한 값을 buyer 에 되돌려 저장한다.
    buyer['email'] = _require_buyer_email(buyer, member_row)

    # ── 결제수단 사전선택 (2026-08-07) ─────────────────────────────────────
    #   체크아웃에서 수단(카드·계좌이체·가상계좌·휴대폰)을 먼저 고르면 결제창이
    #   해당 수단으로 '바로' 열린다(PC gopaymethod 단일 지정 · 모바일 P_INI_PAYMENT 매핑).
    #   payMethod 미전송(구버전 캐시 체크아웃) 시에는 종전대로 전체 수단 결제창을
    #   열어 하위호환한다 — 이 경우 환불계좌 요구도 종전과 동일하게 없다.
    _PM_CANON = {'card': 'Card', 'directbank': 'DirectBank', 'vbank': 'VBank', 'hpp': 'HPP'}
    _pm_raw_sel = str(body.get('payMethod') or '').strip()
    pay_sel = _PM_CANON.get(_pm_raw_sel.lower(), '')
    if _pm_raw_sel and not pay_sel:
        raise HTTPException(400, '지원하지 않는 결제수단입니다 — 새로고침 후 다시 시도해 주세요')
    _pm_allowed = [m.strip() for m in INICIS_GOPAYMETHOD.split(':') if m.strip()]
    if pay_sel and pay_sel not in _pm_allowed:
        # INICIS_GOPAYMETHOD 운영 스위치로 내린 수단 — 캐시된 구 화면의 선택을 차단.
        raise HTTPException(400, '현재 이용할 수 없는 결제수단입니다 — 다른 결제수단을 선택해 주세요')

    # ── 가상계좌 환불계좌 필수 (2026-08-07) ────────────────────────────────
    #   가상계좌는 입금 후 취소 시 자동환불이 지원되지 않아(관리자 수동 송금)
    #   본인 명의 환불계좌가 반드시 필요하다. 주문 생성 시점에 필수로 받아
    #   buyer JSON 에 저장한다 — 컬럼 추가 없음(PG ALTER 트랜잭션 중단 리스크 회피),
    #   주문 INSERT 와 원자적으로 함께 저장되며 관리자 주문상세 [환불 계좌]로 표시된다.
    #   클라이언트(mpRefundValidate)와 동일 규칙을 서버가 재검증한다 — 캐시된
    #   구버전 페이지·직접 API 호출로 우회되는 것을 막는다.
    if pay_sel == 'VBank':
        rf = body.get('refund') or {}
        if not isinstance(rf, dict):
            rf = {}
        r_holder = str(rf.get('holder') or '').strip()[:30]
        r_code = ''.join(ch for ch in str(rf.get('bank') or '') if ch.isdigit())
        r_bank = _vbank_bank_name(r_code)          # 금결원 표준코드 → 은행명 (미등록 코드=공백)
        r_acct = ''.join(ch for ch in str(rf.get('acct') or '') if ch.isdigit())
        if not r_bank:
            raise HTTPException(400, '환불받으실 은행을 선택해 주세요 — 가상계좌 주문은 환불계좌 입력이 필수입니다')
        if len(r_holder) < 2:
            raise HTTPException(400, '환불계좌 예금주를 입력해 주세요 (주문자 본인 명의)')
        if not (6 <= len(r_acct) <= 16):
            raise HTTPException(400, "환불계좌번호를 확인해 주세요 ('-' 없이 숫자만 입력)")
        if not rf.get('agree'):
            raise HTTPException(400, '환불계좌 정보 수집·이용에 동의해 주세요 — 환불 처리에 필요한 필수 동의입니다')
        buyer['refund'] = {'holder': r_holder, 'bank': r_bank,
                           'bank_code': r_code.zfill(2)[-2:], 'acct': r_acct}

    phone_norm = ''.join(ch for ch in str(buyer.get('phone') or '') if ch.isdigit())
    if phone_norm.startswith('82'):
        phone_norm = '0' + phone_norm[2:]
    if not customer_id:
        try:
            import admin_v2
            customer_id = admin_v2.guest_customer_ensure(buyer.get('name') or '', phone_norm)
        except Exception:
            customer_id = ''

    # GA4 클라이언트/세션 식별자 — 결제 승인 시 서버사이드 purchase 백업 전송에 사용.
    # 체크아웃의 /api/orders 호출은 동일 오리진(XHR)이라 _ga 쿠키가 함께 도착한다.
    ga_cid, ga_sid = _ga_cookie_ids(req)
    _ga_ok = _has_ga_cols()              # 주문 트랜잭션 밖에서 판정 (내부 예외 → PG abort 방지)

    changed_stock_ids = []
    with db() as c:                      # ← 단일 트랜잭션: 검증·재고차감·주문생성 원자 처리
        sub, resolved = 0, []
        for it in items:
            pid = str(it.get('id', '')); q = max(1, min(99, int(it.get('q', 1))))
            row = None
            for cand in _product_id_candidates(pid):      # 클린 URL(.html 숨김) 대응: 두 형태 모두 조회
                row = c.one(f'SELECT * FROM products WHERE id=?{LOCK}', (cand,))
                if row: break
            if not row: raise HTTPException(400, f'알 수 없는 상품: {pid}')
            db_id = row['id']                              # 이후 재고차감·주문라인은 매칭된 실제 DB ID 사용
            if row['soldout']: raise HTTPException(400, f'품절: {row["name"][:30]}')
            if row['price'] <= 0: raise HTTPException(400, f'가격 확인 필요: {row["name"][:30]}')
            if row['stock'] is not None:                     # 재고 관리 대상 상품
                if row['stock'] < q:
                    raise HTTPException(409, f'재고 부족: {row["name"][:30]} (남은 수량 {row["stock"]})')
                c.exec('UPDATE products SET stock=stock-? WHERE id=?', (q, db_id))
                changed_stock_ids.append(db_id)
                if row['stock'] - q == 0:
                    c.exec('UPDATE products SET soldout=1 WHERE id=?', (db_id,))
            sub += row['price'] * q
            resolved.append({'id': db_id, 'n': row['name'], 'p': row['price'], 'q': q})
        # ── 배송비 ──
        #   드롭(mpd::) 상품 포함 주문: 금액 무관 3,000원 정액(무료배송 기준 미적용).
        #   일반 주문: 30,000원 이상 무료, 미만 3,000원. 픽업은 항상 무료.
        has_drop = any(str(r['id']).startswith(DROP_PREFIX) for r in resolved)
        if ship == 'pickup':
            ship_fee = 0
        elif has_drop:
            ship_fee = DROP_SHIP_FEE
        else:
            ship_fee = 0 if sub >= FREE_SHIP_OVER else SHIP_FEE
        amount = sub + ship_fee
        order_id = f'MD-{kst_naive():%Y%m%d}-{secrets.token_hex(3).upper()}'
        # ga_* 컬럼 존재 여부는 지연 프로브(_has_ga_cols, 1회 판정 캐시)로 확정한다.
        #   트랜잭션 내부 try/except 폴백은 PG 에서 트랜잭션 abort 를 유발하므로 금지.
        if _ga_ok:
            c.exec('INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method,customer_id,member_id,contact_phone_norm,ga_cid,ga_sid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (order_id, kst_iso(), 'PENDING',
                    amount, json.dumps(buyer, ensure_ascii=False),
                    json.dumps(resolved, ensure_ascii=False), ship, customer_id or None, member_id or None,
                    phone_norm or None, ga_cid or None, ga_sid or None))
        else:
            c.exec('INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method,customer_id,member_id,contact_phone_norm) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (order_id, kst_iso(), 'PENDING',
                    amount, json.dumps(buyer, ensure_ascii=False),
                    json.dumps(resolved, ensure_ascii=False), ship, customer_id or None, member_id or None,
                    phone_norm or None))
        if customer_id:
            try:
                c.exec('INSERT INTO account_order_links(order_id,customer_id,member_id,link_source,linked_at,verified_at) VALUES(?,?,?,?,?,?)',
                       (order_id, customer_id, member_id, 'CHECKOUT_SESSION' if member_id else 'GUEST_CHECKOUT',
                        kst_iso(), kst_iso()))
            except Exception:
                pass
    # 새 상품마스터 재고 화면도 결제 직후 동일 수량을 보도록 호환 투영을 동기화한다.
    try:
        import admin_v2
        for pid in changed_stock_ids:
            admin_v2.catalog_inventory_from_legacy(pid)
    except Exception:
        pass
    # ── 구매 국가(접속 국가) 기록 (2026-09-01) ───────────────────────────────
    #   요청 스레드에서는 헤더(CF-IPCountry · X-Forwarded-For)·체크아웃 기기 힌트(body.client)만
    #   뽑고, DB 기록과 외부 GeoIP 조회는 admin_v2 가 백그라운드 스레드로 처리한다 —
    #   결제창(클릭 제스처 내 동기 XHR) 응답 지연 0. 실패해도 주문·결제에 영향 없음.
    try:
        import admin_v2 as _av; _av.order_geo_capture(req, body, order_id)
    except Exception:
        pass
    name0 = resolved[0]['n'][:28]
    order_name = name0 + (f' 외 {len(resolved)-1}건' if len(resolved) > 1 else '')
    _pay_log(order_id, 'CREATED', '%s · %s원 · %s%s' %
             ('모바일' if _is_mobile_ua(req) else 'PC', format(amount, ','), order_name[:60],
              (' · 선택수단 ' + pay_sel) if pay_sel else ''))

    # ── INIStdPay STEP1 서명 파라미터 생성 (oid=order_id, price=amount) ──
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    price = str(amount)
    signature = _ini_signature({'oid': order_id, 'price': price, 'timestamp': ts})
    verification = _ini_signature({'oid': order_id, 'price': price,
                                   'signKey': INICIS_SIGNKEY, 'timestamp': ts})
    mkey = _ini_hash(INICIS_SIGNKEY)
    origin = _req_origin(req)
    inicis = {
        'version': '1.0', 'mid': INICIS_MID, 'oid': order_id, 'price': price,
        'timestamp': ts, 'use_chkfake': 'Y', 'signature': signature,
        'verification': verification, 'mKey': mkey, 'currency': 'WON',
        'goodname': order_name, 'buyername': (buyer.get('name') or '맵달 고객')[:30],
        'buyertel': (buyer.get('phone') or ''), 'buyeremail': (buyer.get('email') or ''),
        # acceptmethod: centerCd(Y)=IDC센터코드 수신(필수), below1000=1천원이하 카드결제 허용,
        #   HPP(2)=휴대폰결제 상품유형 '실물'(맵달=실물상품). 휴대폰결제 노출 시 HPP(1|2) 필수.
        #   va_receipt=가상계좌 채번 시 현금영수증 정보 입력창 노출(실물 커머스 표준),
        #   vbank(YYYYMMDDhhmm)=가상계좌 입금기한 지정 — 채번 당일 23:59(KST) 마감.
        #   기한 없는 계좌가 발급되거나 화면·SMS 안내에 기한이 비는 일을 막는다.
        #   기한 산출은 _vbank_due() 단일 소스(모바일 P_VBANK_DT/TM 과 동일 값).
        # gopaymethod: 결제창에 노출할 수단. 빈 문자열('')이면 이니시스가 '선택 수단 없음'으로
        #   해석해 카드 탭이 아예 렌더링되지 않는다. 반드시 수단 코드를 콜론(:)으로 명시한다.
        #   Card=신용/체크카드(간편결제 포함), DirectBank=계좌이체, VBank=가상계좌, HPP=휴대폰
        #   → INICIS_GOPAYMETHOD 환경변수로 재배포 없이 조정 가능(가상계좌 미계약 대응).
        #   체크아웃 사전선택(pay_sel)이 있으면 해당 수단만 지정해 결제창이 그 수단으로
        #   바로 열린다. 미선택(구버전 캐시)은 종전대로 전체 수단 노출 — 하위호환.
        #   ※ gopaymethod 는 서명(oid·price·timestamp) 대상이 아니므로 단일 지정해도 안전.
        'gopaymethod': pay_sel or INICIS_GOPAYMETHOD,
        'acceptmethod': 'centerCd(Y):below1000:HPP(2):va_receipt:vbank(%s)' % _vbank_due(),
        'returnUrl': origin + '/inicis/return', 'closeUrl': origin + '/inicis/close',
    }
    # ── 모바일 결제 파라미터 (PC와 별개 모듈) ──
    #   이니시스는 PC/모바일 모듈이 분리되어 있어 모바일에서 INIStdPay.js 를 호출하면
    #   'Dev. Error' 로 결제창이 뜨지 않는다. 두 벌을 모두 내려주고 클라이언트가
    #   기기에 맞는 쪽을 선택한다(서버 UA 판별 결과도 함께 전달).
    inicis_mobile = _ini_mobile_params(order_id, amount, order_name, buyer, origin, pay_sel)
    return {'orderId': order_id, 'amount': amount, 'orderName': order_name,
            'sub': sub, 'shipFee': ship_fee, 'inicis': inicis,
            'inicisMobile': inicis_mobile,
            'mobilePayUrl': 'https://mobile.inicis.com/smart/payment/',
            'isMobile': _is_mobile_ua(req)}

@app.post('/inicis/return')
async def inicis_return(req: Request):
    """STEP2 인증결과 수신 → STEP3 승인요청 → 성공 시 PAID 후 완료페이지로 리다이렉트."""
    form = await req.form()
    result_code = form.get('resultCode', '')
    oid = form.get('orderNumber', '') or form.get('oid', '')
    auth_token = form.get('authToken', '')
    auth_url = form.get('authUrl', '')
    idc_name = form.get('idc_name', '')
    net_cancel_url = form.get('netCancelUrl', '')

    def _fail(msg):
        m = urllib.parse.quote(msg[:80])
        return RedirectResponse(f'/checkout?fail=1&msg={m}', status_code=303)

    if result_code != '0000':
        _pay_log(oid, 'AUTH_FAIL', '[%s] %s' % (result_code, form.get('resultMsg', '인증 실패')))
        return _fail(form.get('resultMsg', '인증 실패'))
    if not (oid and auth_token and auth_url):
        _pay_log(oid, 'AUTH_BAD', '인증 응답 파라미터 누락')
        return _fail('인증 응답 파라미터 누락')
    # 보안: authUrl 이 이니시스 도메인 + idc_name 일치 확인
    if not _ini_idc_host_ok(idc_name, auth_url):
        _pay_log(oid, 'AUTH_BAD', '승인 URL 검증 실패')
        return _fail('승인 URL 검증 실패')

    with db() as c:
        order = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
    if not order:
        return _fail('주문을 찾을 수 없습니다')
    if order['status'] == 'PAID':                    # 멱등: 이미 승인
        return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

    # ── STEP3 승인요청 ──
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sign = _ini_signature({'authToken': auth_token, 'timestamp': ts})
    veri = _ini_signature({'authToken': auth_token, 'signKey': INICIS_SIGNKEY, 'timestamp': ts})
    payload = urllib.parse.urlencode({
        'mid': INICIS_MID, 'authToken': auth_token, 'timestamp': ts,
        'signature': sign, 'verification': veri, 'charset': 'UTF-8', 'format': 'JSON',
    }).encode('utf-8')
    reqx = urllib.request.Request(auth_url, data=payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(reqx, timeout=25) as r:
            res = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        # 승인 통신 실패 → 망취소 시도 후 실패 처리
        _pay_log(oid, 'APPROVE_ERR', '승인 통신 오류 — 망취소 시도')
        _ini_net_cancel(net_cancel_url, auth_token)
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('승인 통신 오류')

    if res.get('resultCode') != '0000':
        _pay_log(oid, 'APPROVE_FAIL', '[%s] %s' % (res.get('resultCode', ''), res.get('resultMsg', '승인 실패')))
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail(res.get('resultMsg', '승인 실패'))

    # 금액 위변조 검증: 승인금액(TotPrice) == 주문금액
    tot = int(str(res.get('TotPrice', '0')).replace(',', '') or 0)
    if tot != int(order['amount']):
        _pay_log(oid, 'AMOUNT_MISMATCH', '승인 %s원 ≠ 주문 %s원 — 망취소' % (format(tot, ','), format(int(order['amount']), ',')))
        _ini_net_cancel(net_cancel_url, auth_token)
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('결제 금액 불일치')

    tid = res.get('tid', ''); method = res.get('payMethod', '')

    # ── 가상계좌: 승인 = '계좌 발급'일 뿐 입금 완료가 아니다 ──
    #   이니시스는 채번 시점에 resultCode 0000 을 준다. 이를 PAID 로 처리하면
    #   ① 입금하지 않은 주문이 결제완료로 잡혀 미입금 발송 사고가 나고
    #   ② 완료 화면이 '결제 승인' 문구를 띄워 고객이 입금 계좌를 못 받는다.
    #   판정을 payMethod 문자열 하나에 의존하지 않는다 — 연동 변형에서 이 필드가
    #   비거나 표기가 다르면 그대로 ①②가 터진다(2026-07-27 무통장입금 CS 재발 방지).
    #   payMethod · TID 접두(StdpayVBNK) · 채번 계좌번호(VACT_Num) 존재 중
    #   하나라도 가상계좌를 가리키면 가상계좌로 처리한다.
    #   실제 입금은 별도 노티(/inicis/vbank-noti)로 통보되며 그때 PAID 로 바꾼다.
    vact_num = str(res.get('VACT_Num') or res.get('vactNum') or '').strip()
    if (str(method).strip().lower() in ('vbank', 'vacct')
            or str(tid).startswith('StdpayVBNK') or vact_num):
        vbank = (res.get('vactBankName') or res.get('VACT_BankName')
                 or _vbank_bank_name(res.get('VACT_BankCode')) or '')
        vname = res.get('VACT_Name') or res.get('vactName') or ''
        vdate = (str(res.get('VACT_Date') or '') + str(res.get('VACT_Time') or '')).strip()
        return _vbank_finalize(oid, tid, vact_num, vbank, vname, vdate)

    # 어떤 카드·어떤 페이로 결제됐는지는 이 승인응답에만 실려 온다(CARD_Code·CARD_Num·
    # applNum·CARD_SrcCode 등). 여기서 받아 두지 않으면 나중에는 이니시스 거래조회를
    # 따로 돌려야 알 수 있다. 돈이 움직이는 경로라 실패는 전부 삼킨다.
    _pdet = ''
    try:
        import admin_v2 as _av; _pdet = _av.pay_detail_save(oid, res, 'STEP3') or ''
    except Exception:
        _pdet = ''
    _pay_log(oid, 'PAID', '%s · TID …%s%s' % (method or 'Card', str(tid)[-6:],
                                              (' · ' + _pdet) if _pdet else ''))
    try:
        with db() as c:                              # 중복 승인 레이스 방지 가드
            c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=?, paid_at=? "
                   "WHERE order_id=? AND status<>'PAID'", (tid, method, '', kst_iso(), oid))
    except Exception:                                 # paid_at 컬럼 미생성 DB 대비
        with db() as c:
            c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=? "
                   "WHERE order_id=? AND status<>'PAID'", (tid, method, '', oid))
    _award_purchase_points(oid)
    _ga4_mp_purchase(oid)                             # 서버사이드 purchase 백업 (무해 실패)
    try:
        import admin_v2 as _av; _av.order_notify_async(oid, 'paid')
    except Exception:
        pass
    return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

@app.api_route('/inicis/mobile-return', methods=['GET', 'POST'])
async def inicis_mobile_return(req: Request):
    """모바일 STEP2 인증결과 수신 → P_REQ_URL 로 승인요청 → 성공 시 PAID.

    이니시스 모바일 모듈은 카드사·인증사 상황에 따라 POST/GET 을 선택적으로 사용하므로
    두 메서드를 모두 수용해야 한다(매뉴얼 명시). 인증결과로 받은 P_REQ_URL 에
    P_MID·P_TID 를 POST 하면 승인이 이루어진다.
    """
    if req.method == 'POST':
        form = dict(await req.form())
    else:
        form = dict(req.query_params)
    # 일부 구간에서 두 방식이 섞여 올 수 있어 쿼리도 함께 병합한다.
    for k, v in req.query_params.items():
        form.setdefault(k, v)

    status  = str(form.get('P_STATUS', ''))
    oid     = form.get('P_OID', '') or form.get('P_NOTI', '')
    req_url = form.get('P_REQ_URL', '')
    tid     = form.get('P_TID', '')
    rmesg   = form.get('P_RMESG1', '') or form.get('P_RMESG2', '')

    def _fail(msg):
        m = urllib.parse.quote(str(msg)[:80])
        return RedirectResponse(f'/checkout?fail=1&msg={m}', status_code=303)

    if status != '00':
        _pay_log(oid, 'AUTH_FAIL', '[%s] %s' % (status, rmesg or '인증 실패'))
        return _fail(rmesg or '인증 실패')

    # ── 가상계좌: 모바일은 '인증결과 = 채번 완료'다 (별도 승인요청 없음) ──
    #   이니시스 모바일 일반결제 규격상 VBANK 는 P_NEXT_URL 수신 시점에 이미
    #   채번이 끝났고 P_REQ_URL 승인요청 대상이 아니다. 종전 코드는 모든 수단에
    #   P_REQ_URL 을 요구해 가상계좌 건이 '인증 응답 파라미터 누락'으로 실패
    #   페이지로 튕겼고, 이니시스 쪽에는 계좌가 발급됐는데 고객은 계좌를 안내받지
    #   못했다(2026-07-27 무통장입금 CS 재발 방지). 인증결과 폼에서 곧바로 확정한다.
    p_type_auth = str(form.get('P_TYPE', '') or '').strip()
    m_vact_num  = str(form.get('P_VACT_NUM', '') or '').strip()
    if oid and (p_type_auth.upper() in ('VBANK', 'VACCT') or m_vact_num):
        with db() as c:
            order = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
        if not order:
            return _fail('주문을 찾을 수 없습니다')
        if order['status'] == 'PAID':                # 멱등: 입금통보가 먼저 도착한 경우
            return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)
        try:                                          # 채번 금액 확인 — 불일치는 기록만.
            m_amt = int(str(form.get('P_AMT', '0')).replace(',', '') or 0)
        except ValueError:                            # 돈이 움직이는 관문은 입금통보이며,
            m_amt = 0                                 # 그쪽(P_AMT==주문금액)에서 최종 검증한다.
        if m_amt and m_amt != int(order['amount']):
            _pay_log(oid, 'AMOUNT_MISMATCH', '채번 %s원 ≠ 주문 %s원 — 입금통보에서 재검증'
                     % (format(m_amt, ','), format(int(order['amount']), ',')))
        vbank = (form.get('P_VACT_BANK_NAME') or form.get('P_FN_NM')
                 or _vbank_bank_name(form.get('P_VACT_BANK_CODE')) or '')
        vname = form.get('P_VACT_NAME') or ''
        vdate = (str(form.get('P_VACT_DATE') or '') + str(form.get('P_VACT_TIME') or '')).strip()
        return _vbank_finalize(oid, tid, m_vact_num, vbank, vname, vdate)

    if not (oid and tid and req_url):
        _pay_log(oid, 'AUTH_BAD', '인증 응답 파라미터 누락')
        return _fail('인증 응답 파라미터 누락')
    if not _ini_mobile_req_url_ok(req_url):
        _pay_log(oid, 'AUTH_BAD', '승인 URL 검증 실패')
        return _fail('승인 URL 검증 실패')

    with db() as c:
        order = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
    if not order:
        return _fail('주문을 찾을 수 없습니다')
    if order['status'] == 'PAID':                    # 멱등: 이미 승인
        return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

    # ── 승인요청 (P_REQ_URL 에 P_MID + P_TID) ──
    payload = urllib.parse.urlencode({
        'P_MID': INICIS_MID, 'P_TID': tid,
    }).encode('utf-8')
    reqx = urllib.request.Request(req_url, data=payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(reqx, timeout=25) as r:
            body = r.read().decode('utf-8', 'replace')
    except Exception:
        _pay_log(oid, 'APPROVE_ERR', '승인 통신 오류')
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('승인 통신 오류')

    # 승인 응답은 key=value&... 형태의 평문(NVP)으로 온다.
    res = dict(urllib.parse.parse_qsl(body.strip(), keep_blank_values=True))
    if str(res.get('P_STATUS', '')) != '00':
        _pay_log(oid, 'APPROVE_FAIL', '[%s] %s' % (res.get('P_STATUS', ''), res.get('P_RMESG1', '') or '승인 실패'))
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail(res.get('P_RMESG1', '') or '승인 실패')

    # 금액 위변조 검증: 승인금액 == 주문금액
    try:
        paid = int(str(res.get('P_AMT', '0')).replace(',', '') or 0)
    except ValueError:
        paid = 0
    if paid != int(order['amount']):
        _pay_log(oid, 'AMOUNT_MISMATCH', '승인 %s원 ≠ 주문 %s원' % (format(paid, ','), format(int(order['amount']), ',')))
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('결제 금액 불일치')

    pay_tid = res.get('P_TID', '') or tid
    method  = res.get('P_TYPE', '')

    # 가상계좌는 채번(계좌발급)일 뿐 입금 완료가 아니다 → 입금대기로 둔다.
    # (승인요청을 요구하는 연동 변형 대비 안전망 — 정상 규격은 위의 인증결과 분기가 처리)
    # 실제 입금은 P_NOTI_URL(/inicis/mobile-noti) 로 통보되며 그때 PAID 로 바꾼다.
    if (str(method).strip().lower() in ('vbank', 'vacct')
            or str(res.get('P_VACT_NUM') or '').strip()):
        vnum = str(res.get('P_VACT_NUM') or res.get('VACT_Num') or '').strip()
        vbank = (res.get('P_VACT_BANK_NAME') or res.get('P_FN_NM')
                 or _vbank_bank_name(res.get('P_VACT_BANK_CODE')) or '')
        vname = res.get('P_VACT_NAME') or ''
        vdate = (str(res.get('P_VACT_DATE') or '') + str(res.get('P_VACT_TIME') or '')).strip()
        return _vbank_finalize(oid, pay_tid, vnum, vbank, vname, vdate)

    _pdet = ''                                        # 카드사·간편결제 상세 적재 (PC 분기와 동일)
    try:
        import admin_v2 as _av; _pdet = _av.pay_detail_save(oid, res, 'STEP3M') or ''
    except Exception:
        _pdet = ''
    _pay_log(oid, 'PAID', '%s · TID …%s%s' % (method or 'Card', str(pay_tid)[-6:],
                                              (' · ' + _pdet) if _pdet else ''))
    try:
        with db() as c:                              # 중복 승인 레이스 방지 가드
            c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=?, paid_at=? "
                   "WHERE order_id=? AND status<>'PAID'", (pay_tid, method, '', kst_iso(), oid))
    except Exception:                                 # paid_at 컬럼 미생성 DB 대비
        with db() as c:
            c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=? "
                   "WHERE order_id=? AND status<>'PAID'", (pay_tid, method, '', oid))
    _award_purchase_points(oid)
    _ga4_mp_purchase(oid)                             # 서버사이드 purchase 백업 (무해 실패)
    try:
        import admin_v2 as _av; _av.order_notify_async(oid, 'paid')
    except Exception:
        pass
    return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

def _noti_client_ip(req) -> str:
    """노티 발신 IP — Cloudflare 프록시 뒤라 CF-Connecting-IP 를 우선한다.
       이니시스 공지 IP: 203.238.37.15 / 183.109.71.153 (기록용 — 차단하지 않는다.
       IP 로 막다가 이니시스 대역 변경 시 입금통보가 전멸하는 쪽이 훨씬 위험하고,
       위조 방어는 주문존재·금액일치·상태가드가 이미 수행한다)."""
    try:
        return (req.headers.get('cf-connecting-ip')
                or req.headers.get('x-forwarded-for', '').split(',')[0].strip()
                or (req.client.host if req.client else '') or '-')
    except Exception:
        return '-'

def _vbank_deposit_apply(oid: str, amt: int, tid: str, via: str, extra: str = ''):
    """가상계좌 입금통보 공통 적용 — PC(no_*)·모바일/PRO(P_STATUS=02) 양쪽에서 호출.

    반환 (code, note):
      'paid'      전환 성공 (포인트·GA·알림까지 완료)
      'dup'       이미 PAID — 재수신 무시 (멱등)
      'cancelled' 취소된 주문의 뒤늦은 입금 — 되살리지 않고 환불 필요 알림
      'mismatch'  금액 불일치 — 전환하지 않고 관리자 경고
      'missing'   주문 없음
    응답(OK/FAIL) 판단은 호출부가 한다."""
    with db() as c:
        order = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
    if not order:
        return 'missing', '주문 없음'
    st = str(order['status'] or '')
    if st == 'CANCELLED':
        # 취소된 주문의 뒤늦은 입금 — PAID 로 되살리지 않는다(재고 이미 복원됨).
        _pay_log(oid, 'DEPOSIT_AFTER_CANCEL',
                 '취소된 주문에 가상계좌 입금 %s원 · %s — 수동 환불 필요' % (format(amt, ','), extra or via))
        try:
            import admin_v2 as _av
            _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '취소후입금', oid,
                      '가상계좌 %s원 입금(%s) — 이니시스 상점관리자에서 환불 처리 필요' % (format(amt, ','), via))
        except Exception:
            pass
        return 'cancelled', '취소후입금'
    if st == 'PAID':
        _pay_log(oid, 'VBANK_NOTI', '입금통보 재수신(%s) — 이미 결제완료 · 무시' % via)
        return 'dup', '이미 PAID'
    if amt != int(order['amount']):
        _pay_log(oid, 'AMOUNT_MISMATCH',
                 '입금통보 %s원 ≠ 주문 %s원 (%s) — 전환 보류, 관리자 확인 필요'
                 % (format(amt, ','), format(int(order['amount']), ','), via))
        try:
            import admin_v2 as _av
            _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '입금금액불일치', oid,
                      '통보 %s원 ≠ 주문 %s원 — 이니시스 입금내역 대조 필요'
                      % (format(amt, ','), format(int(order['amount']), ',')))
        except Exception:
            pass
        return 'mismatch', '금액 불일치'
    # 채번TID(payment_key)는 환불 API 의 기준값 — 입금TID(no_tid)로 덮지 않는다.
    # 비어 있을 때만 통보 TID 로 보충한다.
    with db() as c:
        try:
            c.exec("UPDATE orders SET status='PAID', pay_method='VBank', paid_at=?, "
                   "payment_key=CASE WHEN COALESCE(payment_key,'')='' THEN ? ELSE payment_key END "
                   "WHERE order_id=? AND status<>'PAID'", (kst_iso(), tid or '', oid))
        except Exception:                             # paid_at 컬럼 미생성 DB 대비
            c.exec("UPDATE orders SET status='PAID', pay_method='VBank', "
                   "payment_key=CASE WHEN COALESCE(payment_key,'')='' THEN ? ELSE payment_key END "
                   "WHERE order_id=? AND status<>'PAID'", (tid or '', oid))
    _pay_log(oid, 'PAID', '가상계좌 입금 확인 · %s원%s' % (format(amt, ','), (' · ' + extra) if extra else ''))
    _award_purchase_points(oid)                       # 멱등: 내부에서 중복 방지
    _ga4_mp_purchase(oid)                             # 서버사이드 purchase 백업
    try:
        import admin_v2 as _av; _av.order_notify_async(oid, 'paid')
    except Exception:
        pass
    return 'paid', '입금 확인'

def _noti_unparsed_audit(via: str, req, form: dict):
    """주문번호를 못 찾은 노티 — 원문을 감사로그에 남겨 사람이 추적할 수 있게 한다.
       (2026-07-29 사고 교훈: 무처리 + 'OK' 응답은 입금을 소리 없이 유실시킨다)"""
    try:
        raw = '&'.join('%s=%s' % (k, v) for k, v in list(form.items())[:25])[:300]
        import admin_v2 as _av
        _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '노티파싱실패', via,
                  'IP %s · %s' % (_noti_client_ip(req), raw or '(빈 본문)'))
    except Exception:
        pass

@app.api_route('/inicis/vbank-noti', methods=['GET', 'POST'])
async def inicis_vbank_noti(req: Request):
    """가상계좌 입금통보 수신 (PC NOTIPC · PRO/모바일 겸용).

    이니시스 공식 규격(manual.inicis.com/pay/etc-noti.html) 두 계열을 모두 처리한다:
      ① PC(NOTIPC)  : no_oid(주문번호) · no_tid(입금TID, 채번TID와 다름) ·
                       amt_input(입금액) · type_msg(0200=정상) · nm_input(입금자)
                       — euc-kr POST. 상점관리자 > 거래내역 > 가상계좌 >
                       입금통보방식선택 'URL 수신사용' 에 이 주소를 등록해야 온다.
                       ※ MID 를 바꾸면 새 MID 상점관리자에 다시 등록해야 한다.
      ② PRO/모바일   : P_STATUS '00'=채번통보(입금 아님!) / '02'=입금통보 ·
                       P_OID · P_AMT · P_TID(채번TID)
    처리 성공/멱등 시 평문 "OK" — 그 외 응답이면 이니시스가 24시간 동안
    약 10분 주기(최대 10회) 재전송하므로, 내부 오류·파싱 실패 시에는 일부러
    OK 를 주지 않아 재시도 기회를 남긴다.
    """
    form = {}
    try:
        if req.method == 'POST':
            raw = await req.body()
            txt = None
            for enc in ('utf-8', 'euc-kr', 'cp949'):
                try:
                    txt = raw.decode(enc); break
                except Exception:
                    continue
            form = dict(urllib.parse.parse_qsl(txt or '', keep_blank_values=True))
        else:
            form = dict(req.query_params)
        for k, v in req.query_params.items():
            form.setdefault(k, v)
        if not form:                                  # 파라미터 없는 접근(모니터링 핑 등)
            return PlainTextResponse('OK')

        def _amt(*keys):
            for k in keys:
                v = str(form.get(k) or '').replace(',', '').strip()
                if v:
                    try: return int(v)
                    except ValueError: pass
            return 0

        # ── ① PC(NOTIPC) 계열: no_* 필드 ─────────────────────────────
        if form.get('no_oid') or form.get('no_tid') or form.get('amt_input'):
            oid = str(form.get('no_oid') or '').strip()
            tid = str(form.get('no_tid') or '').strip()
            tmsg = str(form.get('type_msg') or '').strip()
            amt = _amt('amt_input')
            extra = ('입금자 %s · %s %s' % (form.get('nm_input') or '-',
                                            form.get('nm_inputbank') or '', form.get('no_vacct') or '')).strip()
            if not oid:
                _noti_unparsed_audit('PC가상계좌노티', req, form)
                return PlainTextResponse('FAIL')
            if tmsg and tmsg != '0200':               # 정상(0200) 외 구분 — 기록만
                _pay_log(oid, 'VBANK_NOTI', 'PC 노티 수신 · 거래구분 %s (미처리) · %s원' % (tmsg, format(amt, ',')))
                return PlainTextResponse('OK')
            code, _note = _vbank_deposit_apply(oid, amt, tid, 'PC노티', extra)
            return PlainTextResponse('FAIL' if code == 'missing' else 'OK')

        # ── ② PRO/모바일 계열: P_STATUS ──────────────────────────────
        oid = str(form.get('P_OID') or form.get('oid') or form.get('MOID')
                  or form.get('P_NOTI') or '').strip()
        tid = str(form.get('P_TID') or form.get('tid') or '').strip()
        st = str(form.get('P_STATUS') or '').strip()
        amt = _amt('P_AMT', 'price', 'TotPrice')
        if not oid:
            _noti_unparsed_audit('가상계좌노티', req, form)
            return PlainTextResponse('FAIL')
        if st == '02':                                # 입금통보 — 유일한 PAID 전환 신호
            extra = ('입금자 %s · %s' % (form.get('P_UNAME') or '-', form.get('P_FN_NM') or '')).strip(' ·')
            code, _note = _vbank_deposit_apply(oid, amt, tid, 'P노티(02)', extra)
            return PlainTextResponse('FAIL' if code == 'missing' else 'OK')
        if st == '00':                                # 채번통보 — 입금 아님. 기록만.
            _pay_log(oid, 'VBANK_ISSUED', '채번통보 수신(P_STATUS=00) · %s원 — 입금 아님, 대기 유지' % format(amt, ','))
            return PlainTextResponse('OK')
        _pay_log(oid, 'VBANK_NOTI', '미상 노티 수신 · P_STATUS=%s · %s원 (미처리)' % (st or '-', format(amt, ',')))
        return PlainTextResponse('OK')
    except Exception as e:
        # OK 를 돌려주면 재전송이 끊겨 입금이 유실된다 — 실패를 알리고 재시도를 받는다.
        try:
            import admin_v2 as _av
            _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '노티처리오류', '/inicis/vbank-noti',
                      (str(e)[:120] or 'unknown'))
        except Exception:
            pass
        return PlainTextResponse('FAIL', status_code=500)

@app.api_route('/inicis/mobile-noti', methods=['GET', 'POST'])
async def inicis_mobile_noti(req: Request):
    """모바일 백단 결과통보(P_NOTI_URL).

    P_STATUS 규격: '00' = 승인/채번 성공, '02' = 가상계좌 입금통보.
    · 가상계좌 + '00' 은 '계좌 발급'일 뿐이다 — PAID 로 만들면 미입금 발송 사고.
      (2026-07-29 이전 코드가 이 오인을 갖고 있었다)
    · 실제 입금은 '02' 에서만 PAID 전환한다.
    · 가상계좌가 아닌 수단의 '00'(1trs 승인통보)은 종전대로 금액검증 후 PAID.
    """
    form = {}
    try:
        if req.method == 'POST':
            form = dict(await req.form())
        else:
            form = dict(req.query_params)
        for k, v in req.query_params.items():
            form.setdefault(k, v)
        if not form:
            return PlainTextResponse('OK')

        status = str(form.get('P_STATUS', '')).strip()
        oid    = str(form.get('P_OID', '') or form.get('P_NOTI', '')).strip()
        tid    = str(form.get('P_TID', '')).strip()
        ptype  = str(form.get('P_TYPE', '') or '').strip()
        try:
            amt = int(str(form.get('P_AMT', '0')).replace(',', '').strip() or 0)
        except ValueError:
            amt = 0
        vbankish = ptype.upper() in ('VBANK', 'VACCT') or bool(str(form.get('P_VACT_NUM') or '').strip())

        if not oid:
            _noti_unparsed_audit('모바일노티', req, form)
            return PlainTextResponse('FAIL')

        if status == '02':                            # 가상계좌 입금통보 → PAID
            extra = ('입금자 %s · %s' % (form.get('P_UNAME') or '-', form.get('P_FN_NM') or '')).strip(' ·')
            code, _note = _vbank_deposit_apply(oid, amt, tid, '모바일노티(02)', extra)
            return PlainTextResponse('FAIL' if code == 'missing' else 'OK')

        if status == '00' and vbankish:               # 채번통보 — 입금 아님. 기록만.
            _pay_log(oid, 'VBANK_ISSUED', '모바일 채번통보 수신(00) · %s원 — 입금 아님, 대기 유지' % format(amt, ','))
            return PlainTextResponse('OK')

        if status == '00':                            # 1trs 승인통보(카드 등) — 종전 동작 유지
            with db() as c:
                order = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
                _late = bool(order and order['status'] == 'CANCELLED')
                _paid = False
                if (not _late) and order and order['status'] != 'PAID' and amt == int(order['amount']):
                    c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, paid_at=? "
                           "WHERE order_id=? AND status<>'PAID'",
                           (tid, ptype, kst_iso(), oid))
                    _paid = True
            if _late:
                _pay_log(oid, 'DEPOSIT_AFTER_CANCEL',
                         '취소된 주문에 승인통보 %s원 · TID …%s — 수동 환불 필요' % (format(amt, ','), tid[-6:]))
                try:
                    import admin_v2 as _av
                    _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '취소후입금', oid,
                              '모바일 노티 %s원 — 이니시스 상점관리자에서 환불 처리 필요' % format(amt, ','))
                except Exception:
                    pass
            if _paid:
                _pdet = ''                            # 카드사·간편결제 상세 적재 (노티 전문에도 P_CARD_* 가 실린다)
                try:
                    import admin_v2 as _av; _pdet = _av.pay_detail_save(oid, dict(form), 'NOTI') or ''
                except Exception:
                    _pdet = ''
                _pay_log(oid, 'PAID', '%s · 모바일 승인통보 · TID …%s%s'
                         % (ptype or 'Card', tid[-6:], (' · ' + _pdet) if _pdet else ''))
                _award_purchase_points(oid)
                _ga4_mp_purchase(oid)                 # 서버사이드 purchase 백업
                try:
                    import admin_v2 as _av; _av.order_notify_async(oid, 'paid')
                except Exception:
                    pass
            return PlainTextResponse('OK')

        _pay_log(oid, 'VBANK_NOTI', '모바일 노티 수신 · P_STATUS=%s (미처리)' % (status or '-'))
        return PlainTextResponse('OK')
    except Exception as e:
        try:
            import admin_v2 as _av
            _av.audit({'name': 'SYSTEM', 'role': 'NOTI'}, '노티처리오류', '/inicis/mobile-noti',
                      (str(e)[:120] or 'unknown'))
        except Exception:
            pass
        return PlainTextResponse('FAIL', status_code=500)

def _ga_cookie_ids(req):
    """요청 쿠키에서 GA4 client_id / session_id 추출 (없으면 빈 문자열 2개).

    · _ga = GA1.1.<rand>.<epoch>            → cid = "<rand>.<epoch>"
    · _ga_<측정ID뒷부분> 세션 쿠키
        구형 GS1.1.<sid>.<n>...             → 3번째 세그먼트
        신형 GS2.1.s<sid>$o<n>$...          → 's' 접두 토큰
    파싱 실패는 조용히 무시한다 — 계측은 어떤 경우에도 주문을 방해하지 않는다."""
    cid = sid = ''
    try:
        raw = req.cookies.get('_ga', '')
        parts = raw.split('.')
        if len(parts) >= 4 and parts[-2].isdigit() and parts[-1].isdigit():
            cid = parts[-2] + '.' + parts[-1]
        suf = GA4_ID.split('-', 1)[1] if '-' in GA4_ID else ''
        if suf:
            sraw = req.cookies.get('_ga_' + suf, '')
            for tok in sraw.replace('$', '.').split('.'):
                if tok[:1] == 's' and tok[1:].isdigit():
                    sid = tok[1:]; break
            if not sid:
                sp = sraw.split('.')
                if len(sp) >= 3 and sp[2].isdigit():
                    sid = sp[2]
    except Exception:
        pass
    return cid[:64], sid[:32]

def _ga4_item_cat(pid, name=''):
    """GA4 item_category 추론 — 프리픽스 우선, 이름 키워드 보조."""
    p = str(pid or '')
    s = (p + ' ' + str(name or '')).lower()
    if p.startswith('mpd::'): return 'drops'
    if p.startswith('k2g::') or 'album' in s: return 'album'
    if any(k in s for k in ('tteok', 'kimbap', 'bowl', 'kfood', 'k-food', '떡볶이', '김밥')): return 'kfood'
    if any(k in s for k in ('hood', 'tee', 'shirt', 'cap', 'apparel', '후디', '티셔츠', '볼캡')): return 'apparel'
    if any(k in s for k in ('glass', 'mat', 'gift', 'living', '유리', '매트', '기프트')): return 'living'
    return 'shop'

def _ga4_mp_purchase(oid: str):
    """PAID 확정 주문의 purchase 를 GA4 Measurement Protocol 로 서버 전송.

    · 백그라운드 데몬 스레드 — 결제 플로우를 1ms 도 지연시키지 않는다.
    · 클라이언트 이벤트와 동일 transaction_id + client_id → GA4 가 중복 제거.
    · cid 미보유 주문은 전송하지 않는다(새 사용자 생성으로 인한 이중 계상 방지).
    · ga_mp_sent 클레임(행 잠금)으로 PC/모바일/가상계좌 노티 중복 수신에도 1회만 전송."""
    if not (GA4_ID and GA4_API_SECRET and _has_ga_cols()):
        return
    def _run():
        try:
            with db() as c:
                o = c.one(f'SELECT * FROM orders WHERE order_id=?{LOCK}', (oid,))
                if not o or o.get('status') != 'PAID':
                    return
                cid = (o.get('ga_cid') or '').strip()
                if not cid:
                    return
                if str(o.get('ga_mp_sent') or '') == '1':
                    return
                c.exec("UPDATE orders SET ga_mp_sent='1' WHERE order_id=?", (oid,))
                items = json.loads(o.get('items') or '[]')
                amount = int(o.get('amount') or 0)
                sid = str(o.get('ga_sid') or '').strip()
            g_items, sub = [], 0
            for it in items[:100]:                        # GA4 items 한도(200) 대비 여유
                p = int(it.get('p') or 0); q = int(it.get('q') or 1); sub += p * q
                g_items.append({'item_id': str(it.get('id') or '')[:100],
                                'item_name': str(it.get('n') or '')[:100],
                                'price': p, 'quantity': q,
                                'item_category': _ga4_item_cat(it.get('id'), it.get('n'))})
            params = {'transaction_id': oid, 'value': amount, 'currency': 'KRW',
                      'shipping': max(0, amount - sub), 'items': g_items,
                      'engagement_time_msec': 1}
            if sid.isdigit():
                params['session_id'] = int(sid)           # 세션 연결 → 유입경로 귀속 유지
            payload = {'client_id': cid,
                       'events': [{'name': 'purchase', 'params': params}]}
            url = ('https://www.google-analytics.com/mp/collect?measurement_id=%s&api_secret=%s'
                   % (GA4_ID, urllib.parse.quote(GA4_API_SECRET)))
            body = json.dumps(payload).encode('utf-8')
            for attempt in (1, 2):                        # 일시 장애 대비 1회 재시도
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        url, data=body, headers={'Content-Type': 'application/json'}),
                        timeout=4)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f'[ga4] MP purchase 전송 실패 {oid}: {type(e).__name__}: {e}', flush=True)
        except Exception as e:
            print(f'[ga4] MP purchase 처리 실패 {oid}: {type(e).__name__}: {e}', flush=True)
    threading.Thread(target=_run, daemon=True).start()

def _award_purchase_points(oid: str):
    """결제 완료 주문에 구매 적립 1% 지급 (드롭 상품·배송비 제외).

    - 적립 기준액 = 일반(mpd:: 제외) 상품 결제금액 합계. 배송비는 제외한다.
    - NEW/DROPS(mpd::) 라인은 적립 대상이 아니다.
    - 원 단위 절사(내림). 0원이면 원장에 기록하지 않는다.
    - event_key 로 멱등 보장 — 재진입/새로고침 시 중복 적립되지 않는다.
    - 비회원(GUEST) 주문은 적립하지 않는다.
    """
    try:
        with db() as c:
            o = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
        if not o or o['status'] != 'PAID':
            return
        cid = o['customer_id'] or ''
        mid = o['member_id'] or ''
        if not (cid and mid):                       # 로그인 회원 주문만 적립
            return
        lines = json.loads(o['items'] or '[]')
        base = sum(int(l.get('p', 0)) * int(l.get('q', 1))
                   for l in lines if not str(l.get('id', '')).startswith(DROP_PREFIX))
        pts = (base * POINT_RATE_BP) // 10000       # 1% · 원 단위 절사
        if pts <= 0:
            return
        import admin_v2
        admin_v2.ensure_ready()
        admin_v2.point_apply(cid, mid, 'PURCHASE_REWARD', pts, 'purchase:%s' % oid,
                             '구매 적립 1%%(대상금액 %s원)' % format(base, ','), order_id=oid)
    except Exception:
        pass                                        # 적립 실패가 결제 완료를 막지 않는다

def _pay_log(oid: str, code: str, msg: str = ''):
    """주문 결제 진행 이력 추가 — orders.pay_log (JSON 배열, 최대 40건).

    PENDING 사유 추적용: 주문생성→결제창 호출→창닫힘/이탈→인증→승인→완료의
    각 단계를 남겨 관리자 상세에서 미결제 원인을 확인한다. 실패는 결제를 막지 않는다."""
    if not oid:
        return
    try:
        with db() as c:
            r = c.one('SELECT pay_log FROM orders WHERE order_id=?', (oid,))
            if r is None:
                return
            try:
                log = json.loads(r['pay_log'] or '[]')
            except Exception:
                log = []
            if not isinstance(log, list):
                log = []
            log.append({'t': kst_iso()[:19], 'c': str(code)[:24], 'm': str(msg or '')[:160]})
            c.exec('UPDATE orders SET pay_log=? WHERE order_id=?',
                   (json.dumps(log[-40:], ensure_ascii=False), oid))
    except Exception:
        pass                                        # 컬럼 미생성 등 — 이력만 포기

# 클라이언트 비콘 이벤트 화이트리스트 — 메시지는 서버 고정 문구(로그 오염 방지)
_PAY_EVENT_CODES = {
    'open_pc': ('PAY_OPEN_PC', 'PC 결제창 호출'),
    'open_m':  ('PAY_OPEN_M',  '모바일 결제창으로 이동'),
    'close':   ('WINDOW_CLOSE', '구매자가 결제창을 닫음'),
    'exit':    ('PAGE_EXIT',    '결제 미완료 상태로 페이지 이탈'),
}

@app.post('/api/pay/event')
async def api_pay_event(req: Request):
    """체크아웃 페이지 sendBeacon 수신 — 결제창 호출/창닫힘/이탈 시그널.

    closeUrl(/inicis/close)은 쿼리 금지(V023)라 주문번호를 못 받으므로,
    close 페이지가 부모창에 postMessage → 부모(체크아웃)가 여기로 비콘을 쏜다.
    PENDING 주문에만 기록하며 항상 200 (비콘은 응답을 읽지 않는다)."""
    try:
        d = json.loads((await req.body())[:400].decode('utf-8', 'replace') or '{}')
    except Exception:
        return {'ok': True}
    oid = str(d.get('oid') or '')[:40]
    ev = _PAY_EVENT_CODES.get(str(d.get('c') or ''))
    if oid and ev:
        try:
            with db() as c:
                r = c.one('SELECT status FROM orders WHERE order_id=?', (oid,))
            if r and r['status'] == 'PENDING':
                _pay_log(oid, ev[0], ev[1])
        except Exception:
            pass
    return {'ok': True}

@app.api_route('/inicis/close', methods=['GET', 'POST'])
async def inicis_close(req: Request):
    """결제창 닫기 URL(closeUrl) — 쿼리스트링 없는 순수 경로(V023 회피).
       결제 팝업/레이어를 닫고 결제 페이지로 복귀시킨다.
       부모창(체크아웃)에 postMessage 로 '창 닫힘'을 알려 PENDING 사유를 기록한다."""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>결제 취소</title>"
        "<script>"
        # 레이어(iframe) 모드: 부모창에 취소 통지 + INIStdPay 레이어를 직접 닫는다(viewOff).
        # 일부 버전은 iframe 이 이중 중첩되므로 parent 와 top 양쪽에 시도한다
        # (교차출처 접근 예외는 전부 try 로 흡수).
        "try{if(parent&&parent!==window){"
        "try{parent.postMessage('mapdal:ini-close','*')}catch(e){}"
        "try{if(parent.INIStdPay&&parent.INIStdPay.viewOff)parent.INIStdPay.viewOff()}catch(e){}"
        "}}catch(e){}"
        "try{if(top&&top!==window&&top!==parent){"
        "try{top.postMessage('mapdal:ini-close','*')}catch(e){}"
        "try{if(top.INIStdPay&&top.INIStdPay.viewOff)top.INIStdPay.viewOff()}catch(e){}"
        "}}catch(e){}"
        # 팝업 모드: 부모창 통지 후 팝업 닫기. iframe 은 여기서 절대 내비게이션하지
        # 않는다 — 종전에는 iframe 을 /checkout 으로 이동시켜 레이어 안에 체크아웃
        # 페이지가 통째로 로드될 수 있었다. 최상위 창으로 직접 열린 경우에만 복귀.
        "try{if(window.opener){window.opener.postMessage('mapdal:ini-close','*');window.close();}"
        "else if(parent===window){location.replace('/checkout');}}catch(e){}"
        "</script>"
        "<body style=\"font-family:sans-serif;padding:40px;text-align:center;color:#141414\">"
        "결제를 취소했습니다. 창이 닫히지 않으면 <a href='/checkout'>여기</a>를 눌러 주세요.</body>")

def _ini_net_cancel(net_cancel_url: str, auth_token: str):
    """승인 처리 중 예외 발생 시 망취소(인증결과 응답 후 10분 이내)."""
    if not net_cancel_url or not auth_token:
        return
    try:
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        sign = _ini_signature({'authToken': auth_token, 'timestamp': ts})
        veri = _ini_signature({'authToken': auth_token, 'signKey': INICIS_SIGNKEY, 'timestamp': ts})
        payload = urllib.parse.urlencode({
            'mid': INICIS_MID, 'authToken': auth_token, 'timestamp': ts,
            'signature': sign, 'verification': veri, 'charset': 'UTF-8', 'format': 'JSON',
        }).encode('utf-8')
        reqx = urllib.request.Request(net_cancel_url, data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        urllib.request.urlopen(reqx, timeout=15).read()
    except Exception:
        pass   # 망취소 실패는 로깅만 (여기선 무시) — 재고/주문은 FAILED로 남음

@app.get('/api/orders/{order_id}')
def get_order(order_id: str):
    """주문 조회. 가상계좌 컬럼이 없는 구형 DB 에서도 500 이 나지 않도록 분기한다."""
    base = 'order_id,created,status,amount,items,ship_method'
    cols = (base + ',pay_method,vbank_num,vbank_name,vbank_holder,vbank_due'
            ) if _has_vbank_cols() else base
    if _has_ship_cols():
        cols += ',fulfill,tracking,courier'
    with db() as c:
        row = c.one('SELECT %s FROM orders WHERE order_id=?' % cols, (order_id,))
    if not row: raise HTTPException(404, 'not found')
    try:
        row['items'] = json.loads(row['items'] or '[]')
    except Exception:
        row['items'] = []
    # 입금대기 건은 계좌 안내를 함께 내려준다(주문완료 화면·마이페이지에서 표시).
    row['vbank'] = {'num': row.pop('vbank_num', '') or '', 'bank': row.pop('vbank_name', '') or '',
                    'holder': row.pop('vbank_holder', '') or '', 'due': row.pop('vbank_due', '') or ''}
    row.setdefault('pay_method', '')
    # 비회원도 주문번호만으로 배송 추적이 가능하도록 운송장 정보를 함께 내려준다.
    for k in ('fulfill', 'tracking', 'courier'):
        row.setdefault(k, '')
    return row

@app.get('/admin', response_class=HTMLResponse)
def admin(token: str = Query('')):
    if token != ADMIN_TOKEN: raise HTTPException(403, 'forbidden')
    with db() as c:
        rows = c.all('SELECT * FROM orders ORDER BY created DESC LIMIT 300')
        paid = c.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID'")
    tr = ''
    for r in rows:
        buyer, items = json.loads(r['buyer']), json.loads(r['items'])
        inm = items[0]['n'][:22] + (f' 외 {len(items)-1}' if len(items) > 1 else '')
        color = {'PAID':'#0a7d38','PENDING':'#b58900','FAILED':'#c0392b'}.get(r['status'],'#333')
        rcpt = f"<a href='{r['receipt_url']}' target='_blank'>영수증</a>" if r['receipt_url'] else '-'
        tr += (f"<tr><td>{r['order_id']}</td><td>{r['created'][5:16]}</td>"
               f"<td style='color:{color};font-weight:700'>{r['status']}</td>"
               f"<td style='text-align:right'>{r['amount']:,}</td><td>{inm}</td>"
               f"<td>{buyer.get('name','')}</td><td>{buyer.get('phone','')}</td>"
               f"<td>{r['ship_method']}</td><td>{rcpt}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>MAPDAL 주문 관리</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;margin:30px;background:#F7F6F2}}h1{{font-size:20px}}
.kpi{{display:inline-block;background:#141414;color:#fff;padding:10px 18px;margin:0 8px 16px 0;font-size:13px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px}}
th,td{{border:1px solid #ddd;padding:8px 10px}}th{{background:#141414;color:#fff;font-size:11px}}</style>
<h1>MAPDAL SEOUL — 주문 관리</h1>
<span class="kpi">결제완료 {paid['n']}건</span><span class="kpi">결제액 ₩{paid['s']:,}</span>
<span class="kpi">DB: {"PostgreSQL" if IS_PG else "SQLite(로컬)"}</span>
<table><tr><th>주문번호</th><th>일시</th><th>상태</th><th>금액</th><th>품목</th><th>주문자</th><th>연락처</th><th>배송</th><th>영수증</th></tr>{tr}</table>"""

@app.get('/healthz')
def healthz():
    """Render 배포용 liveness: 웹 프로세스가 응답하면 항상 200을 반환한다.

    DB는 서버 기동 후 백그라운드에서 재연결하므로, 이 경로에서 DB 준비를
    요구하면 Render가 정상 컨테이너를 배포 실패로 오판할 수 있다.
    """
    return {'ok': True, 'service': 'mapdal-seoul',
            'db_ready': DB_READY, 'db': 'pg' if IS_PG else 'sqlite'}

@app.get('/readyz')
def readyz():
    """운영 모니터링용 readiness: DB 실제 연결까지 확인한다."""
    if not DB_READY:
        raise HTTPException(503, 'db connecting')
    with db() as c: c.one('SELECT 1 AS ok')
    return {'ok': True, 'db': 'pg' if IS_PG else 'sqlite'}

@app.get('/')
def root(): return RedirectResponse('/home', status_code=301)
try:
    from hero_api import router as hero_router
    app.include_router(hero_router)
except Exception as _e:
    print('hero_api load skipped:', _e)
try:
    from admin_v2 import admin_router
    app.include_router(admin_router)
except Exception as _e:
    print('admin load skipped:', _e)
app.mount('/', StaticFiles(directory=os.path.join(BASE, 'static'), html=True), name='static')
