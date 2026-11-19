"""주문 CS 보조 (2026-11-19) — 통합 타임라인 · 결제 금액 내역 · 취소 실행 가드 · 취소 안내.

배경: 고객이 취소 요청을 철회한 주문이 관리자 화면에서 '고객 요청' 사유로 취소됐는데,
주문 상세에는 결제 로그만 보여 요청·철회·실행 계정·쿠폰 할인액을 확인할 수 없었다
(소비자상담 2026-0625731). 기록은 member_requests · audit_log · notify_log · orders.coupon 에
이미 있었으므로, 여기서는 새 테이블 없이 그 기록을 모아 보여주고 취소 실행 직전에 검사한다.

admin_v2 를 지연 import 한다(shipfee 와 같은 방식) — 순환 import 를 피하고 테스트의 monkeypatch 가 그대로 듣는다.
"""
import re

from fastapi import HTTPException

# 취소 사유 코드 → (관리자 화면 표기, 고객 안내 표기). 고객 안내에는 내부 메모를 내보내지 않는다.
REASONS = {
    'CUSTOMER':  ('고객 요청', '고객 요청'),
    'SOLDOUT':   ('품절·재고 부족', '상품 품절'),
    'UNPAID':    ('결제 기한 경과', '결제 기한 경과'),
    'DUPLICATE': ('중복 주문', '중복 주문'),
    'OPERATOR':  ('운영자 판단', '판매자 사정'),
    'OTHER':     ('기타', '판매자 사정'),
}
REASON_ORDER = ('CUSTOMER', 'SOLDOUT', 'UNPAID', 'DUPLICATE', 'OPERATOR', 'OTHER')
CHANNELS = ('전화', '이메일', '1:1 문의', '카카오톡', '매장 방문', '기타')
OPEN_ST = ('접수', '처리중')
NOTICE_TAG = '주문취소안내'
CS_MAIL = 'cx@mealzip.kr'


def _av():
    import admin_v2
    return admin_v2


def _t(v, n=19):
    return str(v or '')[:n].replace('T', ' ')


def _rows(sql, args=()):
    try:
        return [dict(x) for x in (_av().rows(sql, args) or [])]
    except Exception:
        return []


def reason_code(code, reason_text=''):
    """요청의 사유 코드 정규화. 코드 없이 문구만 보낸 구 화면·외부 호출은 문구로 추정한다 —
    '고객 요청' 으로 시작하면 CUSTOMER(가드 대상), 그 외는 OTHER."""
    c = str(code or '').strip().upper()
    if c in REASONS:
        return c
    t = str(reason_text or '').strip()
    if t.startswith('고객 요청') or t.startswith('고객요청') or t.startswith('고객 취소요청'):
        return 'CUSTOMER'
    return 'OTHER'


def requests_of(oid):
    return _rows('SELECT id, rtype, reason, created, status, admin_memo, updated FROM member_requests '
                 'WHERE order_id=? ORDER BY created', (oid,))


def cancel_context(r):
    """취소 확인창이 열릴 때마다 새로 조회하는 판단 자료 — 화면을 열어 둔 사이 바뀐 요청 상태를 반영한다."""
    av = _av()
    oid = r['order_id']
    reqs = [q for q in requests_of(oid) if (q.get('rtype') or '') == 'cancel']
    open_ = [q for q in reqs if (q.get('status') or '') in OPEN_ST]
    withdrawn = [q for q in reqs if (q.get('status') or '') == '철회']
    b = av.jload(r.get('buyer'), {}) or {}
    phone = av.digits(b.get('phone'))
    is_sf = str(oid).startswith('SF-')
    can_sms = bool(re.fullmatch(r'01\d{8,9}', phone)) and not is_sf
    return {
        'order_id': oid, 'status': r.get('status') or '', 'amount': av.num(r.get('amount')),
        'reasons': [{'code': c, 'label': REASONS[c][0]} for c in REASON_ORDER],
        'channels': list(CHANNELS),
        'open_requests': [{'id': q['id'], 'created': _t(q.get('created'), 16), 'reason': q.get('reason') or '',
                           'status': q.get('status') or ''} for q in open_],
        'withdrawn': [{'created': _t(q.get('created'), 16), 'withdrawn_at': _t(q.get('updated'), 16),
                       'reason': q.get('reason') or ''} for q in withdrawn],
        # 유효 요청 없이 철회 이력만 있는 주문 — 확인창 맨 위 붉은 경고의 조건
        'warn_withdrawn': bool(withdrawn and not open_),
        'can_notify': can_sms, 'phone_mask': (phone[:3] + '-****-' + phone[-4:]) if can_sms else '',
        'notify_default': bool(can_sms and (r.get('status') in ('PAID', 'WAITING_DEPOSIT'))),
        'notice_preview': {c: notice_text(r, c) for c in REASON_ORDER},
    }


