"""포인트 결제 사용 (2026-11-06 · 대표 지시 — 운영팀 요청)

결제 시 포인트를 차감해 결제금액을 줄인다. 포인트는 두 갈래로 나눠 관리한다.
  · 적립금(POINT)  — 구매 적립·가입 혜택·관리자 조정 등 일반 포인트
  · 기타 할인(REWARD) — 이세계아이돌 예약판매 보상 적립 1% (고객 표기 '이세계아이돌 예약판매 1%')
상품·이벤트마다 관리자 체크박스 [적립금 사용 가능]·[기타 할인]이 켜진 상품 금액에만 쓸 수 있다
(판정은 admin_v2.product_rules — points_ok / etc_ok).

흐름
  1) 주문 생성(/api/orders) 직전 spend() 가 잔액을 잠그고 원장에 ORDER_USE_* 로 차감한다.
     주문 트랜잭션이 실패하면 restore() 로 즉시 되돌린다.
  2) 결제 실패(FAILED)·주문 취소(_order_cancel_core: 관리자 취소·환불·입금기한 만료·미결제 정리)는
     restore() 로 사용분을 돌려준다(원장 ORDER_RESTORE_*, 멱등).
  3) 결제창을 닫고 떠난 주문(PENDING)은 같은 고객이 20분 뒤 다시 결제할 때 자동으로 풀어준다(release_stale).
     풀린 뒤에 그 주문이 늦게 결제완료되면 on_paid() 가 사용분을 다시 차감한다(ORDER_REUSE_*).
잔액은 customer_profiles.points_balance(전체) 를 기준으로, 기타 할인 몫은 원장 합계로 나눈다.
"""
import datetime

from fastapi import APIRouter, HTTPException, Request

pointpay_router = APIRouter()

REWARD_LABEL = '이세계아이돌 예약판매 1%'
POINT_LABEL = '적립금'
MIN_PAY = 100                 # PG 최소 결제금액 (쿠폰과 동일 기준)
STALE_MIN = 20                # 결제 미완료 주문의 포인트를 풀어주는 시간(분)
BUCKETS = ('POINT', 'REWARD')
REWARD_TYPES = ('COMPENSATION', 'COMPENSATION_REVOKE',
                'ORDER_USE_REWARD', 'ORDER_RESTORE_REWARD', 'ORDER_REUSE_REWARD')
USE_TYPES = tuple('%s_%s' % (p, b) for p in ('ORDER_USE', 'ORDER_RESTORE', 'ORDER_REUSE') for b in BUCKETS)
TYPE_NAMES = {'ORDER_USE_POINT': '적립금 사용 (주문 결제)',
              'ORDER_USE_REWARD': REWARD_LABEL + ' 사용 (주문 결제)',
              'ORDER_RESTORE_POINT': '적립금 사용 취소 (결제 미완료·주문 취소)',
              'ORDER_RESTORE_REWARD': REWARD_LABEL + ' 사용 취소 (결제 미완료·주문 취소)',
              'ORDER_REUSE_POINT': '적립금 사용 (결제완료 재반영)',
              'ORDER_REUSE_REWARD': REWARD_LABEL + ' 사용 (결제완료 재반영)'}


def _av():
    import admin_v2
    return admin_v2


def _int(v):
    try:
        return max(0, int(float(v or 0)))
    except Exception:
        return 0


def _in(types):
    return '(' + ','.join("'%s'" % t for t in types) + ')'


# ── 잔액 ─────────────────────────────────────────────────────────────────
def _split(total, reward_raw):
    reward = max(0, min(int(reward_raw or 0), int(total or 0)))
    return {'total': int(total or 0), 'reward': reward, 'point': int(total or 0) - reward}


def balances(cid):
    """{'total','point','reward'} — 기타 할인 몫은 보상 적립 원장 합계(전체 잔액 이내)."""
    av = _av()
    if not cid:
        return _split(0, 0)
    t = av.one('SELECT points_balance FROM customer_profiles WHERE id=?', (cid,)) or {}
    r = av.one('SELECT COALESCE(SUM(amount),0) AS s FROM point_ledger WHERE customer_id=? AND event_type IN '
               + _in(REWARD_TYPES), (cid,)) or {}
    return _split(av.num(t.get('points_balance')), av.num(r.get('s')))


