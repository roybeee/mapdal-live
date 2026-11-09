"""주문 생성 남용 방지 — 속도 제한 · 미결제 보유 상한 · 행사 상품 수량 · 취소된 주문 승인 차단."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ordergate as og  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ('ORDER_RATE_MEMBER', 'ORDER_RATE_IP', 'ORDER_RATE_WINDOW_S', 'ORDER_OPEN_MAX',
              'ORDER_PROMO_MAX_QTY', 'ORDER_PENDING_PROMO_MIN'):
        monkeypatch.delenv(k, raising=False)
    og._LIMITER.clear()
    yield
    og._LIMITER.clear()


def test_sliding_window_limits_and_recovers():
    w = og.SlidingWindow()
    assert all(w.hit('k', 3, 60, now=t) for t in (0, 1, 2))
    assert not w.hit('k', 3, 60, now=3)
    assert w.hit('k', 3, 60, now=61)           # 첫 시도가 창을 벗어나면 다시 허용
    assert w.hit('', 1, 60) and w.hit('x', 0, 60)  # 키 없음·한도 0 = 제한 없음


def test_sliding_window_bounded_memory():
    w = og.SlidingWindow(max_keys=3)
    for i in range(10):
        w.hit('k%d' % i, 5, 60, now=0)
    assert len(w._hits) == 3


def test_rate_check_member_and_ip(monkeypatch):
    monkeypatch.setenv('ORDER_RATE_MEMBER', '2')
    monkeypatch.setenv('ORDER_RATE_IP', '3')
    lim = og.SlidingWindow()
    assert og.rate_check('m1', '1.1.1.1', now=0, limiter=lim) == ''
    assert og.rate_check('m1', '1.1.1.1', now=1, limiter=lim) == ''
    assert og.rate_check('m1', '1.1.1.1', now=2, limiter=lim) != ''     # 회원 한도
    assert og.rate_check('m2', '1.1.1.1', now=3, limiter=lim) == ''     # IP 3번째
    assert og.rate_check('m3', '1.1.1.1', now=4, limiter=lim) != ''     # IP 한도


def test_open_cap(monkeypatch):
    assert og.open_cap_message(4) == ''
    assert '5건' in og.open_cap_message(5)
    monkeypatch.setenv('ORDER_OPEN_MAX', '0')
    assert og.open_cap_message(100) == ''


def test_promo_qty_cap_off_by_default(monkeypatch):
    items = [{'id': 'mp::promo', 'q': 50}, {'id': 'k2g::x', 'q': 99}]
    assert og.promo_qty_message(items, {'mp::promo'}) == ''
    monkeypatch.setenv('ORDER_PROMO_MAX_QTY', '10')
    assert '10개' in og.promo_qty_message(items, {'mp::promo'})
    assert og.promo_qty_message(items, set()) == ''


# ── app 연동 ───────────────────────────────────────────────────────────
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as appmod
    import admin_v2
    dbp = tmp_path / 'mapdal.db'
    with sqlite3.connect(dbp) as db:
        db.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, created TEXT, status TEXT, amount INTEGER, buyer TEXT,
              items TEXT, ship_method TEXT, payment_key TEXT, pay_method TEXT, receipt_url TEXT, customer_id TEXT,
              member_id TEXT, contact_phone_norm TEXT, paid_at TEXT, pay_log TEXT);
            CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT, price INTEGER, soldout INTEGER, kind TEXT, stock INTEGER);
        """)
    monkeypatch.setattr(appmod, 'SQLITE_PATH', str(dbp))
    monkeypatch.setattr(appmod, 'IS_PG', False)
    monkeypatch.setattr(appmod, 'PAY_CONFIG_ERRORS', [])
    monkeypatch.setattr(admin_v2, 'ensure_ready', lambda: None)
    monkeypatch.setattr(admin_v2, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(admin_v2, 'member_of', lambda req: {'id': 'm1', 'customer_id': 'c1', 'status': 'ACTIVE',
                                                           'email': 'a@b.co'})
    monkeypatch.setattr(admin_v2, 'guest_checkout_gate', lambda *a, **k: None)
    body = {'items': [{'id': 'nope::1', 'q': 1}],
            'buyer': {'name': '홍길동', 'phone': '01012345678', 'addr1': '서울', 'zip': '04000', 'email': 'a@b.co'}}
    return TestClient(appmod.app), dbp, body


def test_create_order_rate_limited(app_env, monkeypatch):
    client, _dbp, body = app_env
    monkeypatch.setenv('ORDER_RATE_MEMBER', '1')
    first = client.post('/api/orders', json=body)
    assert first.status_code != 429
    second = client.post('/api/orders', json=body)
    assert second.status_code == 429


def test_create_order_blocked_by_open_unpaid_orders(app_env):
    client, dbp, body = app_env
    import app as appmod
    now = appmod.kst_iso()
    with sqlite3.connect(dbp) as db:
        db.executemany("INSERT INTO orders(order_id, created, status, amount, customer_id) VALUES(?,?,?,?,?)",
                       [('MD-OPEN-%d' % i, now, 'PENDING', 1000, 'c1') for i in range(5)])
    r = client.post('/api/orders', json=body)
    assert r.status_code == 429 and '결제하지 않은 주문' in r.json()['detail']


def test_inicis_return_does_not_approve_cancelled_order(app_env, monkeypatch):
    client, dbp, _body = app_env
    import urllib.request
    with sqlite3.connect(dbp) as db:
        db.execute("INSERT INTO orders(order_id, created, status, amount, pay_log) VALUES('MD-X-1','2026-11-10T09:00:00','CANCELLED',1000,'[]')")

    def no_network(*a, **k):
        raise AssertionError('취소된 주문에 승인 요청을 보내면 안 된다')
    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    r = client.post('/inicis/return', data={'resultCode': '0000', 'orderNumber': 'MD-X-1', 'authToken': 't',
                                            'authUrl': 'https://fcstdpay.inicis.com/api/payAuth',
                                            'idc_name': 'fc', 'netCancelUrl': ''}, follow_redirects=False)
    assert r.status_code == 303 and 'fail=1' in r.headers['location']
    with sqlite3.connect(dbp) as db:
        assert db.execute("SELECT status FROM orders WHERE order_id='MD-X-1'").fetchone()[0] == 'CANCELLED'