def guard(r, code, channel='', channel_note=''):
    """'고객 요청' 취소의 실행 조건 — 접수·처리중인 취소 요청이 있거나, 요청 경로와 내용을 적어야 한다.
    통과하면 종결할 요청 id 목록을 돌려준다. 다른 사유는 검사하지 않는다."""
    if code != 'CUSTOMER':
        return []
    reqs = [q for q in requests_of(r['order_id']) if (q.get('rtype') or '') == 'cancel']
    open_ = [q for q in reqs if (q.get('status') or '') in OPEN_ST]
    if open_:
        return [q['id'] for q in open_]
    ch = str(channel or '').strip()
    note = str(channel_note or '').strip()
    if ch in CHANNELS and len(note) >= 4:
        return []
    wd = [q for q in reqs if (q.get('status') or '') == '철회']
    if wd:
        last = max(wd, key=lambda q: str(q.get('updated') or ''))
        raise HTTPException(409, '고객이 %s 취소 요청을 철회했습니다 — 지금 유효한 취소 요청이 없습니다. '
                                 '철회 이후 고객이 다시 요청했다면 요청 경로(전화·이메일 등)와 일시·내용을 입력하고, '
                                 '아니라면 다른 취소 사유를 선택하세요.' % _t(last.get('updated'), 16))
    raise HTTPException(409, '이 주문에는 접수된 취소 요청이 없습니다 — 고객이 요청한 경로(전화·이메일 등)와 '
                             '일시·내용을 입력하거나 다른 취소 사유를 선택하세요.')


def build_reason(code, memo='', channel='', channel_note=''):
    """감사로그·결제이력·관리자메모에 남는 사유 문구. 첫 토막은 항상 사유 표기라 나중에 코드로 되읽을 수 있다."""
    parts = [REASONS[code][0]]
    ch = str(channel or '').strip()
    if code == 'CUSTOMER' and ch in CHANNELS:
        parts.append('%s 접수: %s' % (ch, str(channel_note or '').strip()[:80]))
    m = str(memo or '').strip()
    if m and m != REASONS[code][0]:
        parts.append(m[:100])
    return ' · '.join(parts)[:200]


def close_requests(ids, memo):
    av = _av()
    for rid in ids or []:
        try:
            av.run("UPDATE member_requests SET status='완료', admin_memo=?, updated=? WHERE id=? AND status IN ('접수','처리중')",
                   (memo[:300], av.now_iso(), rid))
        except Exception:
            pass


def _cancel_entry(r):
    log = _av().jload(r.get('pay_log'), []) or []
    return next((e for e in reversed(log) if isinstance(e, dict) and e.get('c') == 'CANCELLED'), None)


def code_from_text(text):
    head = str(text or '').split(' · ')[0].strip()
    if head.startswith('고객 취소요청'):
        return 'CUSTOMER'
    for c in REASON_ORDER:
        if head == REASONS[c][0]:
            return c
    return ''


def customer_cancel_info(r):
    """마이페이지 주문 상세의 취소 일시·사유. 내부 메모는 내보내지 않고 고객용 표기만 준다."""
    if (r.get('status') or '') != 'CANCELLED':
        return None
    e = _cancel_entry(r) or {}
    m = str(e.get('m') or '')
    code = code_from_text(m)
    if not code:
        if '자동취소' in m or '기한' in m or '미입금' in m:
            code = 'UNPAID'
        elif '고객 직접' in m or '셀프' in m:
            code = 'CUSTOMER'
    return {'at': _t(e.get('t'), 16), 'reason': REASONS[code][1] if code else '',
            'refunded': '환불완료' in m}


def notice_text(r, code, shipfee_refund=0):
    av = _av()
    paid = (r.get('status') or '') == 'PAID'
    lines = ['[맵달SEOUL] 주문이 취소되었습니다.', '주문번호: %s' % r['order_id'],
             '취소 사유: %s' % REASONS.get(code, REASONS['OTHER'])[1]]
    if paid:
        lines.append('환불 금액: %s원 (결제하신 수단으로 환불 · 카드사에 따라 3~7영업일)' % format(av.num(r.get('amount')), ','))
    if shipfee_refund:
        lines.append('추가 배송비 %s원도 함께 환불됩니다.' % format(int(shipfee_refund), ','))
    lines.append('문의: %s' % CS_MAIL)
    return '\n'.join(lines)


