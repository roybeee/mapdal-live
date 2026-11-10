"""paysec — 노티 진위 검증 순수 로직 (FastAPI 불필요)."""
import hashlib
import io
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paysec  # noqa: E402


ORDER = {'order_id': 'MD-20261110-ABC123', 'amount': 43000, 'payment_key': 'StdpayVBNKINIpayTest20261110'}


def inquire_with(table):
    """table: tid -> 응답 dict 또는 Exception."""
    calls = []

    def _inq(tid, oid):
        calls.append((tid, oid))
        v = table.get(tid)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return {'resultCode': '00', 'status': '9', 'price': '0'}
        return v
    _inq.calls = calls
    return _inq


def test_hash_matches_manual_formula():
    h = paysec.inquiry_hash('KEY', '20261110090000', '1.2.3.4', 'MID01')
    assert h == hashlib.sha512(b'KEYExtraInquiry202611100900001.2.3.4MID01').hexdigest()
    assert len(h) == 128


def test_deposit_ok_only_when_status_Y_and_amount_match():
    inq = inquire_with({ORDER['payment_key']: {'resultCode': '00', 'status': 'Y', 'price': '43000'}})
    verdict, note, tid = paysec.verify_deposit(ORDER, 'noti-tid', inq)
    assert verdict == 'ok' and tid == ORDER['payment_key']
    assert inq.calls[0] == (ORDER['payment_key'], ORDER['order_id'])   # 채번 TID 우선


@pytest.mark.parametrize('resp', [
    {'resultCode': '00', 'status': 'N', 'price': '43000'},   # 입금대기 — 위조 노티
    {'resultCode': '00', 'status': 'C', 'price': '43000'},   # 입금 전 취소
    {'resultCode': '00', 'status': 'Y', 'price': '1000'},    # 다른 금액
    {'resultCode': '00', 'status': '9', 'price': '0'},       # 거래없음
])
def test_deposit_rejected_for_forged_or_mismatched(resp):
    inq = inquire_with({ORDER['payment_key']: resp, 'noti-tid': resp})
    verdict, note, _ = paysec.verify_deposit(ORDER, 'noti-tid', inq)
    assert verdict == 'reject'


def test_deposit_falls_back_to_noti_tid():
    o = dict(ORDER, payment_key='')
    inq = inquire_with({'P-TID-1': {'resultCode': '00', 'status': 'Y', 'price': '43,000'}})
    assert paysec.verify_deposit(o, 'P-TID-1', inq)[0] == 'ok'


def test_inquiry_errors_mean_retry_not_paid():
    inq = inquire_with({ORDER['payment_key']: paysec.InquiryError('timeout'),
                        'noti-tid': paysec.InquiryError('timeout')})
    assert paysec.verify_deposit(ORDER, 'noti-tid', inq)[0] == 'retry'


def test_no_tid_is_rejected_without_calling_inicis():
    inq = inquire_with({})
    assert paysec.verify_deposit(dict(ORDER, payment_key=''), '', inq)[0] == 'reject'
    assert inq.calls == []


def test_approval_requires_status_0():
    o = dict(ORDER, payment_key='')
    ok = inquire_with({'StdpayCARD1': {'resultCode': '00', 'status': '0', 'price': '43000'}})
    assert paysec.verify_approval(o, 'StdpayCARD1', ok)[0] == 'ok'
    cancelled = inquire_with({'StdpayCARD1': {'resultCode': '00', 'status': '1', 'price': '43000'}})
    assert paysec.verify_approval(o, 'StdpayCARD1', cancelled)[0] == 'reject'


def test_ip_gate_modes(monkeypatch):
    monkeypatch.delenv('INICIS_NOTI_IPS', raising=False)
    monkeypatch.setenv('INICIS_NOTI_IP_MODE', 'log')
    assert paysec.ip_gate('6.6.6.6')[0] is True and paysec.ip_gate('6.6.6.6')[1]
    monkeypatch.setenv('INICIS_NOTI_IP_MODE', 'enforce')
    assert paysec.ip_gate('6.6.6.6')[0] is False
    assert paysec.ip_gate('203.238.37.15') == (True, '')
    monkeypatch.setenv('INICIS_NOTI_IPS', '10.0.0.0/8, bad-value')
    assert paysec.ip_gate('10.1.2.3') == (True, '')
    assert paysec.ip_gate('garbage')[0] is False


def test_inquiry_v1_request_and_errors(monkeypatch):
    monkeypatch.setenv('INICIS_CLIENT_IP', '9.9.9.9')
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout=0):
        seen['url'] = req.full_url
        seen['form'] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return R(json.dumps({'resultCode': '00', 'status': 'Y', 'price': '100'}).encode())

    res = paysec.inquiry_v1('MID01', 'KEY', 'TID9', 'OID1', timestamp='20261110090000', opener=opener)
    assert res['status'] == 'Y'
    f = seen['form']
    assert seen['url'] == paysec.INQUIRY_URL_DEFAULT
    assert (f['type'], f['paymethod'], f['originalTid'], f['oid'], f['clientIp']) == ('Extra', 'Inquiry', 'TID9', 'OID1', '9.9.9.9')
    assert f['hashData'] == paysec.inquiry_hash('KEY', '20261110090000', '9.9.9.9', 'MID01')

    def bad(req, timeout=0):
        return R(json.dumps({'resultCode': '01', 'resultMsg': 'hash error'}).encode())
    with pytest.raises(paysec.InquiryError):
        paysec.inquiry_v1('MID01', 'KEY', 'TID9', opener=bad)
    with pytest.raises(paysec.InquiryError):
        paysec.inquiry_v1('', 'KEY', 'TID9', opener=bad)
