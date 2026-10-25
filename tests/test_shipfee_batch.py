"""추가 배송비 청구 묶음(batch) — 복구주문 22건 추가 청구 · 묶음별 조회/발송/엑셀."""
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

R1, R2 = shipfee.RESTORED_ORDERS[3], shipfee.RESTORED_ORDERS[9]      # 문서율 · 백성현


def _order(oid, phone, name, zip_, amount, price, ship='standard', status='PAID', created='2026-10-19T20:45:00'):
    b = {"name": name, "phone": phone, "zip": zip_, "addr1": "서울 " + zip_, "addr2": "1"}
    it = [{"id": "mpo::AD", "n": "이세계아이돌 [Across Dimensions]", "p": price, "q": 1}]
    return (oid, status, json.dumps(it, ensure_ascii=False), amount, created, created, "VBank", "",
            json.dumps(b, ensure_ascii=False), "NEW", None, ship, phone.replace('-', ''), None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setitem(shipfee._READY, "ok", False)
    monkeypatch.setattr(admin, "get_actor", lambda req: {"name": "tester", "role": "OWNER"})
    monkeypatch.setattr(admin, "need", lambda a, lvl, what: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setattr(admin, "product_rules", lambda items: {"mpo::AD": {"ship_required": True}})
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, amount INTEGER, paid_at TEXT, created TEXT,
              pay_method TEXT, receipt_url TEXT, buyer TEXT, fulfill TEXT, customer_id TEXT, ship_method TEXT,
              contact_phone_norm TEXT, discount TEXT);
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
        """)
        db.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            _order(R1, "010-8434-7777", "문서율", "02585", 93600, 93600),
            _order(R2, "010-3702-0396", "백성현", "07579", 93600, 93600),
            _order("MD-20261019-OTHER1", "010-1111-2222", "타고객", "11111", 50400, 50400),      # 목록 밖 → 대상 아님
            _order("MD-20261019-OLD001", "010-9999-0000", "기청구", "22222", 50400, 50400),
        ])
    shipfee.ensure()
    admin.run("INSERT INTO ship_charges(id, token, phone, name, order_ids, amount, status, created, sent_count) "
              "VALUES(?,?,?,?,?,?,?,?,1)", ("old1", "tokOLD1234567890abcd", "01099990000", "기청구",
                                            json.dumps(["MD-20261019-OLD001"]), 3000, "OPEN", "2026-10-20T20:00:00"))
    app = FastAPI(); app.include_router(shipfee.shipfee_router)
    with TestClient(app) as c:
        yield c


def test_seed_creates_only_restored_and_is_idempotent(client):
    res = shipfee.seed_restored()
    assert res["customers"] == 2 and res["amount"] == 6000 and res["batch"] == shipfee.RESTORED_BATCH
    assert shipfee.seed_restored() is None                                  # 1회만
    rows = client.get("/admin/api/shipfee/list").json()
    names = {r["name"]: r for r in rows["rows"]}
    assert set(names) == {"문서율", "백성현", "기청구"}                      # OTHER1 은 목록 밖
    assert names["문서율"]["batch"] == shipfee.RESTORED_BATCH and names["기청구"]["batch"] == shipfee.LEGACY_BATCH
    bt = {b["name"]: b for b in rows["batches"]}
    assert bt[shipfee.RESTORED_BATCH]["n"] == 2 and bt[shipfee.RESTORED_BATCH]["unsent"] == 2
    assert [b["name"] for b in rows["batches"]] == [shipfee.LEGACY_BATCH, shipfee.RESTORED_BATCH]


def test_seed_tags_charge_already_created_by_admin(client):
    d = client.post("/admin/api/shipfee/generate", json={"dry_run": False, "batch": "수동"}).json()
    assert d["customers"] == 3 and d["batch"] == "수동"                      # 관리자가 먼저 전체 생성
    shipfee.seed_restored()
    rs = client.get("/admin/api/shipfee/list").json()["rows"]
    assert len(rs) == 4                                                      # 중복 청구 없음
    by = {r["name"]: r["batch"] for r in rs}
    assert by["문서율"] == by["백성현"] == shipfee.RESTORED_BATCH and by["타고객"] == "수동"


def test_send_and_export_filtered_by_batch(client):
    shipfee.seed_restored()
    pv = client.post("/admin/api/shipfee/send", json={"scope": "unpaid", "preview": True, "channel": "sms",
                                                      "batch": shipfee.RESTORED_BATCH}).json()
    assert pv["count"] == 2
    allpv = client.post("/admin/api/shipfee/send", json={"scope": "unpaid", "preview": True, "channel": "sms"}).json()
    assert allpv["count"] == 3
    csv = client.get("/admin/api/shipfee.csv", params={"batch": shipfee.RESTORED_BATCH}).text
    assert "문서율" in csv and "기청구" not in csv
    assert client.get("/admin/api/shipfee.xlsx", params={"batch": shipfee.LEGACY_BATCH}).status_code == 200
