"""
추가 배송비 청구 (2026-10-20) — 프로모션 상품 배송비(3,000원) 누락분을 기결제 고객에게 개별 링크로 받는다.

배경: 프로모션(예약판매·럭키드로우·팬사인회·영상통화 등)은 수량·금액과 무관하게 주문당 배송비 3,000원이
원칙인데, 판매 페이지를 NEW/DROPS → KPOP(음반)으로 옮기는 과정에서 규칙이 빠져 3만원 이상 주문이
무료배송으로 결제됐다(2026-10-20 18시대 수정 배포). 이미 결제된 주문의 차액을 받기 위한 기능.

구성
  · 대상 산출: 결제완료(PAID) · 일반배송 · 배송비 0원 결제 · 배송비 필수부과 상품 포함 주문
    (판정은 admin_v2.product_rules — 결제·쿠폰과 같은 단일 규칙). 픽업·해외·SF 주문 제외.
  · 청구 = 고객(휴대폰) 1명당 1건 · 금액 = 3,000원 × 해당 주문 수. 이미 청구에 포함된 주문은 제외(재실행 안전).
  · 고객 링크 /shipfee/<token> — 사이트 메뉴·검색에 노출되지 않는 비공개 페이지(noindex). 토큰 없이는 접근 불가.
  · 결제: 시도마다 SF- 주문을 만들어 기존 이니시스 승인(위변조·금액 검증·환불) 흐름을 그대로 쓴다.
  · 결제 여부: 청구 ↔ SF 주문(items id 'shipfee::<청구ID>')을 조회 시점에 매칭 — 별도 동기화 없이 항상 최신.
    SF 주문이 취소·환불되면 자동으로 '미결제'로 돌아간다.
"""
import json, re, secrets, datetime, threading, urllib.parse
from fastapi import APIRouter, Request, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, Response

shipfee_router = APIRouter()

SF_FEE = 3000
SF_PREFIX = 'SF-'
SF_ITEM = 'shipfee::'
DEFAULT_SINCE = '2026-10-19T15:00'          # 이세계아이돌 예약판매 오픈
_PAY_METHODS = {'card': 'Card', 'directbank': 'DirectBank', 'hpp': 'HPP'}   # 가상계좌 제외(즉시 확인 가능 수단만)


def _app():
    import app as _a
    return _a


def _av():
    import admin_v2 as _a
    return _a


_READY = {'ok': False}


def ensure():
    if _READY['ok']:
        return
    av = _av()
    av.run("""CREATE TABLE IF NOT EXISTS ship_charges(id TEXT PRIMARY KEY, token TEXT UNIQUE, phone TEXT, name TEXT,
              email TEXT, order_ids TEXT, amount INTEGER, status TEXT DEFAULT 'OPEN', created TEXT, created_by TEXT,
              sent_at TEXT, sent_count INTEGER DEFAULT 0, view_at TEXT, view_count INTEGER DEFAULT 0, note TEXT)""")
    for sql in ('CREATE INDEX IF NOT EXISTS idx_ship_charges_phone ON ship_charges(phone)',
                'ALTER TABLE ship_charges ADD COLUMN mail_at TEXT',
                'ALTER TABLE ship_charges ADD COLUMN mail_count INTEGER DEFAULT 0'):
        try: av.run(sql)                         # 컬럼 1개당 1문장 — 이미 있으면 조용히 실패
        except Exception: pass
    _READY['ok'] = True
    try: dedupe()
    except Exception as e: print('[shipfee] dedupe skipped: %s' % e, flush=True)


def dedupe():
    """중복 청구 정리 (2026-10-20 [청구 생성] 동시 2회 실행 사고 복구 · 멱등).
    같은 고객·같은 원주문 묶음의 청구가 여러 건이면 1건만 남긴다 — 링크 열람 이력이 있는 건 → 먼저 생성된 건 순으로 유지.
    결제(SF 주문)·문자·메일 발송 이력이 있는 사본은 지우지 않는다."""
    av = _av()
    chs = av.rows('SELECT id, phone, order_ids, created, view_count, sent_count, mail_count FROM ship_charges')
    groups = {}
    for ch in chs:
        groups.setdefault((ch.get('phone') or '', tuple(sorted(_orders_of(ch)))), []).append(ch)
    used = set(_sf_map().keys())
    drop = []
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda c: (0 if (av.num(c.get('view_count')) or c['id'] in used or av.num(c.get('sent_count'))
                                   or av.num(c.get('mail_count'))) else 1, str(c.get('created') or ''), c['id']))
        for c in g[1:]:
            if c['id'] in used or av.num(c.get('sent_count')) or av.num(c.get('mail_count')):
                continue
            drop.append(c['id'])
    for i in range(0, len(drop), 200):
        part = drop[i:i + 200]
        av.run('DELETE FROM ship_charges WHERE id IN (%s)' % ','.join('?' * len(part)), tuple(part))
    if drop:
        try: av.audit({'name': 'SYSTEM', 'role': 'OWNER'}, '추가배송비중복정리', 'shipfee', '중복 청구 %d건 삭제' % len(drop))
        except Exception: pass
        print('[shipfee] duplicate charges removed: %d' % len(drop), flush=True)
    return len(drop)


_GEN_LOCK = threading.Lock()


# ─────────────────────────── 매칭 · 상태 ───────────────────────────
def _sf_map():
    """청구ID → {'paid': [SF주문…], 'pending': n} — SF 주문 전체를 한 번에 읽어 매칭."""
    out = {}
    for r in _av().rows("SELECT order_id, status, items, amount, paid_at, created, pay_method FROM orders "
                        "WHERE order_id LIKE ?", (SF_PREFIX + '%',)):
        for it in (_av().jload(r.get('items'), []) or []):
            iid = str((it or {}).get('id') or '')
            if not iid.startswith(SF_ITEM):
                continue
            m = out.setdefault(iid[len(SF_ITEM):], {'paid': [], 'pending': 0})
            if r.get('status') == 'PAID':
                m['paid'].append({'order_id': r['order_id'], 'paid_at': str(r.get('paid_at') or r.get('created') or ''),
                                  'amount': _av().num(r.get('amount')), 'method': r.get('pay_method') or ''})
            elif r.get('status') == 'PENDING':
                m['pending'] += 1
    return out


def _state(ch, m, due=None):
    if (m or {}).get('paid'):
        return 'PAID'
    st = ch.get('status') or 'OPEN'
    if st in ('WAIVED', 'CANCELLED'):
        return st
    if due is not None and due <= 0:
        return 'VOID'                            # 원주문이 모두 취소됨 — 받을 배송비 없음
    return 'UNPAID'