def _write(c, av, cid, mid, rows_, bal):
    """잠근 연결 c 안에서 원장 행 기록 + 잔액 갱신. rows_=[(type, amount, key, oid, reason)]"""
    for et, amt, key, oid, why in rows_:
        bal += amt
        c.execute(av._q('INSERT INTO point_ledger(id,customer_id,member_id,event_type,amount,balance_after,'
                        'event_key,order_id,reason,expires_at,created_at,by_admin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'),
                  (av.uid(), cid, mid or '', et, amt, bal, key, oid, why[:200], '', av.now_iso(), 'SYSTEM'))
    c.execute(av._q('UPDATE customer_profiles SET points_balance=?, updated_at=? WHERE id=?'),
              (bal, av.now_iso(), cid))
    return bal


def _locked(c, av, cid):
    lock = ' FOR UPDATE' if av.IS_PG else ''
    r = c.execute(av._q('SELECT points_balance FROM customer_profiles WHERE id=?' + lock), (cid,)).fetchone()
    if not r:
        raise HTTPException(400, '포인트 계정을 찾을 수 없습니다 — 다시 로그인해 주세요')
    total = av.num(dict(r).get('points_balance'))
    rr = c.execute(av._q('SELECT COALESCE(SUM(amount),0) AS s FROM point_ledger WHERE customer_id=? AND event_type IN '
                         + _in(REWARD_TYPES)), (cid,)).fetchone()
    return _split(total, av.num(dict(rr).get('s')))


def _sync_member(av, cid, bal):
    try:
        av.run('UPDATE members SET points=? WHERE customer_id=?', (bal, cid))
    except Exception:
        pass


def spend(cid, mid, oid, use_point, use_reward):
    """주문 생성 직전 차감 — 잔액 부족이면 400(아무것도 기록하지 않음). 반환 {'point','reward'}."""
    use_point, use_reward = _int(use_point), _int(use_reward)
    if not (use_point or use_reward):
        return {'point': 0, 'reward': 0}
    av = _av()
    with av._conn() as c:
        if not av.IS_PG:
            c.execute('BEGIN IMMEDIATE')
        b = _locked(c, av, cid)
        if use_reward > b['reward']:
            raise HTTPException(400, '%s 잔액이 부족합니다 (보유 %sP) — 새로고침 후 다시 확인해 주세요'
                                % (REWARD_LABEL, format(b['reward'], ',')))
        if use_point > b['point']:
            raise HTTPException(400, '적립금 잔액이 부족합니다 (보유 %sP) — 새로고침 후 다시 확인해 주세요'
                                % format(b['point'], ','))
        rs = []
        if use_reward:
            rs.append(('ORDER_USE_REWARD', -use_reward, 'ptuse:reward:%s' % oid, oid,
                       '%s 사용 · 주문 %s' % (REWARD_LABEL, oid)))
        if use_point:
            rs.append(('ORDER_USE_POINT', -use_point, 'ptuse:point:%s' % oid, oid, '적립금 사용 · 주문 %s' % oid))
        bal = _write(c, av, cid, mid, rs, b['total'])
        c.commit()
    _sync_member(av, cid, bal)
    return {'point': use_point, 'reward': use_reward}


def _ledger(oid):
    av = _av()
    return av.rows('SELECT customer_id, member_id, event_type, amount FROM point_ledger WHERE order_id=? AND event_type IN '
                   + _in(USE_TYPES), (oid,)) or []


def usage(oid):
    """주문의 포인트 사용 현황 — used(최초 사용)·net(현재 차감 유지분)·restores(되돌린 횟수)."""
    used = {'point': 0, 'reward': 0}; net = {'point': 0, 'reward': 0}; cnt = {}
    cid = mid = ''
    for r in _ledger(oid):
        et = r['event_type']; b = 'reward' if et.endswith('_REWARD') else 'point'
        amt = int(r.get('amount') or 0)
        cid = cid or (r.get('customer_id') or ''); mid = mid or (r.get('member_id') or '')
        if et.startswith('ORDER_USE_'):
            used[b] += -amt
        net[b] += -amt
        k = et.rsplit('_', 1)[0] + ':' + b
        cnt[k] = cnt.get(k, 0) + 1
    return {'used': used, 'net': net, 'total': used['point'] + used['reward'],
            'cnt': cnt, 'customer_id': cid, 'member_id': mid}


