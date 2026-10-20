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
    for sql in ('CREATE INDEX IF NOT EXISTS idx_ship_charges_phone ON ship_charges(phone)',):
        try: av.run(sql)
        except Exception: pass
    _READY['ok'] = True


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


def _state(ch, m):
    if (m or {}).get('paid'):
        return 'PAID'
    st = ch.get('status') or 'OPEN'
    return st if st in ('WAIVED', 'CANCELLED') else 'UNPAID'


def _charge_by_token(token):
    ensure()
    t = str(token or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', t):
        return None
    return _av().one('SELECT * FROM ship_charges WHERE token=?', (t,))


def _orders_of(ch):
    return [str(x) for x in (_av().jload(ch.get('order_ids'), []) or []) if x]


# ─────────────────────────── 대상 산출 ───────────────────────────
def candidates(since=DEFAULT_SINCE):
    """배송비 0원으로 결제된 배송비 필수부과 주문 → 고객별 묶음. 이미 청구(취소 제외)에 포함된 주문은 제외."""
    av = _av()
    ensure()
    rs = av.rows("SELECT order_id, created, amount, discount, items, buyer, ship_method, contact_phone_norm "
                 "FROM orders WHERE created>=? AND status='PAID' AND order_id NOT LIKE ? "
                 "AND COALESCE(ship_method,'standard') NOT IN ('pickup','intl','none') ORDER BY created",
                 (since, SF_PREFIX + '%'))
    parsed, ids = [], set()
    for r in rs:
        its = [x for x in (av.jload(r.get('items'), []) or []) if isinstance(x, dict)]
        sub = sum(av.num(x.get('p')) * max(1, av.num(x.get('q')) or 1) for x in its)
        disc = av.num(r.get('discount'))
        if av.num(r.get('amount')) - sub + disc != 0:          # 배송비를 이미 냈거나 금액 구조가 다른 주문
            continue
        parsed.append((r, its))
        ids.update(str(x.get('id') or '') for x in its)
    rules = av.product_rules([{'id': i} for i in ids if i])     # 상품별 1회 판정
    covered = set()
    for ch in av.rows("SELECT order_ids FROM ship_charges WHERE COALESCE(status,'OPEN')<>'CANCELLED'"):
        covered.update(_orders_of(ch))
    groups = {}
    for r, its in parsed:
        if r['order_id'] in covered:
            continue
        if not any((rules.get(str(x.get('id') or '')) or {}).get('ship_required') for x in its):
            continue
        b = av.jload(r.get('buyer'), {}) or {}
        ph = av.digits(r.get('contact_phone_norm') or b.get('phone') or '')
        key = ph or ('order:' + r['order_id'])
        g = groups.setdefault(key, {'phone': ph, 'name': str(b.get('name') or '')[:30],
                                    'email': str(b.get('email') or '').lower()[:120], 'orders': []})
        g['orders'].append(r['order_id'])
    return list(groups.values())


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
    gs = candidates(since)
    n_orders = sum(len(g['orders']) for g in gs)
    if body.get('dry_run', True):
        return {'ok': True, 'dry_run': True, 'customers': len(gs), 'orders': n_orders, 'amount': n_orders * SF_FEE}
    av = _av()
    made = 0
    for g in gs:
        av.run('INSERT INTO ship_charges(id,token,phone,name,email,order_ids,amount,status,created,created_by,sent_count,view_count) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,0,0)',
               (av.uid(), secrets.token_urlsafe(18), g['phone'], g['name'], g['email'],
                json.dumps(g['orders']), SF_FEE * len(g['orders']), 'OPEN', av.now_iso(), a['name']))
        made += 1
    av.audit(a, '추가배송비청구생성', since, '%d명 · %d주문 · %s원' % (made, n_orders, format(n_orders * SF_FEE, ',')))
    return {'ok': True, 'dry_run': False, 'customers': made, 'orders': n_orders, 'amount': n_orders * SF_FEE}


def _list_rows():
    ensure()
    av = _av()
    m = _sf_map()
    out = []
    for ch in av.rows('SELECT * FROM ship_charges ORDER BY created, name'):
        mm = m.get(ch['id']) or {}
        st = _state(ch, mm)
        paid = mm.get('paid') or []
        out.append({'id': ch['id'], 'name': ch.get('name') or '', 'phone': ch.get('phone') or '',
                    'email': ch.get('email') or '', 'orders': _orders_of(ch), 'amount': av.num(ch.get('amount')),
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
    for k in ('PAID', 'UNPAID', 'WAIVED', 'CANCELLED'):
        sel = [r for r in rs if r['state'] == k]
        summ[k] = {'n': len(sel), 'amount': sum(r['amount'] for r in sel)}
    summ['dup_paid'] = sum(r['dup_paid'] for r in rs)
    summ['unsent'] = len([r for r in rs if r['state'] == 'UNPAID' and not r['sent_count']])
    return {'rows': rs, 'summary': summ}


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
    if _state(ch, _sf_map().get(ch['id'])) == 'PAID':
        raise HTTPException(409, '이미 결제된 청구입니다 — 환불은 해당 SF 주문의 주문 상세에서 처리하세요')
    note = str(body.get('note') or ch.get('note') or '')[:200]
    _av().run('UPDATE ship_charges SET status=?, note=? WHERE id=?', (st, note, ch['id']))
    _av().audit(a, '추가배송비상태', ch['id'], '%s → %s %s' % (ch.get('status'), st, note))
    return {'ok': True}


_SMS_TEXT = ('[맵달SEOUL] 추가 배송비 결제 안내\n\n'
             '{name}님, 맵달SEOUL을 이용해 주셔서 감사합니다.\n\n'
             '예약판매·이벤트 등 프로모션 상품은 구매 수량·금액과 관계없이 주문당 배송비 3,000원이 부과되는 상품입니다. '
             '판매 페이지 이전 과정의 시스템 오류로 고객님의 주문({orders})에 배송비가 누락되어 결제되었습니다.\n\n'
             '번거로우시겠지만 아래 고객님 전용 링크에서 배송비 {amount}원을 결제해 주시기 바랍니다. 불편을 드려 진심으로 죄송합니다.\n'
             '{link}\n\n'
             '문의: cx@mealzip.kr')


def sms_text(r):
    av = _av()
    return _SMS_TEXT.format(name=(r.get('name') or '고객'), orders=', '.join(r['orders'][:3]) + (' 외' if len(r['orders']) > 3 else ''),
                            amount=format(av.num(r['amount']), ','), link=r['link'])


@shipfee_router.post('/admin/api/shipfee/send')
def api_send(request: Request, body: dict = Body(...)):
    """문자(LMS) 발송 — scope='unsent'(미결제·미발송) | 'unpaid'(미결제 전체 재발송) | ids 지정. 백그라운드 처리."""
    a = _actor(request, 2, '추가 배송비 문자 발송')
    av = _av()
    scope = str(body.get('scope') or 'unsent')
    ids = set(str(x) for x in (body.get('ids') or []))
    rs = [r for r in _list_rows() if r['state'] == 'UNPAID' and r['phone'] and len(r['phone']) >= 10
          and ((r['id'] in ids) if ids else (scope == 'unpaid' or not r['sent_count']))]
    if not rs:
        return {'ok': True, 'queued': 0}
    cf = av.solapi_conf()
    dry = not (cf.get('key') and cf.get('sec') and cf.get('sender'))
    if body.get('preview'):
        return {'ok': True, 'queued': 0, 'count': len(rs), 'dry': dry, 'sample': sms_text(rs[0])}

    def work():
        ok = fail = 0
        for r in rs:
            text = sms_text(r)
            status, detail = 'DRY', '발송사 미설정 — 기록만'
            if not dry:
                try:
                    res = av.solapi_send({'to': r['phone'], 'from': av.digits(cf['sender']), 'text': text,
                                          'type': 'LMS', 'subject': '맵달SEOUL 추가 배송비 안내'})
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
 var btn=this;btn.disabled=true;btn.textContent='결제창 준비중…';
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
 btn.disabled=false;btn.textContent='배송비 결제하기';
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
                            '결제금액 %s원 · 결제번호 %s<br>협조해 주셔서 감사합니다. 주문하신 상품은 예정대로 발송됩니다.'
                            % (format(av.num(r['amount']), ','), _h(r['order_id'])))
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
    try:
        av.run('UPDATE ship_charges SET view_at=?, view_count=COALESCE(view_count,0)+1 WHERE id=?', (av.now_iso(), ch['id']))
    except Exception:
        pass
    m = _sf_map().get(ch['id'])
    st = _state(ch, m)
    if st == 'PAID':
        p = m['paid'][0]
        return _status_page('✅', '이미 결제가 완료되었습니다',
                            '추가 배송비 %s원 · %s 결제<br>추가로 결제하실 필요가 없습니다.'
                            % (format(p['amount'], ','), _h(p['paid_at'][:16].replace('T', ' '))))
    if st in ('WAIVED', 'CANCELLED'):
        return _status_page('ℹ️', '결제하실 금액이 없습니다', '이 안내는 더 이상 결제가 필요하지 않습니다.')
    ods = _order_summ(_orders_of(ch))
    amt = av.num(ch.get('amount'))
    body = ('<h1>추가 배송비 결제</h1>'
            + ('<div class="err">결제가 완료되지 않았습니다: %s</div>' % _h(fail) if fail else '')
            + '<div class="box"><p class="p"><b>%s</b>님, 맵달SEOUL을 이용해 주셔서 감사합니다.<br><br>'
              '예약판매·이벤트 등 프로모션 상품은 구매 수량·금액과 관계없이 <b>주문당 배송비 3,000원</b>이 부과됩니다. '
              '판매 페이지 이전 과정의 시스템 오류로 아래 주문에 배송비가 누락되어 결제되었습니다. '
              '번거로우시겠지만 추가 배송비를 결제해 주시기 바랍니다. 불편을 드려 죄송합니다.</p></div>' % _h(_mask(ch.get('name')))
            + '<div class="box">' + ''.join('<div class="od"><div class="n">%s · %s</div>%s</div>'
                                              % (_h(o['oid']), _h(o['date']), _h(o['label'])) for o in ods)
            + '<div class="od amt"><span>추가 배송비 (%d건 × 3,000원)</span><b>%s원</b></div></div>' % (len(ods) or 1, format(amt, ','))
            + '<div class="box"><div style="font-weight:700;font-size:14px">결제수단</div><div class="pm">'
              '<label><input type="radio" name="pm" value="card" checked>카드·간편결제</label>'
              '<label><input type="radio" name="pm" value="directbank">계좌이체</label>'
              '<label><input type="radio" name="pm" value="hpp">휴대폰</label></div>'
              '<label class="ag"><input type="checkbox" id="ag"> 위 주문의 추가 배송비 결제 내용을 확인했습니다.</label>'
              '<button id="pay">배송비 결제하기</button>'
              '<p class="hint" style="margin-top:10px">이 페이지는 받으신 문자의 링크로만 열리는 고객님 전용 결제 페이지입니다. '
              '결제 즉시 자동으로 확인되며 별도 연락은 필요하지 않습니다.<br>문의: cx@mealzip.kr</p></div>')
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
    st = _state(ch, _sf_map().get(ch['id']))
    if st == 'PAID':
        raise HTTPException(409, '이미 결제가 완료되었습니다')
    if st != 'UNPAID':
        raise HTTPException(409, '결제하실 금액이 없습니다')
    pay_sel = _PAY_METHODS.get(str(body.get('payMethod') or 'card').lower())
    if not pay_sel:
        raise HTTPException(400, '지원하지 않는 결제수단입니다')
    A, av = _app(), _av()
    amount = av.num(ch.get('amount'))
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
<div class="bar"><b>2. 안내 발송</b><button class="g" onclick="sendPv()">문자 미리보기</button><button class="r" onclick="send('unsent')">미발송 고객에게 문자 발송</button>
 <button class="g" onclick="send('unpaid')">미결제 고객 전체 재발송</button><a class="btn g" href="/admin/api/shipfee.csv">CSV 다운로드 (링크 포함)</a>
 <span class="hint">문자는 LMS로 발송되며 결제가 끝난 고객에게는 보내지 않습니다.</span></div>
<div class="bar"><b>3. 결제 현황</b> <select id="fst" onchange="draw()"><option value="">전체</option><option value="UNPAID">미결제</option><option value="PAID">결제완료</option><option value="WAIVED">면제</option><option value="CANCELLED">취소</option></select>
 <input id="q" placeholder="이름·휴대폰·주문번호" oninput="draw()"><button class="g" onclick="load()">새로고침</button><span class="hint" id="upd"></span></div>
<table><thead><tr><th>상태</th><th>고객</th><th>원주문</th><th>금액</th><th>결제</th><th>문자</th><th>열람</th><th>링크</th><th></th></tr></thead><tbody id="tb"></tbody></table>
</main><div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let D={rows:[],summary:{}};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const won=n=>Number(n||0).toLocaleString('ko-KR')+'원';const LAB={PAID:'결제완료',UNPAID:'미결제',WAIVED:'면제',CANCELLED:'취소'};
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500)}
async function api(u,o){const r=await fetch(u,o);const j=await r.json().catch(()=>({}));if(r.status===401||(r.status===403&&j.detail==='forbidden')){location.reload();throw new Error('세션 만료')}if(!r.ok)throw new Error(j.detail||'오류');return j}
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
async function load(){try{D=await api('/admin/api/shipfee/list');const s=D.summary;
 $('#cards').innerHTML=[['청구 고객',s.customers+'명',s.orders+'주문'],['결제완료',s.PAID.n+'명',won(s.PAID.amount)],['미결제',s.UNPAID.n+'명',won(s.UNPAID.amount)],
  ['면제·취소',(s.WAIVED.n+s.CANCELLED.n)+'명',''],['문자 미발송',s.unsent+'명',''],['중복결제',s.dup_paid+'건',s.dup_paid?'환불 필요':'']]
  .map(c=>`<div class="c"><small>${c[0]}</small><b>${c[1]}</b> <span class="hint">${c[2]}</span></div>`).join('');
 $('#upd').textContent='갱신 '+new Date().toLocaleTimeString('ko-KR');draw()}catch(e){toast(e.message)}}