def _charge_by_token(token):
    ensure()
    t = str(token or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', t):
        return None
    return _av().one('SELECT * FROM ship_charges WHERE token=?', (t,))


def _orders_of(ch):
    return [str(x) for x in (_av().jload(ch.get('order_ids'), []) or []) if x]


# ─────────────────────────── 결제 기한 · 청구액(실시간) ───────────────────────────
DEFAULT_DEADLINE = '2026-10-31T23:59'


# 고객 결제 페이지·안내문 문구 — 관리자 화면에서 수정하면 모든 고객 링크에 즉시 반영 (2026-10-20)
#   {name} = 고객 이름(마스킹) · {deadline} = 결제 기한(예: 10월 31일(토) 23시 59분) · {link} = 고객 전용 결제 링크(안내문 필수)
TEXT_FIELDS = [
    ('page_title', '페이지 제목', 60),
    ('intro', '인사·사유 안내 ({name} = 고객 이름)', 2000),
    ('notices', '유의사항 (한 줄에 하나)', 2000),
    ('confirm_label', '동의 체크 문구', 200),
    ('pay_button', '결제 버튼', 40),
    ('footer', '하단 안내', 1000),
    ('done_msg', '결제 완료 화면 문구', 1000),
    ('subject', '안내 문자·메일 제목', 80),
    ('message', '안내 문자·메일 본문 ({link} 필수)', 4000),
]


def default_texts():
    return {
        'page_title': '추가 배송비 결제',
        'intro': ('{name}님, 안녕하세요. 맵달SEOUL입니다.\n\n'
                  '안내 페이지에는 배송비가 명시되어 있었으나, 결제 시스템 오류로 인해 실제 결제 시 배송비가 누락되고 '
                  '상품 금액만 결제된 것으로 확인되었습니다. 이에 누락된 배송비의 추가 결제를 요청드리오니, '
                  '아래 내용을 확인해 주시기 바랍니다.'),
        'notices': ('※ 기한 내 배송비 미결제 시 기존 주문은 취소 처리되며, 구매를 원하시는 경우 새롭게 주문해 주시기 바랍니다.\n'
                    '※ 반드시 기존 주문서에 기재하신 주문자명과 동일한 이름으로 결제해 주시기 바랍니다.\n'
                    '※ 제주도 및 도서·산간지역은 추가 배송비가 발생합니다.'),
        'confirm_label': '위 주문의 추가 배송비 결제 내용을 확인했습니다.',
        'pay_button': '배송비 결제하기',
        'footer': ('이 페이지는 받으신 문자의 링크로만 열리는 고객님 전용 결제 페이지입니다. '
                   '결제 즉시 자동으로 확인되며 별도 연락은 필요하지 않습니다.\n문의: cx@mealzip.kr'),
        'done_msg': '협조해 주셔서 감사합니다. 주문하신 상품은 예정대로 발송됩니다.',
        'subject': MSG_SUBJECT,
        'message': MSG_TEXT,
    }


def conf():
    try:
        r = _av().one("SELECT value FROM site_settings WHERE key='shipfee_conf'")
        c = _av().jload((r or {}).get('value'), {}) or {}
    except Exception:
        c = {}
    t = default_texts()
    for k, v in ((c.get('texts') or {}) if isinstance(c.get('texts'), dict) else {}).items():
        if k in t and isinstance(v, str) and v.strip():
            t[k] = v
    import shipregion
    extra = shipregion.rates()
    return {'deadline': str(c.get('deadline') or DEFAULT_DEADLINE)[:16], 'texts': t, 'extra': extra}


def _int(v, d):
    try:
        return max(0, int(v))
    except Exception:
        return d


# ── 제주·도서산간 추가 배송비 — shipregion(업계 표준 우편번호 기준표 · 결제와 공용)으로 판정 ──
def region_of(b, ex=None):
    """배송지 → ('jeju'|'remote'|'', 추가금액)."""
    import shipregion
    return shipregion.extra_for(b or {}, ex)


def _raw_conf():
    try:
        r = _av().one("SELECT value FROM site_settings WHERE key='shipfee_conf'")
        return _av().jload((r or {}).get('value'), {}) or {}
    except Exception:
        return {}


def deadline_text():
    d = deadline_dt()
    return ('%d월 %d일(%s) %d시 %02d분' % (d.month, d.day, '월화수목금토일'[d.weekday()], d.hour, d.minute)) if d else ''


def _para(text, **kw):
    """관리자 입력 문구 → 안전한 HTML (이스케이프 · 줄바꿈 · {name} 굵게 · {deadline})."""
    h = _h(text)
    if 'name' in kw: h = h.replace('{name}', '<b>%s</b>' % _h(kw['name']))
    h = h.replace('{deadline}', _h(deadline_text()))
    return h.replace('\n', '<br>')


def deadline_dt():
    return _av()._drop_dt(conf()['deadline'])


def overdue():
    d = deadline_dt()
    return bool(d and _av().kst_now() > d)


def _orders_info(oids):
    """주문번호 → (상태, 배송지키). 청구 화면·결제 시점에 원주문 취소를 실시간 반영하기 위함."""
    av, out, ids = _av(), {}, [o for o in dict.fromkeys(oids) if o]
    ex = conf()['extra']
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for r in av.rows('SELECT order_id, status, buyer FROM orders WHERE order_id IN (%s)' % ','.join('?' * len(part)), tuple(part)):
            b = av.jload(r.get('buyer'), {}) or {}
            reg, add = region_of(b, ex)
            out[r['order_id']] = (r.get('status') or '', addr_key(b), reg, add)
    return out


def due_breakdown(ch, oinfo=None):
    """{배송지키: (지역, 추가금)} — 취소되지 않은 원주문 기준."""
    oids = _orders_of(ch)
    oinfo = oinfo if oinfo is not None else _orders_info(oids)
    addrs = {}
    for o in oids:
        st, ak, reg, add = (tuple(oinfo.get(o) or ()) + ('', '', '', 0))[:4]
        if st in ('CANCELLED', 'FAILED'):
            continue
        addrs[ak or ('order:' + o)] = (reg, _av().num(add))
    return addrs


def due_of(ch, oinfo=None):
    """현재 받아야 할 배송비 = 배송지마다 (기본 3,000원 + 제주·도서산간 추가금) — 취소되지 않은 원주문 기준.
    원주문이 취소되면 자동으로 줄고, 모두 취소되면 0원. 추가금 설정을 바꾸면 미결제 청구에 즉시 반영."""
    return sum(SF_FEE + add for _reg, add in due_breakdown(ch, oinfo).values())


# ─────────────────────────── 대상 산출 ───────────────────────────
def addr_key(b):
    """배송지 동일 판정 키 — 우편번호·주소·상세주소에서 공백·구두점·대소문자 차이를 무시한다.
    주소가 비어 있으면 '' (주문별로 따로 취급)."""
    b = b or {}
    raw = ' '.join(str(b.get(k) or '') for k in ('zip', 'addr1', 'addr2'))
    return re.sub(r'[\s\-_,.()\[\]/#·]+', '', raw).lower()


def candidates(since=DEFAULT_SINCE):
    """배송비 0원으로 결제된 배송비 필수부과 주문 → 고객별 묶음.
    청구액 = 3,000원 × 고객의 '서로 다른 배송지' 수 (2026-10-20: 같은 주소로 가는 주문은 배송비 1회).
    · 같은 고객이 같은 주소로 이미 배송비를 낸 주문(3만원 미만 등)이 있으면 그 주소는 청구하지 않는다.
    · 이미 청구(취소 제외)에 포함된 주문·주소는 제외 — 재실행해도 같은 주소에 두 번 청구되지 않는다."""
    av = _av()
    ensure()
    ex = conf()['extra']
    rs = av.rows("SELECT order_id, created, amount, discount, items, buyer, ship_method, contact_phone_norm "
                 "FROM orders WHERE created>=? AND status='PAID' AND order_id NOT LIKE ? "
                 "AND COALESCE(ship_method,'standard') NOT IN ('pickup','intl','none') ORDER BY created",
                 (since, SF_PREFIX + '%'))
    info, zero, ids, settled = {}, [], set(), set()
    for r in rs:
        its = [x for x in (av.jload(r.get('items'), []) or []) if isinstance(x, dict)]
        b = av.jload(r.get('buyer'), {}) or {}
        ph = av.digits(r.get('contact_phone_norm') or b.get('phone') or '')
        ck = ph or ('order:' + r['order_id'])
        ak = addr_key(b) or ('order:' + r['order_id'])
        info[r['order_id']] = (ck, ak)
        sub = sum(av.num(x.get('p')) * max(1, av.num(x.get('q')) or 1) for x in its)
        shipped = av.num(r.get('amount')) - sub + av.num(r.get('discount'))
        if shipped == 0:
            zero.append((r, its, b, ph, ck, ak))
            ids.update(str(x.get('id') or '') for x in its)
        elif shipped > 0:
            settled.add((ck, ak))                               # 이 주소는 이미 배송비를 냄
    rules = av.product_rules([{'id': i} for i in ids if i])     # 상품별 1회 판정
    covered = set()
    for ch in av.rows("SELECT order_ids FROM ship_charges WHERE COALESCE(status,'OPEN')<>'CANCELLED'"):
        for o in _orders_of(ch):
            covered.add(o)
            if o in info:
                settled.add(info[o])                            # 이미 청구된 주소
    groups = {}
    for r, its, b, ph, ck, ak in zero:
        if r['order_id'] in covered:
            continue
        if not any((rules.get(str(x.get('id') or '')) or {}).get('ship_required') for x in its):
            continue
        g = groups.setdefault(ck, {'phone': ph, 'name': str(b.get('name') or '')[:30],
                                   'email': str(b.get('email') or '').lower()[:120], 'orders': [], 'addrs': {}, 'skipped': []})
        if (ck, ak) in settled:
            g['skipped'].append(r['order_id'])                 # 같은 주소 배송비 기납부·기청구 → 추가 청구 없음
            continue
        g['orders'].append(r['order_id'])
        g['addrs'].setdefault(ak, []).append(r['order_id'])
        g.setdefault('extra', {})[ak] = region_of(b, ex)[1]
    out = []
    for g in groups.values():
        if not g['orders']:
            continue
        g['shipments'] = len(g['addrs'])
        g['amount'] = sum(SF_FEE + g['extra'].get(k, 0) for k in g['addrs'])
        out.append(g)
    return out


# ─────────────────────────── 관리자 API ───────────────────────────
def _actor(request, lvl, what):
    av = _av()
    a = av.get_actor(request)
    av.need(a, lvl, what)
    return a


def _site():
    try:
        return (_app().SITE_ORIGIN or 'https://mapdal.kr').rstrip('/')
    except Exception:
        return 'https://mapdal.kr'


def _link(ch):
    return '%s/shipfee/%s' % (_site(), ch['token'])


@shipfee_router.post('/admin/api/shipfee/generate')
def api_generate(request: Request, body: dict = Body(...)):
    """대상 산출(미리보기) 또는 청구 생성. dry_run=true 면 숫자만 반환."""
    a = _actor(request, 2, '추가 배송비 청구')
    since = str(body.get('since') or DEFAULT_SINCE).strip().replace(' ', 'T')[:16]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', since):
        raise HTTPException(400, '기준 시각 형식이 올바르지 않습니다 (예: 2026-10-19T15:00)')
    if not body.get('dry_run', True):
        if not _GEN_LOCK.acquire(blocking=False):
            raise HTTPException(409, '청구 생성이 이미 진행 중입니다 — 잠시 후 새로고침해 확인하세요')
        try:
            return _generate(a, since)
        finally:
            _GEN_LOCK.release()
    gs = candidates(since)
    n_orders = sum(len(g['orders']) for g in gs)
    n_ship = sum(g['shipments'] for g in gs)
    total = sum(g['amount'] for g in gs)
    summ = {'customers': len(gs), 'orders': n_orders, 'shipments': n_ship, 'amount': total,
            'merged': n_orders - n_ship}
    return dict(ok=True, dry_run=True, **summ)


def _generate(a, since):
    """청구 생성 본체 — _GEN_LOCK 안에서만 실행(동시 2회 실행 시 중복 청구 방지). 한 번에 일괄 INSERT."""
    av = _av()
    gs = candidates(since)
    n_orders = sum(len(g['orders']) for g in gs)
    n_ship = sum(g['shipments'] for g in gs)
    total = sum(g['amount'] for g in gs)
    now = av.now_iso()
    pairs = [('INSERT INTO ship_charges(id,token,phone,name,email,order_ids,amount,status,created,created_by,sent_count,view_count) '
              'VALUES(?,?,?,?,?,?,?,?,?,?,0,0)',
              (av.uid(), secrets.token_urlsafe(18), g['phone'], g['name'], g['email'],
               json.dumps(g['orders']), g['amount'], 'OPEN', now, a['name'])) for g in gs]
    for i in range(0, len(pairs), 200):
        av.runmany(pairs[i:i + 200])
    removed = dedupe()
    av.audit(a, '추가배송비청구생성', since, '%d명 · %d주문 · 배송지 %d곳 · %s원'
             % (len(gs), n_orders, n_ship, format(total, ',')))
    return dict(ok=True, dry_run=False, customers=len(gs), orders=n_orders, shipments=n_ship, amount=total,
                merged=n_orders - n_ship, dedup_removed=removed)


def _list_rows():
    ensure()
    av = _av()
    m = _sf_map()
    out = []
    chs = av.rows('SELECT * FROM ship_charges ORDER BY created, name')
    oinfo = _orders_info([o for ch in chs for o in _orders_of(ch)])
    for ch in chs:
        mm = m.get(ch['id']) or {}
        due = due_of(ch, oinfo)
        st = _state(ch, mm, due)
        paid = mm.get('paid') or []
        cxl = [o for o in _orders_of(ch) if oinfo.get(o, ('',))[0] == 'CANCELLED']
        out.append({'id': ch['id'], 'name': ch.get('name') or '', 'phone': ch.get('phone') or '',
                    'email': ch.get('email') or '', 'orders': _orders_of(ch), 'amount': av.num(ch.get('amount')),
                    'due': due, 'cancelled_orders': cxl,
                    'regions': sorted({reg for reg, _a in due_breakdown(ch, oinfo).values() if reg}),
                    'shipments': len(due_breakdown(ch, oinfo)),
                    'mail_at': (ch.get('mail_at') or '')[:16].replace('T', ' '), 'mail_count': av.num(ch.get('mail_count')),
                    'state': st, 'paid_at': (paid[0]['paid_at'] if paid else '')[:16].replace('T', ' '),
                    'paid_order': paid[0]['order_id'] if paid else '', 'paid_method': paid[0]['method'] if paid else '',
                    'dup_paid': max(0, len(paid) - 1), 'pending': mm.get('pending', 0),
                    'sent_at': (ch.get('sent_at') or '')[:16].replace('T', ' '), 'sent_count': av.num(ch.get('sent_count')),
                    'views': av.num(ch.get('view_count')), 'view_at': (ch.get('view_at') or '')[:16].replace('T', ' '),
                    'note': ch.get('note') or '', 'link': _link(ch), 'created': (ch.get('created') or '')[:16].replace('T', ' ')})
    return out


@shipfee_router.get('/admin/api/shipfee/list')
def api_list(request: Request):
    _actor(request, 0, '추가 배송비 조회')
    rs = _list_rows()
    summ = {'customers': len(rs), 'orders': sum(len(r['orders']) for r in rs if r['state'] != 'CANCELLED')}
    for k in ('PAID', 'UNPAID', 'WAIVED', 'CANCELLED', 'VOID'):
        sel = [r for r in rs if r['state'] == k]
        summ[k] = {'n': len(sel), 'amount': sum((r['due'] if k == 'UNPAID' else r['amount']) for r in sel)}
    summ['dup_paid'] = sum(r['dup_paid'] for r in rs)
    summ['unsent'] = len([r for r in rs if r['state'] == 'UNPAID' and not r['sent_count']])
    summ['unmailed'] = len([r for r in rs if r['state'] == 'UNPAID' and r['email'] and not r['mail_count']])
    summ['refund_flag'] = len([r for r in rs if '부분환불 필요' in (r['note'] or '')])
    return {'rows': rs, 'summary': summ, 'deadline': conf()['deadline'], 'overdue': overdue()}


@shipfee_router.post('/admin/api/shipfee/conf')
def api_conf(request: Request, body: dict = Body(...)):
    a = _actor(request, 2, '추가 배송비 설정')
    cur = _raw_conf()
    new = {'deadline': str(cur.get('deadline') or DEFAULT_DEADLINE)[:16],
           'texts': dict(cur.get('texts') or {}) if isinstance(cur.get('texts'), dict) else {}}
    log = []
    if 'deadline' in body:
        dl = str(body.get('deadline') or '').strip().replace(' ', 'T')[:16]
        if not _av()._drop_dt(dl):
            raise HTTPException(400, '결제 기한 형식이 올바르지 않습니다 (예: 2026-10-31T23:59)')
        new['deadline'] = dl; log.append('기한 ' + dl)
    if isinstance(body.get('extra'), dict):
        import shipregion
        e = body['extra']; cur_r = shipregion.rates()
        try:
            jj = int(e.get('jeju', cur_r['jeju'])); rr = int(e.get('remote', cur_r['remote']))
        except Exception:
            raise HTTPException(400, '추가 배송비는 숫자로 입력하세요')
        if not (0 <= jj <= 50000 and 0 <= rr <= 50000):
            raise HTTPException(400, '추가 배송비는 0~50,000원 사이여야 합니다')
        shipregion.set_rates(jj, rr, a['name']); log.append('지역추가 제주%d·도서%d' % (jj, rr))
    if isinstance(body.get('texts'), dict):
        lim = {k: n for k, _, n in TEXT_FIELDS}
        for k, v in body['texts'].items():
            if k not in lim:
                continue
            v = str(v if v is not None else '').replace('\r', '')
            if len(v) > lim[k]:
                raise HTTPException(400, '%s 문구가 너무 깁니다 (최대 %d자)' % (dict((a_, b_) for a_, b_, _ in TEXT_FIELDS)[k], lim[k]))
            if k == 'message' and v.strip() and '{link}' not in v:
                raise HTTPException(400, '안내 문자·메일 본문에 {link} 가 있어야 고객 전용 링크가 들어갑니다')
            if v.strip() == default_texts()[k].strip() or not v.strip():
                new['texts'].pop(k, None)          # 기본값과 같거나 비우면 기본 문구 사용
            else:
                new['texts'][k] = v
            log.append(k)
    _av()._setting_put('shipfee_conf', new, a['name'])
    _av().audit(a, '추가배송비설정', 'shipfee', ', '.join(log)[:200])
    c = conf()
    return {'ok': True, 'deadline': c['deadline'], 'texts': c['texts'], 'extra': c['extra']}


@shipfee_router.get('/admin/api/shipfee/conf')
def api_conf_get(request: Request):
    _actor(request, 0, '추가 배송비 조회')
    c = conf()
    return {'deadline': c['deadline'], 'texts': c['texts'], 'extra': c['extra'], 'defaults': default_texts(),
            'fields': [{'key': k, 'label': l, 'max': n} for k, l, n in TEXT_FIELDS]}


@shipfee_router.get('/admin/api/shipfee.csv')
def api_csv(request: Request):
    a = _actor(request, 1, '추가 배송비 CSV')
    lab = {'PAID': '결제완료', 'UNPAID': '미결제', 'WAIVED': '면제', 'CANCELLED': '취소'}
    q = lambda v: '"' + str(v if v is not None else '').replace('"', '""') + '"'
    lines = [','.join(q(x) for x in ('이름', '휴대폰', '이메일', '원주문번호', '주문수', '청구금액', '결제상태', '결제일시',
                                       '결제주문번호', '문자발송', '링크열람', '결제링크', '메모'))]
    for r in _list_rows():
        lines.append(','.join(q(x) for x in (r['name'], r['phone'], r['email'], ' '.join(r['orders']), len(r['orders']),
                                             r['amount'], lab.get(r['state'], r['state']), r['paid_at'], r['paid_order'],
                                             r['sent_at'], r['views'], r['link'], r['note'])))
    _av().audit(a, 'CSV다운로드', 'shipfee', '%d건' % (len(lines) - 1))
    return Response('﻿' + '\n'.join(lines), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="mapdal_shipfee_%s.csv"'
                             % datetime.datetime.now().strftime('%Y%m%d_%H%M')})


