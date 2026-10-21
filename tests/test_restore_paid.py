"""입금 확인 복구 (/admin/api/orders/restore-paid) — 취소 재고복원과 정확히 대칭인지 검증."""
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

SET_ITEMS = [{"id": "mpo::set", "n": "STAR/RACE 세트", "p": 50400, "q": 3,
              "parts": [{"id": "mpo::star", "n": "STAR", "q": 3}, {"id": "mpo::race", "n": "RACE", "q": 3}]},
             {"id": "mpo::stayg", "n": "STAYG", "p": 11100, "q": 2}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setattr(admin, "catalog_inventory_from_legacy", lambda pid: None)
    sent = []
    monkeypatch.setattr(admin, "order_notify_async", lambda oid, ev: sent.append((oid, ev)))
    monkeypatch.setitem(admin._state, "ocols", {"order_id", "status", "items", "fulfill", "admin_memo", "paid_at", "pay_method", "pay_log", "buyer", "amount"})
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT, stock INTEGER, soldout INTEGER DEFAULT 0);
            CREATE TABLE product_variants(legacy_product_id TEXT, sale_status TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, fulfill TEXT, admin_memo TEXT,
              paid_at TEXT, pay_method TEXT, pay_log TEXT, buyer TEXT, amount INTEGER, customer_id TEXT, member_id TEXT);
            CREATE TABLE mp_coupon_uses(order_id TEXT, code TEXT, created TEXT, amount_off INTEGER, status TEXT);
            CREATE TABLE mp_coupons(code TEXT PRIMARY KEY, uses_left INTEGER);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        db.executemany("INSERT INTO products VALUES(?,?,?,0)", [
            ("mpo::set", "세트", None), ("mpo::star", "STAR", 10), ("mpo::race", "RACE", 3), ("mpo::stayg", "STAYG", None)])
        log = json.dumps([{"c": "VBANK_ISSUED", "m": "x"}, {"c": "CANCELLED", "m": "입금기한 만료 자동취소 · 이전상태 WAITING_DEPOSIT"}])
        rows = [("MD-20261019-AAAAAA", "CANCELLED", json.dumps(SET_ITEMS), "CANCELLED", "[취소] 입금기한 만료 자동취소", "", "VBank", log, '{"name":"서지운"}', 173400),
                ("MD-20261019-BBBBBB", "CANCELLED", json.dumps(SET_ITEMS), "CANCELLED", "[취소] 수동환불(외부 처리)", "", "VBank", log, '{"name":"윤선재"}', 173400),
                ("MD-20261019-CCCCCC", "PAID", "[]", "NEW", "", "", "VBank", log, '{"name":"정상"}', 1000),
                ("MD-20261019-DDDDDD", "CANCELLED", json.dumps([{"id": "mpo::race", "q": 9}]), "CANCELLED", "", "", "VBank", log, '{"name":"재고부족"}', 1),
                ("MD-20261019-EEEEEE", "CANCELLED", "[]", "CANCELLED", "", "", "Card", "[]", '{"name":"카드"}', 1)]
        db.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,'','')", rows)
        db.execute("INSERT INTO mp_coupon_uses VALUES('MD-20261019-AAAAAA','HOME-X','',5040,'RELEASED')")
        db.execute("INSERT INTO mp_coupons VALUES('HOME-X',1)")
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        c.sent = sent
        yield c


def post(c, **b):
    return c.post("/admin/api/orders/restore-paid", json=b)


def stock(pid):
    return admin.one("SELECT stock FROM products WHERE id=?", (pid,))["stock"]


def test_preview_classifies(client):
    d = post(client, order_ids="MD-20261019-AAAAAA\nMD-20261019-BBBBBB, MD-20261019-CCCCCC MD-20261019-DDDDDD MD-20261019-EEEEEE", dry_run=True).json()
    by = {r["order_id"]: r for r in d["rows"]}
    assert by["MD-20261019-AAAAAA"]["ok"] and d["ok_count"] == 1
    assert "환불" in by["MD-20261019-BBBBBB"]["why"]
    assert "취소 상태가 아님" in by["MD-20261019-CCCCCC"]["why"]
    assert "재고 부족" in by["MD-20261019-DDDDDD"]["why"]
    assert "가상계좌" in by["MD-20261019-EEEEEE"]["why"]
    assert stock("mpo::star") == 10                                  # 미리보기는 아무것도 바꾸지 않음


def test_restore_reverses_cancel_exactly(client):
    assert post(client, order_ids=["MD-20261019-AAAAAA"]).status_code == 400   # 확인 문구 필수
    d = post(client, order_ids=["MD-20261019-AAAAAA", "MD-20261019-DDDDDD"], confirm="복구", note="CS 확인").json()
    assert d["ok_count"] == 1
    assert stock("mpo::star") == 7 and stock("mpo::race") == 0 and stock("mpo::stayg") is None
    assert admin.one("SELECT soldout FROM products WHERE id='mpo::race'")["soldout"] == 1
    o = admin.one("SELECT * FROM orders WHERE order_id='MD-20261019-AAAAAA'")
    assert o["status"] == "PAID" and o["fulfill"] == "NEW" and o["paid_at"] and o["admin_memo"].startswith("[복구]")
    assert admin.one("SELECT status FROM mp_coupon_uses WHERE order_id='MD-20261019-AAAAAA'")["status"] == "USED"
    assert admin.one("SELECT uses_left FROM mp_coupons WHERE code='HOME-X'")["uses_left"] == 0
    assert client.sent == []                                           # 알림톡은 선택했을 때만
    # 재고 부족 건은 손대지 않음
    assert admin.one("SELECT status FROM orders WHERE order_id='MD-20261019-DDDDDD'")["status"] == "CANCELLED"
    # 두 번 실행해도 이중 차감 없음
    d2 = post(client, order_ids=["MD-20261019-AAAAAA"], confirm="복구").json()
    assert d2["ok_count"] == 0 and stock("mpo::star") == 7
    # 다시 취소하면 정확히 원래 재고로 돌아온다 (취소 코어와 대칭)
    admin._cancel_order_stock("MD-20261019-AAAAAA", ["status='CANCELLED'"], [], True, expected_status="PAID")
    assert stock("mpo::star") == 10 and stock("mpo::race") == 3
