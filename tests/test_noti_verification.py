"""결제 노티 위조 차단 — /inicis/vbank-noti · /inicis/mobile-noti 가 거래조회 확인 없이 PAID 로 바꾸지 않는다."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod  # noqa: E402
import admin_v2  # noqa: E402
import paysec  # noqa: E402

OID = 'MD-20261110-F00001'


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / 'mapdal.db'
    with sqlite3.connect(dbp) as db:
        db.execute('CREATE TABLE orders(order_id TEXT PRIMARY KEY, created TEXT, status TEXT, amount INTEGER,'
                   ' payment_key TEXT, pay_method TEXT, paid_at TEXT, pay_log TEXT, receipt_url TEXT)')
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                   (OID, '2026-11-10T09:00:00', 'PENDING', 43000, '', '', '', '[]', ''))
    monkeypatch.setattr(appmod, 'SQLITE_PATH', str(dbp))
    monkeypatch.setattr(appmod, 'IS_PG', False)
    for fn in ('_award_purchase_points', '_ga4_mp_purchase'):
        monkeypatch.setattr(appmod, fn, lambda oid: None)
    audits, notified = [], []
    monkeypatch.setattr(admin_v2, 'audit', lambda *a, **k: audits.append(a))
    monkeypatch.setattr(admin_v2, 'order_notify_async', lambda oid, ev: notified.append((oid, ev)))
    monkeypatch.setattr(admin_v2, 'pay_detail_save', lambda *a, **k: '')
    monkeypatch.setattr(admin_v2, '_inicis_cred_for_tid', lambda tid: None)
    monkeypatch.delenv('INICIS_NOTI_VERIFY', raising=False)
    monkeypatch.setenv('INICIS_NOTI_IP_MODE', 'log')
    inq = {'resp': None, 'calls': []}

    def fake_inquiry(mid, key, tid, oid=''):
        inq['calls'].append(tid)
        r = inq['resp']
        if isinstance(r, Exception):
            raise r
        return r or {'resultCode': '00', 'status': '9', 'price': '0'}
    monkeypatch.setattr(paysec, 'inquiry_v1', fake_inquiry)
    client = TestClient(appmod.app)          # lifespan 미실행 — DB 초기화 없이 라우트만 검증
    yield client, dbp, inq, audits, notified


def status(dbp):
    with sqlite3.connect(dbp) as db:
        return db.execute('SELECT status FROM orders WHERE order_id=?', (OID,)).fetchone()[0]


def log_codes(dbp):
    with sqlite3.connect(dbp) as db:
        return [x['c'] for x in json.loads(db.execute('SELECT pay_log FROM orders WHERE order_id=?', (OID,)).fetchone()[0])]


PC_NOTI = {'no_oid': OID, 'no_tid': 'DEPOSITTID1', 'amt_input': '43000', 'type_msg': '0200'}


def test_forged_pc_deposit_noti_does_not_mark_paid(env):
    client, dbp, inq, audits, notified = env
    inq['resp'] = {'resultCode': '00', 'status': 'N', 'price': '43000'}   # 이니시스 기준 미입금
    r = client.post('/inicis/vbank-noti', data=PC_NOTI)
    assert r.text == 'FAIL'
    assert status(dbp) == 'PENDING'
    assert 'NOTI_UNVERIFIED' in log_codes(dbp)
    assert notified == []
    assert any(a[1] == '입금통보검증실패' for a in audits)


def test_genuine_deposit_noti_marks_paid(env):
    client, dbp, inq, audits, notified = env
    inq['resp'] = {'resultCode': '00', 'status': 'Y', 'price': '43000'}
    r = client.post('/inicis/vbank-noti', data=PC_NOTI)
    assert r.text == 'OK'
    assert status(dbp) == 'PAID'
    assert notified == [(OID, 'paid')]


def test_inquiry_outage_keeps_order_and_asks_inicis_to_retry(env):
    client, dbp, inq, _a, _n = env
    inq['resp'] = paysec.InquiryError('timeout')
    r = client.post('/inicis/vbank-noti', data={'P_OID': OID, 'P_STATUS': '02', 'P_AMT': '43000', 'P_TID': 'VBNK1'})
    assert r.text == 'FAIL'
    assert status(dbp) == 'PENDING'


def test_forged_mobile_card_noti_does_not_mark_paid(env):
    client, dbp, inq, _a, notified = env
    r = client.post('/inicis/mobile-noti', data={'P_STATUS': '00', 'P_OID': OID, 'P_AMT': '43000',
                                                 'P_TID': 'FAKE', 'P_TYPE': 'CARD'})
    assert r.text == 'FAIL'
    assert status(dbp) == 'PENDING'
    assert inq['calls'] == ['FAKE']
    assert notified == []


def test_genuine_mobile_card_noti_marks_paid(env):
    client, dbp, inq, _a, notified = env
    inq['resp'] = {'resultCode': '00', 'status': '0', 'price': '43000'}
    r = client.post('/inicis/mobile-noti', data={'P_STATUS': '00', 'P_OID': OID, 'P_AMT': '43000',
                                                 'P_TID': 'StdmobCARD1', 'P_TYPE': 'CARD'})
    assert r.text == 'OK'
    assert status(dbp) == 'PAID'


def test_ip_enforce_mode_blocks_unknown_sender(env, monkeypatch):
    client, dbp, inq, _a, _n = env
    monkeypatch.setenv('INICIS_NOTI_IP_MODE', 'enforce')
    inq['resp'] = {'resultCode': '00', 'status': 'Y', 'price': '43000'}
    r = client.post('/inicis/vbank-noti', data=PC_NOTI, headers={'cf-connecting-ip': '6.6.6.6'})
    assert r.status_code == 403
    assert status(dbp) == 'PENDING'
    r = client.post('/inicis/vbank-noti', data=PC_NOTI, headers={'cf-connecting-ip': '203.238.37.15'})
    assert r.text == 'OK' and status(dbp) == 'PAID'