def _xlsx(headers, rows, widths, sheet='추가배송비'):
    """의존성 없는 최소 .xlsx 작성기 — 시트 1개 · 머리행 굵게/고정 · 자동필터 · 문자열은 inlineStr(전화번호 앞자리 0 보존)."""
    import io, zipfile
    from xml.sax.saxutils import escape as xe
    def col(i):
        s_ = ''
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s_ = chr(65 + r) + s_
        return s_
    def cell(ri, ci, v, style=0):
        ref = '%s%d' % (col(ci), ri)
        st = ' s="%d"' % style if style else ''
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s"%s><v>%s</v></c>' % (ref, st, v)
        t = xe(str(v if v is not None else '')).replace('\n', '&#10;')
        return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, st, t)
    xml_rows = ['<row r="1">' + ''.join(cell(1, i, h, 1) for i, h in enumerate(headers)) + '</row>']
    for ri, r in enumerate(rows, start=2):
        xml_rows.append('<row r="%d">' % ri + ''.join(cell(ri, i, v, 2 if isinstance(v, int) else 0)
                                                     for i, v in enumerate(r)) + '</row>')
    last = '%s%d' % (col(len(headers) - 1), max(1, len(rows) + 1))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 '<cols>' + ''.join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w) for i, w in enumerate(widths)) + '</cols>'
                 '<sheetData>' + ''.join(xml_rows) + '</sheetData>'
                 '<autoFilter ref="A1:%s"/></worksheet>' % last)
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0"/></numFmts>'
              '<fonts count="2"><font><sz val="10"/><name val="Arial"/></font><font><b/><sz val="10"/><name val="Arial"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFEDECE7"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    files = {
        '[Content_Types].xml': ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'),
        '_rels/.rels': ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        'xl/workbook.xml': ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">\'%s\'!$A$1:$%s$%d</definedName></definedNames>'
            '</workbook>' % (xe(sheet), xe(sheet), col(len(headers) - 1), max(1, len(rows) + 1))),
        'xl/_rels/workbook.xml.rels': ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'),
        'xl/worksheets/sheet1.xml': sheet_xml,
        'xl/styles.xml': styles,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


@shipfee_router.get('/admin/api/shipfee.xlsx')
def api_xlsx(request: Request):
    """고객 전용 링크 리스트 엑셀 — 조회 시점의 결제 상태·청구액 반영."""
    a = _actor(request, 1, '추가 배송비 엑셀')
    lab = {'PAID': '결제완료', 'UNPAID': '미결제', 'WAIVED': '면제', 'CANCELLED': '취소', 'VOID': '원주문취소'}
    rs = _list_rows()
    headers = ['No', '이름', '휴대폰', '이메일', '원주문번호', '주문수', '배송지수', '청구액(원)', '결제상태',
               '결제일시', '결제주문번호', '문자발송', '메일발송', '링크열람', '고객 전용 결제 링크', '메모']
    rows = []
    for i, r in enumerate(rs, start=1):
        due = r['due'] if r['state'] in ('UNPAID', 'VOID') else r['amount']
        rows.append([i, r['name'], r['phone'], r['email'], ', '.join(r['orders']), len(r['orders']),
                     r['shipments'], int(due), lab.get(r['state'], r['state']),
                     r['paid_at'], r['paid_order'], r['sent_at'] or '미발송', r['mail_at'] or '미발송',
                     int(r['views']), r['link'], r['note']])
    data = _xlsx(headers, rows, [6, 10, 14, 26, 26, 7, 8, 11, 10, 17, 22, 17, 17, 8, 60, 30])
    _av().audit(a, '엑셀다운로드', 'shipfee', '%d건' % len(rows))
    fn = 'mapdal_shipfee_links_%s.xlsx' % datetime.datetime.now().strftime('%Y%m%d_%H%M')
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % fn, 'Cache-Control': 'no-store'})


