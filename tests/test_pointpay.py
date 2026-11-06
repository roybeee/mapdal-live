"""포인트 결제 (2026-11-06) — 갈래별 잔액 · 차감/부족 · 한도 · 반환/재반영 멱등 · 상품/이벤트 판매 규칙."""
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin
import pointpay as pp

NOW = datetime.datetime(2026, 11, 10, 12, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setattr(admin, "kst_naive", lambda: NOW)
    with sqlite3.connect(tmp_path / "mapdal.db") as c:
        c.executescript("""
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, created TEXT, customer_id TEXT, member_id TEXT);
            CREATE TABLE customer_profiles(id TEXT PRIMARY KEY, points_balance INTEGER, updated_at TEXT);
            CREATE TABLE members(id TEXT, customer_id TEXT, points INTEGER);
            CREATE TABLE point_ledger(id TEXT, customer_id TEXT, member_id TEXT, event_type TEXT, amount INTEGER,
              balance_after INTEGER, event_key TEXT UNIQUE, order_id TEXT, reason TEXT, expires_at TEXT, created_at TEXT, by_admin TEXT);
            CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT, price INTEGER, badge TEXT,
              ship_required INTEGER, coupon_ok INTEGER, points_ok INTEGER, etc_ok INTEGER);
        """)
        c.execute("INSERT INTO customer_profiles VALUES('c1', 3000, '')")
        c.execute("INSERT INTO members VALUES('m1','c1',3000)")
        # 적립금 2,000 (가입·구매) + 보상 1,000
        for i, (et, amt) in enumerate((("SIGNUP_BONUS", 1500), ("PURCHASE_REWARD", 500), ("COMPENSATION", 1000))):
            c.execute("INSERT INTO point_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("x%d" % i, "c1", "m1", et, amt, 0, "seed:%d" % i, "", "", "", "", ""))
        c.executemany("INSERT INTO products VALUES(?,?,?,?,?,?,?,?)", [
            ("mp::normal", "일반 앨범", 20000, "", None, None, None, None),
            ("mp::promo", "예약판매 앨범", 20000, "예약판매", None, None, None, None),
            ("mp::checked", "적립금 체크 상품", 10000, "", 0, 1, 1, 0),
            ("mpd::lucky", "럭키드로우 옵션", 15600, "", None, None, None, None),
        ])
    admin._state.update(pcols={"id", "name", "price", "badge", "ship_required", "coupon_ok", "points_ok", "etc_ok"},
                        pname="name", ocols=set())
    drops = [{"id": 7, "title": "LUCKY DRAW", "on": True, "pub": "now", "coupon_ok": False,
              "points_ok": False, "etc_ok": True,
              "options": [{"name": "랜덤", "price": 15600, "product_id": "mpd::lucky", "managed": 1}]}]
    monkeypatch.setattr(admin, "_drops_all", lambda: drops)
    yield


def bal():
    return pp.balances("c1")


def test_balance_split(db):
    assert bal() == {"total": 3000, "reward": 1000, "point": 2000}


def test_rules_defaults_and_checkboxes(db):
    r = admin.product_rules([{"id": x} for x in ("mp::normal", "mp::promo", "mp::checked", "mpd::lucky")])
    assert (r["mp::normal"]["points_ok"], r["mp::normal"]["etc_ok"]) == (False, True)      # 일반: 기타 할인만
    assert (r["mp::promo"]["points_ok"], r["mp::promo"]["etc_ok"]) == (False, False)       # 행사 자동판정: 불가
    assert (r["mp::checked"]["points_ok"], r["mp::checked"]["etc_ok"]) == (True, False)    # 명시값 우선
    assert (r["mpd::lucky"]["points_ok"], r["mpd::lucky"]["etc_ok"]) == (False, True)      # 이벤트 체크박스
    assert r["mpd::lucky"]["coupon_ok"] is False and r["mpd::lucky"]["ship_required"] is True


def test_spend_and_insufficient(db):
    with pytest.raises(HTTPException) as e:
        pp.spend("c1", "m1", "MD-A", 0, 1001)
    assert e.value.status_code == 400 and "이세계아이돌 예약판매 1%" in e.value.detail
    with pytest.raises(HTTPException):
        pp.spend("c1", "m1", "MD-A", 2001, 0)
    assert bal()["total"] == 3000                                   # 실패 시 아무것도 기록하지 않음
    assert pp.spend("c1", "m1", "MD-A", 500, 1000) == {"point": 500, "reward": 1000}
    assert bal() == {"total": 1500, "reward": 0, "point": 1500}
    assert admin.one("SELECT points FROM members WHERE id='m1'")["points"] == 1500


def test_restore_reuse_cycle_idempotent(db):
    admin.run("INSERT INTO orders VALUES('MD-B','PENDING','2026-11-10T11:00:00','c1','m1')")
    pp.spend("c1", "m1", "MD-B", 200, 300)
    assert bal()["total"] == 2500
    # PENDING 상태에서 statuses=FAILED 조건이면 되돌리지 않는다
    assert pp.restore("MD-B", "x", statuses=("FAILED",)) == 0
    # 20분 지난 미결제 → 자동 반환 (멱등)
    assert pp.release_stale("c1") == 500 and pp.release_stale("c1") == 0
    assert bal() == {"total": 3000, "reward": 1000, "point": 2000}
    # 늦게 결제완료 → 재차감, 두 번 불러도 한 번만
    admin.run("UPDATE orders SET status='PAID' WHERE order_id='MD-B'")
    assert pp.on_paid("MD-B") == 0 and pp.on_paid("MD-B") == 0
    assert bal() == {"total": 2500, "reward": 700, "point": 1800}
    # 이후 취소 → 다시 반환 (두 번째 반환도 정상 · 멱등)
    admin.run("UPDATE orders SET status='CANCELLED' WHERE order_id='MD-B'")
    assert pp.restore("MD-B", "주문 취소") == 500 and pp.restore("MD-B", "주문 취소") == 0
    assert bal() == {"total": 3000, "reward": 1000, "point": 2000}
    u = pp.usage("MD-B")
    assert u["used"] == {"point": 200, "reward": 300} and u["net"] == {"point": 0, "reward": 0}


def test_restore_without_order_row(db):
    pp.spend("c1", "m1", "MD-GONE", 0, 400)          # 주문 생성 실패(롤백) 경우
    assert pp.restore("MD-GONE", "주문 생성 실패") == 400
    assert bal()["reward"] == 1000


def test_check_use_limits(db):
    # 기타 할인 대상 상품 금액까지만
    with pytest.raises(HTTPException):
        pp.check_use(0, 16000, 0, 15600, 15600, 3000, 0)
    with pytest.raises(HTTPException):
        pp.check_use(100, 0, 0, 15600, 15600, 3000, 0)          # 적립금 불가 상품
    assert pp.check_use(0, 15600, 0, 15600, 15600, 3000, 0) == 15600   # 배송비 3,000원은 결제
    with pytest.raises(HTTPException) as e:                            # 무료배송 전액 포인트 → 최소 결제금액
        pp.check_use(30000, 0, 30000, 0, 30000, 0, 0)
    assert "최소 결제금액" in e.value.detail
    assert pp.check_use(29900, 0, 30000, 0, 30000, 0, 0) == 29900
    with pytest.raises(HTTPException):                                 # 쿠폰 할인 후 금액 초과
        pp.check_use(0, 20000, 0, 20000, 20000, 3000, 1000)


def test_requested_parsing():
    assert pp.requested({"points": {"point": "1,000", "reward": -5}}) == (0, 0)
    assert pp.requested({"points": {"point": 1000, "reward": 300}}) == (1000, 300)
    assert pp.requested({"points": None}) == (0, 0)
    assert pp.requested({}) == (0, 0)


def test_checkout_inject_idempotent():
    html = ("<div class=\"sum-row total\">x</div><script>var _off=0;window.mpCpApplied=_off;return {sub,ship,total:sub+ship-_off,drop,pts};"
            "xhr.send(JSON.stringify({coupon:(window.mpCpCode||''),lang:1}))</script></body>")
    out = pp.checkout_apply(html, "/checkout")
    assert 'id="mpPt"' in out and "mpPtJs" in out and "points:(window.mpPtReq" in out and "total:sub+ship-_off-_pu,drop,pts:_pe" in out
    assert pp.checkout_apply(out, "/checkout") == out
    assert pp.checkout_apply(html, "/cart") == html
    assert pp.checkout_apply("<div>no anchors</div></body>", "/checkout") == "<div>no anchors</div></body>"
