"""주문 CS 보조(ordercs) — 통합 이력 · 결제 금액 내역 · 취소 실행 가드 · 취소 안내 · 마이페이지 취소 사유.

재현 대상: 고객이 취소 요청을 두 번 철회한 주문이 관리자 화면 기본 문구 '고객 요청' 으로 취소된 건(2026-11-08)."""
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
import ordercs
import shipfee

SET = json.dumps([{"id": "mpo::set", "n": "앨범 (세트)", "p": 50400, "q": 3}], ensure_ascii=False)
BUYER = json.dumps({"name": "고객", "phone": "010-1234-5678"}, ensure_ascii=False)
OLD, NEW, REQ = "MD-20261019-DA72CA", "MD-20261108-558761", "MD-20261101-OPENRQ"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "BASE", str(tmp_path))
    monkeypatch.setattr(admin, "IS_PG", False)
    monkeypatch.setattr(admin, "ensure_ready", lambda: None)
    monkeypatch.setitem(shipfee._READY, "ok", False)
    monkeypatch.setitem(admin._state, "ocols", {"order_id", "status", "items", "fulfill", "buyer", "created", "admin_memo", "confirmed_at"})
    me = {"id": "m1", "customer_id": "c1", "name": "회원", "status": "ACTIVE"}
    monkeypatch.setattr(admin, "member_required", lambda req: me)
    monkeypatch.setattr(admin, "_own_order", lambda m, oid: admin.one("SELECT * FROM orders WHERE order_id=?", (oid,)))
    sms = []
    monkeypatch.setattr(admin, "system_sms", lambda phone, text, tag, order_id="": (sms.append((phone, text, tag, order_id)), (True, False))[1])
    cancelled = []

    def fake_core(a, r, reason, manual=False, restock=True):      # 환불·재고는 다른 테스트의 몫 — 상태와 이력만 남긴다
        if r.get("status") == "CANCELLED":
            raise admin.HTTPException(400, "이미 취소된 주문입니다")
        log = json.loads(r.get("pay_log") or "[]") + [{"t": admin.now_iso(), "c": "CANCELLED", "m": reason + " · 이전상태 PAID · 환불완료"}]
        admin.run("UPDATE orders SET status='CANCELLED', fulfill='CANCELLED', admin_memo=?, pay_log=? WHERE order_id=?",
                  ("[취소] " + reason, json.dumps(log, ensure_ascii=False), r["order_id"]))
        admin.audit(a, "환불", r["order_id"], reason)
        cancelled.append((r["order_id"], reason))
        return {"ok": True, "refunded": True, "shipfee": None}
    monkeypatch.setattr(admin, "_order_cancel_core", fake_core)
    with sqlite3.connect(tmp_path / "mapdal.db") as db:
        db.executescript("""
            CREATE TABLE admin_sessions(id TEXT PRIMARY KEY, admin_id TEXT, expires TEXT);
            CREATE TABLE admins(id TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER, username TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, created TEXT, status TEXT, amount INTEGER, buyer TEXT, items TEXT,
              ship_method TEXT, pay_method TEXT, fulfill TEXT, admin_memo TEXT, customer_id TEXT, member_id TEXT, pay_log TEXT,
              paid_at TEXT, coupon TEXT, discount TEXT, confirmed_at TEXT, tracking TEXT, courier TEXT, receipt_url TEXT);
            CREATE TABLE member_requests(id TEXT, member_id TEXT, order_id TEXT, rtype TEXT, reason TEXT, created TEXT,
              status TEXT, admin_memo TEXT, updated TEXT, customer_id TEXT);
            CREATE TABLE audit_log(id TEXT, created TEXT, actor TEXT, role TEXT, action TEXT, target TEXT, detail TEXT);
            CREATE TABLE notify_log(id TEXT, created TEXT, order_id TEXT, phone TEXT, kind TEXT, template TEXT, status TEXT, detail TEXT, by_admin TEXT);
            CREATE TABLE member_inquiries(id TEXT, order_id TEXT, created TEXT, category TEXT, title TEXT, status TEXT,
              answered_at TEXT, answered_by TEXT, deleted_at TEXT);
            CREATE TABLE mp_coupon_uses(order_id TEXT, code TEXT, created TEXT, amount_off INTEGER, status TEXT);
            CREATE TABLE members(id TEXT, email TEXT);
        """)
        db.execute("INSERT INTO admin_sessions VALUES(?,?,?)", (hashlib.sha256(b"owner").hexdigest(), "__master__", "9999-12-31"))
        plog = json.dumps([{"t": "2026-10-19T19:33:17", "c": "CREATED", "m": "PC · 136,080원"},
                           {"t": "2026-10-19T19:33:56", "c": "PAID", "m": "Card"}])
        o = "INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method,pay_method,fulfill,customer_id,member_id,pay_log,paid_at,coupon,discount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        db.execute(o, (OLD, "2026-10-19T19:33:17", "PAID", 136080, BUYER, SET, "standard", "Card", "NEW", "c1", "m1", plog, "2026-10-19T19:33:56", "HOME-5JKRT8", "15120"))
        db.execute(o, (NEW, "2026-11-08T15:43:02", "PAID", 154200, BUYER, SET, "standard", "Card", "NEW", "c1", "m1", "[]", "2026-11-08T15:43:49", None, None))
        db.execute(o, (REQ, "2026-11-01T10:00:00", "PAID", 151200, BUYER, SET, "standard", "Card", "NEW", "c1", "m1", "[]", "2026-11-01T10:01:00", None, None))
        rq = "INSERT INTO member_requests VALUES(?,?,?,?,?,?,?,?,?,?)"
        db.execute(rq, ("r1", "m1", OLD, "cancel", "다시 구매", "2026-11-06T13:35:23", "철회", "고객 직접 철회", "2026-11-06T13:40:51", "c1"))
        db.execute(rq, ("r2", "m1", OLD, "cancel", "재구매", "2026-11-06T13:46:53", "철회", "고객 직접 철회", "2026-11-06T14:00:23", "c1"))
        db.execute(rq, ("r3", "m1", REQ, "cancel", "단순 변심", "2026-11-02T09:00:00", "접수", "", "2026-11-02T09:00:00", "c1"))
        db.execute("INSERT INTO mp_coupon_uses VALUES(?,?,?,?,?)", (OLD, "HOME-5JKRT8", "2026-10-19T19:33:17", 15120, "USED"))
        db.execute("INSERT INTO notify_log VALUES('n1','2026-10-21T10:18:36',?,'010','sms','추가배송비안내','FAIL','HTTP Error 402','대표')", (OLD,))
        db.execute("INSERT INTO member_inquiries VALUES('q1',?,'2026-11-11T17:51:01','취소&환불','[재문의] 경위 확인','접수',NULL,NULL,NULL)", (OLD,))
    app = FastAPI(); app.include_router(admin.admin_router)
    with TestClient(app) as c:
        c.cookies.set("mp_sess", "owner")
        c.sms, c.cancelled = sms, cancelled
        yield c