@shipfee_router.post('/admin/api/shipfee/status')
def api_status(request: Request, body: dict = Body(...)):
    """면제(WAIVED)·청구취소(CANCELLED)·재오픈(OPEN). 결제완료 건은 변경 불가(환불은 주문 화면에서)."""
    a = _actor(request, 2, '추가 배송비 상태 변경')
    st = str(body.get('status') or '')
    if st not in ('OPEN', 'WAIVED', 'CANCELLED'):
        raise HTTPException(400, '상태 값이 올바르지 않습니다')
    ensure()
    ch = _av().one('SELECT * FROM ship_charges WHERE id=?', (str(body.get('id') or ''),))
    if not ch:
        raise HTTPException(404, '청구를 찾을 수 없습니다')
    if _state(ch, _sf_map().get(ch['id']), due_of(ch)) == 'PAID':
        raise HTTPException(409, '이미 결제된 청구입니다 — 환불은 해당 SF 주문의 주문 상세에서 처리하세요')
    note = str(body.get('note') or ch.get('note') or '')[:200]
    _av().run('UPDATE ship_charges SET status=?, note=? WHERE id=?', (st, note, ch['id']))
    _av().audit(a, '추가배송비상태', ch['id'], '%s → %s %s' % (ch.get('status'), st, note))
    return {'ok': True}


# 안내문 — 대표 제공 원문 그대로(2026-10-20 20:00). '링크' 자리에만 고객 전용 결제 링크를 넣는다.
#   ※ 문자(LMS)에서는 4바이트 이모지(📌)를 발송사가 삭제한다 — 메일에서는 그대로 보인다.
MSG_SUBJECT = '[맵달SEOUL] 배송비 추가 결제 안내'
MSG_TEXT = """안녕하세요, 맵달SEOUL입니다.

배송비 결제 누락과 관련하여 안내드립니다.

안내 페이지에는 배송비가 명시되어 있었으나, 
결제 시스템 오류로 인해 실제 결제 시 배송비가 누락되고 상품 금액만 결제된 것으로 확인되었습니다.

이에 누락된 배송비의 추가 결제를 요청드리오니, 
아래 내용을 확인해 주시기 바랍니다.

📌 추가 배송비 안내

기본 배송비: 3,000원

제주도 및 도서·산간지역: 추가 배송비 발생

📌 결제 안내

[배송비 결제]
{link}

[결제 기한]
10월 31일(토) 23시 59분까지

※ 기한 내 배송비 미결제 시 기존 주문은 취소 처리되며, 구매를 원하시는 경우 새롭게 주문해 주시기 바랍니다.

※ 반드시 기존 주문서에 기재하신 주문자명과 동일한 이름으로 결제해 주시기 바랍니다.

당사 결제 시스템 오류로 불편을 드린 점 진심으로 사과드리며, 고객님의 너른 양해 부탁드립니다.

다시 한번 이용에 불편을 드려 죄송합니다.

감사합니다.
맵달SEOUL 드림"""


def sms_text(r, t=None):
    t = t or conf()['texts']
    return t['message'].replace('{link}', r['link']).replace('{deadline}', deadline_text()).replace('{name}', r.get('name') or '고객')


def mail_html(r):
    t = conf()['texts']
    body = _h(t['message'].replace('{deadline}', deadline_text()).replace('{name}', r.get('name') or '고객')).replace('{link}', '<a href="%s" style="display:inline-block;background:#E8332A;color:#fff;'
                                   'padding:12px 20px;text-decoration:none;font-weight:700">배송비 결제하기</a><br>'
                                   '<span style="font-size:12px;color:#888">%s</span>' % (_h(r['link']), _h(r['link'])))
    return ('<div style="font-family:Apple SD Gothic Neo,Malgun Gothic,sans-serif;font-size:15px;line-height:1.7;'
            'color:#141414;max-width:560px;margin:0 auto;padding:24px">%s</div>' % body.replace('\n', '<br>'))


@shipfee_router.post('/admin/api/shipfee/send')
def api_send(request: Request, body: dict = Body(...)):
    """문자(LMS) 발송 — scope='unsent'(미결제·미발송) | 'unpaid'(미결제 전체 재발송) | ids 지정. 백그라운드 처리."""
    a = _actor(request, 2, '추가 배송비 문자 발송')
    av = _av()
    scope = str(body.get('scope') or 'unsent')
    channel = 'email' if str(body.get('channel') or '') == 'email' else 'sms'
    ids = set(str(x) for x in (body.get('ids') or []))
    if channel == 'email':
        rs = [r for r in _list_rows() if r['state'] == 'UNPAID' and '@' in (r['email'] or '')
              and ((r['id'] in ids) if ids else (scope == 'unpaid' or not r['mail_count']))]
    else:
        rs = [r for r in _list_rows() if r['state'] == 'UNPAID' and r['phone'] and len(r['phone']) >= 10
              and ((r['id'] in ids) if ids else (scope == 'unpaid' or not r['sent_count']))]
    if not rs:
        return {'ok': True, 'queued': 0, 'count': 0}
    if channel == 'email':
        import growth as _gr
        dry = not _gr.mail_enabled()
        if body.get('preview'):
            return {'ok': True, 'queued': 0, 'count': len(rs), 'dry': dry, 'channel': 'email',
                    'subject': conf()['texts']['subject'], 'sample': sms_text(rs[0])}

        def mwork():
            ok = fail = 0
            for r in rs:
                n = av.num(r.get('mail_count')) + 1
                sent = False
                if not dry:
                    try:
                        sent = _gr.send_mail(r['email'], conf()['texts']['subject'], mail_html(r), kind='shipfee',
                                             ref='%s:%d' % (r['id'], n), text=sms_text(r))
                    except Exception:
                        sent = False
                ok += 1 if sent else 0
                fail += 0 if (sent or dry) else 1
                if sent or dry:
                    try: av.run('UPDATE ship_charges SET mail_at=?, mail_count=COALESCE(mail_count,0)+1 WHERE id=?',
                                (av.now_iso(), r['id']))
                    except Exception: pass
            try: av.audit(a, '추가배송비메일', scope, '성공 %d · 실패 %d%s' % (ok, fail, ' · DRY' if dry else ''))
            except Exception: pass
        threading.Thread(target=mwork, daemon=True).start()
        return {'ok': True, 'queued': len(rs), 'dry': dry, 'channel': 'email'}
    cf = av.solapi_conf()
    dry = not (cf.get('key') and cf.get('sec') and cf.get('sender'))
    if body.get('preview'):
        return {'ok': True, 'queued': 0, 'count': len(rs), 'dry': dry, 'channel': 'sms', 'sample': sms_text(rs[0])}

    def work():
        ok = fail = 0
        for r in rs:
            text = sms_text(r)
            status, detail = 'DRY', '발송사 미설정 — 기록만'
            if not dry:
                try:
                    res = av.solapi_send({'to': r['phone'], 'from': av.digits(cf['sender']), 'text': text,
                                          'type': 'LMS', 'subject': conf()['texts']['subject'][:40]})
                    status, detail = 'SENT', json.dumps(res, ensure_ascii=False)[:200]
                    ok += 1
                except Exception as e:
                    status, detail = 'FAIL', str(e)[:200]
                    fail += 1
            try:
                av.run('INSERT INTO notify_log VALUES(?,?,?,?,?,?,?,?,?)',
                       (av.uid(), av.now_iso(), ' '.join(r['orders'])[:120], r['phone'], 'sms', '추가배송비안내',
                        status, detail, a['name']))
                if status in ('SENT', 'DRY'):
                    av.run('UPDATE ship_charges SET sent_at=?, sent_count=COALESCE(sent_count,0)+1 WHERE id=?',
                           (av.now_iso(), r['id']))
            except Exception:
                pass
        try: av.audit(a, '추가배송비문자', scope, '성공 %d · 실패 %d%s' % (ok, fail, ' · DRY' if dry else ''))
        except Exception: pass
    threading.Thread(target=work, daemon=True).start()
    return {'ok': True, 'queued': len(rs), 'dry': dry}


# ─────────────────────────── 고객 결제 페이지 ───────────────────────────
def _mask(nm):
    nm = str(nm or '')
    if len(nm) <= 1: return nm or '고객'
    if len(nm) == 2: return nm[0] + '*'
    return nm[0] + '*' * (len(nm) - 2) + nm[-1]


def _order_summ(oids):
    av = _av()
    out = []
    for oid in oids[:10]:
        r = av.one('SELECT order_id, created, items FROM orders WHERE order_id=?', (oid,))
        if not r: continue
        its = av.jload(r.get('items'), []) or []
        nm = str((its[0] or {}).get('n') or '') if its else ''
        out.append({'oid': oid, 'date': str(r.get('created') or '')[:10],
                    'label': nm[:46] + ('…' if len(nm) > 46 else '') + (' 외 %d건' % (len(its) - 1) if len(its) > 1 else '')})
    return out


