"""안내 페이지 편집기(/admin/api/guide-pages) — 실제 라우터 + SQLite."""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import admin_v2 as admin

PAGES = ["support.html", "shipping.html", "returns.html"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setattr(admin, "STATIC_DIR", str(ROOT / "static"))
    audits = []
    monkeypatch.setattr(admin, "audit", lambda a, act, tgt="", det="": audits.append((act, tgt)))
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE page_edits(path TEXT PRIMARY KEY, html TEXT, updated TEXT, by_admin TEXT);
            CREATE TABLE page_history(id TEXT PRIMARY KEY, path TEXT, html TEXT, saved TEXT, by_admin TEXT);
        """)
        for sid, aid in [("owner-session", "__master__"), ("viewer-session", "viewer")]:
            db.execute("INSERT INTO admin_sessions VALUES(?,?,?)",
                       (hashlib.sha256(sid.encode()).hexdigest(), aid, "9999-12-31"))
        db.execute("INSERT INTO admins VALUES('viewer','조회','VIEWER',1,'viewer')")
    app = FastAPI()
    app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner-session")
        c.audits = audits
        yield c


def get(c, path):
    r = c.get("/admin/api/guide-pages/get", params={"path": path})
    assert r.status_code == 200, r.text
    return r.json()


def save(c, d):
    return c.post("/admin/api/guide-pages/save",
                  json={"path": d["path"], "ver": d["ver"], "blocks": [{"id": b["id"], "data": b["data"]} for b in d["blocks"]]})


@pytest.mark.parametrize("path", PAGES)
def test_roundtrip_is_byte_identical(path):
    html = (ROOT / "static" / path).read_text(encoding="utf-8")
    assert admin.gx_apply(html, admin.gx_parse(html)) == html


def test_list_and_unchanged_save(client):
    rows = client.get("/admin/api/guide-pages").json()["rows"]
    assert [r["path"] for r in rows] == PAGES
    d = get(client, "shipping.html")
    r = save(client, d)
    assert r.json() == {"ok": True, "changed": False, "ver": d["ver"]}


def test_edit_table_row_and_card(client):
    d = get(client, "shipping.html")
    tbl = next(b for b in d["blocks"] if b["id"] == "table:0")
    tbl["data"]["rows"][0]["b"] = "3,000원 · 30,000원 이상 무료 (이벤트 상품 제외)"
    tbl["data"]["rows"].append({"th": "새 항목", "b": "", "t": "자세한 내용은 [교환/반품](/returns) 참고\n둘째 줄 <script>x</script>"})
    r = save(client, d)
    assert r.status_code == 200 and r.json()["changed"], r.text
    html, edited, _ = admin._page_effective("shipping.html")
    assert edited
    assert "<b>3,000원 · 30,000원 이상 무료 (이벤트 상품 제외)</b> — 오후 2시" in html
    assert '<tr><th>새 항목</th><td>자세한 내용은 <a href="/returns">교환/반품</a> 참고<br>둘째 줄 &lt;script&gt;x&lt;/script&gt;</td></tr>' in html
    assert "<script>x</script>" not in html
    # 이력 1건 + 감사 로그
    assert admin.one("SELECT COUNT(*) AS n FROM page_history WHERE path='shipping.html'")["n"] == 1
    assert ("안내페이지수정", "shipping.html") in client.audits
    # 다시 불러오면 편집 내용이 그대로
    d2 = get(client, "shipping.html")
    rows = next(b for b in d2["blocks"] if b["id"] == "table:0")["data"]["rows"]
    assert rows[-1]["t"] == "자세한 내용은 [교환/반품](/returns) 참고\n둘째 줄 <script>x</script>"


def test_support_card_bullets(client):
    d = get(client, "support.html")
    card = next(b for b in d["blocks"] if b["data"].get("h") == "교환/반품")
    card["data"]["rows"] = [{"t": "접수 → 회수 → 검수 → 환불 4단계"}, {"t": "교환 신청: 수령 후 5영업일 이내"}, {"t": ""}]
    assert save(client, d).status_code == 200
    html, _, _ = admin._page_effective("support.html")
    assert "<ul><li>접수 → 회수 → 검수 → 환불 4단계</li><li>교환 신청: 수령 후 5영업일 이내</li></ul></a>" in html
    # 다른 카드·마크업은 그대로
    assert html.count('class="pt-card"') == (ROOT / "static/support.html").read_text(encoding="utf-8").count('class="pt-card"')


def test_stale_version_and_bad_input(client):
    d = get(client, "returns.html")
    assert save(client, d).status_code == 200
    d["ver"] = "stale"
    assert save(client, d).status_code == 409
    d = get(client, "returns.html")
    d["blocks"] = [{"id": "table:99", "data": {"rows": []}}]
    assert save(client, d).status_code == 409
    assert client.get("/admin/api/guide-pages/get", params={"path": "../admin.html"}).status_code == 400
    d = get(client, "returns.html")
    next(b for b in d["blocks"] if b["type"] == "note")["data"]["t"] = "x" * 3001
    assert save(client, d).status_code == 400


def test_unsafe_link_scheme_stays_text():
    assert admin.gx_html("[클릭](javascript:alert(1))") == "[클릭](javascript:alert(1))"
    assert admin.gx_html('[a](/x?q="1")').startswith('<a href="/x?q=&quot;1&quot;">')


def test_viewer_cannot_save(client):
    d = get(client, "support.html")
    client.cookies.set("mp_sess", "viewer-session")
    assert save(client, d).status_code == 403
