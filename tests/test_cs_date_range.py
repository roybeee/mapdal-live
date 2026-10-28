"""CS 기간 지정 (2026-10-28) — /admin/api/cs 목록과 /admin/api/cs/search 에 from/to 적용."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cs_search import client as _base_client  # noqa: F401  (실제 라우터 + SQLite 픽스처 재사용)

import admin_v2 as admin


@pytest.fixture
def client(_base_client):
    with sqlite3.connect(Path(admin.BASE) / "mapdal.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS customer_profiles(id TEXT PRIMARY KEY, customer_no TEXT)")
        # 같은 날 23:59 접수 — 종료일 포함 여부(반열린 구간) 확인용
        db.execute("INSERT INTO member_inquiries VALUES('i4','m1','','늦은 밤 문의','내용','2026-10-20T23:59:30','접수','','','','c1','')")
    return _base_client


def lst(c, **p):
    r = c.get("/admin/api/cs", params=p)
    assert r.status_code == 200, r.text
    d = r.json()
    return {x["id"] for x in d["inq"]}, {x["id"] for x in d["reqs"]}, {x["id"] for x in d["pqna"]}, d["range"]


def test_no_range_returns_everything(client):
    inq, req, pq, rng = lst(client)
    assert inq == {"i1", "i2", "i3", "i4"} and req == {"r1", "r2", "r3"} and pq == {"q1"}
    assert rng == {"from": "", "to": ""}


def test_single_day_includes_whole_day(client):
    inq, req, pq, rng = lst(client, **{"from": "2026-10-20", "to": "2026-10-20"})
    assert inq == {"i4"} and req == {"r2"} and pq == set()
    assert rng == {"from": "2026-10-20", "to": "2026-10-20"}


def test_only_one_side_means_that_day(client):
    assert lst(client, to="2026-10-21")[0] == {"i1", "i3"}
    assert lst(client, **{"from": "2026-10-15"})[0] == {"i2"}


def test_range_and_reversed_order(client):
    a = lst(client, **{"from": "2026-10-18", "to": "2026-10-21"})
    b = lst(client, **{"from": "2026-10-21", "to": "2026-10-18"})
    assert a == b
    assert a[0] == {"i1", "i3", "i4"} and a[1] == {"r1", "r2", "r3"} and a[2] == {"q1"}
    assert a[3] == {"from": "2026-10-18", "to": "2026-10-21"}


def test_bad_date_is_400(client):
    assert client.get("/admin/api/cs", params={"from": "2026-13-40"}).status_code == 400


def test_search_respects_range_over_sentence(client):
    # 문장 속 '오늘'보다 화면 기간이 우선
    r = client.get("/admin/api/cs/search", params={"q": "오늘 배송비", "from": "2026-10-20", "to": "2026-10-20"}).json()
    assert {x["id"] for x in r["rows"]} == {"r2"}
    assert {"k": "기간", "v": "2026-10-20"} in r["labels"]


def test_search_empty_query_with_range_lists_period(client):
    r = client.get("/admin/api/cs/search", params={"q": "", "from": "2026-10-21", "to": "2026-10-21"})
    assert r.status_code == 200
    assert {x["id"] for x in r.json()["rows"]} == {"r1", "i1", "i3", "q1"}
    assert client.get("/admin/api/cs/search", params={"q": ""}).status_code == 400