def _amount_lines(ch, oinfo, amt, n_orders):
    bd = due_breakdown(ch, oinfo)
    n = max(1, len(bd))
    lab = {'jeju': '제주 지역 추가 배송비', 'remote': '도서·산간 지역 추가 배송비'}
    h = '<div class="od" style="display:flex;justify-content:space-between;font-size:14px"><span>기본 배송비 (배송지 %d곳 × 3,000원)</span><span>%s원</span></div>' % (n, format(SF_FEE * n, ','))
    for reg in ('jeju', 'remote'):
        adds = [a for r, a in bd.values() if r == reg and a > 0]
        if adds:
            h += ('<div class="od" style="display:flex;justify-content:space-between;font-size:14px"><span>%s</span><span>+%s원</span></div>'
                  % (lab[reg], format(sum(adds), ',')))
    h += '<div class="od amt"><span>결제하실 금액</span><b>%s원</b></div>' % format(amt, ',')
    if n_orders > n:
        h += '<p class="hint" style="margin-top:6px">같은 배송지로 가는 주문은 배송비를 한 번만 받습니다.</p>'
    return h


def _h(s):
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


_PAGE = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive">
<title>추가 배송비 결제 — 맵달SEOUL</title>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'IBM Plex Sans KR',sans-serif;background:#F7F4EC;color:#141414;padding:0 0 60px}
header{background:#fff;border-bottom:1px solid #e5e2da;padding:16px 20px}header b{font-family:'Black Han Sans';font-size:24px;color:#E8332A;font-weight:400}
main{max-width:520px;margin:0 auto;padding:22px 16px}h1{font-size:20px;margin-bottom:12px}
.box{background:#fff;border:1px solid #e5e2da;padding:18px;margin-bottom:14px}.p{font-size:14px;line-height:1.7;color:#333}
.od{border-top:1px solid #eee;padding:10px 0;font-size:13px}.od:first-child{border-top:0}.od .n{font-family:monospace;color:#666;font-size:12px}
.amt{display:flex;justify-content:space-between;align-items:center;font-size:15px;font-weight:700}.amt b{font-size:24px;color:#E8332A}
.pm{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 12px}.pm label{flex:1 1 30%;border:1.5px solid #ccc;padding:11px 6px;text-align:center;cursor:pointer;font-size:14px;font-weight:700;background:#fff}
.pm input{display:none}.pm label:has(input:checked){border-color:#141414;background:#141414;color:#fff}
.ag{display:flex;gap:8px;font-size:13px;color:#555;align-items:flex-start;margin-bottom:12px}
button#pay{width:100%;background:#E8332A;color:#fff;border:0;padding:16px;font-size:17px;font-weight:700;cursor:pointer}button#pay:disabled{opacity:.5}
.st{text-align:center;padding:30px 18px}.st .ic{font-size:44px;margin-bottom:8px}.st h2{font-size:20px;margin-bottom:6px}
.err{background:#fff2f1;color:#B71F18;padding:10px 12px;font-size:13px;margin-bottom:12px}.hint{font-size:12px;color:#888;line-height:1.6}</style>
__HEAD__</head><body><header><b>맵달SEOUL</b></header><main>__BODY__</main>__SCRIPT__</body></html>'''

_PAY_JS = r'''<script>
document.getElementById('pay').addEventListener('click',function(){
 if(!document.getElementById('ag').checked){alert('결제 내용 확인에 동의해 주세요.');return}
 var pm=(document.querySelector('input[name=pm]:checked')||{}).value||'card';
 var btn=this;btn.dataset.t=btn.dataset.t||btn.textContent;btn.disabled=true;btn.textContent='결제창 준비중…';
 try{var x=new XMLHttpRequest();x.open('POST','/api/shipfee/__TOKEN__/pay',false);x.setRequestHeader('Content-Type','application/json');
  x.send(JSON.stringify({payMethod:pm}));var od=JSON.parse(x.responseText||'{}');
  if(x.status<200||x.status>=300)throw new Error(od.detail||'결제 준비에 실패했습니다');
  var ua=(navigator.userAgent||'').toLowerCase();
  var mob=od.isMobile||/android|iphone|ipod|ipad|iemobile|opera mini|silk|mobile/.test(ua)||(navigator.maxTouchPoints>1&&/macintosh/.test(ua));
  var f=document.createElement('form');f.id='sfIniForm';f.method='POST';f.acceptCharset='UTF-8';
  var P=mob?od.inicisMobile:od.inicis;if(mob){f.action=od.mobilePayUrl;f.target='_self'}
  Object.keys(P).forEach(function(k){var i=document.createElement('input');i.type='hidden';i.name=k;i.value=P[k];f.appendChild(i)});
  document.body.appendChild(f);
  if(mob){f.submit();return}
  if(typeof INIStdPay==='undefined')throw new Error('결제 모듈 로딩 실패 — 새로고침 후 다시 시도해 주세요');
  INIStdPay.pay('sfIniForm');
 }catch(e){alert(e.message)}
 btn.disabled=false;btn.textContent=btn.dataset.t||btn.textContent;
});
</script>'''


def _page(body, head='', script=''):
    html = _PAGE.replace('__HEAD__', head).replace('__BODY__', body).replace('__SCRIPT__', script)
    return HTMLResponse(html, headers={'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': 'no-store',
                                       'Referrer-Policy': 'no-referrer'})


def _status_page(icon, title, msg):
    return _page('<div class="box st"><div class="ic">%s</div><h2>%s</h2><p class="p">%s</p></div>'
                 '<p class="hint" style="text-align:center">문의: cx@mealzip.kr</p>' % (icon, title, msg))


@shipfee_router.get('/shipfee/done')
def page_done(request: Request, oid: str = ''):
    av = _av()
    r = av.one('SELECT order_id, status, amount, paid_at FROM orders WHERE order_id=?', (str(oid)[:40],)) \
        if str(oid).startswith(SF_PREFIX) else None
    if r and r.get('status') == 'PAID':
        return _status_page('✅', '추가 배송비 결제가 완료되었습니다',
                            '결제금액 %s원 · 결제번호 %s<br>%s'
                            % (format(av.num(r['amount']), ','), _h(r['order_id']), _para(conf()['texts']['done_msg'])))
    return _status_page('⚠️', '결제가 확인되지 않았습니다', '받으신 결제 링크로 다시 시도해 주세요.')


@shipfee_router.get('/shipfee/fail')
def page_fail(request: Request, oid: str = '', msg: str = ''):
    """이니시스 인증·승인 실패 → 해당 고객 링크로 되돌린다."""
    av = _av()
    tok = ''
    r = av.one('SELECT items FROM orders WHERE order_id=?', (str(oid)[:40],)) if str(oid).startswith(SF_PREFIX) else None
    for it in (av.jload((r or {}).get('items'), []) or []):
        iid = str((it or {}).get('id') or '')
        if iid.startswith(SF_ITEM):
            ch = av.one('SELECT token FROM ship_charges WHERE id=?', (iid[len(SF_ITEM):],))
            tok = (ch or {}).get('token') or ''
    if tok:
        return RedirectResponse('/shipfee/%s?fail=%s' % (tok, urllib.parse.quote(str(msg)[:80])), status_code=303)
    return _status_page('⚠️', '결제가 완료되지 않았습니다', _h(msg) or '받으신 결제 링크로 다시 시도해 주세요.')


@shipfee_router.get('/shipfee/{token}')
def page_charge(token: str, request: Request, fail: str = ''):
    ch = _charge_by_token(token)
    if not ch:
        return _status_page('🔒', '유효하지 않은 링크입니다', '받으신 문자의 링크를 다시 확인해 주세요.')
    av = _av()
    is_admin = False
    try:
        av.get_actor(request); is_admin = True       # 관리자 미리보기는 고객 열람 수에 넣지 않는다
    except Exception:
        is_admin = False
    if not is_admin:
        try:
            av.run('UPDATE ship_charges SET view_at=?, view_count=COALESCE(view_count,0)+1 WHERE id=?', (av.now_iso(), ch['id']))
        except Exception:
            pass
    m = _sf_map().get(ch['id'])
    oinfo = _orders_info(_orders_of(ch))
    due = due_of(ch, oinfo)
    st = _state(ch, m, due)
    if st == 'PAID':
        p = m['paid'][0]
        return _status_page('✅', '이미 결제가 완료되었습니다',
                            '추가 배송비 %s원 · %s 결제<br>추가로 결제하실 필요가 없습니다.'
                            % (format(p['amount'], ','), _h(p['paid_at'][:16].replace('T', ' '))))
    if st in ('WAIVED', 'CANCELLED'):
        return _status_page('ℹ️', '결제하실 금액이 없습니다', '이 안내는 더 이상 결제가 필요하지 않습니다.')
    if st == 'VOID':
        return _status_page('ℹ️', '결제하실 금액이 없습니다', '해당 주문이 취소되어 추가 배송비를 결제하실 필요가 없습니다.')
    if overdue():
        return _status_page('⏰', '결제 기한이 지났습니다',
                            '추가 배송비 결제 기한(%s)이 지났습니다.<br>문의: cx@mealzip.kr'
                            % _h(conf()['deadline'].replace('T', ' ')))
    ods = _order_summ([o for o in _orders_of(ch) if oinfo.get(o, ('',))[0] not in ('CANCELLED', 'FAILED')])
    amt = due
    dl = deadline_dt()
    dl_txt = ('%d월 %d일(%s) %d시 %02d분까지' % (dl.month, dl.day, '월화수목금토일'[dl.weekday()], dl.hour, dl.minute)) if dl else ''
    T = conf()['texts']
    body = ('<h1>%s</h1>' % _h(T['page_title'])
            + ('<div class="err">결제가 완료되지 않았습니다: %s</div>' % _h(fail) if fail else '')
            + '<div class="box"><p class="p">%s</p></div>' % _para(T['intro'], name=_mask(ch.get('name')))
            + '<div class="box">' + ''.join('<div class="od"><div class="n">%s · %s</div>%s</div>'
                                              % (_h(o['oid']), _h(o['date']), _h(o['label'])) for o in ods)
            + _amount_lines(ch, oinfo, amt, len(ods))
            + ('<div class="od" style="font-size:13px"><b>결제 기한</b> %s</div>' % _h(dl_txt) if dl_txt else '')
            + '</div>'
            + ('<div class="box"><p class="hint" style="color:#555">%s</p></div>' % _para(T['notices']) if T['notices'].strip() else '')
            + '<div class="box"><div style="font-weight:700;font-size:14px">결제수단</div><div class="pm">'
              '<label><input type="radio" name="pm" value="card" checked>카드·간편결제</label>'
              '<label><input type="radio" name="pm" value="directbank">계좌이체</label>'
              '<label><input type="radio" name="pm" value="hpp">휴대폰</label></div>'
              '<label class="ag"><input type="checkbox" id="ag"> %s</label>'
              '<button id="pay">%s</button>'
              '<p class="hint" style="margin-top:10px">%s</p></div>' % (_h(T['confirm_label']), _h(T['pay_button']), _para(T['footer'])))
    head = '<script src="https://stdpay.inicis.com/stdjs/INIStdPay.js" charset="UTF-8"></script>'
    if _app().INICIS_MID == 'INIpayTest':
        head = '<script src="https://stgstdpay.inicis.com/stdjs/INIStdPay.js" charset="UTF-8"></script>'
    return _page(body, head, _PAY_JS.replace('__TOKEN__', ch['token']))


@shipfee_router.post('/api/shipfee/{token}/pay')
def api_pay(token: str, request: Request, body: dict = Body(...)):
    """결제 시도 1회 = SF 주문 1건(PENDING) 생성 + 이니시스 PC/모바일 파라미터 반환."""
    ch = _charge_by_token(token)
    if not ch:
        raise HTTPException(404, '유효하지 않은 링크입니다')
    due = due_of(ch)
    st = _state(ch, _sf_map().get(ch['id']), due)
    if st == 'PAID':
        raise HTTPException(409, '이미 결제가 완료되었습니다')
    if st != 'UNPAID':
        raise HTTPException(409, '결제하실 금액이 없습니다')
    if overdue():
        raise HTTPException(409, '추가 배송비 결제 기한이 지났습니다')
    pay_sel = _PAY_METHODS.get(str(body.get('payMethod') or 'card').lower())
    if not pay_sel:
        raise HTTPException(400, '지원하지 않는 결제수단입니다')
    A, av = _app(), _av()
    amount = due                                   # 원주문 취소가 반영된 현재 청구액
    if amount <= 0:
        raise HTTPException(409, '결제하실 금액이 없습니다')
    oids = _orders_of(ch)
    oid = '%s%s-%s' % (SF_PREFIX, A.kst_naive().strftime('%Y%m%d'), secrets.token_hex(3).upper())
    name = '[추가 배송비] ' + ', '.join(oids[:2]) + (' 외 %d건' % (len(oids) - 2) if len(oids) > 2 else '')
    buyer = {'name': ch.get('name') or '고객', 'phone': ch.get('phone') or '', 'email': ch.get('email') or '',
             'memo': '추가 배송비 청구 · 원주문 ' + ' '.join(oids)}
    items = [{'id': SF_ITEM + ch['id'], 'n': name[:120], 'p': amount, 'q': 1}]
    with A.db() as c:
        c.exec('INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method,customer_id,member_id,contact_phone_norm) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (oid, A.kst_iso(), 'PENDING', amount, json.dumps(buyer, ensure_ascii=False),
                json.dumps(items, ensure_ascii=False), 'none', None, None, ch.get('phone') or None))
    try:   # 실물 발송 대상이 아님 — 배송 처리 목록에서 '배송완료'로 보이게 한다
        av.run("UPDATE orders SET fulfill='DONE', admin_memo=? WHERE order_id=?",
               ('추가 배송비 청구 결제 · 원주문 ' + ' '.join(oids), oid))
    except Exception:
        pass
    try: A._pay_log(oid, 'CREATED', '추가배송비 · %s원 · %s' % (format(amount, ','), pay_sel))
    except Exception: pass
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    price = str(amount)
    origin = A._req_origin(request)
    inicis = {
        'version': '1.0', 'mid': A.INICIS_MID, 'oid': oid, 'price': price, 'timestamp': ts, 'use_chkfake': 'Y',
        'signature': A._ini_signature({'oid': oid, 'price': price, 'timestamp': ts}),
        'verification': A._ini_signature({'oid': oid, 'price': price, 'signKey': A.INICIS_SIGNKEY, 'timestamp': ts}),
        'mKey': A._ini_hash(A.INICIS_SIGNKEY), 'currency': 'WON', 'goodname': name[:80],
        'buyername': buyer['name'][:30], 'buyertel': buyer['phone'], 'buyeremail': buyer['email'],
        'gopaymethod': pay_sel, 'acceptmethod': 'centerCd(Y):below1000:HPP(2)',
        'returnUrl': origin + '/inicis/return', 'closeUrl': origin + '/inicis/close',
    }
    return {'orderId': oid, 'amount': amount, 'inicis': inicis,
            'inicisMobile': A._ini_mobile_params(oid, amount, name[:80], buyer, origin, pay_sel),
            'mobilePayUrl': 'https://mobile.inicis.com/smart/payment/', 'isMobile': A._is_mobile_ua(request)}


# ─────────────────────────── 관리자 화면 ───────────────────────────
_ADMIN = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>추가 배송비 청구 — MAPDAL SEOUL</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'IBM Plex Sans KR',system-ui,sans-serif;background:#F7F6F2;color:#141414;font-size:13px}
header{background:#141414;color:#fff;padding:14px 22px;display:flex;gap:16px;align-items:center}header a{color:#bbb;text-decoration:none}header h1{font-size:17px}
main{padding:18px 22px;max-width:1400px;margin:0 auto}.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.c{background:#fff;border:1px solid #e3e1db;padding:12px 16px;min-width:150px}.c small{color:#888;display:block}.c b{font-size:20px}
.bar{background:#fff;border:1px solid #e3e1db;padding:12px;margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button,.btn{font:inherit;font-weight:700;border:0;background:#141414;color:#fff;padding:8px 12px;cursor:pointer;text-decoration:none}
button.g,.btn.g{background:#fff;color:#141414;border:1px solid #999}button.r{background:#E8332A}button.sm{padding:4px 8px;font-size:12px}
input,select{font:inherit;padding:7px 8px;border:1px solid #ccc}table{width:100%;border-collapse:collapse;background:#fff}
th,td{border-bottom:1px solid #eee;padding:7px 8px;text-align:left;vertical-align:top}th{background:#faf9f6;font-size:12px;color:#666;position:sticky;top:0}
.mono{font-family:ui-monospace,monospace;font-size:11.5px}.st{font-weight:800;padding:2px 7px;font-size:11px}
.PAID{background:#e9f7ee;color:#0a7d38}.UNPAID{background:#fff6e0;color:#9a6b00}.WAIVED,.CANCELLED{background:#eee;color:#888}
.hint{color:#888;font-size:12px}.dup{color:#B71F18;font-weight:700}#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#141414;color:#fff;padding:10px 16px;display:none}</style></head>
<body><header><a href="/admin/dashboard">← 대시보드</a><h1>추가 배송비 청구 · 결제 현황</h1></header><main>
<div class="cards" id="cards"></div>
<div class="bar"><b>1. 대상 산출</b> 기준 시각 <input id="since" value="2026-10-19T15:00" style="width:160px">
 <button class="g" onclick="gen(true)">대상 미리보기</button><button onclick="gen(false)">청구 생성</button>
 <span class="hint">결제완료 · 일반배송 · 배송비 0원 결제 · 배송비 필수부과 상품 포함 주문. 이미 청구된 주문은 제외되므로 여러 번 실행해도 안전합니다(입금 대기 주문이 나중에 결제되면 다시 실행).</span></div>
<div class="bar"><b>결제 기한</b> <input id="dl" type="datetime-local" style="width:200px"><button class="g" onclick="saveDl()">기한 저장</button>
 <span class="hint" id="dlst"></span></div>
<div class="bar"><b>지역 추가 배송비</b> 제주 +<input id="exj" type="number" min="0" step="500" style="width:90px">원
 도서·산간 +<input id="exr" type="number" min="0" step="500" style="width:90px">원
 <button class="g" onclick="saveEx()">추가 배송비 저장</button>
 <span class="hint">우편번호 기준 자동 판정(업계 표준 제주·도서산간 기준표) · <b>쇼핑몰 결제에도 공통 적용</b> · 저장하면 미결제 청구액에 즉시 반영</span></div>
<details class="bar" id="txbox" style="display:block"><summary style="cursor:pointer;font-weight:700;color:#E8332A">고객 결제 페이지 · 안내문 문구 수정 <span style="font-weight:400;font-size:12px;color:#E8332A">— 저장하면 모든 고객 링크·이후 발송 문자/메일에 즉시 반영</span></summary>
 <div class="hint" style="margin:8px 0">사용 가능한 치환어: <b>{name}</b> 고객 이름(결제 페이지는 가운데 * 처리) · <b>{deadline}</b> 결제 기한 · <b>{link}</b> 고객 전용 링크(안내문 본문 필수). 줄바꿈은 그대로 반영됩니다. 비우면 기본 문구로 돌아갑니다.</div>
 <div id="txf" style="display:grid;grid-template-columns:200px 1fr;gap:8px 12px;align-items:start"></div>
 <div style="display:flex;gap:8px;margin-top:10px"><button onclick="saveTx()">문구 저장 (일괄 반영)</button><button class="g" onclick="pvTx()">결제 페이지 미리보기</button><button class="g" onclick="resetTx()">전체 기본 문구로 되돌리기</button><span class="hint" id="txst"></span></div>
</details>
<div class="bar"><b>2. 안내 발송</b><button class="g" onclick="sendPv('sms')">문자 미리보기</button><button class="r" onclick="send('unsent','sms')">미발송 고객에게 문자 발송</button>
 <button class="g" onclick="send('unpaid','sms')">미결제 전체 문자 재발송</button>
 <button class="g" onclick="sendPv('email')">메일 미리보기</button><button class="r" onclick="send('unsent','email')">미발송 고객에게 메일 발송</button>
 <a class="btn" href="/admin/api/shipfee.xlsx">엑셀 다운로드 (링크 포함)</a> <a class="btn g" href="/admin/api/shipfee.csv">CSV</a>
 <span class="hint">결제가 끝났거나 원주문이 모두 취소된 고객에게는 보내지 않습니다. 문자(LMS)에서는 📌 이모지가 빠집니다.</span></div>
<div class="bar"><b>3. 결제 현황</b> <select id="fst" onchange="draw()"><option value="">전체</option><option value="UNPAID">미결제</option><option value="PAID">결제완료</option><option value="WAIVED">면제</option><option value="CANCELLED">취소</option></select>
 <input id="q" placeholder="이름·휴대폰·주문번호" oninput="draw()"><button class="g" onclick="load()">새로고침</button><span class="hint" id="upd"></span></div>
<div class="bar" id="odbar" style="display:none;border-color:#E8332A"><b style="color:#B71F18">결제 기한 경과</b><span class="hint" id="odtxt"></span>
 <button class="r" onclick="cancelOverdue()">미결제 고객 원주문 일괄 취소(환불)</button></div>
<table><thead><tr><th>상태</th><th>고객</th><th>원주문</th><th>청구액</th><th>결제</th><th>문자</th><th>메일</th><th>열람</th><th>링크</th><th></th></tr></thead><tbody id="tb"></tbody></table>
</main><div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let D={rows:[],summary:{}};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const won=n=>Number(n||0).toLocaleString('ko-KR')+'원';const LAB={PAID:'결제완료',UNPAID:'미결제',WAIVED:'면제',CANCELLED:'취소',VOID:'원주문취소'};
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500)}
async function api(u,o){const r=await fetch(u,o);const j=await r.json().catch(()=>({}));if(r.status===401||(r.status===403&&j.detail==='forbidden')){location.reload();throw new Error('세션 만료')}if(!r.ok)throw new Error(j.detail||'오류');return j}
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
async function load(){try{D=await api('/admin/api/shipfee/list');const s=D.summary;
 $('#cards').innerHTML=[['청구 고객',s.customers+'명',s.orders+'주문'],['결제완료',s.PAID.n+'명',won(s.PAID.amount)],['미결제',s.UNPAID.n+'명',won(s.UNPAID.amount)],
  ['면제·취소',(s.WAIVED.n+s.CANCELLED.n)+'명',''],['원주문 취소',s.VOID.n+'명','청구 없음'],['문자 미발송',s.unsent+'명',''],['메일 미발송',s.unmailed+'명',''],['중복결제',s.dup_paid+'건',s.dup_paid?'환불 필요':''],['부분환불 필요',s.refund_flag+'건','']]
  .map(c=>`<div class="c"><small>${c[0]}</small><b>${c[1]}</b> <span class="hint">${c[2]}</span></div>`).join('');
 $('#upd').textContent='갱신 '+new Date().toLocaleTimeString('ko-KR');
 if(document.activeElement!==$('#dl'))$('#dl').value=D.deadline||'';$('#dlst').textContent=D.overdue?'기한 경과':'기한 전 — 고객 결제 가능';
 $('#odbar').style.display=D.overdue?'flex':'none';$('#odtxt').textContent=D.overdue?` 미결제 ${s.UNPAID.n}명 — 안내문 기준 원주문 취소 대상`:'';draw()}catch(e){toast(e.message)}}
function draw(){const f=$('#fst').value,q=($('#q').value||'').trim().toLowerCase();
 $('#tb').innerHTML=D.rows.filter(r=>(!f||r.state===f)&&(!q||(r.name+r.phone+r.orders.join(' ')).toLowerCase().includes(q))).map(r=>`<tr>
 <td><span class="st ${r.state}">${LAB[r.state]||r.state}</span>${r.dup_paid?`<div class="dup">중복결제 ${r.dup_paid}건</div>`:''}${r.pending&&r.state==='UNPAID'?`<div class="hint">결제창 진행 ${r.pending}</div>`:''}</td>
 <td><b>${esc(r.name)}</b>${(r.regions||[]).map(g=>` <span class="st" style="background:#e8f3ff;color:#1a5fb4">${g==='jeju'?'제주':'도서산간'}</span>`).join('')}<div class="mono">${esc(r.phone)}</div></td><td class="mono">${r.orders.map(o=>esc(o)).join("<br>")}</td>
 <td><b>${won(r.state==='UNPAID'||r.state==='VOID'?r.due:r.amount)}</b>${r.due<r.amount&&r.state!=='PAID'?`<div class="hint" style="text-decoration:line-through">${won(r.amount)}</div>`:''}${r.orders.length>r.shipments&&r.shipments?`<div class="hint">주문 ${r.orders.length}건 · 배송지 ${r.shipments}곳</div>`:''}${r.cancelled_orders.length?`<div class="hint" style="color:#B71F18">취소 ${r.cancelled_orders.length}건</div>`:''}</td><td>${r.paid_order?`<div class="mono">${esc(r.paid_order)}</div><div class="hint">${esc(r.paid_at)} ${esc(r.paid_method)}</div>`:''}</td>
 <td class="hint">${r.sent_count?esc(r.sent_at)+' ('+r.sent_count+'회)':'미발송'}</td><td class="hint">${r.email?(r.mail_count?esc(r.mail_at)+' ('+r.mail_count+'회)':'미발송'):'-'}</td><td class="hint">${r.views?r.views+'회<br>'+esc(r.view_at):'-'}</td>
 <td><button class="sm g" onclick="cp('${esc(r.link)}')">복사</button></td>
 <td>${r.state==='UNPAID'?`<button class="sm g" onclick="st('${r.id}','WAIVED')">면제</button> <button class="sm g" onclick="send1('${r.id}')">문자</button>`:''}${r.state==='WAIVED'||r.state==='CANCELLED'?`<button class="sm g" onclick="st('${r.id}','OPEN')">재청구</button>`:''}${r.note?`<div class="hint">${esc(r.note)}</div>`:''}</td></tr>`).join('')||'<tr><td colspan="10" class="hint" style="padding:30px;text-align:center">청구 내역이 없습니다 — 위 [대상 미리보기] 후 [청구 생성]</td></tr>'}
let _genBusy=false;
async function gen(dry){if(_genBusy)return;_genBusy=true;document.querySelectorAll('.bar button').forEach(b=>{if(/청구 생성|대상 미리보기/.test(b.textContent))b.disabled=true});
 try{await gen2(dry)}finally{_genBusy=false;document.querySelectorAll('.bar button').forEach(b=>b.disabled=false)}}
async function gen2(dry){try{if(!dry&&!confirm('청구를 생성합니다 (문자는 발송되지 않습니다). 진행할까요?'))return;
 const d=await post('/admin/api/shipfee/generate',{since:$('#since').value,dry_run:dry});
 if(dry){toast(`대상 ${d.customers}명 · ${d.orders}주문 · 배송지 ${d.shipments}곳(같은 주소 ${d.merged}건 합산) · ${won(d.amount)}`);return}
 if(!d.customers){toast('새로 청구할 대상이 없습니다');return}toast(`청구 생성 ${d.customers}명 · ${won(d.amount)}`);load()}catch(e){toast(e.message)}}
const CH={sms:'문자(LMS)',email:'메일'};
async function sendPv(channel){try{const d=await post('/admin/api/shipfee/send',{scope:'unsent',preview:true,channel});
 if(!d.count){toast('발송 대상이 없습니다');return}alert(`${CH[channel]} 발송 대상 ${d.count}명${d.dry?' (발송 설정 없음 — 실제 발송 안 됨)':''}${d.subject?'\n제목: '+d.subject:''}\n\n── 예시 ──\n${d.sample}`)}catch(e){toast(e.message)}}
async function send(scope,channel){try{const pv=await post('/admin/api/shipfee/send',{scope,preview:true,channel});if(!pv.count){toast('발송 대상이 없습니다');return}
 if(!confirm(`${pv.count}명에게 추가 배송비 안내 ${CH[channel]}을(를) 발송합니다.${pv.dry?'\n(발송 설정 없음 — 기록만 남습니다)':''}\n\n진행할까요?`))return;
 const d=await post('/admin/api/shipfee/send',{scope,channel});toast(`${d.queued}명 발송 시작 — 잠시 후 새로고침`);setTimeout(load,4000)}catch(e){toast(e.message)}}
async function saveDl(){try{const d=await post('/admin/api/shipfee/conf',{deadline:$('#dl').value});toast('결제 기한 저장: '+d.deadline.replace('T',' '));load()}catch(e){toast(e.message)}}
async function cancelOverdue(){try{const pv=await post('/admin/api/shipfee/cancel-overdue',{preview:true});
 if(!pv.orders){toast('취소 대상이 없습니다');return}
 const c=prompt(`결제 기한이 지난 미결제 고객 ${pv.customers}명의 원주문 ${pv.orders}건을 취소·환불합니다.\n되돌릴 수 없습니다. 진행하려면 '일괄취소'를 입력하세요.`);
 if(c!=='일괄취소'){if(c!==null)toast('확인 문구가 일치하지 않습니다');return}
 const d=await post('/admin/api/shipfee/cancel-overdue',{preview:false,confirm:c});toast(`${d.queued}건 취소 진행 중 — 잠시 후 새로고침`);setTimeout(load,8000)}catch(e){toast(e.message)}}
async function send1(id){if(!confirm('이 고객에게 안내 문자를 발송할까요?'))return;try{const d=await post('/admin/api/shipfee/send',{ids:[id]});toast(d.queued?'발송 시작':'발송 대상 아님');setTimeout(load,3000)}catch(e){toast(e.message)}}
async function st(id,s){const note=s==='WAIVED'?prompt('면제 사유 (선택)')||'':'';if(s==='WAIVED'&&note===null)return;
 try{await post('/admin/api/shipfee/status',{id,status:s,note});toast('변경했습니다');load()}catch(e){toast(e.message)}}
function cp(u){(navigator.clipboard?navigator.clipboard.writeText(u):Promise.reject()).then(()=>toast('링크를 복사했습니다')).catch(()=>prompt('링크',u))}
let TX=null;
async function saveEx(){try{const d=await post('/admin/api/shipfee/conf',{extra:{jeju:Number($('#exj').value||0),remote:Number($('#exr').value||0)}});
 toast(`추가 배송비 저장 — 제주 +${won(d.extra.jeju)} · 도서산간 +${won(d.extra.remote)}`);load()}catch(e){toast(e.message)}}
async function loadTx(){try{TX=await api('/admin/api/shipfee/conf');
 if(TX.extra){$('#exj').value=TX.extra.jeju;$('#exr').value=TX.extra.remote}
 $('#txf').innerHTML=TX.fields.map(f=>`<label style="font-weight:700;padding-top:6px">${esc(f.label)}</label><textarea data-k="${f.key}" rows="${f.max>1500?(f.key==='message'?16:5):(f.max>100?3:1)}" maxlength="${f.max}" style="width:100%;font:inherit;padding:7px 8px;border:1px solid #ccc">${esc(TX.texts[f.key]||'')}</textarea>`).join('');
 $('#txst').textContent=''}catch(e){toast(e.message)}}
function txVals(){const o={};document.querySelectorAll('#txf textarea').forEach(t=>o[t.dataset.k]=t.value);return o}
async function saveTx(){try{const d=await post('/admin/api/shipfee/conf',{texts:txVals()});TX.texts=d.texts;toast('문구를 저장했습니다 — 모든 고객 링크에 반영');$('#txst').textContent='저장됨 '+new Date().toLocaleTimeString('ko-KR');loadTx()}catch(e){toast(e.message)}}
async function resetTx(){if(!confirm('모든 문구를 기본 문구로 되돌릴까요?'))return;const o={};TX.fields.forEach(f=>o[f.key]='');try{await post('/admin/api/shipfee/conf',{texts:o});toast('기본 문구로 되돌렸습니다');loadTx()}catch(e){toast(e.message)}}
function pvTx(){const r=(D.rows||[]).find(x=>x.state==='UNPAID');if(!r){toast('미리볼 미결제 청구가 없습니다');return}window.open(r.link,'_blank')}
load();loadTx();setInterval(load,60000);
</script></body></html>'''


@shipfee_router.get('/admin/shipfee')
def admin_page(request: Request):
    try:
        _actor(request, 0, '추가 배송비 조회')
    except HTTPException as e:
        if e.status_code in (401, 403) and 'forbidden' in str(e.detail):   # 미로그인·세션만료 → 관리자 로그인 화면
            return HTMLResponse(_av().LOGIN_HTML)
        return HTMLResponse('<meta charset=utf-8><body style="font-family:sans-serif;padding:60px;text-align:center">'
                            '<h3>%s</h3><a href="/admin/dashboard">대시보드로 돌아가기</a>' % _h(e.detail), status_code=e.status_code)
    return HTMLResponse(_ADMIN, headers={'Cache-Control': 'no-store'})


# ─────────────────────────── 원주문 취소 ↔ 배송비 결제 연동 ───────────────────────────
def links_for_order(oid):
    """원주문 ↔ 추가 배송비 결제 연결 (2026-10-22) — 주문 상세(관리자·마이페이지)에서 함께 보여준다.
    원주문이면 그 주문이 묶인 청구와 배송비 결제(SF-)를, SF- 결제면 원주문 목록을 돌려준다. 없으면 None."""
    oid = str(oid or '')
    if not oid:
        return None
    av = _av()
    try:
        ensure()
        if oid.startswith(SF_PREFIX):
            r = av.one('SELECT items FROM orders WHERE order_id=?', (oid,))
            cid = next((str(it.get('id'))[len(SF_ITEM):] for it in (av.jload((r or {}).get('items'), []) or [])
                        if str((it or {}).get('id') or '').startswith(SF_ITEM)), '')
            ch = av.one('SELECT * FROM ship_charges WHERE id=?', (cid,)) if cid else None
        else:
            ch = av.one('SELECT * FROM ship_charges WHERE order_ids LIKE ?', ('%"' + oid + '"%',))
    except Exception:
        return None
    if not ch:
        return None
    pays = []
    for r in av.rows("SELECT order_id, status, amount, paid_at, created, pay_method, receipt_url FROM orders "
                     "WHERE order_id LIKE ? AND items LIKE ? ORDER BY created", (SF_PREFIX + '%', '%' + SF_ITEM + ch['id'] + '%')):
        if r.get('status') not in ('PAID', 'CANCELLED'):
            continue                                   # 결제창만 열고 끝낸 시도(PENDING·FAILED)는 표시하지 않음
        pays.append({'order_id': r['order_id'], 'status': r.get('status'), 'amount': av.num(r.get('amount')),
                     'paid_at': str(r.get('paid_at') or r.get('created') or '')[:16].replace('T', ' '),
                     'method': r.get('pay_method') or '', 'receipt': r.get('receipt_url') or ''})
    paid = [p for p in pays if p['status'] == 'PAID']
    st = 'PAID' if paid else (ch.get('status') or 'OPEN')
    label = {'PAID': '결제완료', 'OPEN': '미결제', 'UNPAID': '미결제', 'WAIVED': '청구 면제', 'CANCELLED': '청구 취소'}.get(st, st)
    return {'charge_id': ch['id'], 'state': st, 'state_kr': label, 'charge_amount': av.num(ch.get('amount')),
            'orders': _orders_of(ch), 'payments': pays, 'paid_amount': sum(p['amount'] for p in paid),
            'created': str(ch.get('created') or '')[:16].replace('T', ' ')}


def on_order_cancelled(actor, oid):
    """원주문이 취소(환불)된 직후 admin_v2._order_cancel_core 가 호출한다 — 실패해도 원주문 취소에는 영향 없음.
    · 미결제 청구: 청구액이 실시간으로 줄어든다(due_of) — 여기서 할 일 없음.
    · 결제완료 청구: 남은 원주문 기준 청구액이 0원이면 배송비 결제(SF 주문)를 자동 취소·환불한다.
      일부만 줄었으면(배송지가 여러 곳인 고객) 부분환불 금액을 메모로 남겨 관리자가 처리하게 한다."""
    oid = str(oid or '')
    if not oid or oid.startswith(SF_PREFIX):
        return None
    av = _av()
    try:
        ensure()
        ch = av.one('SELECT * FROM ship_charges WHERE order_ids LIKE ?', ('%"' + oid + '"%',))
    except Exception:
        return None
    if not ch:
        return None
    paid = (_sf_map().get(ch['id']) or {}).get('paid') or []
    if not paid:
        return {'charge': ch['id'], 'action': 'due_reduced', 'due': due_of(ch)}
    due = due_of(ch)
    paid_amt = sum(p['amount'] for p in paid)
    if due <= 0:
        results = []
        for p in paid:
            r = av.one('SELECT * FROM orders WHERE order_id=?', (p['order_id'],))
            if not r or r.get('status') == 'CANCELLED':
                continue
            try:
                av._order_cancel_core(actor, r, '원주문 %s 취소에 따른 추가 배송비 환불' % oid)
                results.append(p['order_id'])
            except Exception as e:
                msg = '배송비 자동환불 실패(%s): %s — 주문 상세에서 직접 환불 필요' % (p['order_id'], getattr(e, 'detail', e))
                av.run('UPDATE ship_charges SET note=? WHERE id=?', (msg[:200], ch['id']))
                av.audit(actor, '추가배송비환불실패', ch['id'], msg[:200])
        if results:
            av.audit(actor, '추가배송비자동환불', ch['id'], '원주문 %s 취소 → %s 환불' % (oid, ', '.join(results)))
        return {'charge': ch['id'], 'action': 'refunded', 'orders': results}
    if due < paid_amt:
        msg = '부분환불 필요 %s원 (원주문 %s 취소 · 결제 %s원 → 청구 %s원)' % (
            format(paid_amt - due, ','), oid, format(paid_amt, ','), format(due, ','))
        av.run('UPDATE ship_charges SET note=? WHERE id=?', (msg[:200], ch['id']))
        av.audit(actor, '추가배송비부분환불필요', ch['id'], msg)
        return {'charge': ch['id'], 'action': 'partial_refund_needed', 'amount': paid_amt - due}
    return {'charge': ch['id'], 'action': 'none'}


@shipfee_router.post('/admin/api/shipfee/cancel-overdue')
def api_cancel_overdue(request: Request, body: dict = Body(...)):
    """결제 기한 경과 미결제 고객의 원주문 일괄 취소(환불). 기한 전에는 실행 불가.
    preview=true 면 대상만 집계. 실행은 '일괄취소' 확인 문구 필수 · 백그라운드 처리 · 건별 결과를 메모에 기록."""
    a = _actor(request, 2, '배송비 미결제 주문 일괄 취소')
    if not overdue():
        raise HTTPException(409, '결제 기한(%s) 전에는 실행할 수 없습니다' % conf()['deadline'].replace('T', ' '))
    av = _av()
    rows_ = [r for r in _list_rows() if r['state'] == 'UNPAID' and r['due'] > 0]
    targets = [(r, o) for r in rows_ for o in r['orders'] if o not in r['cancelled_orders']]
    if body.get('preview', True):
        return {'ok': True, 'customers': len(rows_), 'orders': len(targets)}
    if str(body.get('confirm') or '') != '일괄취소':
        raise HTTPException(400, "확인 문구 '일괄취소'를 입력하세요")

    def work():
        ok = fail = 0
        for r, o in targets:
            row = av.one('SELECT * FROM orders WHERE order_id=?', (o,))
            if not row or row.get('status') == 'CANCELLED':
                continue
            try:
                av._order_cancel_core(a, row, '추가 배송비 미결제 — 결제 기한(%s) 경과 취소' % conf()['deadline'].replace('T', ' '))
                ok += 1
            except Exception as e:
                fail += 1
                try: av.run('UPDATE ship_charges SET note=? WHERE id=?',
                            (('원주문 %s 취소 실패: %s' % (o, getattr(e, 'detail', e)))[:200], r['id']))
                except Exception: pass
        av.audit(a, '배송비미결제일괄취소', 'shipfee', '성공 %d · 실패 %d' % (ok, fail))
    threading.Thread(target=work, daemon=True).start()
    return {'ok': True, 'queued': len(targets)}
