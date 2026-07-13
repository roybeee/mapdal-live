"""
MAPDAL SEOUL — 라이브 커머스 백엔드 v2 (확장형)
FastAPI + PostgreSQL(운영) / SQLite(로컬 폴백) + 토스페이먼츠
- DATABASE_URL 환경변수가 있으면 PostgreSQL, 없으면 SQLite로 자동 전환
- 동시 주문 안전: 트랜잭션 + 행 잠금(FOR UPDATE), 재고 원자적 차감
- 결제 승인 멱등 처리 (중복 승인 방지)
"""
import os, json, secrets, datetime, base64
import urllib.request, urllib.error
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
IS_PG = DATABASE_URL.startswith('postgresql')

TOSS_CLIENT_KEY = os.getenv('TOSS_CLIENT_KEY', 'test_ck_D5GePWvyJnrK0W0k6q8gLzN97Eoq')
TOSS_SECRET_KEY = os.getenv('TOSS_SECRET_KEY', 'test_sk_zXLkKEypNArWmo50nX3lmeaxYG5R')
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
        ins = ('INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING'
               if IS_PG else 'INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)')
        for r in rows: c.exec(ins, r)
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

# ── API ─────────────────────────────────────────────────────────
@app.get('/api/config')
def config():
    return {'clientKey': TOSS_CLIENT_KEY, 'freeShipOver': FREE_SHIP_OVER, 'shipFee': SHIP_FEE}

@app.post('/api/orders')
async def create_order(req: Request):
    body = await req.json()
    items, buyer = body.get('items') or [], body.get('buyer') or {}
    ship = body.get('shipMethod', 'standard')
    if not items: raise HTTPException(400, '장바구니가 비어 있습니다')
    if body.get('intl'): raise HTTPException(400, '해외 배송(DDP) 온라인 결제는 준비중입니다. global@mealzip.kr로 문의해 주세요.')
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
    return {'orderId': order_id, 'amount': amount,
            'orderName': name0 + (f' 외 {len(resolved)-1}건' if len(resolved) > 1 else ''),
            'sub': sub, 'shipFee': ship_fee}

@app.post('/api/payments/confirm')
async def confirm(req: Request):
    body = await req.json()
    pk, oid, amt = body.get('paymentKey'), body.get('orderId'), int(body.get('amount', 0))
    with db() as c:
        row = c.one('SELECT * FROM orders WHERE order_id=?', (oid,))
    if not row: raise HTTPException(404, '주문을 찾을 수 없습니다')
    if row['status'] == 'PAID':          # 멱등: 이미 승인됨
        return {'status': 'PAID', 'orderId': oid, 'amount': row['amount'],
                'method': row['pay_method'], 'receipt': row['receipt_url']}
    if int(row['amount']) != amt:
        raise HTTPException(400, '결제 금액이 주문 금액과 일치하지 않습니다')
    auth = base64.b64encode(f'{TOSS_SECRET_KEY}:'.encode()).decode()
    q = urllib.request.Request('https://api.tosspayments.com/v1/payments/confirm',
        data=json.dumps({'paymentKey': pk, 'orderId': oid, 'amount': amt}).encode(),
        headers={'Authorization': f'Basic {auth}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(q, timeout=25) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode())
        with db() as c:
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        raise HTTPException(400, err.get('message', '결제 승인 실패'))
    receipt = (res.get('receipt') or {}).get('url', ''); method = res.get('method', '')
    with db() as c:                      # 중복 승인 레이스 방지 가드
        c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, receipt_url=? "
               "WHERE order_id=? AND status<>'PAID'", (pk, method, receipt, oid))
    return {'status': 'PAID', 'orderId': oid, 'amount': amt, 'method': method, 'receipt': receipt}

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
def root(): return RedirectResponse('/mapdal_home_mockup_v1.html')
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
