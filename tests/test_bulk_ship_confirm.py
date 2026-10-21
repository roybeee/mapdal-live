"""일괄 발송완료 → 고객 구매확정 — 대상 판정, 일괄 처리, 구매확정 후 반품·교환 차단."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin

ISK = json.dumps([{"id": "mpd::x", "n": "이세계아이돌 [Across Dimensions]", "p": 1000, "q": 1}], ensure_ascii=False)
ETC = json.dumps([{"id": "mp::y", "n": "에이티즈", "p": 1000, "q": 1}], ensure_ascii=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(admin, "order_notify_async", lambda oid, ev: sent.append((oid, ev)))
    monkeypatch.setitem(admin._state, "ocols", {"order_id", "status", "items", "fulfill", "shipped_at", "confirmed_at", "buyer", "created", "country"})
    me = {"id": "m1", "customer_id": "c1", "name": "회원", "status": "ACTIVE"}
    monkeypatch.setattr(admin, "member_required", lambda req: me)
    monkeypatch.setattr(admin, "_own_order", lambda m, oid: admin.one("SELECT * FROM orders WHERE order_id=?", (oid,)))
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, fulfill TEXT, shipped_at TEXT,
              confirmed_at TEXT, buyer TEXT, created TEXT, country TEXT, customer_id TEXT);
            CREATE TABLE member_requests(id TEXT, member_id TEXT, order_id TEXT, rtype TEXT, reason TEXT, created TEXT,
              status TEXT, admin_memo TEXT, updated TEXT, customer_id TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        rows = [("MD-20261021-AAAAAA", "PAID", ISK, "NEW"), ("MD-20261021-BBBBBB", "PAID", ISK, "PREPARING"),
                ("MD-20261021-CCCCCC", "PAID", ISK, "SHIPPED"), ("MD-20261021-DDDDDD", "CANCELLED", ISK, "CANCELLED"),
                ("MD-20261021-EEEEEE", "WAITING_DEPOSIT", ISK, "NEW"), ("MD-20261021-FFFFFF", "PAID", ETC, "NEW")]
        for oid, st, it, ff in rows:
            db.execute("INSERT INTO orders VALUES(?,?,?,?,'','','{\"name\":\"고객\"}','2026-10-20T10:00:00','KR','c1')", (oid, st, it, ff))
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        c.sent = sent
        yield c


def ff(oid):
    return admin.one("SELECT fulfill, shipped_at, confirmed_at FROM orders WHERE order_id=?", (oid,))


def test_candidates_by_item_keyword(client):
    d = client.get("/admin/api/ship-candidates", params={"item": "Across Dimensions"}).json()
    assert d["ids"] == ["MD-20261021-AAAAAA", "MD-20261021-BBBBBB"]


def test_bulk_ship_then_customer_confirm(client):
    ids = "MD-20261021-AAAAAA\nMD-20261021-BBBBBB MD-20261021-CCCCCC,MD-20261021-DDDDDD MD-20261021-EEEEEE"
    pv = client.post("/admin/api/orders/bulk-ship", json={"order_ids": ids, "dry_run": True}).json()
    assert pv["ok_count"] == 2 and ff("MD-20261021-AAAAAA")["fulfill"] == "NEW"
    why = {r["order_id"]: r["why"] for r in pv["rows"]}
    assert "이미 발송" in why["MD-20261021-CCCCCC"] and "결제완료" in why["MD-20261021-EEEEEE"]
    d = client.post("/admin/api/orders/bulk-ship", json={"order_ids": ids, "notify": True}).json()
    assert d["ok_count"] == 2
    assert ff("MD-20261021-AAAAAA")["fulfill"] == "SHIPPED" and ff("MD-20261021-AAAAAA")["shipped_at"]
    assert sorted(client.sent) == [("MD-20261021-AAAAAA", "shipped"), ("MD-20261021-BBBBBB", "shipped")]
    assert client.post("/admin/api/orders/bulk-ship", json={"order_ids": ids}).json()["ok_count"] == 0   # 재실행 무해

    # 고객: 발송된 주문에 구매확정 버튼 → 확정 → 배송완료·반품 불가
    det = client.get("/api/member/orders/MD-20261021-AAAAAA").json()
    assert det["can_confirm"] and det["can_return"]
    assert client.post("/api/member/orders/MD-20261021-FFFFFF/confirm").status_code == 400            # 미발송
    assert client.post("/api/member/orders/MD-20261021-AAAAAA/confirm").status_code == 200
    o = ff("MD-20261021-AAAAAA"); assert o["fulfill"] == "DONE" and o["confirmed_at"]
    det = client.get("/api/member/orders/MD-20261021-AAAAAA").json()
    assert det["status_kr"] == "구매확정" and not det["can_confirm"] and not det["can_return"]
    assert client.post("/api/member/orders/MD-20261021-AAAAAA/confirm").status_code == 400            # 중복
    r = client.post("/api/member/orders/MD-20261021-AAAAAA/request", json={"rtype": "return", "reason": "x"})
    assert r.status_code == 400 and "구매확정" in r.json()["detail"]


def test_open_return_request_blocks_confirm(client):
    client.post("/admin/api/orders/bulk-ship", json={"order_ids": ["MD-20261021-BBBBBB"]})
    assert client.post("/api/member/orders/MD-20261021-BBBBBB/request", json={"rtype": "exchange", "reason": "사이즈"}).status_code == 200
    r = client.post("/api/member/orders/MD-20261021-BBBBBB/confirm")
    assert r.status_code == 400 and "반품·교환" in r.json()["detail"]


def _set_buyer(oid, name, phone):
    admin.run("UPDATE orders SET buyer=? WHERE order_id=?", (json.dumps({"name": name, "phone": phone}, ensure_ascii=False), oid))


def test_tracking_bulk_by_order_id_and_name(client, monkeypatch):
    admin._state["ocols"] = set(admin._state["ocols"]) | {"tracking", "courier"}
    admin.run("ALTER TABLE orders ADD COLUMN tracking TEXT")
    admin.run("ALTER TABLE orders ADD COLUMN courier TEXT")
    _set_buyer("MD-20261021-AAAAAA", "김용한", "010-9461-4233")
    _set_buyer("MD-20261021-BBBBBB", "홍길동", "010-1111-2222")
    _set_buyer("MD-20261021-FFFFFF", "홍길동", "010-1111-2222")          # 같은 고객 2건
    _set_buyer("MD-20261021-CCCCCC", "이몽룡", "010-3333-4444")          # 이미 발송·구매확정
    admin.run("UPDATE orders SET fulfill='DONE', confirmed_at='2026-11-08T10:00:00' WHERE order_id='MD-20261021-CCCCCC'")
    rows = [{"order_id": "MD-20261021-AAAAAA", "tracking": "6012-3456-7890", "courier": "CJ대한통운"},
            {"names": ["홍길동"], "phone": "01011112222", "tracking": "222233334444", "courier": "롯데"},
            {"names": ["이몽룡"], "phone": "010-3333-4444", "tracking": "999"},
            {"names": ["없는사람"], "tracking": "1"},
            {"order_id": "MD-20261021-DDDDDD", "tracking": "2"},
            {"names": ["김용한"], "tracking": ""}]
    pv = client.post("/admin/api/orders/tracking-bulk", json={"rows": rows, "dry_run": True, "set_shipped": True}).json()
    res = {r["row"]: r for r in pv["rows"]}
    assert res[1]["ok"] and res[3]["ok"] and not res[2]["ok"] and "2건" in res[2]["why"]
    assert not res[4]["ok"] and not res[5]["ok"] and "송장번호 없음" in res[6]["why"]
    assert pv["orders"] == 2
    d = client.post("/admin/api/orders/tracking-bulk", json={"rows": rows, "set_shipped": True, "notify": True}).json()
    assert d["orders"] == 2 and d["shipped"] == 1
    a = admin.one("SELECT fulfill, tracking, courier FROM orders WHERE order_id='MD-20261021-AAAAAA'")
    assert a == {"fulfill": "SHIPPED", "tracking": "6012-3456-7890", "courier": "cj"}
    c = admin.one("SELECT fulfill, tracking, confirmed_at FROM orders WHERE order_id='MD-20261021-CCCCCC'")
    assert c["fulfill"] == "DONE" and c["tracking"] == "999" and c["confirmed_at"]      # 구매확정 유지
    # 같은 고객 전 주문 동일 송장 옵션
    d = client.post("/admin/api/orders/tracking-bulk", json={"rows": rows[1:2], "multi": "all", "set_shipped": True}).json()
    assert d["orders"] == 2
    assert admin.one("SELECT tracking FROM orders WHERE order_id='MD-20261021-FFFFFF'")["tracking"] == "222233334444"
    assert admin.one("SELECT courier FROM orders WHERE order_id='MD-20261021-FFFFFF'")["courier"] == "lotte"