def restore(oid, why='', statuses=None):
    """사용분을 되돌린다(멱등) — 주문이 있고 statuses 가 주어지면 그 상태일 때만. 반환: 되돌린 포인트."""
    oid = str(oid or '')
    if not oid:
        return 0
    av = _av()
    if statuses:
        o = av.one('SELECT status FROM orders WHERE order_id=?', (oid,))
        if o and o.get('status') not in statuses:
            return 0
    u = usage(oid)
    if not u['customer_id']:
        return 0
    back = 0
    for b in ('reward', 'point'):
        n = u['net'][b]
        if n <= 0:
            continue
        seq = u['cnt'].get('ORDER_RESTORE:' + b, 0)
        av.point_apply(u['customer_id'], u['member_id'], 'ORDER_RESTORE_' + b.upper(), n,
                       'ptrestore:%s:%s:%d' % (b, oid, seq),
                       '%s 사용 취소 · 주문 %s%s' % (REWARD_LABEL if b == 'reward' else '적립금', oid,
                                                  (' (%s)' % why) if why else ''),
                       order_id=oid, by_admin='SYSTEM')
        back += n
    return back


def on_paid(oid):
    """결제완료 — 미결제로 풀렸던 사용분을 다시 차감한다(잔액 범위 · 멱등). 반환: 부족분."""
    u = usage(oid)
    if not u['customer_id'] or not u['total']:
        return 0
    need = {b: u['used'][b] - u['net'][b] for b in ('point', 'reward')}
    if need['point'] <= 0 and need['reward'] <= 0:
        return 0
    av = _av(); short = 0
    with av._conn() as c:
        if not av.IS_PG:
            c.execute('BEGIN IMMEDIATE')
        b = _locked(c, av, u['customer_id'])
        rs = []
        for k in ('reward', 'point'):
            if need[k] <= 0:
                continue
            take = min(need[k], b[k])
            short += need[k] - take
            if take:
                seq = u['cnt'].get('ORDER_REUSE:' + k, 0)
                rs.append(('ORDER_REUSE_' + k.upper(), -take, 'ptreuse:%s:%s:%d' % (k, oid, seq), oid,
                           '%s 사용 재반영 · 주문 %s 결제완료' % (REWARD_LABEL if k == 'reward' else '적립금', oid)))
        bal = _write(c, av, u['customer_id'], u['member_id'], rs, b['total']) if rs else b['total']
        c.commit()
    _sync_member(av, u['customer_id'], bal)
    if short:
        print('[pointpay] %s 결제완료 재반영 부족 %dP' % (oid, short), flush=True)
    return short


def release_stale(cid):
    """같은 고객의 결제 미완료(PENDING·FAILED) 주문이 20분 넘게 잡고 있는 포인트를 풀어준다."""
    if not cid:
        return 0
    av = _av()
    cut = (av.kst_naive() - datetime.timedelta(minutes=STALE_MIN)).isoformat(timespec='seconds')
    try:
        rs = av.rows("SELECT DISTINCT l.order_id AS oid FROM point_ledger l JOIN orders o ON o.order_id=l.order_id "
                     "WHERE l.customer_id=? AND l.event_type IN ('ORDER_USE_POINT','ORDER_USE_REWARD') "
                     "AND o.status IN ('PENDING','FAILED') AND o.created < ? LIMIT 20", (cid, cut)) or []
    except Exception:
        return 0
    n = 0
    for r in rs:
        try:
            n += restore(r['oid'], '결제 미완료 %d분 경과' % STALE_MIN, statuses=('PENDING', 'FAILED'))
        except Exception:
            pass
    return n


# ── 사용 한도 (순수 계산 — 주문 트랜잭션 안에서 호출) ─────────────────────────
def check_use(use_point, use_reward, elig_point, elig_reward, sub, ship_fee, discount):
    """요청 사용액을 검증하고 결제 차감 합계를 돌려준다. 위반 시 400."""
    use_point, use_reward = _int(use_point), _int(use_reward)
    if use_reward > elig_reward:
        raise HTTPException(400, '%s은(는) 사용 가능한 상품 금액(%s원)까지만 쓸 수 있습니다'
                            % (REWARD_LABEL, format(elig_reward, ',')))
    if use_point > elig_point:
        raise HTTPException(400, '적립금은 사용 가능한 상품 금액(%s원)까지만 쓸 수 있습니다' % format(elig_point, ','))
    used = use_point + use_reward
    if used > max(0, sub - discount):
        raise HTTPException(400, '포인트는 상품 금액(할인 후)까지만 사용할 수 있습니다')
    if used and sub + ship_fee - discount - used < MIN_PAY:
        raise HTTPException(400, '최소 결제금액은 %d원입니다 — 포인트 사용액을 줄여 주세요' % MIN_PAY)
    return used


def requested(body):
    p = body.get('points') if isinstance(body, dict) else None
    if not isinstance(p, dict):
        return 0, 0
    return _int(p.get('point')), _int(p.get('reward'))


