"""
MAPDAL SEOUL — 라이브 커머스 백엔드 v2 (확장형)
FastAPI + PostgreSQL(운영) / SQLite(로컬 폴백) + 토스페이먼츠
- DATABASE_URL 환경변수가 있으면 PostgreSQL, 없으면 SQLite로 자동 전환
- 동시 주문 안전: 트랜잭션 + 행 잠금(FOR UPDATE), 재고 원자적 차감
- 결제 승인 멱등 처리 (중복 승인 방지)
"""
import os, json, secrets, datetime, base64, hashlib
import urllib.request, urllib.error, urllib.parse
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse

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
# 결제 returnUrl/closeUrl 도메인 — 이니시스는 요청페이지와 도메인 일치를 검증(V023).
#   Cloudflare/Render 프록시 뒤에서는 req.base_url이 실제 도메인과 달라질 수 있으므로
#   SITE_ORIGIN 환경변수로 실도메인을 고정하는 것이 가장 안전. 미설정 시 헤더로 추론.
SITE_ORIGIN    = os.getenv('SITE_ORIGIN', 'https://mapdal.kr').rstrip('/')
ADMIN_TOKEN     = os.getenv('ADMIN_TOKEN', 'mapdal-admin-2026')
FREE_SHIP_OVER, SHIP_FEE = 30000, 3000

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
      payment_key TEXT, pay_method TEXT, receipt_url TEXT);
    CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created);
    '''
    with db() as c:
        for stmt in ddl.strip().split(';'):
            if stmt.strip(): c.exec(stmt)
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
        try:
            removed = {x['id'] for x in c.all('SELECT id FROM own_removed')}
        except Exception:
            removed = set()
        ins = ('INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING'
               if IS_PG else 'INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)')
        for r in rows:
            if r[0] in removed: continue
            c.exec(ins, r)
        print(f'[seed] products: {len(rows)} ({"PostgreSQL" if IS_PG else "SQLite"})')

from contextlib import asynccontextmanager
import threading

def _init_db():
    global DB_READY
    try:
        if IS_PG:
            _connect_pg_with_retry()
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

# ── 클린 URL: .html 숨김 · 홈은 /home ─────────────────────────────────────
_STATIC_DIR = os.path.join(BASE, 'static')
_HOME_FILE = 'mapdal_home_mockup_v1.html'

@app.middleware('http')
async def clean_urls(request, call_next):
    if request.method in ('GET', 'HEAD'):
        p = request.url.path
        if p == '/home':
            # 클린 주소 → 실제 홈 파일을 내부 매핑 (주소창은 /home 유지)
            request.scope['path'] = '/' + _HOME_FILE
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

@app.get('/api/config')
def config():
    return {'pg': 'inicis', 'mid': INICIS_MID,
            'freeShipOver': FREE_SHIP_OVER, 'shipFee': SHIP_FEE}

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

    with db() as c:                      # ← 단일 트랜잭션: 검증·재고차감·주문생성 원자 처리
        sub, resolved = 0, []
        for it in items:
            pid = str(it.get('id', '')); q = max(1, min(99, int(it.get('q', 1))))
            row = c.one(f'SELECT * FROM products WHERE id=?{LOCK}', (pid,))
            if not row: raise HTTPException(400, f'알 수 없는 상품: {pid}')
            if row['soldout']: raise HTTPException(400, f'품절: {row["name"][:30]}')
            if row['price'] <= 0: raise HTTPException(400, f'가격 확인 필요: {row["name"][:30]}')
            if row['stock'] is not None:                     # 재고 관리 대상 상품
                if row['stock'] < q:
                    raise HTTPException(409, f'재고 부족: {row["name"][:30]} (남은 수량 {row["stock"]})')
                c.exec('UPDATE products SET stock=stock-? WHERE id=?', (q, pid))
                if row['stock'] - q == 0:
                    c.exec('UPDATE products SET soldout=1 WHERE id=?', (pid,))
            sub += row['price'] * q
            resolved.append({'id': pid, 'n': row['name'], 'p': row['price'], 'q': q})
        ship_fee = 0 if (ship == 'pickup' or sub >= FREE_SHIP_OVER) else SHIP_FEE
        amount = sub + ship_fee
        order_id = f'MD-{datetime.datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}'
        c.exec('INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method) VALUES(?,?,?,?,?,?,?)',
               (order_id, datetime.datetime.now().isoformat(timespec='seconds'), 'PENDING',
                amount, json.dumps(buyer, ensure_ascii=False),
                json.dumps(resolved, ensure_ascii=False), ship))
    name0 = resolved[0]['n'][:28]
    order_name = name0 + (f' 외 {len(resolved)-1}건' if len(resolved) > 1 else '')

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
        'gopaymethod': '', 'acceptmethod': 'centerCd(Y):below1000:HPP(2)',
        'returnUrl': origin + '/inicis/return', 'closeUrl': origin + '/inicis/close',
    }
    return {'orderId': order_id, 'amount': amount, 'orderName': order_name,
            'sub': sub, 'shipFee': ship_fee, 'inicis': inicis}

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
        return _fail(form.get('resultMsg', '인증 실패'))
    if not (oid and auth_token and auth_url):
        return _fail('인증 응답 파라미터 누락')
    # 보안: authUrl 이 이니시스 도메인 + idc_name 일치 확인
    if not _ini_idc_host_ok(idc_name, auth_url):
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
        _ini_net_cancel(net_cancel_url, auth_token)
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('승인 통신 오류')

    if res.get('resultCode') != '0000':
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail(res.get('resultMsg', '승인 실패'))

    # 금액 위변조 검증: 승인금액(TotPrice) == 주문금액
    tot = int(str(res.get('TotPrice', '0')).replace(',', '') or 0)
    if tot != int(order['amount']):
        _ini_net_cancel(net_cancel_url, auth_token)
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        return _fail('결제 금액 불일치')

    tid = res.get('tid', ''); method = res.get('payMethod', '')
    with db() as c:                                  # 중복 승인 레이스 방지 가드
        c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=? "
               "WHERE order_id=? AND status<>'PAID'", (tid, method, '', oid))
    return RedirectResponse(f'/order-complete?oid={oid}', status_code=303)

@app.api_route('/inicis/close', methods=['GET', 'POST'])
async def inicis_close(req: Request):
    """결제창 닫기 URL(closeUrl) — 쿼리스트링 없는 순수 경로(V023 회피).
       결제 팝업/레이어를 닫고 결제 페이지로 복귀시킨다."""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>결제 취소</title>"
        "<script>try{if(window.opener){window.close();}"
        "else{location.replace('/checkout');}}catch(e){location.replace('/checkout');}</script>"
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
    with db() as c:
        row = c.one('SELECT order_id,created,status,amount,items,ship_method FROM orders WHERE order_id=?', (order_id,))
    if not row: raise HTTPException(404, 'not found')
    row['items'] = json.loads(row['items']); return row

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