function draw(){const f=$('#fst').value,q=($('#q').value||'').trim().toLowerCase();
 $('#tb').innerHTML=D.rows.filter(r=>(!f||r.state===f)&&(!q||(r.name+r.phone+r.orders.join(' ')).toLowerCase().includes(q))).map(r=>`<tr>
 <td><span class="st ${r.state}">${LAB[r.state]||r.state}</span>${r.dup_paid?`<div class="dup">중복결제 ${r.dup_paid}건</div>`:''}${r.pending&&r.state==='UNPAID'?`<div class="hint">결제창 진행 ${r.pending}</div>`:''}</td>
 <td><b>${esc(r.name)}</b><div class="mono">${esc(r.phone)}</div></td><td class="mono">${r.orders.map(o=>esc(o)).join("<br>")}</td>
 <td><b>${won(r.amount)}</b></td><td>${r.paid_order?`<div class="mono">${esc(r.paid_order)}</div><div class="hint">${esc(r.paid_at)} ${esc(r.paid_method)}</div>`:''}</td>
 <td class="hint">${r.sent_count?esc(r.sent_at)+' ('+r.sent_count+'회)':'미발송'}</td><td class="hint">${r.views?r.views+'회<br>'+esc(r.view_at):'-'}</td>
 <td><button class="sm g" onclick="cp('${esc(r.link)}')">복사</button></td>
 <td>${r.state==='UNPAID'?`<button class="sm g" onclick="st('${r.id}','WAIVED')">면제</button> <button class="sm g" onclick="send1('${r.id}')">문자</button>`:''}${r.state==='WAIVED'||r.state==='CANCELLED'?`<button class="sm g" onclick="st('${r.id}','OPEN')">재청구</button>`:''}${r.note?`<div class="hint">${esc(r.note)}</div>`:''}</td></tr>`).join('')||'<tr><td colspan="9" class="hint" style="padding:30px;text-align:center">청구 내역이 없습니다 — 위 [대상 미리보기] 후 [청구 생성]</td></tr>'}
