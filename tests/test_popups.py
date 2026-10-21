"""사이트 공지 팝업 — 노출 위치·기간 판단, 저장 검증, 1회 시드, 페이지 주입."""
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

NOW = datetime.datetime(2026, 10, 22, 12, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "kst_now", lambda: NOW)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    monkeypatch.setitem(admin._state, "pname", "name")
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
            CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"viewer").hexdigest(), "v", "9999-12-31"))
        db.execute("INSERT INTO admins VALUES('v','조회','VIEWER',1,'v')")
        db.execute("INSERT INTO products VALUES('mp::isekai','이세계아이돌 The 2nd Mini Album (STAYG ver.)')")
        db.execute("INSERT INTO products VALUES('mp::other','에이티즈 앨범')")
        db.execute("INSERT INTO site_settings VALUES('drops',?,'','')", (json.dumps([{"id": 6, "title": "이세계"}, {"id": 4, "title": "알파"}]),))
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        yield c


def shown(c, path, **q):
    r = c.get("/api/popups", params={"path": path, **q})
    assert r.status_code == 200
    return [p["id"] for p in r.json()["popups"]]


def test_seed_targets_isekai_pages_only(client):
    admin._migrate_popup_seed()
    admin._migrate_popup_seed()                                     # 멱등
    assert [p["id"] for p in admin.popups_all()] == ["isekai-point-1021"]
    p = admin.popups_all()[0]
    assert p["title"] == "[이세계아이돌 예약판매] 포인트 적립 안내"
    assert "• 적립 혜택: 최종 구매 금액의 1% 포인트 적립" in p["body"] and p["body"].endswith("맵달SEOUL 드림")
    assert shown(client, "/home") == ["isekai-point-1021"]
    assert shown(client, "/") == ["isekai-point-1021"]
    assert shown(client, "/new-drops") == ["isekai-point-1021"]
    assert shown(client, "/new-drops", id="6") == ["isekai-point-1021"]
    assert shown(client, "/new-drops", id="4") == []                # 다른 이벤트 상세
    assert shown(client, "/p/mp::isekai") == ["isekai-point-1021"]
    assert shown(client, "/p/mp::other") == []                      # 다른 상품
    assert shown(client, "/kpop") == ["isekai-point-1021"]
    assert shown(client, "/checkout") == [] and shown(client, "/cart") == []
    # 관리자가 지운 뒤 재부팅해도 다시 만들지 않는다
    client.post("/admin/api/popups/delete", json={"id": "isekai-point-1021"})
    admin._migrate_popup_seed()
    assert admin.popups_all() == []


def test_period_and_toggle(client):
    base = {"title": "공지", "body": "내용", "targets": ["all"], "on": True}
    client.post("/admin/api/popups/save", json={**base, "id": "future", "start": "2026-10-23T00:00"})
    client.post("/admin/api/popups/save", json={**base, "id": "past", "end": "2026-10-21T23:59"})
    client.post("/admin/api/popups/save", json={**base, "id": "off", "on": False})
    client.post("/admin/api/popups/save", json={**base, "id": "now", "order": 2})
    client.post("/admin/api/popups/save", json={**base, "id": "first", "order": 1})
    assert shown(client, "/shop") == ["first", "now"]
    assert shown(client, "/order-complete") == []


def test_validation_permission_and_version(client):
    bad = client.post("/admin/api/popups/save", json={"title": "t", "body": "b", "targets": []})
    assert bad.status_code == 400
    bad = client.post("/admin/api/popups/save", json={"title": "t", "targets": ["home"], "link_url": "javascript:alert(1)"})
    assert bad.status_code == 400
    r1 = client.post("/admin/api/popups/save", json={"id": "x", "title": "t", "body": "b", "targets": ["home"], "on": True}).json()["row"]
    r2 = client.post("/admin/api/popups/save", json={**r1, "on": False}).json()["row"]
    assert r2["ver"] == r1["ver"]                                    # 켜고 끄기만 → 버전 유지
    r3 = client.post("/admin/api/popups/save", json={**r2, "body": "바뀐 내용"}).json()["row"]
    assert r3["ver"] != r1["ver"]                                    # 내용 변경 → 닫은 고객에게도 다시
    client.cookies.set("mp_sess", "viewer")
    assert client.post("/admin/api/popups/save", json={"title": "t", "targets": ["home"]}).status_code == 403


def test_loader_injected_once():
    html = "<html><head></head><body><p>x</p></body></html>"
    out = admin._inject_auth(html, "/home")
    assert out.count('id="mpPopupJs"') == 1
    assert admin._inject_auth(out, "/home").count('id="mpPopupJs"') == 1


def test_loader_on_standalone_product_page(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "growth", types.SimpleNamespace(html_apply=lambda h, p: h, body_snippet=lambda h: ""))
    out = admin._track_apply("<html><body>x</body></html>", "/p/mp::isekai")
    assert out.count('id="mpPopupJs"') == 1