def send_notice(actor, r, code, shipfee_res=None):
    """취소 안내 문자 1건. 관리자가 확인창에서 발송을 선택했을 때만 호출된다 — 일괄·자동 취소는 보내지 않는다.
    실패해도 취소는 이미 끝난 뒤라 결과만 돌려준다."""
    av = _av()
    b = av.jload(r.get('buyer'), {}) or {}
    phone = av.digits(b.get('phone'))
    if not re.fullmatch(r'01\d{8,9}', phone) or str(r['order_id']).startswith('SF-'):
        return {'sent': False, 'why': '국내 휴대폰 번호가 아니어서 문자를 보내지 않았습니다'}
    sf_amt = 0
    try:
        if isinstance(shipfee_res, dict) and shipfee_res.get('action') == 'refunded':
            for sid in shipfee_res.get('orders') or []:
                sf_amt += av.num((av.one('SELECT amount FROM orders WHERE order_id=?', (sid,)) or {}).get('amount'))
    except Exception:
        sf_amt = 0
    try:
        ok, dry = av.system_sms(phone, notice_text(r, code, sf_amt), NOTICE_TAG, r['order_id'])
    except Exception as e:
        ok, dry = False, False
        print('[ordercs] notice %s: %s' % (r['order_id'], e), flush=True)
    try:
        av.audit(actor, '취소안내발송', r['order_id'], '문자 %s' % ('발송' if ok and not dry else ('미설정(DRY)' if dry else '실패')))
    except Exception:
        pass
    return {'sent': bool(ok and not dry), 'dry': bool(dry),
            'why': '' if ok else '문자 발송에 실패했습니다 — 주문 이력의 알림 행을 확인하고 다시 보내세요'}


# ── 결제 금액 내역 ─────────────────────────────────────────────────────
def amount_view(r, points_used=None):
    """상품 합계 − 쿠폰 − 포인트 + 배송비 = 결제액. 배송비는 주문에 따로 저장되지 않아 나머지로 역산한다."""
    av = _av()
    oid = str(r.get('order_id') or '')
    its = [x for x in (av.jload(r.get('items'), []) or []) if isinstance(x, dict)]
    sub = sum(av.num(x.get('p') or x.get('price') or 0) * av.num(x.get('q') or 1) for x in its)
    amount = av.num(r.get('amount'))
    try:
        disc = int(float(r.get('discount') or 0))
    except Exception:
        disc = 0
    code = str(r.get('coupon') or '').strip()
    pts = av.num((points_used or {}).get('total')) if isinstance(points_used, dict) else 0
    is_sf = oid.startswith('SF-')
    ship = 0 if is_sf else amount - sub + disc + pts
    cst = ''
    if code:
        u = _rows('SELECT status FROM mp_coupon_uses WHERE order_id=? AND code=?', (oid, code))
        cst = {'USED': '사용됨', 'HELD': '예약', 'RELEASED': '복원됨'}.get((u[0].get('status') if u else '') or '', '')
    out = {'items_total': sub, 'coupon_code': code, 'coupon_off': disc, 'coupon_state': cst,
           'points': pts, 'ship_fee': ship if ship > 0 else 0, 'amount': amount,
           # 역산이 음수면 저장값끼리 맞지 않는 주문 — 화면에 '확인 필요' 로 드러낸다
           'mismatch': (ship < 0), 'extra_ship_paid': 0, 'total_paid': amount, 'kind': '추가 배송비' if is_sf else '일반'}
    return out


# ── 통합 타임라인 ──────────────────────────────────────────────────────
_REQ_KR = {'cancel': '취소', 'return': '반품', 'exchange': '교환'}


def _who(actor, role=''):
    a = str(actor or '')
    if a.startswith('고객셀프') or role == 'SELF':
        return '고객'
    if a.startswith('시스템') or a.upper() == 'SYSTEM' or a == '시스템':
        return '시스템'
    return ('관리자 · ' + a) if a else '관리자'


