"""결제완료(PAID) 주문 ↔ 이니시스 거래조회 대조 — 노티 위조 의심 건 찾기 (읽기 전용).

배경: 2026-11-10 이전에는 /inicis/vbank-noti · /inicis/mobile-noti 가 노티 본문만 보고 PAID 로
바꿨다. 이 스크립트는 그 기간의 PAID 주문을 이니시스 INIAPI 거래조회로 하나씩 확인해서
'이니시스에는 결제·입금이 없는데 맵달에는 결제완료' 인 주문을 찾는다. DB 에 아무것도 쓰지 않는다.

사용 (Render Shell 등 운영 환경변수가 있는 곳에서):
  python scripts/reconcile_paid_orders.py --since 2026-07-01              # 노티 경로 PAID 만 목록화(조회 없음)
  python scripts/reconcile_paid_orders.py --since 2026-07-01 --inquire    # 거래조회까지 실행
  python scripts/reconcile_paid_orders.py --since 2026-07-01 --inquire --all --csv out.csv

판정
  ok        이니시스 기준 승인(0)/입금완료(Y) · 금액 일치
  SUSPECT   거래조회 결과가 결제 아님(미입금·취소·거래없음) 또는 금액 불일치 → 이니시스 상점관리자에서 확인
  NO_TID    TID 없이 PAID (수동 처리 또는 위조 의심) → pay_log·관리자 감사로그 확인
  ERROR     조회 실패(자격정보·통신) → 재실행

결과 CSV 에는 주문번호·금액·판정만 담는다(고객 개인정보 미포함).
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 운영 로그에서 PAID 가 노티로 전환됐음을 나타내는 pay_log 문구
NOTI_MARKS = ('가상계좌 입금 확인', '모바일 승인통보')


def paid_via_noti(pay_log_raw):
    try:
        log = json.loads(pay_log_raw or '[]')
    except Exception:
        return False
    for e in log if isinstance(log, list) else []:
        if (e or {}).get('c') == 'PAID' and any(m in str(e.get('m') or '') for m in NOTI_MARKS):
            return True
    return False


def expected_status(row):
    return 'Y' if str(row.get('pay_method') or '').lower() in ('vbank', 'vacct') else '0'


def judge(row, inquire):
    """row(dict) → (verdict, note). inquire(tid, oid) 는 paysec.inquiry_v1 래퍼."""
    import paysec
    tid = str(row.get('payment_key') or '').strip()
    if not tid:
        return 'NO_TID', 'TID 없음'
    try:
        res = inquire(tid, row.get('order_id') or '')
    except paysec.InquiryError as e:
        return 'ERROR', str(e)[:80]
    st = str(res.get('status') or '').strip()
    pr = paysec._price(res.get('price'))
    want = expected_status(row)
    if st == want and pr == int(row.get('amount') or 0):
        return 'ok', 'status %s · %s원' % (st, pr)
    return 'SUSPECT', '이니시스 status %s · %s원 (기대 %s · %s원)' % (st or '-', pr, want, row.get('amount'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--since', required=True, help='생성일 하한 (YYYY-MM-DD, KST)')
    ap.add_argument('--until', default='9999-12-31', help='생성일 상한(미포함)')
    ap.add_argument('--inquire', action='store_true', help='이니시스 거래조회 실행')
    ap.add_argument('--all', action='store_true', help='노티 경로뿐 아니라 모든 PAID 주문 대조')
    ap.add_argument('--csv', help='결과 CSV 경로')
    ap.add_argument('--sleep', type=float, default=0.2, help='조회 간격(초)')
    a = ap.parse_args(argv)

    import app as A
    if A.IS_PG:
        A._connect_pg_with_retry(max_attempts=3, delay=2)
    with A.db() as c:
        rows = c.all("SELECT order_id, created, amount, pay_method, payment_key, pay_log FROM orders "
                     "WHERE status='PAID' AND created>=? AND created<? ORDER BY created", (a.since, a.until))
    targets = [r for r in rows if a.all or paid_via_noti(r.get('pay_log'))]
    print('PAID %d건 중 대조 대상 %d건 (%s)' % (len(rows), len(targets), '전체' if a.all else '노티 경로'))

    out, counts = [], {}
    for r in targets:
        if a.inquire:
            verdict, note = judge(r, A._noti_inquire)
            time.sleep(max(0.0, a.sleep))
        else:
            verdict, note = ('LISTED', '조회 미실행 (--inquire)')
        counts[verdict] = counts.get(verdict, 0) + 1
        out.append({'order_id': r['order_id'], 'created': str(r.get('created') or '')[:19],
                    'amount': r.get('amount'), 'pay_method': r.get('pay_method') or '',
                    'verdict': verdict, 'note': note})
        if verdict not in ('ok', 'LISTED'):
            print('%-8s %s %s원 %s' % (verdict, r['order_id'], r.get('amount'), note))
    print('요약:', ', '.join('%s %d' % kv for kv in sorted(counts.items())) or '대상 없음')
    if a.csv:
        with open(a.csv, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=['order_id', 'created', 'amount', 'pay_method', 'verdict', 'note'])
            w.writeheader(); w.writerows(out)
        print('CSV:', a.csv)
    return 1 if counts.get('SUSPECT') or counts.get('NO_TID') else 0


if __name__ == '__main__':
    sys.exit(main())