# ── 체크아웃 미리보기 ─────────────────────────────────────────────────────
@pointpay_router.post('/api/points/checkout')
async def api_points_checkout(request: Request):
    """체크아웃 표시용 — 보유 포인트(갈래별)와 상품별 사용 가능 여부. 실제 금액은 /api/orders 가 재검증."""
    av = _av()
    try:
        av.ensure_ready()
    except Exception:
        pass
    try:
        body = await request.json()
    except Exception:
        body = {}
    m = None
    try:
        m = av.member_of(request)
    except Exception:
        m = None
    if not m or (m.get('status') or 'ACTIVE') != 'ACTIVE' or not m.get('customer_id'):
        return {'login': False}
    cid = m['customer_id']
    try:
        release_stale(cid)
    except Exception:
        pass
    b = balances(cid)
    items = [x for x in (body.get('items') or [])[:50] if isinstance(x, dict)]
    try:
        rs = av.product_rules(items)
    except Exception:
        rs = {}
    return {'login': True, 'point': b['point'], 'reward': b['reward'],
            'reward_label': REWARD_LABEL, 'point_label': POINT_LABEL, 'min_pay': MIN_PAY,
            'rules': {k: {'point': bool(v.get('points_ok')), 'reward': bool(v.get('etc_ok'))} for k, v in rs.items()}}


# ── 체크아웃 화면 주입 (admin_v2._inject_auth 끝단 · 멱등) ───────────────────
_CALC_OLD = "window.mpCpApplied=_off;return {sub,ship,total:sub+ship-_off,drop,pts};"
_CALC_NEW = ("window.mpCpApplied=_off;var _pu=(window.mpPtCalc?window.mpPtCalc(sub,ship,_off):0);"
             "window.mpPtApplied=_pu;var _pe=_pu?Math.max(0,pts-Math.floor(_pu*(CFG.pointRateBp||100)/10000)):pts;"
             "return {sub,ship,total:sub+ship-_off-_pu,drop,pts:_pe};")
_SEND_OLD = "coupon:(window.mpCpCode||''),"
_SEND_NEW = "coupon:(window.mpCpCode||''),points:(window.mpPtReq?window.mpPtReq():null),"
_ANCHOR = '<div class="sum-row total">'

_UI = r'''<div id="mpPt" style="display:none;padding:10px 0;border-bottom:1px solid var(--line)">
<div style="font-weight:700;font-size:13px;margin-bottom:6px">포인트 사용</div>
<div id="mpPtR" class="mpPtBox" style="display:none;margin-bottom:8px"><div style="display:flex;justify-content:space-between;font-size:12.5px;gap:8px"><span><b>기타 할인</b> · <span id="mpPtRL"></span></span><span style="color:var(--steel)">보유 <b id="mpPtRB">0</b>P</span></div>
<div style="display:flex;gap:6px;margin-top:4px"><input class="f-input" id="mpPtRIn" inputmode="numeric" placeholder="0" aria-label="기타 할인 사용 포인트" style="margin:0;font-size:14px;text-align:right">
<button type="button" id="mpPtRAll" class="mpPtAll" style="background:var(--ink);color:#fff;border:0;padding:0 12px;font-weight:700;cursor:pointer;white-space:nowrap">전액 사용</button></div>
<div id="mpPtRM" style="font-size:11.5px;color:var(--steel);min-height:14px;margin-top:3px"></div></div>
<div id="mpPtP" class="mpPtBox" style="display:none"><div style="display:flex;justify-content:space-between;font-size:12.5px;gap:8px"><span><b>적립금</b></span><span style="color:var(--steel)">보유 <b id="mpPtPB">0</b>P</span></div>
<div style="display:flex;gap:6px;margin-top:4px"><input class="f-input" id="mpPtPIn" inputmode="numeric" placeholder="0" aria-label="적립금 사용 포인트" style="margin:0;font-size:14px;text-align:right">
<button type="button" id="mpPtPAll" class="mpPtAll" style="background:var(--ink);color:#fff;border:0;padding:0 12px;font-weight:700;cursor:pointer;white-space:nowrap">전액 사용</button></div>
<div id="mpPtPM" style="font-size:11.5px;color:var(--steel);min-height:14px;margin-top:3px"></div></div>
</div>
<div class="sum-row" id="mpPtRow" style="display:none"><span>포인트 사용</span><span id="mpPtV" style="color:var(--red)"></span></div>'''

