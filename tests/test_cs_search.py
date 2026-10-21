"""CS 통합·자연어 검색 (/admin/api/cs/search) — 실제 라우터 + SQLite."""
import datetime
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin

NOW = datetime.datetime(2026, 10, 21, 18, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "kst_now", lambda: NOW)
    monkeypatch.setitem(admin._state, "pname", "name")
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE members(id TEXT PRIMARY KEY, name TEXT, phone TEXT, email TEXT, customer_id TEXT);
            CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, buyer TEXT, contact_phone_norm TEXT);
            CREATE TABLE member_requests(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT, rtype TEXT, reason TEXT,
              created TEXT, status TEXT, admin_memo TEXT, updated TEXT, customer_id TEXT);
            CREATE TABLE member_inquiries(id TEXT PRIMARY KEY, member_id TEXT, order_id TEXT, title TEXT, body TEXT,
              created TEXT, status TEXT, answer TEXT, answered_at TEXT, answered_by TEXT, customer_id TEXT, category TEXT);
            CREATE TABLE member_pqna(id TEXT PRIMARY KEY, member_id TEXT, product_id TEXT, question TEXT, created TEXT,
              status TEXT, answer TEXT, answered_at TEXT, answered_by TEXT, customer_id TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        db.executemany("INSERT INTO members VALUES(?,?,?,?,?)", [
            ("m1", "김민서", "010-1111-2222", "", "c1"), ("m2", "김대혁", "010-7109-4344", "", "c2"), ("m3", "송건우", "010-8869-1445", "", "c3")])
        db.execute("INSERT INTO products VALUES('p1','이세계아이돌 앨범')")
        db.execute("""INSERT INTO orders VALUES('MD-20261020-A16377','{"name":"김대혁","phone":"010-7109-4344"}','01071094344')""")
        db.executemany("INSERT INTO member_requests VALUES(?,?,?,?,?,?,?,?,?,?)", [
            ("r1", "m1", "MD-20261019-E23291", "cancel", "판매처에서 결제이후 배송비 고지하여 결제취소합니다.", "2026-10-21T12:29:00", "완료", "", "", "c1"),
            ("r2", "m3", "MD-20261020-DBCA26", "cancel", "배송비 오류라고 해서 새로 주문했습니다", "2026-10-20T12:16:00", "접수", "", "", "c3"),
            ("r3", "m1", "MD-20261018-AAAAAA", "return", "포토카드가 찢어져 왔어요", "2026-10-18T10:00:00", "접수", "", "", "c1")])
        db.executemany("INSERT INTO member_inquiries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("i1", "m2", "MD-20261020-A16377", "가상계좌로 입금했는데 주문취소가 되었습니다", "입금 확인 부탁드립니다", "2026-10-21T10:00:00", "접수", "", "", "", "c2", ""),
            ("i2", "m1", "", "포카 안 왔어요", "특전 포카가 누락됐습니다", "2026-10-15T09:00:00", "답변완료", "재발송했습니다", "", "", "c1", ""),
            ("i3", "m3", "", "배송지 변경", "주소를 바꾸고 싶어요", "2026-10-21T09:00:00", "접수", "", "", "", "c3", "")])
        db.execute("INSERT INTO member_pqna VALUES('q1','m3','p1','택배비는 얼마인가요?','2026-10-21T08:00:00','접수','','','','c3')")
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        yield c


def ids(c, q, kind=""):
    r = c.get("/admin/api/cs/search", params={"q": q, "kind": kind})
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()["rows"]], r.json()


def test_natural_language_mixed_filters(client):
    got, d = ids(client, "어제 배송비 때문에 취소한 사람")
    assert got == ["r2"]
    assert {"k": "기간", "v": "어제"} in d["labels"]
    got, _ = ids(client, "배송비 때문에 취소한 사람")
    assert set(got) == {"r1", "r2"}


def test_synonyms_typo_and_kinds(client):
    assert set(ids(client, "택배비")[0]) == {"q1", "r1", "r2"}         # 택배비 = 배송비 그룹
    assert set(ids(client, "배숑비")[0]) == {"q1", "r1", "r2"}         # 오타 1글자
    assert ids(client, "포토카드 안 왔다는 문의")[0][0] == "i2"        # 포카 = 포토카드
    assert ids(client, "택배비", kind="pqna")[0] == ["q1"]
    assert set(ids(client, "반품 요청")[0]) == {"r3"}


def test_status_name_order_phone(client):
    assert set(ids(client, "미처리 취소 요청")[0]) == {"r2"}
    assert set(ids(client, "김민서")[0]) == {"r1", "r3", "i2"}
    assert ids(client, "E23291")[0] == ["r1"]
    assert ids(client, "MD-20261020-A16377")[0] == ["i1"]
    assert ids(client, "4344")[0] == ["i1"]
    assert ids(client, "입금했는데 취소됐다는 고객 미답변")[0] == ["i1"]
    assert set(ids(client, "주소 변경")[0]) == {"i3"}


def test_empty_query_rejected(client):
    assert client.get("/admin/api/cs/search", params={"q": " "}).status_code == 400
