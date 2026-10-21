"""회원 전용 구매 — 비회원 주문 차단 게이트, 로그인 후 결제 화면 복귀, 관리자 허용 전환."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "audit", lambda *a, **k: None)
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE site_settings(key TEXT PRIMARY KEY, value TEXT, updated TEXT, by_admin TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        yield c


def test_gate_default_blocks_guests(client):
    with pytest.raises(HTTPException) as e:
        admin.guest_checkout_gate("", "domestic")
    assert e.value.status_code == 401 and "회원만" in e.value.detail
    with pytest.raises(HTTPException):
        admin.guest_checkout_gate("", "intl")
    admin.guest_checkout_gate("member-1", "domestic")          # 회원은 통과


def test_admin_toggle(client):
    assert client.get("/admin/api/checkout-guest").json() == {"domestic": False, "intl": False}
    client.post("/admin/api/checkout-guest", json={"domestic": False, "intl": True})
    admin.guest_checkout_gate("", "intl")                       # 해외만 허용
    with pytest.raises(HTTPException):
        admin.guest_checkout_gate("", "domestic")


def test_safe_next_path():
    ok = ["/checkout", "/checkout?dom=1", "/en/checkout-global", "/checkout.html"]
    bad = ["//evil.com/checkout", "https://evil.com", "/account", "/checkoutx", "/admin", "/checkout\\x"]
    assert all(admin.safe_next_path(v) == v for v in ok)
    assert all(admin.safe_next_path(v) == "" for v in bad)


def test_account_returns_to_checkout_after_login(client, monkeypatch):
    monkeypatch.setattr(admin, "member_of", lambda req: {"id": "m1", "customer_id": "c1", "status": "ACTIVE"})
    client.cookies.set("mp_next", "/checkout")
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/checkout"
    client.cookies.set("mp_next", "https://evil.com")
    r = client.get("/account", follow_redirects=False)
    assert r.status_code != 302


def test_login_page_shows_members_only_notice(client, monkeypatch):
    monkeypatch.setattr(admin, "member_of", lambda req: None)
    assert "회원만 구매할 수 있습니다" in client.get("/account?need=login").text
    assert "회원만 구매할 수 있습니다" not in client.get("/account").text