def post_cancel(c, oid, **body):
    return c.post("/admin/api/orders/%s/cancel" % oid, json=body)


def test_detail_shows_requests_withdrawals_and_amounts(client):
    d = client.get("/admin/api/orders/" + OLD).json()
    labels = [(e["t"], e["label"], e["who"]) for e in d["timeline"]]
    assert ("2026-11-06 14:00:23", "취소 요청 철회", "고객") in labels
    assert ("2026-11-06 13:35:23", "취소 요청 접수", "고객") in labels
    assert any(e["kind"] == "알림" and e["warn"] and "402" in e["m"] for e in d["timeline"])
    assert any(e["kind"] == "문의" and e["warn"] for e in d["timeline"])            # 미답변 문의
    assert [e["t"] for e in d["timeline"]] == sorted((e["t"] for e in d["timeline"]), reverse=True)
    a = d["amounts"]
    assert (a["items_total"], a["coupon_code"], a["coupon_off"], a["ship_fee"], a["amount"]) == (151200, "HOME-5JKRT8", 15120, 0, 136080)
    assert a["coupon_state"] == "사용됨"
    b = client.get("/admin/api/orders/" + NEW).json()["amounts"]
    assert (b["items_total"], b["coupon_off"], b["ship_fee"], b["amount"]) == (151200, 0, 3000, 154200)