_JS = r'''<script id="mpPtJs">(function(){try{
var $=function(i){return document.getElementById(i)},F=function(n){return Math.round(n).toLocaleString('ko-KR')};
var S={on:false,point:0,reward:0,rules:{},minPay:100,ep:0,er:0,up:0,ur:0};
function cart(){try{return JSON.parse(localStorage.getItem('mapdal_cart')||'[]')}catch(e){return []}}
function num(v){v=String(v||'').replace(/[^\d]/g,'');return v?parseInt(v,10):0}
function elig(){var ep=0,er=0;cart().forEach(function(i){var r=S.rules[String(i.id||'')]||{},t=(Number(i.p)||0)*(Number(i.q)||1);if(r.point)ep+=t;if(r.reward)er+=t});S.ep=ep;S.er=er}
/* calc() 에서 호출 — 한도 안으로 맞춘 합계를 돌려준다(입력칸도 함께 보정) */
window.mpPtCalc=function(sub,ship,off){if(!S.on)return 0;elig();
 var room=Math.max(0,Math.min(sub-off,sub+ship-off-S.minPay));
 var ur=Math.min(num(($('mpPtRIn')||{}).value),S.reward,S.er,room);
 var up=Math.min(num(($('mpPtPIn')||{}).value),S.point,S.ep,Math.max(0,room-ur));
 S.ur=ur;S.up=up;return ur+up};
window.mpPtReq=function(){return (S.on&&(S.up||S.ur))?{point:S.up,reward:S.ur}:null};
function paint(){var tot=(S.up||0)+(S.ur||0),r=$('mpPtRow');if(r){r.style.display=tot?'':'none';$('mpPtV').textContent='−₩'+F(tot)}
 [['R',S.ur,S.reward,S.er],['P',S.up,S.point,S.ep]].forEach(function(x){var inp=$('mpPt'+x[0]+'In'),m=$('mpPt'+x[0]+'M'),b=$('mpPt'+x[0]+'All');if(!inp)return;
  var ok=x[3]>0;inp.disabled=!ok;if(b){b.disabled=!ok;b.style.opacity=ok?'1':'.4'}
  if(document.activeElement!==inp)inp.value=x[1]?F(x[1]):'';
  m.textContent=ok?(x[1]?'−'+F(x[1])+'P 적용':''):'장바구니 상품에 사용할 수 없습니다';m.style.color=ok?'var(--steel)':'var(--red)'})}
var _rs=window.renderSum;if(typeof _rs==='function'){window.renderSum=function(){_rs.apply(this,arguments);paint()}}
function rs(){try{(window.renderSum||function(){})()}catch(e){}paint()}
function load(){fetch('/api/points/checkout',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',
 body:JSON.stringify({items:cart().map(function(i){return {id:i.id,q:i.q}})})}).then(function(r){return r.ok?r.json():null}).then(function(d){
 if(!d||!d.login||(!(d.point>0)&&!(d.reward>0))){S.on=false;$('mpPt').style.display='none';rs();return}
 S.on=true;S.point=d.point||0;S.reward=d.reward||0;S.rules=d.rules||{};S.minPay=d.min_pay||100;
 $('mpPt').style.display='';$('mpPtRL').textContent=d.reward_label||'';
 $('mpPtR').style.display=S.reward>0?'':'none';$('mpPtP').style.display=S.point>0?'':'none';
 $('mpPtRB').textContent=F(S.reward);$('mpPtPB').textContent=F(S.point);rs()}).catch(function(){})}
['R','P'].forEach(function(k){var inp=$('mpPt'+k+'In'),b=$('mpPt'+k+'All');
 if(inp){inp.addEventListener('input',function(){rs()});inp.addEventListener('blur',function(){paint()})}
 if(b)b.addEventListener('click',function(){var i=$('mpPt'+k+'In');i.value=String(k==='R'?S.reward:S.point);rs();paint()})});
load();window.addEventListener('pageshow',function(e){if(e.persisted)load()});
}catch(e){}})();</script>'''


def checkout_apply(html, path=''):
    """국내 체크아웃에 포인트 사용 UI 주입 — 쿠폰 주입(growth.o2o_apply) 뒤에 실행(멱등)."""
    if not isinstance(html, str) or path != '/checkout' or 'id="mpPt"' in html:
        return html
    if _CALC_OLD not in html or _SEND_OLD not in html or _ANCHOR not in html:
        return html                     # 기대한 체크아웃 구조가 아니면 손대지 않는다(결제 화면 보호)
    html = html.replace(_CALC_OLD, _CALC_NEW, 1).replace(_SEND_OLD, _SEND_NEW, 1)
    html = html.replace(_ANCHOR, _UI + _ANCHOR, 1)
    i = html.lower().rfind('</body>')
    return (html[:i] + _JS + html[i:]) if i >= 0 else html + _JS
