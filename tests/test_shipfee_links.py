"""원주문 ↔ 추가 배송비 결제(SF-) 연결 — 관리자 주문 상세·마이페이지 표시."""
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
import shipfee


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setitem(shipfee._READY, "ok", False)
    me = {"id": "m1", "customer_id": "c1", "name": "회원", "status": "ACTIVE"}
    monkeypatch.setattr(admin, "member_required", lambda req: me)
    monkeypatch.setattr(admin, "_own_order", lambda m, oid: admin.one("SELECT * FROM orders WHERE order_id=?", (oid,)))
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, amount INTEGER, paid_at TEXT, created TEXT,
              pay_method TEXT, receipt_url TEXT, buyer TEXT, fulfill TEXT, customer_id TEXT);
            CREATE TABLE member_requests(id TEXT, order_id TEXT, customer_id TEXT, rtype TEXT, status TEXT);
        """)
        it = lambda cid: json.dumps([{"id": "shipfee::" + cid, "n": "[추가 배송비]", "p": 3000, "q": 1}])
        db.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            ("MD-20261019-AAAAAA", "PAID", "[]", 93600, "2026-10-19T18:58:56", "2026-10-19T18:57:52", "Card", "", "{}", "NEW", "c1"),
            ("MD-20261019-BBBBBB", "PAID", "[]", 1000, "", "2026-10-19T19:00:00", "Card", "", "{}", "NEW", "c1"),
            ("MD-20261019-CCCCCC", "PAID", "[]", 1000, "", "2026-10-19T19:00:00", "Card", "", "{}", "NEW", "c1"),
            ("SF-20261021-111111", "FAILED", it("ch1"), 3000, "", "2026-10-21T10:00:00", "Card", "", "{}", "DONE", None),
            ("SF-20261021-222222", "PAID", it("ch1"), 3000, "2026-10-21T10:05:00", "2026-10-21T10:04:00", "Card", "https://r", "{}", "DONE", None),
        ])
    shipfee.ensure()
    admin.run("INSERT INTO ship_charges(id, token, phone, name, order_ids, amount, status, created) VALUES(?,?,?,?,?,?,?,?)",
              ("ch1", "tok1234567890abcdef", "010", "고객", json.dumps(["MD-20261019-AAAAAA", "MD-20261019-BBBBBB"]), 3000, "OPEN", "2026-10-20T09:00:00"))
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        yield c


def test_links_both_directions(client):
    s = shipfee.links_for_order("MD-20261019-AAAAAA")
    assert s["state"] == "PAID" and s["paid_amount"] == 3000
    assert [p["order_id"] for p in s["payments"]] == ["SF-20261021-222222"]          # 실패 시도는 숨김
    assert s["orders"] == ["MD-20261019-AAAAAA", "MD-20261019-BBBBBB"]
    back = shipfee.links_for_order("SF-20261021-222222")
    assert back["orders"] == ["MD-20261019-AAAAAA", "MD-20261019-BBBBBB"]
    assert shipfee.links_for_order("MD-20261019-CCCCCC") is None


def test_member_detail_shows_paid_shipfee(client):
    d = client.get("/api/member/orders/MD-20261019-BBBBBB").json()
    assert d["shipfee"]["payments"][0]["amount"] == 3000 and "token" not in json.dumps(d["shipfee"])
    assert client.get("/api/member/orders/MD-20261019-CCCCCC").json()["shipfee"] is None