def test_legacy_default_reason_is_blocked_after_withdrawal(client):
    r = post_cancel(client, OLD, reason="고객 요청")                               # 구 화면의 prompt 기본값
    assert r.status_code == 409 and "철회" in r.json()["detail"] and "2026-11-06 14:00" in r.json()["detail"]
    assert client.cancelled == [] and admin.one("SELECT status FROM orders WHERE order_id=?", (OLD,))["status"] == "PAID"
    ctx = client.get("/admin/api/orders/%s/cancel-check" % OLD).json()
    assert ctx["warn_withdrawn"] and len(ctx["withdrawn"]) == 2 and ctx["open_requests"] == []
    assert "136,080원" in ctx["notice_preview"]["SOLDOUT"] and "상품 품절" in ctx["notice_preview"]["SOLDOUT"]


def test_customer_reason_needs_channel_when_no_open_request(client):
    assert post_cancel(client, NEW, reason_code="CUSTOMER").status_code == 409
    assert post_cancel(client, NEW, reason_code="CUSTOMER", channel="전화", channel_note="a").status_code == 409
    r = post_cancel(client, NEW, reason_code="CUSTOMER", channel="전화", channel_note="11/8 12:40 통화", memo="본인 확인")
    assert r.status_code == 200
    assert client.cancelled == [(NEW, "고객 요청 · 전화 접수: 11/8 12:40 통화 · 본인 확인")]
    assert client.sms == []                                                         # notify 미선택 — 발송 없음


def test_open_request_passes_and_is_closed(client):
    r = post_cancel(client, REQ, reason_code="CUSTOMER", notify=True)
    assert r.status_code == 200 and r.json()["notice"]["sent"]
    assert admin.one("SELECT status FROM member_requests WHERE id='r3'")["status"] == "완료"
    phone, text, tag, oid = client.sms[0]
    assert (phone, tag, oid) == ("01012345678", "주문취소안내", REQ)
    assert "취소 사유: 고객 요청" in text and "151,200원" in text and "cx@mealzip.kr" in text
    acts = [x["action"] for x in admin.rows("SELECT action FROM audit_log WHERE target=?", (REQ,))]
    assert "환불" in acts and "취소안내발송" in acts


def test_other_reasons_do_not_need_a_request_and_actor_is_shown(client):
    r = post_cancel(client, OLD, reason_code="SOLDOUT", memo="RACE ver. 재고 부족")
    assert r.status_code == 200 and client.cancelled == [(OLD, "품절·재고 부족 · RACE ver. 재고 부족")]
    d = client.get("/admin/api/orders/" + OLD).json()
    top = next(e for e in d["timeline"] if e["label"] == "주문 취소")
    assert top["who"].startswith("관리자 · ") and top["warn"]                        # 실행 계정이 이력에 보인다
    assert sum(1 for e in d["timeline"] if e["kind"] == "조치" and e["label"] == "환불") == 0   # 같은 조치 중복 표기 없음
    m = client.get("/api/member/orders/" + OLD).json()["cancel_info"]
    assert m["at"] and (m["reason"], m["refunded"]) == ("상품 품절", True)          # 내부 메모는 고객에게 나가지 않는다
    assert "RACE" not in json.dumps(m, ensure_ascii=False)


def test_one_click_refuses_withdrawn_request(client):
    r = client.post("/admin/api/cs/req-cancel-order", json={"id": "r2"})
    assert r.status_code == 409 and "철회" in r.json()["detail"] and client.cancelled == []
    ok = client.post("/admin/api/cs/req-cancel-order", json={"id": "r3", "notify": True})
    assert ok.status_code == 200 and ok.json()["notice"]["sent"] and client.cancelled[0][0] == REQ


def test_csv_has_amount_and_cancel_columns(client):
    post_cancel(client, OLD, reason_code="OPERATOR")
    lines = client.get("/admin/api/orders.csv").text.lstrip("﻿").split("\n")
    head = lines[0].split(",")
    row = next(l for l in lines if l.startswith(OLD))
    assert head[-8:] == ["상품합계", "쿠폰코드", "쿠폰할인", "배송비", "취소일시", "취소사유", "주문구분", "추출시각"]
    assert "151200,HOME-5JKRT8,15120,0,20" in row and "운영자 판단" in row


def test_reason_code_inference():
    assert ordercs.reason_code("soldout") == "SOLDOUT"
    assert ordercs.reason_code("", "고객 요청") == "CUSTOMER"
    assert ordercs.reason_code("", "수동환불(외부 처리)") == "OTHER"
    assert ordercs.code_from_text("고객 취소요청 · 단순 변심 · 이전상태 PAID") == "CUSTOMER"