async function gen(dry){try{const d=await post('/admin/api/shipfee/generate',{since:$('#since').value,dry_run:dry});
 if(dry){toast(`대상 ${d.customers}명 · ${d.orders}주문 · ${won(d.amount)}`);return}
 if(!d.customers){toast('새로 청구할 대상이 없습니다');return}toast(`청구 생성 ${d.customers}명 · ${won(d.amount)}`);load()}catch(e){toast(e.message)}}
async function sendPv(){try{const d=await post('/admin/api/shipfee/send',{scope:'unsent',preview:true});
 if(!d.count){toast('발송 대상이 없습니다');return}alert(`발송 대상 ${d.count}명${d.dry?' (문자 발송사 미설정 — 실제 발송 안 됨)':''}\n\n── 예시 ──\n${d.sample}`)}catch(e){toast(e.message)}}
async function send(scope){try{const pv=await post('/admin/api/shipfee/send',{scope,preview:true});if(!pv.count){toast('발송 대상이 없습니다');return}
 if(!confirm(`${pv.count}명에게 추가 배송비 안내 문자(LMS)를 발송합니다.${pv.dry?'\n(발송사 미설정 — 기록만 남습니다)':''}\n\n진행할까요?`))return;
 const d=await post('/admin/api/shipfee/send',{scope});toast(`${d.queued}명 발송 시작 — 잠시 후 새로고침`);setTimeout(load,4000)}catch(e){toast(e.message)}}
async function send1(id){if(!confirm('이 고객에게 안내 문자를 발송할까요?'))return;try{const d=await post('/admin/api/shipfee/send',{ids:[id]});toast(d.queued?'발송 시작':'발송 대상 아님');setTimeout(load,3000)}catch(e){toast(e.message)}}
async function st(id,s){const note=s==='WAIVED'?prompt('면제 사유 (선택)')||'':'';if(s==='WAIVED'&&note===null)return;
 try{await post('/admin/api/shipfee/status',{id,status:s,note});toast('변경했습니다');load()}catch(e){toast(e.message)}}
function cp(u){(navigator.clipboard?navigator.clipboard.writeText(u):Promise.reject()).then(()=>toast('링크를 복사했습니다')).catch(()=>prompt('링크',u))}
load();setInterval(load,60000);
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