def timeline(r, pay_log_view=None):
    """주문 1건의 이력을 한 표로 — 결제 로그 · 취소/교환 요청 · 관리자 조치 · 알림 발송 · 1:1 문의.
    최신이 위. 각 행: t(시각) · kind · who(주체) · label · m(내용) · warn."""
    av = _av()
    oid = r['order_id']
    ev = []
    audits = _rows('SELECT created, actor, role, action, detail FROM audit_log WHERE target=? ORDER BY created', (oid,))
    used = set()

    def _actor_near(ts, actions):
        """결제 로그의 취소 행과 같은 시각(±3초)에 남은 감사로그에서 실행 계정을 찾는다."""
        for i, a in enumerate(audits):
            if i in used or a.get('action') not in actions:
                continue
            if _near(ts, a.get('created'), 3):
                used.add(i)
                return a
        return None

    for e in (pay_log_view if pay_log_view is not None else []):
        c = e.get('c') or ''
        who = '고객'
        m = e.get('m') or ''
        warn = False
        if c == 'CANCELLED':
            a = _actor_near(e.get('t'), ('환불', '주문취소', '수동환불처리'))
            who = _who(a.get('actor'), a.get('role')) if a else ('시스템' if '자동취소' in m else '관리자')
            warn = True
        elif c in ('PAID', 'VBANK_ISSUED', 'AMOUNT_MISMATCH', 'APPROVE_FAIL', 'APPROVE_ERR', 'DEPOSIT_AFTER_CANCEL'):
            who = '결제사'
        elif c == 'CONFIRMED':
            who = '고객' if '고객' in m or not m else '관리자'
        ev.append({'t': _t(e.get('t')), 'kind': '결제', 'who': who, 'label': e.get('label') or c, 'm': m, 'warn': warn})

    for q in requests_of(oid):
        kr = _REQ_KR.get(q.get('rtype'), q.get('rtype') or '요청')
        ev.append({'t': _t(q.get('created')), 'kind': '요청', 'who': '고객', 'label': kr + ' 요청 접수',
                   'm': q.get('reason') or '', 'warn': False})
        st = q.get('status') or ''
        if st and st != '접수' and q.get('updated') and q.get('updated') != q.get('created'):
            memo = q.get('admin_memo') or ''
            self_done = st == '철회' or '고객 직접' in memo
            ev.append({'t': _t(q.get('updated')), 'kind': '요청', 'who': '고객' if self_done else '관리자',
                       'label': kr + ' 요청 ' + st, 'm': memo, 'warn': st == '철회'})

    for i, a in enumerate(audits):
        if i in used:
            continue
        ev.append({'t': _t(a.get('created')), 'kind': '조치', 'who': _who(a.get('actor'), a.get('role')),
                   'label': a.get('action') or '', 'm': a.get('detail') or '', 'warn': False})

    for n in _rows('SELECT created, kind, template, status, detail, by_admin FROM notify_log WHERE order_id=? ORDER BY created', (oid,)):
        ok = (n.get('status') or '') in ('SENT', 'DRY')
        by = n.get('by_admin') or ''
        ev.append({'t': _t(n.get('created')), 'kind': '알림', 'who': _who(by) if by and by != '시스템' else '시스템',
                   'label': '%s %s' % ({'alimtalk': '알림톡', 'sms': '문자'}.get(n.get('kind'), n.get('kind') or '알림'),
                                       {'SENT': '발송', 'DRY': '미발송(발송사 미설정)'}.get(n.get('status'), '발송 실패')),
                   'm': (n.get('template') or '') + ('' if ok else ' — ' + str(n.get('detail') or '')[:80]), 'warn': not ok})

    for q in _rows('SELECT created, category, title, status, answered_at, answered_by, deleted_at FROM member_inquiries '
                   'WHERE order_id=? ORDER BY created', (oid,)):
        ev.append({'t': _t(q.get('created')), 'kind': '문의', 'who': '고객',
                   'label': '1:1 문의' + (' · ' + q['category'] if q.get('category') else ''), 'm': q.get('title') or '',
                   'warn': (q.get('status') or '') == '접수'})
        if q.get('answered_at'):
            ev.append({'t': _t(q.get('answered_at')), 'kind': '문의', 'who': _who(q.get('answered_by')),
                       'label': '문의 답변', 'm': q.get('title') or '', 'warn': False})
        if q.get('deleted_at'):
            ev.append({'t': _t(q.get('deleted_at')), 'kind': '문의', 'who': '고객', 'label': '문의 삭제',
                       'm': q.get('title') or '', 'warn': False})

    ev = [e for e in ev if e['t']]
    ev.sort(key=lambda e: e['t'], reverse=True)
    return ev[:200]


def _near(a, b, sec):
    import datetime
    try:
        x = datetime.datetime.fromisoformat(str(a)[:19].replace(' ', 'T'))
        y = datetime.datetime.fromisoformat(str(b)[:19].replace(' ', 'T'))
        return abs((x - y).total_seconds()) <= sec
    except Exception:
        return False
