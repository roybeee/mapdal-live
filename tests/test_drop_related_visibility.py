"""Real router/SQLite tests; catalogue matching and product sync are isolated."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "_setting_backup", lambda key: None)
    monkeypatch.setattr(admin, "_drop_product_sync", lambda rec, prev, actor: rec["options"])
    matcher = Mock(return_value=[{"id": "album", "name": "앨범"}])
    monkeypatch.setattr(admin, "_drop_related_products", matcher)
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
        """)
        for sid, aid in [("owner-session", "__master__"), ("viewer-session", "viewer")]:
            db.execute("INSERT INTO admin_sessions VALUES(?,?,?)",
                       (hashlib.sha256(sid.encode()).hexdigest(), aid, "9999-12-31"))
        db.execute("INSERT INTO admins VALUES('viewer','조회','VIEWER',1,'viewer')")
        drops = [{"id": 1, "title": "ATEEZ 이벤트", "artist": "ATEEZ", "on": True},
                 {"id": 2, "title": "같은 아티스트 이벤트", "artist": "ATEEZ", "on": True}]
        db.execute("INSERT INTO site_settings VALUES('drops',?,'','')", (json.dumps(drops),))
    app = FastAPI()
    app.include_router(admin.admin_router)
    with TestClient(app) as test_client:
        test_client.cookies.set("mp_sess", "owner-session")
        test_client.matcher = matcher
        yield test_client


def save(client, **values):
    return client.post("/admin/api/drops/save", json={"id": 1, "title": "ATEEZ 이벤트", **values})


def stored():
    return next(row for row in admin._drops_all() if row["id"] == 1)


def test_legacy_drop_defaults_to_visible(client):
    data = client.get("/api/drops/1").json()
    assert data["show_related"] is True
    assert data["related_products"]
    assert data["related"]


def test_visibility_survives_save_and_reload_and_can_be_restored(client):
    assert save(client, show_related=False).status_code == 200
    assert stored()["show_related"] is False
    row = next(row for row in client.get("/admin/api/drops").json()["rows"] if row["id"] == 1)
    assert row["show_related"] is False
    data = client.get("/api/drops/1").json()
    assert data["show_related"] is False
    assert data["related_products"] == []
    assert data["related"] == []
    client.matcher.assert_not_called()
    assert save(client, show_related=True).status_code == 200
    assert stored()["show_related"] is True
    restored = client.get("/api/drops/1").json()
    assert restored["show_related"] is True
    assert restored["related_products"]


def test_old_editor_omitting_field_preserves_hidden_choice(client):
    assert save(client, show_related=False).status_code == 200
    assert save(client).status_code == 200
    assert stored()["show_related"] is False


def test_new_drop_defaults_to_visible(client):
    response = client.post("/admin/api/drops/save", json={"title": "신규 이벤트"})
    assert response.status_code == 200
    data = client.get(f"/api/drops/{response.json()['id']}").json()
    assert data["show_related"] is True


@pytest.mark.parametrize("invalid", [None, "false", "true", 0, 1, [], {}])
def test_non_boolean_visibility_is_rejected_without_writing(client, invalid):
    before = admin._drops_all()
    assert save(client, show_related=invalid).status_code == 400
    assert admin._drops_all() == before


@pytest.mark.parametrize("session", [None, "viewer-session"])
def test_unauthorized_actor_cannot_change_visibility(client, session):
    before = admin._drops_all()
    client.cookies.clear()
    if session:
        client.cookies.set("mp_sess", session)
    assert save(client, show_related=False).status_code == 403
    assert admin._drops_all() == before
