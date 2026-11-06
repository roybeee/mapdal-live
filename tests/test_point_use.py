"""결제 시 포인트 사용 — [할인 적용 항목] 설정 · 주문 차감 · 취소 복원 · 재주문 해제 (실제 /api/orders, SQLite)."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin
import app as appmod

BUYER = {"name": "테스트", "phone": "010-1234-5678", "zip": "04790", "addr1": "서울 성동구", "email": "t@example.com"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "SQLITE_PATH", str(tmp_path / "mapdal.db"))
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "VBANK_EXPIRE_SWEEP", 0)
    monkeypatch.setattr(admin, "catalog_inventory_from_legacy", lambda pid: None)
    monkeypatch.setattr(admin, "order_notify_async", lambda *a, **k: None)
    monkeypatch.setattr(admin, "order_geo_capture", lambda *a, **k: None)
    monkeypatch.setitem(admin._state, "ready", False)
    appmod._migrate_columns()
    appmod.seed()
    appmod._migrate_columns()
    admin.ensure_ready()
    now = "2026-11-06T09:00:00"
    admin.run("INSERT INTO customer_profiles(id,customer_no,points_balance,created_at,updated_at,status) VALUES('c1','CUS-1',0,?,?,'ACTIVE')", (now, now))
    admin.run("INSERT INTO members(id,email,status,customer_id,points) VALUES('m1','t@example.com','ACTIVE','c1',0)")
    admin.run("INSERT INTO member_sessions(id,member_id,expires) VALUES(?,?,?)",
              (hashlib.sha256(b"sid").hexdigest(), "m1", "2999-01-01"))
    for pid, name, extra in (("mp::draw", "럭키드로우", {"reward_ok": 1}), ("mp::plain", "일반 음반", {}),
                             ("mp::both", "둘 다", {"reward_ok": 1, "point_ok": 1}), ("mp::pt", "포인트만", {"point_ok": 1})):
        cols = ["id", "name", "price", "stock", "soldout"] + list(extra)
        vals = [pid, name, 20000, 10, 0] + list(extra.values())
        admin.run("INSERT INTO products(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), tuple(vals))
    admin.point_apply("c1", "m1", "SIGNUP_BONUS", 2000, "signup:c1")
    admin.point_apply("c1", "m1", "COMPENSATION", 770, "comp:x:MD-OLD", order_id="MD-OLD")
    c = TestClient(appmod.app, base_url="https://testserver")
    c.cookies.set("mp_member", "sid")
    yield c


def order(c, pid="mp::draw", points=770, ship="standard", q=1, **kw):
    return c.post("/api/orders", json=dict({"items": [{"id": pid, "q": q}], "buyer": BUYER, "shipMethod": ship, "points": points}, **kw))


def bal():
    return admin.one("SELECT points_balance b FROM customer_profiles WHERE id='c1'")["b"]


def test_defaults_are_off(env):
    r = admin.product_rules([{"id": "mp::plain"}])["mp::plain"]
    assert r["point_ok"] is False and r["reward_ok"] is False
    assert order(env, "mp::plain", 100).status_code == 400
    assert bal() == 2770
    d = env.post("/api/cart/points", json={"items": [{"id": "mp::plain"}]}).json()
    assert d["usable"] == 0 and d["login"] is True


def test_reward_only_product_uses_only_reward_pool(env):
    d = env.post("/api/cart/points", json={"items": [{"id": "mp::draw"}]}).json()
    assert (d["reward"], d["general"], d["usable"], d["reward_ok"], d["point_ok"]) == (770, 2000, 770, True, False)
    r = order(env, points=771)
    assert r.status_code == 400 and "사용 가능 770P" in r.json()["detail"]
    assert admin.one("SELECT stock FROM products WHERE id='mp::draw'")["stock"] == 10   # 롤백
    r = order(env, points=770)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["amount"] == 20000 + 3000 - 770
    assert d["pointUsed"] == 770 and d["inicis"]["price"] == str(d["amount"])
    assert bal() == 2000
    o = admin.one("SELECT point_used, discount, status FROM orders WHERE order_id=?", (d["orderId"],))
    assert o["point_used"] == "770" and o["discount"] == "770" and o["status"] == "PENDING"
    led = admin.rows("SELECT event_type, amount, balance_after FROM point_ledger WHERE order_id=?", (d["orderId"],))
    assert [(x["event_type"], x["amount"], x["balance_after"]) for x in led] == [("REWARD_USE", -770, 2000)]
    assert admin.one("SELECT points FROM members WHERE id='m1'")["points"] == 2000


def test_point_only_and_both(env):
    assert order(env, "mp::pt", 2001).status_code == 400                 # 일반 풀 2,000 초과 (보상 770 은 못 씀)
    assert order(env, "mp::pt", 2000).status_code == 200
    assert bal() == 770
    d = env.post("/api/cart/points", json={"items": [{"id": "mp::both"}]}).json()
    assert d["usable"] == 2770                                            # 미결제 해제분 포함 안내
    r = order(env, "mp::both", 2770)                                      # 재주문 → 앞 주문 해제 후 보상 우선 차감
    assert r.status_code == 200, r.text
    led = admin.rows("SELECT event_type, amount FROM point_ledger WHERE order_id=? ORDER BY amount", (r.json()["orderId"],))
    assert sorted((x["event_type"], x["amount"]) for x in led) == [("POINT_USE", -2000), ("REWARD_USE", -770)]
    assert bal() == 0


def test_mixed_cart_blocked(env):
    r = env.post("/api/orders", json={"items": [{"id": "mp::draw", "q": 1}, {"id": "mp::plain", "q": 1}], "buyer": BUYER,
                                      "shipMethod": "standard", "points": 100})
    assert r.status_code == 400 and "사용할 수 없는" in r.json()["detail"]
    assert bal() == 2770


def test_cap_keeps_minimum_payment(env):
    admin.run("UPDATE products SET price=1500 WHERE id='mp::both'")
    assert order(env, "mp::both", 501, ship="pickup").status_code == 400   # 픽업 1,500원 → 최소 1,000원 남김
    r = order(env, "mp::both", 500, ship="pickup")
    assert r.status_code == 200 and r.json()["amount"] == 1000
    assert order(env, "mp::both", 1501).status_code == 400                # 배송 주문도 상품 금액까지만 (배송비 제외)
    assert bal() == 2770 - 500 + 500                                        # 재주문 시도가 앞 주문 보류를 해제


def test_invalid_input_rejected(env):
    for v in ("abc", -5, 1.5, "1.5", True):
        assert order(env, points=v).status_code == 400, v
    assert bal() == 2770


def test_guest_cannot_use(env, monkeypatch):
    env.cookies.delete("mp_member")
    monkeypatch.setattr(admin, "guest_checkout_allowed", lambda ch="domestic": True)
    assert order(env, points=100).status_code == 400


def test_cancel_restores_and_is_idempotent(env):
    oid = order(env).json()["orderId"]
    r = admin.one("SELECT * FROM orders WHERE order_id=?", (oid,))
    res = admin._order_cancel_core({"name": "t", "role": "OWNER"}, r, "테스트")
    assert res["points_restored"] == 770 and bal() == 2770
    assert admin.point_restore_order(oid) == 0 and bal() == 2770
    d = env.post("/api/cart/points", json={"items": [{"id": "mp::draw"}]}).json()
    assert d["reward"] == 770                                              # 보상 풀로 돌아옴


def test_release_then_paid_resettles(env):
    o1 = order(env, points=500).json()["orderId"]
    assert bal() == 2270
    o2 = order(env, points=770)                                            # 재시도 → o1 해제
    assert o2.status_code == 200 and bal() == 2000
    admin.run("UPDATE orders SET status='PAID', paid_at='2026-11-06T10:00:00' WHERE order_id=?", (o1,))
    appmod._award_purchase_points(o1)                                      # 뒤늦게 결제됨 → 다시 차감 시도
    led = admin.rows("SELECT event_type, amount FROM point_ledger WHERE order_id=?", (o1,))
    assert sum(x["amount"] for x in led if x["event_type"].startswith("REWARD")) == -500
    appmod._award_purchase_points(o1)
    assert sum(x["amount"] for x in admin.rows("SELECT amount FROM point_ledger WHERE order_id=? AND event_type LIKE 'REWARD%'", (o1,))) == -500


def test_second_window_cannot_reuse_points(env):
    o1 = order(env, points=770).json()["orderId"]
    o2 = order(env, points=770).json()["orderId"]                          # o1 해제 → o2 가 잡음
    admin.point_apply("c1", "m1", "ADMIN_ADJUST", -2000, "adj:1", "잔액 소진")
    assert bal() == 0
    assert appmod._point_preapprove(o2, admin.one("SELECT * FROM orders WHERE order_id=?", (o2,))) is True
    assert appmod._point_preapprove(o1, admin.one("SELECT * FROM orders WHERE order_id=?", (o1,))) is False
    assert admin.one("SELECT status FROM orders WHERE order_id=?", (o1,))["status"] == "FAILED"
    assert bal() == 0


def test_no_points_order_skips_preapprove(env):
    r = order(env, "mp::plain", 0)
    assert r.status_code == 200
    oid = r.json()["orderId"]
    assert appmod._point_preapprove(oid, admin.one("SELECT * FROM orders WHERE order_id=?", (oid,))) is True
    assert admin.rows("SELECT id FROM point_ledger WHERE order_id=?", (oid,)) == []


def test_purchase_reward_excludes_points_and_coupon_ship_calc(env):
    oid = order(env, "mp::both", 1000).json()["orderId"]
    admin.run("UPDATE orders SET status='PAID' WHERE order_id=?", (oid,))
    appmod._award_purchase_points(oid)
    pr = admin.one("SELECT amount FROM point_ledger WHERE event_key=?", ("purchase:" + oid,))
    assert pr["amount"] == (20000 - 1000) // 100
    o = admin.one("SELECT amount, discount, items FROM orders WHERE order_id=?", (oid,))
    assert int(o["amount"]) - 20000 + int(o["discount"]) == 3000            # shipfee 의 기납부 배송비 계산이 맞다


def test_drop_flags(env, monkeypatch):
    monkeypatch.setattr(admin, "_drops_all", lambda: [{"id": 1, "options": [{"name": "A", "product_id": "mpd::1", "price": 1000}],
                                                       "reward_ok": True, "point_ok": False}])
    assert admin._drop_disc_index()["mpd::1"] == (False, True)
    assert admin.product_rules([{"id": "mp::plain"}])["mp::plain"]["reward_ok"] is False


def test_checkout_page_has_ui(env):
    r = env.get("/checkout")
    assert r.status_code == 200
    assert 'id="mpPt"' in r.text and "points:(window.mpPtUse||0)" in r.text and "window.mpPtUse=_pt" in r.text
