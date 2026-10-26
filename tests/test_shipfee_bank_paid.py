"""추가 배송비 — 무통장입금 완료 처리 (2026-10-26)."""
import datetime
import json
import sqlite3
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
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
    monkeypatch.setattr(shipfee, "_actor", lambda req, lvl, what: {"name": "관리자", "role": "MANAGER"})
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setattr(shipfee, "conf", lambda: {"extra": {"jeju": 3000, "remote": 5000}, "deadline": "2026-10-31T23:59"})

    class _C:
        def exec(self, sql, args=()):
            admin.run(sql, args)

    @contextmanager
    def _db():
        yield _C()

    stub = types.SimpleNamespace(
        kst_naive=lambda: datetime.datetime(2026, 10, 26, 13, 0),
        kst_iso=lambda: "2026-10-26T13:00:00", db=_db, _pay_log=lambda *a: None)
    monkeypatch.setattr(shipfee, "_app", lambda: stub)
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, amount INTEGER, paid_at TEXT, created TEXT,
              pay_method TEXT, receipt_url TEXT, buyer TEXT, fulfill TEXT, customer_id TEXT, member_id TEXT,
              ship_method TEXT, contact_phone_norm TEXT, admin_memo TEXT);
        """)
        db.executemany("INSERT INTO orders(order_id,status,items,amount,created,buyer) VALUES(?,?,?,?,?,?)", [
            ("MD-20261019-AAAAAA", "PAID", "[]", 93600, "2026-10-19T18:57:52", json.dumps({"zip": "04700", "addr1": "서울"})),
            ("MD-20261019-BBBBBB", "PAID", "[]", 93600, "2026-10-19T18:57:52", json.dumps({"zip": "04701", "addr1": "서울"})),
        ])
    shipfee.ensure()
    for cid, oid, st in (("ch1", "MD-20261019-AAAAAA", "OPEN"), ("ch2", "MD-20261019-BBBBBB", "WAIVED")):
        admin.run("INSERT INTO ship_charges(id, token, phone, name, order_ids, amount, status, created) VALUES(?,?,?,?,?,?,?,?)",
                  (cid, cid + "tok1234567890abcd", "01012345678", "박고객", json.dumps([oid]), 3000, st, "2026-10-20T09:00:00"))
    app = FastAPI(); app.include_router(shipfee.shipfee_router)
    with TestClient(app) as c:
        yield c


def _row(cid):
    return next(r for r in shipfee._list_rows() if r["id"] == cid)


def test_bank_paid_marks_charge_paid(client):
    assert _row("ch1")["state"] == "UNPAID"
    d = client.post("/admin/api/shipfee/bank-paid", json={"id": "ch1", "payer": "박규현", "note": "10/21 입금"}).json()
    assert d["ok"] and d["amount"] == 3000 and d["order_id"].startswith("SF-")
    r = _row("ch1")
    assert r["state"] == "PAID" and r["paid_method"] == "무통장입금" and r["paid_order"] == d["order_id"]
    assert "박규현" in r["note"]
    links = shipfee.links_for_order("MD-20261019-AAAAAA")
    assert links["state"] == "PAID" and links["payments"][0]["method"] == "무통장입금"
    # 두 번 누르면 거절
    assert client.post("/admin/api/shipfee/bank-paid", json={"id": "ch1"}).status_code == 409


def test_waived_can_be_converted(client):
    assert _row("ch2")["state"] == "WAIVED"
    assert client.post("/admin/api/shipfee/bank-paid", json={"id": "ch2", "amount": "3,000"}).status_code == 200
    assert _row("ch2")["state"] == "PAID"


def test_rejects_bad_amount(client):
    assert client.post("/admin/api/shipfee/bank-paid", json={"id": "ch1", "amount": "0"}).status_code == 400
    assert client.post("/admin/api/shipfee/bank-paid", json={"id": "ch1", "amount": "abc"}).status_code == 400


def test_original_cancel_flags_manual_refund(client):
    client.post("/admin/api/shipfee/bank-paid", json={"id": "ch1"})
    admin.run("UPDATE orders SET status='CANCELLED' WHERE order_id='MD-20261019-AAAAAA'")
    res = shipfee.on_order_cancelled({"name": "관리자", "role": "MANAGER"}, "MD-20261019-AAAAAA")
    assert res["action"] == "refunded" and res["orders"] == []
    assert "무통장입금 배송비 환불 필요" in _row("ch1")["note"]
