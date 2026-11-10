import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import paysec  # noqa: E402
import reconcile_paid_orders as rp  # noqa: E402


def test_paid_via_noti_detection():
    noti = json.dumps([{'c': 'CREATED', 'm': 'x'}, {'c': 'PAID', 'm': '가상계좌 입금 확인 · 1,000원'}])
    mob = json.dumps([{'c': 'PAID', 'm': 'CARD · 모바일 승인통보 · TID …123'}])
    step3 = json.dumps([{'c': 'PAID', 'm': 'Card · TID …abc'}])
    assert rp.paid_via_noti(noti) and rp.paid_via_noti(mob)
    assert not rp.paid_via_noti(step3) and not rp.paid_via_noti('broken')


def test_judge():
    row = {'order_id': 'MD-1', 'amount': 1000, 'pay_method': 'VBank', 'payment_key': 'T1'}
    assert rp.judge(row, lambda t, o: {'status': 'Y', 'price': '1000'})[0] == 'ok'
    assert rp.judge(row, lambda t, o: {'status': 'N', 'price': '1000'})[0] == 'SUSPECT'
    assert rp.judge(dict(row, pay_method='Card'), lambda t, o: {'status': '0', 'price': '1000'})[0] == 'ok'
    assert rp.judge(dict(row, payment_key=''), None)[0] == 'NO_TID'

    def boom(t, o):
        raise paysec.InquiryError('x')
    assert rp.judge(row, boom)[0] == 'ERROR'
