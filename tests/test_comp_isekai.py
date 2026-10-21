"""이세계아이돌 예약판매 보상 적립 1% — 기준 시각·대상·금액·멱등·자동 1회·취소 회수."""
import datetime
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

ISK = {"id": "mpd::493957de1c1f", "n": "이세계아이돌 [Across Dimensions] STAYG ver.", "p": 20000, "q": 2}
OTHER = {"id": "mp::other", "n": "에이티즈 앨범", "p": 20000, "q": 1}
CLOCK = {"now": datetime.datetime(2026, 11, 8, 12, 0)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setattr(admin, "kst_now", lambda: CLOCK["now"])
    CLOCK["now"] = datetime.datetime(2026, 11, 8, 12, 0)
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, status TEXT, items TEXT, discount INTEGER, paid_at TEXT,
              created TEXT, customer_id TEXT, member_id TEXT, buyer TEXT, amount INTEGER);
            CREATE TABLE customer_profiles(id TEXT PRIMARY KEY, points_balance INTEGER, updated_at TEXT);
            CREATE TABLE members(id TEXT, customer_id TEXT, points INTEGER);
            CREATE TABLE point_ledger(id TEXT, customer_id TEXT, member_id TEXT, event_type TEXT, amount INTEGER,
              balance_after INTEGER, event_key TEXT UNIQUE, order_id TEXT, reason TEXT, expires_at TEXT, created_at TEXT, by_admin TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        for c in ("c1", "c2", "c3", "c4", "c5", "c6"):
            db.execute("INSERT INTO customer_profiles VALUES(?,?,'')", (c, 500))
        rows = [
            ("MD-1", "PAID", [ISK], 0, "2026-10-20T10:00:00", "c1", "m1"),                 # 40,000 → 400
            ("MD-2", "PAID", [ISK, OTHER], 6000, "2026-11-09T08:59:59", "c2", "m2"),        # 40,000 − 4,000 → 360
            ("MD-3", "PAID", [ISK], 0, "2026-11-09T09:00:01", "c3", "m3"),                  # 기준 이후 결제
            ("MD-4", "CANCELLED", [ISK], 0, "2026-10-20T10:00:00", "c4", "m4"),             # 취소
            ("MD-5", "PAID", [OTHER], 0, "2026-10-20T10:00:00", "c5", "m5"),                # 다른 상품
            ("MD-6", "PAID", [dict(ISK, q=1)], 0, "2026-10-21T10:00:00", "c6", ""),         # 비회원 200
            ("SF-1", "PAID", [dict(ISK, n="추가 배송비 Across Dimensions")], 0, "2026-10-21T10:00:00", "c1", "m1"),
        ]
        for oid, st, it, dc, pa, cid, mid in rows:
            db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (oid, st, json.dumps(it, ensure_ascii=False), dc, pa, pa, cid, mid, '{"name":"고객"}', 0))
        # 기존 구매 적립이 이미 있는 주문 — 보상 적립은 별개로 추가 지급돼야 한다
        db.execute("INSERT INTO point_ledger VALUES('x','c1','m1','PURCHASE_REWARD',400,900,'purchase:MD-1','MD-1','','','','')")
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        yield c


def bal(cid):
    return admin.one("SELECT points_balance FROM customer_profiles WHERE id=?", (cid,))["points_balance"]




def test_full_flow(client):
    t = {x["order_id"]: x for x in admin.comp_isekai_targets()}
    assert set(t) == {"MD-1", "MD-2", "MD-6"}
    assert t["MD-1"]["points"] == 400 and t["MD-2"]["base"] == 36000 and t["MD-2"]["points"] == 360
    assert t["MD-6"]["guest"] and t["MD-6"]["points"] == 200

    # 기준 시각 전: 자동·수동 모두 지급하지 않음
    admin.comp_isekai_tick()
    assert bal("c1") == 500
    assert client.post("/admin/api/comp/isekai/run", json={"confirm": "지급"}).status_code == 400
    pv = client.get("/admin/api/comp/isekai").json()
    assert pv["result"] is None and pv["preview"]["orders"] == 2      # 지금(11/8 12시)까지 결제된 건

    # 기준 시각 이후 스윕 → 1회 자동 지급
    CLOCK["now"] = datetime.datetime(2026, 11, 9, 9, 5)
    admin.comp_isekai_tick()
    assert bal("c1") == 900 and bal("c2") == 860 and bal("c3") == 500 and bal("c4") == 500 and bal("c6") == 700
    led = admin.rows("SELECT event_type, order_id FROM point_ledger WHERE customer_id='c1' ORDER BY event_type")
    assert [r["event_type"] for r in led] == ["COMPENSATION", "PURCHASE_REWARD"]   # 기존 적립과 별개
    res = client.get("/admin/api/comp/isekai").json()["result"]
    assert res["granted"] == 3 and res["points"] == 960 and res["guests"] == 1

    # 다시 돌아도(자동·수동) 중복 지급 없음
    admin.comp_isekai_tick()
    assert client.post("/admin/api/comp/isekai/run", json={"confirm": "지급"}).json()["granted"] == 3
    assert bal("c1") == 900 and bal("c2") == 860

    # 지급 후 취소되면 보상 적립 회수 (구매 적립 회수와 함께)
    admin.point_revoke_purchase("MD-1")
    assert bal("c1") == 100
    admin.point_revoke_purchase("MD-1")
    assert bal("c1") == 100

    csv = client.get("/admin/api/comp/isekai.csv").text
    assert "MD-2" in csv and "MD-3" not in csv and "비회원" in csv
