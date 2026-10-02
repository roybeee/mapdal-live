"""growth.py — MAPDAL SEOUL 그로스 엔진 (2026-10)

하나의 데이터 엔진으로 '광고 → 방문 → 구매 → 재구매' 전 구간을 묶는다.

  [1] 신뢰 정합화   가짜 평점·리뷰수·'N명이 보고 있어요' 제거 → 실제 구매인증 리뷰만 노출
  [2] 어트리뷰션    UTM·gclid·fbclid·ttclid·네이버광고·매장QR 을 first/last touch 쿠키로 보관,
                    주문 생성 시 orders.attr 에 스냅샷 → 채널·캠페인별 매출/ROAS 산출의 기준
  [3] 이벤트 버스   기존 GA4 퍼널(gtag) 호출을 그대로 받아 Meta·TikTok·카카오 픽셀·Google Ads,
                    자사 이벤트 테이블(mp_events)로 동시 전송 — 페이지 수정 없음
  [4] 동의 관리     Consent Mode v2 · EU/UK 방문자는 기본 거부(옵트인), 그 외는 고지+옵트아웃
  [5] 서버 전환     결제완료 시 Meta CAPI · TikTok Events API 로 Purchase 전송(픽셀과 event_id 중복제거)
  [6] 이메일        Resend 또는 SMTP — 주문확인·입금안내·발송안내 + 라이프사이클(장바구니·재구매·윈백)
  [7] O2O           매장 QR(/visit) → 이메일/WhatsApp/LINE 동의 캡처 + '귀국 후 첫 주문' 쿠폰
  [8] 그로스 대시보드 /admin/growth — 채널·캠페인 ROAS, 퍼널, 코호트 재구매, 국가별 LTV, O2O 성과

설계 원칙 (기존 코드와 동일)
  · 모든 외부 연동은 환경변수 설정 시에만 활성 — 미설정 = 완전 무변화(안전 기본값)
  · 계측·발송 실패가 주문·결제·페이지 렌더링에 영향을 주는 일은 절대 없다(전부 try/except)
  · PG 트랜잭션 오염 방지: DDL·쓰기는 문장 1개 = 트랜잭션 1개, 주문 트랜잭션 안에서는 읽기만
"""
import os, re, json, hashlib, hmac, secrets, datetime, threading, time, html as _html
import urllib.request, urllib.parse, urllib.error
from collections import deque

from fastapi import APIRouter, Request, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

growth_router = APIRouter()

KST = datetime.timezone(datetime.timedelta(hours=9))


def _now():
    return datetime.datetime.now(KST).replace(tzinfo=None)


def _iso():
    return _now().isoformat(timespec='seconds')


def _day(dt=None):
    return (dt or _now()).strftime('%Y-%m-%d')


def _env(k, d=''):
    return (os.getenv(k) or d).strip()


def _cid(s):
    """픽셀·계정 ID 화이트리스트 정규화 — 스크립트 주입 방지."""
    return re.sub(r'[^A-Za-z0-9_-]', '', str(s or ''))[:64]


def _app():
    import app as _a
    return _a


def _av():
    import admin_v2 as _a
    return _a


def _e(s):
    return _html.escape(str(s if s is not None else ''), quote=True)


def _site():
    try:
        return (_app().SITE_ORIGIN or 'https://mapdal.kr').rstrip('/')
    except Exception:
        return 'https://mapdal.kr'


# ═══════════════════════════ 설정 (환경변수) ════════════════════════════
def cfg():
    """픽셀·서버전환·메일 설정 스냅샷. 값이 비면 해당 채널은 비활성."""
    aw = _env('GOOGLE_ADS_ID')
    aw = aw if re.fullmatch(r'AW-\d{6,14}', aw or '') else ''
    return {
        'meta': re.sub(r'\D', '', _env('META_PIXEL_ID'))[:20],
        'meta_capi': _env('META_CAPI_TOKEN'),
        'meta_test': _cid(_env('META_TEST_EVENT_CODE')),
        'tt': _cid(_env('TIKTOK_PIXEL_ID')),
        'tt_api': _env('TIKTOK_EVENTS_TOKEN'),
        'kakao': re.sub(r'\D', '', _env('KAKAO_PIXEL_ID'))[:24],
        'aw': aw,
        'aw_label': _cid(_env('GOOGLE_ADS_PURCHASE_LABEL')),
        'aw_lead': _cid(_env('GOOGLE_ADS_LEAD_LABEL')),
    }


# ═══════════════════════════ 스키마 (멱등) ══════════════════════════════
_READY = {'ok': False, 'lock': threading.Lock()}

_DDL = (
    # 통합 이벤트 로그 — 픽셀 손실(차단·동의거부)과 무관한 자사 1st-party 원장
    """CREATE TABLE IF NOT EXISTS mp_events(
      id TEXT PRIMARY KEY, ts TEXT, day TEXT, vid TEXT, sid TEXT, cid TEXT,
      name TEXT, path TEXT, value INTEGER, currency TEXT, oid TEXT,
      src TEXT, med TEXT, cmp TEXT, country TEXT, lang TEXT, dev TEXT, props TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_mpev_day ON mp_events(day, name)",
    "CREATE INDEX IF NOT EXISTS idx_mpev_vid ON mp_events(vid)",
    # 광고비 원장 — CSV 업로드 · Meta/TikTok API 자동수집 공용
    """CREATE TABLE IF NOT EXISTS mp_ad_spend(
      day TEXT, channel TEXT, campaign TEXT, spend INTEGER, impressions INTEGER,
      clicks INTEGER, src TEXT, updated TEXT, PRIMARY KEY(day, channel, campaign))""",
    # O2O·뉴스레터 연락처 (동의 기반) — 귀국 후 재구매 채널
    """CREATE TABLE IF NOT EXISTS mp_contacts(
      id TEXT PRIMARY KEY, created TEXT, email TEXT, phone TEXT, channel TEXT, handle TEXT,
      country TEXT, lang TEXT, source TEXT, consent INTEGER, vid TEXT, coupon TEXT,
      customer_id TEXT, unsub INTEGER DEFAULT 0, last_mail TEXT, mail_step TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_mpct_email ON mp_contacts(email)",
    # 쿠폰 · 사용 이력
    """CREATE TABLE IF NOT EXISTS mp_coupons(
      code TEXT PRIMARY KEY, kind TEXT, value INTEGER, min_sub INTEGER, max_off INTEGER,
      starts TEXT, ends TEXT, uses_left INTEGER, scope TEXT, note TEXT, created TEXT,
      active INTEGER DEFAULT 1, contact_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS mp_coupon_uses(
      order_id TEXT PRIMARY KEY, code TEXT, created TEXT, amount_off INTEGER, status TEXT)""",
    # 메일 발송 로그 (중복발송 방지 키 = kind+ref)
    """CREATE TABLE IF NOT EXISTS mp_mail_log(
      id TEXT PRIMARY KEY, ts TEXT, to_addr TEXT, kind TEXT, ref TEXT, status TEXT, err TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_mpml_ref ON mp_mail_log(kind, ref)",
    # 매장 QR 배치 위치
    """CREATE TABLE IF NOT EXISTS mp_qr(
      code TEXT PRIMARY KEY, label TEXT, created TEXT, active INTEGER DEFAULT 1)""",
    # 서버 전환 전송 로그 (Meta/TikTok — 재전송 방지·진단)
    """CREATE TABLE IF NOT EXISTS mp_conv_log(
      id TEXT PRIMARY KEY, ts TEXT, oid TEXT, channel TEXT, status TEXT, detail TEXT)""",
    # 환율 (표시용) — 관리자 수정 가능
    """CREATE TABLE IF NOT EXISTS mp_fx(
      cur TEXT PRIMARY KEY, per_krw REAL, updated TEXT)""",
)


def ensure():
    """그로스 테이블 생성 — 문장 1개 = 트랜잭션 1개 (PG abort 전파 방지)."""
    if _READY['ok']:
        return True
    with _READY['lock']:
        if _READY['ok']:
            return True
        try:
            a = _app()
            for ddl in _DDL:
                try:
                    with a.db() as c:
                        c.exec(ddl)
                except Exception as e:
                    print('[growth] DDL skip: %s' % str(e)[:120], flush=True)
            _seed_defaults()
            _READY['ok'] = True
            print('[growth] 준비 완료', flush=True)
        except Exception as e:
            print('[growth] 초기화 실패: %s' % e, flush=True)
    return _READY['ok']


def _seed_defaults():
    """기본 매장 QR · 귀국 후 첫주문 쿠폰 · 표시 환율 (이미 있으면 유지)."""
    a = _app()
    ins = 'INSERT INTO %s ON CONFLICT DO NOTHING'
    seeds = [
        ('mp_qr(code,label,created,active) VALUES(?,?,?,1)', ('seongsu-1f', '성수 1F 맵달STATION 카운터', _iso())),
        ('mp_qr(code,label,created,active) VALUES(?,?,?,1)', ('seongsu-4f', '성수 4F 앨범·MD 스토어 계산대', _iso())),
        ('mp_qr(code,label,created,active) VALUES(?,?,?,1)', ('receipt', '영수증·쇼핑백 인쇄 QR', _iso())),
        ('mp_coupons(code,kind,value,min_sub,max_off,starts,ends,uses_left,scope,note,created,active) '
         'VALUES(?,?,?,?,?,?,?,?,?,?,?,1)',
         ('WELCOMEHOME', 'pct', 10, 30000, 30000, '', '', None, 'first',
          '매장 방문 고객 귀국 후 첫 온라인 주문 10% (최대 3만원)', _iso())),
    ]
    for tbl, args in seeds:
        try:
            with a.db() as c:
                c.exec(ins % tbl, args)
        except Exception:
            pass
    for cur, rate in (('USD', 0.00072), ('JPY', 0.108), ('CNY', 0.0052), ('EUR', 0.00066), ('TWD', 0.023)):
        try:
            with a.db() as c:
                c.exec('INSERT INTO mp_fx(cur,per_krw,updated) VALUES(?,?,?) ON CONFLICT DO NOTHING',
                       (cur, rate, _iso()))
        except Exception:
            pass


def _rows(sql, args=()):
    with _app().db() as c:
        return c.all(sql, args)


def _one(sql, args=()):
    with _app().db() as c:
        return c.one(sql, args)


def _run(sql, args=()):
    with _app().db() as c:
        c.exec(sql, args)


# ═══════════════════════════ [1] 신뢰 정합화 ════════════════════════════
#   정적 PDP·관리자 편집본(page_edits)·동적 PDP(/p/) 어디에서 오든 서빙 직전에 한 번 더
#   정리한다(멱등). 원본 정적 파일도 같은 규칙으로 정리해 두었다 — 이 함수는 DB 편집본에
#   남아 있을 수 있는 구 문구를 위한 안전망이다.
_TRUST_MARK = 'mpTrustJs'
_RE_VIEWERS = re.compile(r'<div class="viewers">.*?</div>', re.S)
#   카운터 스크립트는 전부 '단독 문장 한 줄' 형태다(let v=… / var v=… / setInterval(…vCount…)).
#   한 줄에 다른 코드가 섞인 축약본을 지우지 않도록 문장 시작 형태와 길이를 함께 제한한다.
_RE_VCOUNT_LINE = re.compile(r'^[ \t]*(?:let v=|var v=|setInterval\()[^\n]{0,260}vCount[^\n]{0,200}\n', re.M)
_RE_RATING = re.compile(r'<div class="rating-row">.*?</div>', re.S)
_RE_REVTAB_CNT = re.compile(r'(data-tab="rev">[^<]*)<b>[\d,]+</b>')
_RE_REVPANEL = re.compile(r'(<div class="tab-panel" id="tab-rev">).*?(\s*<div class="tab-panel" id="tab-qa">)', re.S)
_OLD_QA = ('전 세계 배송 가능합니다. 관세·세금은 결제 시 선지불(DDP)되어 수령 시 추가 비용이 없습니다.')
NEW_QA = ('해외 배송 가능 여부는 상품마다 다릅니다. 해외 배송 주문 화면에서 국가를 선택하시면 배송 가능 여부와 '
          '배송비가 바로 계산되며, 관세는 수령 국가 기준에 따라 부과될 수 있습니다. 냉동·냉장 K-FOOD는 현재 국내 배송만 가능합니다.')

_TRUST_JS = r"""<script id="mpTrustJs">(function(){try{
var q=new URLSearchParams(location.search);
var pid=(typeof PID!=='undefined'&&PID)?PID:(function(){
 if(/\/album-detail/.test(location.pathname)&&q.get('uid'))return'k2g::'+q.get('uid');
 var m=location.pathname.match(/^\/(product-[A-Za-z0-9._-]+?)(?:\.html)?$/);return m?m[1]:null})();
if(!pid)return;
function go(){
 fetch('/api/reviews?product_id='+encodeURIComponent(pid)).then(function(r){return r.ok?r.json():null}).then(function(d){
  var n=(d&&d.count)||0,a=(d&&d.avg)||0,rr=document.getElementById('mpRate'),rc=document.getElementById('mpRevCnt');
  if(rc)rc.textContent=n?String(n):'';
  if(rr&&n){var s='';for(var i=1;i<=5;i++)s+=(i<=Math.round(a)?'★':'☆');
   rr.innerHTML='<span class="stars">'+s+'</span><b>'+a.toFixed(1)+'</b><a href="#" onclick="try{openTab(\'rev\')}catch(e){}return false">구매인증 리뷰 '+n+'건</a>';
   rr.hidden=false}
 }).catch(function(){});
 /* 리뷰 위젯 id 는 문자열을 쪼개 쓴다 — admin_v2 는 본문에 그 id 문자열이 있으면 위젯을 주입하지 않는다 */
 var host=document.getElementById('mpRevHost'),w=document.getElementById('mpRev'+'iews');
 if(host&&w&&w.parentNode!==host)host.appendChild(w);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',go);else go();
}catch(e){}})();</script>"""


def trust_apply(html, path=''):
    """가짜 사회적 증거 제거 + 실제 리뷰 집계로 대체 (멱등)."""
    if not isinstance(html, str):
        return html
    try:
        if 'vCount' in html:
            html = _RE_VIEWERS.sub('', html)
            html = _RE_VCOUNT_LINE.sub('', html)
        if '<div class="rating-row">' in html:
            html = _RE_RATING.sub('<div class="rating-row" id="mpRate" hidden></div>', html)
        if 'data-tab="rev"' in html:
            html = _RE_REVTAB_CNT.sub(r'\1<b id="mpRevCnt"></b>', html)
        if 'id="tab-rev"' in html and 'mpRevHost' not in html:
            html = _RE_REVPANEL.sub(r'\1<div id="mpRevHost"></div></div>\2', html, count=1)
        if _OLD_QA in html:
            html = html.replace(_OLD_QA, NEW_QA)
        # 제거된 가짜 리뷰 패널 안에 있던 요소를 참조하던 스크립트 — null 안전화
        if "document.getElementById('fitSw').addEventListener" in html:
            html = html.replace("document.getElementById('fitSw').addEventListener",
                                "(document.getElementById('fitSw')||document.createElement('i')).addEventListener")
        if ('mpRate' in html or 'mpRevHost' in html) and _TRUST_MARK not in html:
            i = html.lower().rfind('</body>')
            html = (html[:i] + _TRUST_JS + html[i:]) if i >= 0 else html + _TRUST_JS
    except Exception:
        pass
    return html


# ═══════════════════════════ [2]·[4] 어트리뷰션 + 동의 (head) ════════════
#   <head> 최상단에 주입 — gtag('config') 보다 먼저 Consent Mode 기본값을 깔아야 한다.
#   쿠키(1st-party, 서버 판독 가능):
#     mp_vid  방문자 ID 13개월 · mp_sid 세션 30분
#     mp_ft   최초 유입 터치 180일 · mp_lt 최근 유입 터치 30일 (주문 시 orders.attr 로 스냅샷)
#     mp_consent  'all' | 'ess'
_HEAD_JS = r"""<script id="mpGrowthHead">(function(){try{
var D=document,W=window,L=location;
function gc(n){var m=D.cookie.match('(?:^|; )'+n+'=([^;]*)');return m?decodeURIComponent(m[1]):''}
function sc(n,v,days){D.cookie=n+'='+encodeURIComponent(v)+';path=/;max-age='+Math.round(days*86400)+';samesite=lax'+(L.protocol==='https:'?';secure':'')}
W.mpCookie={get:gc,set:sc};
var vid=gc('mp_vid');if(!vid)vid=Date.now().toString(36)+Math.random().toString(36).slice(2,9);sc('mp_vid',vid,395);
var sid=gc('mp_sid'),fresh=!sid;if(!sid)sid=Math.random().toString(36).slice(2,11);sc('mp_sid',sid,1/48);
var tz='';try{tz=Intl.DateTimeFormat().resolvedOptions().timeZone||''}catch(e){}
var strict=/^(Europe\/|Atlantic\/(Reykjavik|Canary|Madeira|Azores|Faroe)|Arctic\/)/.test(tz);
var cs=gc('mp_consent'),ads=cs?cs==='all':!strict,anl=cs?true:!strict;
W.MP={vid:vid,sid:sid,fresh:fresh,tz:tz,consent:{ads:ads,analytics:anl,decided:!!cs,strict:strict}};
W.dataLayer=W.dataLayer||[];if(!W.gtag)W.gtag=function(){W.dataLayer.push(arguments)};
gtag('consent','default',{ad_storage:ads?'granted':'denied',ad_user_data:ads?'granted':'denied',
 ad_personalization:ads?'granted':'denied',analytics_storage:anl?'granted':'denied',wait_for_update:500});
var q=new URLSearchParams(L.search),t={},K=['utm_source','utm_medium','utm_campaign','utm_content','utm_term',
 'gclid','gbraid','wbraid','fbclid','ttclid','n_media','n_query','NaPm','qr'],i,v;
for(i=0;i<K.length;i++){v=q.get(K[i]);if(v)t[K[i]]=String(v).slice(0,100)}
var rh='';try{rh=D.referrer?new URL(D.referrer).hostname.replace(/^www\./,''):''}catch(e){}
var own=!rh||rh===L.hostname.replace(/^www\./,'')||/(^|\.)mapdal\.kr$/.test(rh)||/(inicis|kakaopay|naverpay|payco|tosspayments)\./.test(rh);
var has=false;for(i in t){has=true;break}
if(has||!own){
 var s=t.utm_source||'',m=t.utm_medium||'';
 if(!s){
  if(t.qr){s='store_qr';m='offline'}
  else if(t.gclid||t.gbraid||t.wbraid){s='google';m='cpc'}
  else if(t.ttclid){s='tiktok';m='cpc'}
  else if(t.n_media||t.NaPm){s='naver';m='cpc'}
  else if(t.fbclid){s=/instagram/.test(rh)?'instagram':'facebook';m='social'}
  else if(/(^|\.)(google|bing|naver|daum|yahoo|baidu|duckduckgo|yandex|ecosia)\./.test(rh)){s=rh.split('.').slice(-2,-1)[0]||rh;if(/naver/.test(rh))s='naver';if(/google/.test(rh))s='google';m='organic'}
  else if(/(instagram|facebook|fb\.|t\.co$|twitter|x\.com|tiktok|youtube|youtu\.be|pinterest|threads|reddit|weibo|xiaohongshu|xhslink|line\.me|kakao|band\.us|douyin|bilibili|lemon8)/.test(rh)){s=rh.replace(/^(l|lm|m)\./,'').split('.')[0];m='social'}
  else{s=rh;m='referral'}
 }
 var tc={s:s.toLowerCase().slice(0,40),m:m.toLowerCase().slice(0,30),c:(t.utm_campaign||t.qr||'').slice(0,80),
  ct:(t.utm_content||'').slice(0,60),k:(t.utm_term||t.n_query||'').slice(0,60),ref:rh.slice(0,60),
  lp:L.pathname.slice(0,80),ts:Math.round(Date.now()/1000)};
 if(t.gclid)tc.gclid=t.gclid;if(t.gbraid)tc.gbraid=t.gbraid;if(t.wbraid)tc.wbraid=t.wbraid;
 if(t.fbclid)tc.fbclid=t.fbclid;if(t.ttclid)tc.ttclid=t.ttclid;
 var js=JSON.stringify(tc);sc('mp_lt',js,30);if(!gc('mp_ft'))sc('mp_ft',js,180);
 if(t.fbclid&&ads)sc('_fbc','fb.1.'+Date.now()+'.'+t.fbclid,90);
 if(t.ttclid&&ads)sc('ttclid',t.ttclid,30);
 W.MP.touch=tc;
}
}catch(e){}})();</script>"""


def _body_js():
    """이벤트 버스 + 픽셀 + 동의배너 — mpAnalytics(GA4) 뒤, 퍼널 런타임(mpEcomJs) 앞에 주입."""
    c = cfg()
    conf = json.dumps({'meta': c['meta'], 'tt': c['tt'], 'kk': c['kakao'], 'aw': c['aw'],
                       'awl': c['aw_label'], 'awlead': c['aw_lead']})
    return (r"""<script id="mpGrowth">(function(){try{
var C=""" + conf + r""",W=window,D=document,MP=W.MP||{consent:{ads:true,analytics:true,decided:true}};
var LANG=(D.documentElement.getAttribute('lang')||'ko').slice(0,2);
var MM={view_item:'ViewContent',add_to_cart:'AddToCart',begin_checkout:'InitiateCheckout',add_payment_info:'AddPaymentInfo',
 sign_up:'CompleteRegistration',purchase:'Purchase',search:'Search',generate_lead:'Lead',add_to_wishlist:'AddToWishlist'};
var TM={view_item:'ViewContent',add_to_cart:'AddToCart',begin_checkout:'InitiateCheckout',add_payment_info:'AddPaymentInfo',
 sign_up:'CompleteRegistration',purchase:'CompletePayment',search:'Search',generate_lead:'SubmitForm',add_to_wishlist:'AddToWishlist'};
var SV={page_view:1,view_item:1,view_item_list:1,select_item:1,add_to_cart:1,remove_from_cart:1,view_cart:1,begin_checkout:1,
 add_payment_info:1,sign_up:1,login:1,search:1,generate_lead:1,purchase:1,qr_scan:1,share:1,lang_switch:1,add_to_wishlist:1};
function items(p){return(p&&p.items)||[]}
function ids(p){return items(p).map(function(i){return String(i.item_id||'')}).filter(Boolean).slice(0,20)}
function beacon(n,p){try{
 var b={n:n,p:location.pathname,v:Math.round(Number(p&&p.value)||0),c:(p&&p.currency)||'KRW',
  o:String((p&&p.transaction_id)||''),l:LANG,
  it:items(p).slice(0,10).map(function(i){return[String(i.item_id||'').slice(0,80),Number(i.quantity)||1,Number(i.price)||0]})};
 if(n==='page_view'){b.r=(D.referrer||'').slice(0,200);b.f=MP.fresh?1:0;b.tz=MP.tz||''}
 if(p&&p.search_term)b.q=String(p.search_term).slice(0,80);
 if(p&&p.method)b.m=String(p.method).slice(0,20);
 var s=JSON.stringify(b);
 if(navigator.sendBeacon&&navigator.sendBeacon('/api/ev',new Blob([s],{type:'text/plain'})))return;
 fetch('/api/ev',{method:'POST',body:s,keepalive:true,headers:{'Content-Type':'text/plain'}}).catch(function(){});
}catch(e){}}
function fan(n,p){p=p||{};
 if(SV[n])beacon(n,p);
 if(!MP.consent.ads)return;
 var cur=p.currency||'KRW',val=Number(p.value)||0,eid=(n==='purchase'&&p.transaction_id)?('pur_'+p.transaction_id):undefined;
 try{if(C.meta&&W.fbq&&MM[n]){var mp={currency:cur,value:val};var ii=ids(p);if(ii.length){mp.content_ids=ii;mp.content_type='product';
   mp.contents=items(p).map(function(i){return{id:String(i.item_id),quantity:Number(i.quantity)||1,item_price:Number(i.price)||0}})}
   if(p.search_term)mp.search_string=p.search_term;
   fbq('track',MM[n],mp,eid?{eventID:eid}:undefined)}}catch(e){}
 try{if(C.tt&&W.ttq&&TM[n]){ttq.track(TM[n],{currency:cur,value:val,content_type:'product',
   contents:items(p).map(function(i){return{content_id:String(i.item_id),content_name:String(i.item_name||''),quantity:Number(i.quantity)||1,price:Number(i.price)||0}})},
   eid?{event_id:eid}:undefined)}}catch(e){}
 try{if(C.kk&&W.kakaoPixel){var k=kakaoPixel(C.kk);
   if(n==='view_item')k.viewContent({id:ids(p)[0]||''});
   else if(n==='add_to_cart')k.addToCart({id:ids(p)[0]||''});
   else if(n==='view_cart')k.viewCart();
   else if(n==='sign_up')k.completeRegistration();
   else if(n==='search')k.search({keyword:p.search_term||''});
   else if(n==='generate_lead')k.participation();
   else if(n==='purchase')k.purchase({total_quantity:String(items(p).length),total_price:String(val),currency:cur,
     products:items(p).map(function(i){return{id:String(i.item_id),name:String(i.item_name||''),quantity:String(i.quantity||1),price:String(i.price||0)}})})}}catch(e){}
 try{if(C.aw&&n==='purchase'&&C.awl)W.__mpG('event','conversion',{send_to:C.aw+'/'+C.awl,value:val,currency:cur,transaction_id:String(p.transaction_id||'')});
   if(C.aw&&n==='generate_lead'&&C.awlead)W.__mpG('event','conversion',{send_to:C.aw+'/'+C.awlead})}catch(e){}
}
W.mpTrack=fan;
/* gtag 래핑 — 기존 GA4 퍼널 호출(mpEcomJs·주문완료 purchase)이 그대로 전 채널로 퍼진다 */
var G=W.gtag||function(){(W.dataLayer=W.dataLayer||[]).push(arguments)};W.__mpG=G;
W.gtag=function(){try{if(arguments[0]==='event'&&arguments[1]!=='conversion')fan(arguments[1],arguments[2])}catch(e){}return G.apply(this,arguments)};
function sload(src,cb){var s=D.createElement('script');s.async=true;s.src=src;if(cb)s.onload=cb;D.head.appendChild(s)}
var loaded=false;
function pixels(){if(loaded||!MP.consent.ads)return;loaded=true;
 if(C.meta){!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?n.callMethod.apply(n,arguments):n.queue.push(arguments)};
  if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;
  s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}(W,D,'script','https://connect.facebook.net/en_US/fbevents.js');
  fbq('init',C.meta);fbq('track','PageView')}
 if(C.tt){!function(w,d,t){w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[];ttq.methods=['page','track','identify','instances','debug','on','off','once','ready','alias','group','enableCookie','disableCookie','holdConsent','revokeConsent','grantConsent'];
  ttq.setAndDefer=function(t,e){t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}};
  for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);
  ttq.instance=function(t){for(var e=ttq._i[t]||[],n=0;n<ttq.methods.length;n++)ttq.setAndDefer(e,ttq.methods[n]);return e};
  ttq.load=function(e,n){var r='https://analytics.tiktok.com/i18n/pixel/events.js';ttq._i=ttq._i||{};ttq._i[e]=[];ttq._i[e]._u=r;ttq._t=ttq._t||{};ttq._t[e]=+new Date;ttq._o=ttq._o||{};ttq._o[e]=n||{};
  var s=d.createElement('script');s.type='text/javascript';s.async=!0;s.src=r+'?sdkid='+e+'&lib='+t;var a=d.getElementsByTagName('script')[0];a.parentNode.insertBefore(s,a)};
  ttq.load(C.tt);ttq.page()}(W,D,'ttq')}
 if(C.kk)sload('https://t1.daumcdn.net/kas/static/kp.js',function(){try{kakaoPixel(C.kk).pageView()}catch(e){}});
 if(C.aw){if(!D.querySelector('script[src*="googletagmanager.com/gtag/js"]'))sload('https://www.googletagmanager.com/gtag/js?id='+C.aw);
  G('js',new Date());G('config',C.aw)}
}
function consent(v){MP.consent.ads=(v==='all');MP.consent.analytics=true;MP.consent.decided=true;
 W.mpCookie&&W.mpCookie.set('mp_consent',v,365);
 G('consent','update',{ad_storage:v==='all'?'granted':'denied',ad_user_data:v==='all'?'granted':'denied',
  ad_personalization:v==='all'?'granted':'denied',analytics_storage:'granted'});
 if(v==='all')pixels();
 try{fetch('/api/consent',{method:'POST',body:JSON.stringify({v:v}),headers:{'Content-Type':'application/json'},keepalive:true}).catch(function(){})}catch(e){}
 var b=D.getElementById('mpCb');if(b)b.remove()}
W.mpConsent=consent;
var TX={ko:['맵달SEOUL은 더 나은 쇼핑 경험과 맞춤 광고를 위해 쿠키를 사용합니다.','모두 허용','필수만','개인정보처리방침'],
 en:['We use cookies to improve your shopping experience and show relevant ads.','Accept all','Essential only','Privacy policy'],
 ja:['より良いショッピング体験と最適な広告のためにCookieを使用しています。','すべて許可','必須のみ','プライバシーポリシー'],
 zh:['我们使用 Cookie 来改善您的购物体验并展示相关广告。','全部接受','仅必要','隐私政策']};
function banner(){if(MP.consent.decided||D.getElementById('mpCb'))return;var t=TX[LANG]||TX.en;
 var b=D.createElement('div');b.id='mpCb';b.setAttribute('role','dialog');b.setAttribute('aria-live','polite');b.setAttribute('aria-label','cookie consent');
 b.innerHTML='<p>'+t[0]+' <a href="/privacy">'+t[3]+'</a></p><div><button type="button" data-v="ess">'+t[2]+'</button><button type="button" data-v="all" class="pri">'+t[1]+'</button></div>';
 b.addEventListener('click',function(e){var v=e.target&&e.target.getAttribute&&e.target.getAttribute('data-v');if(v)consent(v)});
 D.body.appendChild(b)}
var st=D.createElement('style');st.textContent='#mpCb{position:fixed;left:16px;right:16px;bottom:16px;z-index:9990;max-width:560px;margin:0 auto;background:#141414;color:#fff;'
 +'padding:16px 18px;display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap;font:500 13px/1.55 "IBM Plex Sans KR",-apple-system,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.25)}'
 +'#mpCb p{margin:0;flex:1 1 260px}#mpCb a{color:#FFB000;text-decoration:underline}#mpCb div{display:flex;gap:8px}'
 +'#mpCb button{font:700 12.5px/1 inherit;padding:11px 14px;border:1px solid #555;background:transparent;color:#fff;cursor:pointer;min-height:40px}'
 +'#mpCb button.pri{background:#DC2B24;border-color:#DC2B24}#mpCb button:focus-visible{outline:2px solid #FFB000;outline-offset:2px}'
 +'@media(max-width:600px){#mpCb{left:8px;right:8px;bottom:8px;padding:12px 14px;gap:10px;font-size:12px;line-height:1.5}'
 +'#mpCb p{flex-basis:100%}#mpCb div{width:100%}#mpCb button{flex:1;padding:9px 10px;min-height:40px}}';
D.head.appendChild(st);
function boot(){
 if(MP.consent.ads)pixels();
 if(!MP.consent.decided)banner();   /* EU·UK: 옵트인 / 그 외: 고지 + 옵트아웃 */
 fan('page_view',{});
 try{var P=location.pathname.replace(/\.html$/,'');if(P==='/search'){var sq=new URLSearchParams(location.search).get('q');if(sq)fan('search',{search_term:sq})}}catch(e){}
 try{var qr=new URLSearchParams(location.search).get('qr');if(qr)fan('qr_scan',{method:qr})}catch(e){}
}
if(D.readyState==='loading')D.addEventListener('DOMContentLoaded',boot);else boot();
}catch(e){}})();</script>""")


def head_apply(html):
    """<head> 직후 어트리뷰션·동의 스크립트 주입 (멱등)."""
    if not isinstance(html, str) or 'mpGrowthHead' in html:
        return html
    m = re.search(r'<head[^>]*>', html, re.I)
    if not m:
        return html
    # <meta charset> 가 첫 1024바이트 안에 있어야 하므로 charset 메타 뒤에 넣는다.
    mc = re.search(r'<meta[^>]+charset[^>]*>', html[m.end():m.end() + 600], re.I)
    at = m.end() + (mc.end() if mc else 0)
    extra = _HEAD_PWA if 'rel="manifest"' not in html else ''
    if 'name="theme-color"' not in html:
        extra += '<meta name="theme-color" content="#141414">'
    return html[:at] + _HEAD_JS + extra + html[at:]


def body_snippet(html):
    """버스 스니펫 (마커 mpGrowth) — _inject_auth 의 add 목록에서 호출."""
    if 'id="mpGrowth"' in html:
        return ''
    return _body_js()


# ═══════════════════════════ 이벤트 수집 API ════════════════════════════
_EV_NAMES = {'page_view', 'view_item', 'view_item_list', 'select_item', 'add_to_cart', 'remove_from_cart',
             'view_cart', 'begin_checkout', 'add_payment_info', 'sign_up', 'login', 'search',
             'generate_lead', 'purchase', 'qr_scan', 'share', 'lang_switch', 'add_to_wishlist'}
_EVQ = deque(maxlen=20000)
_EV_RATE = {}
_BOT_RE = re.compile(r'bot|crawl|spider|slurp|preview|facebookexternalhit|headless|lighthouse|pingdom|monitor', re.I)


def _touch_from_cookie(req, name):
    try:
        raw = urllib.parse.unquote(req.cookies.get(name) or '')
        d = json.loads(raw) if raw.startswith('{') else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _country(req):
    h = req.headers
    c = (h.get('cf-ipcountry') or h.get('x-vercel-ip-country') or h.get('x-country') or '').upper()
    return c[:2] if re.fullmatch(r'[A-Z]{2}', c[:2] or '') else ''


def _is_mobile(req):
    ua = (req.headers.get('user-agent') or '').lower()
    return any(k in ua for k in ('iphone', 'android', 'ipad', 'mobile'))


def _client_ip(req):
    xf = (req.headers.get('x-forwarded-for') or '').split(',')[0].strip()
    return xf or (req.client.host if req.client else '')


@growth_router.post('/api/ev')
async def api_event(req: Request):
    """1st-party 이벤트 수집 — sendBeacon(text/plain). 항상 204, 실패는 조용히 무시."""
    try:
        if _BOT_RE.search(req.headers.get('user-agent') or ''):
            return Response(status_code=204)
        ip = _client_ip(req)
        now = time.time()
        b = _EV_RATE.get(ip)
        if not b or now - b[0] > 60:
            b = [now, 0]
        b[1] += 1
        _EV_RATE[ip] = b
        if b[1] > 120:
            return Response(status_code=204)
        if len(_EV_RATE) > 5000:
            _EV_RATE.clear()
        raw = (await req.body())[:6000]
        d = json.loads(raw.decode('utf-8', 'replace') or '{}')
        n = str(d.get('n') or '')
        if n not in _EV_NAMES:
            return Response(status_code=204)
        lt = _touch_from_cookie(req, 'mp_lt')
        props = {}
        for k in ('it', 'r', 'q', 'm', 'tz', 'f'):
            if d.get(k) not in (None, '', []):
                props[k] = d.get(k)
        cid = ''
        try:
            m = _av().member_of(req) if n in ('purchase', 'sign_up', 'login', 'begin_checkout') else None
            cid = (m or {}).get('customer_id') or ''
        except Exception:
            cid = ''
        _EVQ.append((secrets.token_hex(8), _iso(), _day(),
                     _cid(req.cookies.get('mp_vid'))[:24], _cid(req.cookies.get('mp_sid'))[:16], cid,
                     n, str(d.get('p') or '')[:120], int(d.get('v') or 0), str(d.get('c') or 'KRW')[:3],
                     str(d.get('o') or '')[:40], str(lt.get('s') or '(direct)')[:40],
                     str(lt.get('m') or '(none)')[:30], str(lt.get('c') or '')[:80],
                     _country(req), str(d.get('l') or '')[:5], 'm' if _is_mobile(req) else 'd',
                     json.dumps(props, ensure_ascii=False)[:1500]))
        _ensure_flusher()
    except Exception:
        pass
    return Response(status_code=204)


_FLUSH = {'t': None}


def _ensure_flusher():
    t = _FLUSH['t']
    if t and t.is_alive():
        return
    t = threading.Thread(target=_flush_loop, daemon=True)
    _FLUSH['t'] = t
    t.start()


def _flush_loop():
    while True:
        time.sleep(4)
        try:
            flush_events()
        except Exception as e:
            print('[growth] flush 실패: %s' % str(e)[:160], flush=True)


def flush_events():
    """메모리 큐 → mp_events 일괄 저장 (풀 연결 1개 · 트랜잭션 1개)."""
    if not _EVQ or not ensure():
        return 0
    batch = []
    while _EVQ and len(batch) < 500:
        batch.append(_EVQ.popleft())
    if not batch:
        return 0
    sql = ('INSERT INTO mp_events(id,ts,day,vid,sid,cid,name,path,value,currency,oid,src,med,cmp,'
           'country,lang,dev,props) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING')
    with _app().db() as c:
        for r in batch:
            c.exec(sql, r)
    return len(batch)


@growth_router.post('/api/consent')
async def api_consent(req: Request):
    """동의 이력 서버 기록(감사 대응) — 쿠키는 클라이언트가 이미 설정."""
    try:
        d = await req.json()
        v = 'all' if d.get('v') == 'all' else 'ess'
        _EVQ.append((secrets.token_hex(8), _iso(), _day(), _cid(req.cookies.get('mp_vid'))[:24], '', '',
                     'consent', '', 0, 'KRW', '', '', '', '', _country(req), '', 'm' if _is_mobile(req) else 'd',
                     json.dumps({'v': v})))
        _ensure_flusher()
    except Exception:
        pass
    return Response(status_code=204)


# ═══════════════════════════ 주문 어트리뷰션 스냅샷 ═════════════════════
def order_attr_capture(req, order_id, body=None):
    """주문 생성 직후(주문 트랜잭션 밖) 유입·동의·식별 쿠키를 orders.attr 에 저장.
    채널별 매출·ROAS·서버전환(CAPI)의 단일 근거. 실패해도 주문에는 영향 없음."""
    try:
        ck = req.cookies
        cs = ck.get('mp_consent') or ''
        a = {
            'ft': _touch_from_cookie(req, 'mp_ft'), 'lt': _touch_from_cookie(req, 'mp_lt'),
            'vid': _cid(ck.get('mp_vid'))[:24], 'sid': _cid(ck.get('mp_sid'))[:16],
            'fbp': str(ck.get('_fbp') or '')[:80], 'fbc': str(ck.get('_fbc') or '')[:200],
            'ttp': str(ck.get('_ttp') or '')[:80], 'ttclid': str(ck.get('ttclid') or '')[:200],
            'ua': (req.headers.get('user-agent') or '')[:300],
            'ads': (cs == 'all') if cs else True,
            'lang': str((body or {}).get('lang') or ck.get('mp_lang') or 'ko')[:5],
            'cur': str((body or {}).get('viewCurrency') or ck.get('mp_cur') or 'KRW')[:3],
        }
        if not a['ttclid']:
            a['ttclid'] = str((a['lt'] or {}).get('ttclid') or '')[:200]
        _run('UPDATE orders SET attr=? WHERE order_id=?', (json.dumps(a, ensure_ascii=False)[:4000], order_id))
    except Exception as e:
        print('[growth] attr capture skip %s: %s' % (order_id, str(e)[:120]), flush=True)


def _order(oid):
    try:
        return _one('SELECT * FROM orders WHERE order_id=?', (oid,))
    except Exception:
        return None


def _jl(s, d):
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return v if v is not None else d
    except Exception:
        return d


def _sha(s):
    s = str(s or '').strip().lower()
    return hashlib.sha256(s.encode('utf-8')).hexdigest() if s else ''


def _e164(phone, country=''):
    """국가코드 포함 숫자열. 국내 010… → 8210…"""
    d = re.sub(r'\D', '', str(phone or ''))
    if not d:
        return ''
    if d.startswith('0') and len(d) in (10, 11) and (country in ('', 'KR')):
        return '82' + d[1:]
    return d


# ═══════════════════════════ [5] 서버 전환 (CAPI) ═══════════════════════
def _post_json(url, payload, headers=None, timeout=10):
    data = json.dumps(payload).encode('utf-8')
    h = {'Content-Type': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()[:2000].decode('utf-8', 'replace')


def _conv_logged(oid, ch):
    try:
        return bool(_one("SELECT id FROM mp_conv_log WHERE oid=? AND channel=? AND status='OK'", (oid, ch)))
    except Exception:
        return False


def _conv_log(oid, ch, status, detail=''):
    try:
        _run('INSERT INTO mp_conv_log(id,ts,oid,channel,status,detail) VALUES(?,?,?,?,?,?)',
             (secrets.token_hex(8), _iso(), oid, ch, status, str(detail)[:500]))
    except Exception:
        pass


def send_server_purchase(oid):
    """Meta Conversions API · TikTok Events API — Purchase. 픽셀과 event_id(pur_<oid>)로 중복 제거.
    광고 동의 거부(attr.ads=False) 주문은 전송하지 않는다."""
    c = cfg()
    if not ((c['meta'] and c['meta_capi']) or (c['tt'] and c['tt_api'])):
        return
    r = _order(oid)
    if not r or r.get('status') != 'PAID':
        return
    at = _jl(r.get('attr'), {}) or {}
    if at.get('ads') is False:
        return
    buyer = _jl(r.get('buyer'), {}) or {}
    items = _jl(r.get('items'), []) or []
    amount = int(r.get('amount') or 0)
    country = (r.get('country') or buyer.get('country') or 'KR')[:2].upper()
    ph = _e164(buyer.get('phone') or r.get('contact_phone_norm'), country)
    em = str(buyer.get('email') or '').strip().lower()
    ext = r.get('customer_id') or ''
    ip = (r.get('client_ip') or '').strip()
    ua = at.get('ua') or ''
    ts = int(time.time())
    url = _site() + '/order-complete'
    if c['meta'] and c['meta_capi'] and not _conv_logged(oid, 'meta'):
        ud = {'em': [_sha(em)] if em else [], 'ph': [_sha(ph)] if ph else [],
              'external_id': [_sha(ext)] if ext else [], 'country': [_sha(country.lower())]}
        if ip: ud['client_ip_address'] = ip
        if ua: ud['client_user_agent'] = ua
        if at.get('fbp'): ud['fbp'] = at['fbp']
        if at.get('fbc'): ud['fbc'] = at['fbc']
        elif (at.get('lt') or {}).get('fbclid'):
            ud['fbc'] = 'fb.1.%d.%s' % (int((at['lt'].get('ts') or ts)) * 1000, at['lt']['fbclid'])
        ud = {k: v for k, v in ud.items() if v not in ([], '', None)}
        ev = {'event_name': 'Purchase', 'event_time': ts, 'event_id': 'pur_' + oid,
              'action_source': 'website', 'event_source_url': url, 'user_data': ud,
              'custom_data': {'currency': 'KRW', 'value': amount, 'order_id': oid, 'content_type': 'product',
                              'num_items': sum(int(i.get('q') or 1) for i in items),
                              'contents': [{'id': str(i.get('id')), 'quantity': int(i.get('q') or 1),
                                            'item_price': int(i.get('p') or 0)} for i in items[:50]]}}
        payload = {'data': [ev]}
        if c['meta_test']:
            payload['test_event_code'] = c['meta_test']
        try:
            st, body = _post_json('https://graph.facebook.com/v21.0/%s/events?access_token=%s'
                                  % (c['meta'], urllib.parse.quote(c['meta_capi'])), payload)
            _conv_log(oid, 'meta', 'OK' if st == 200 else 'ERR', body)
        except Exception as e:
            _conv_log(oid, 'meta', 'ERR', str(e))
    if c['tt'] and c['tt_api'] and not _conv_logged(oid, 'tiktok'):
        user = {}
        if em: user['email'] = _sha(em)
        if ph: user['phone'] = _sha('+' + ph)
        if ext: user['external_id'] = _sha(ext)
        if at.get('ttclid'): user['ttclid'] = at['ttclid']
        if at.get('ttp'): user['ttp'] = at['ttp']
        if ip: user['ip'] = ip
        if ua: user['user_agent'] = ua
        payload = {'event_source': 'web', 'event_source_id': c['tt'],
                   'data': [{'event': 'CompletePayment', 'event_time': ts, 'event_id': 'pur_' + oid,
                             'user': user, 'page': {'url': url},
                             'properties': {'currency': 'KRW', 'value': amount, 'order_id': oid,
                                            'content_type': 'product',
                                            'contents': [{'content_id': str(i.get('id')),
                                                          'content_name': str(i.get('n') or '')[:100],
                                                          'quantity': int(i.get('q') or 1),
                                                          'price': int(i.get('p') or 0)} for i in items[:50]]}}]}
        try:
            st, body = _post_json('https://business-api.tiktok.com/open_api/v1.3/event/track/', payload,
                                  {'Access-Token': c['tt_api']})
            ok = st == 200 and '"code":0' in body.replace(' ', '')
            _conv_log(oid, 'tiktok', 'OK' if ok else 'ERR', body)
        except Exception as e:
            _conv_log(oid, 'tiktok', 'ERR', str(e))


# ═══════════════════════════ [6] 이메일 ═════════════════════════════════
def mail_enabled():
    return bool(_env('RESEND_API_KEY') or (_env('SMTP_HOST') and _env('SMTP_USER')))


def _mail_from():
    return _env('MAIL_FROM', 'MAPDAL SEOUL <hello@mapdal.kr>')


def _mail_sent(kind, ref):
    try:
        return bool(_one("SELECT id FROM mp_mail_log WHERE kind=? AND ref=? AND status='SENT'", (kind, ref)))
    except Exception:
        return False


def send_mail(to, subject, html_body, kind='misc', ref='', text=''):
    """Resend(HTTP) 우선 → SMTP 폴백. (kind, ref) 기준 1회만 발송. 반환 True=발송."""
    to = str(to or '').strip()
    if not to or '@' not in to or not mail_enabled():
        return False
    if ref and _mail_sent(kind, ref):
        return False
    status, err = 'SENT', ''
    try:
        if _env('RESEND_API_KEY'):
            st, body = _post_json('https://api.resend.com/emails',
                                  {'from': _mail_from(), 'to': [to], 'subject': subject, 'html': html_body,
                                   'text': text or re.sub(r'<[^>]+>', ' ', html_body)[:5000],
                                   'reply_to': _env('MAIL_REPLY_TO', 'cx@mealzip.kr')},
                                  {'Authorization': 'Bearer ' + _env('RESEND_API_KEY')})
            if st >= 300:
                status, err = 'ERR', body
        else:
            import smtplib, ssl
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.utils import formataddr, parseaddr
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            nm, ad = parseaddr(_mail_from())
            msg['From'] = formataddr((nm, ad))
            msg['To'] = to
            msg['Reply-To'] = _env('MAIL_REPLY_TO', 'cx@mealzip.kr')
            msg.attach(MIMEText(text or re.sub(r'<[^>]+>', ' ', html_body), 'plain', 'utf-8'))
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))
            port = int(_env('SMTP_PORT', '587') or 587)
            if port == 465:
                s = smtplib.SMTP_SSL(_env('SMTP_HOST'), port, context=ssl.create_default_context(), timeout=15)
            else:
                s = smtplib.SMTP(_env('SMTP_HOST'), port, timeout=15)
                s.starttls(context=ssl.create_default_context())
            s.login(_env('SMTP_USER'), _env('SMTP_PASS'))
            s.sendmail(ad, [to], msg.as_string())
            s.quit()
    except Exception as e:
        status, err = 'ERR', str(e)
    try:
        _run('INSERT INTO mp_mail_log(id,ts,to_addr,kind,ref,status,err) VALUES(?,?,?,?,?,?,?)',
             (secrets.token_hex(8), _iso(), to[:120], kind, str(ref)[:80], status, str(err)[:400]))
    except Exception:
        pass
    return status == 'SENT'


_MT = {  # 메일 문구 — ko/en/ja/zh
    'ko': {'hi': '%s님, 안녕하세요.', 'paid_s': '[맵달SEOUL] 주문이 완료되었습니다 (%s)',
           'paid_h': '주문해 주셔서 감사합니다', 'paid_p': '결제가 정상적으로 완료되었습니다. 준비되는 대로 출고해 드릴게요.',
           'dep_s': '[맵달SEOUL] 입금 안내 (%s)', 'dep_h': '아래 계좌로 입금해 주세요',
           'dep_p': '입금이 확인되면 바로 출고 준비를 시작합니다.',
           'ship_s': '[맵달SEOUL] 상품이 발송되었습니다 (%s)', 'ship_h': '상품이 출발했어요',
           'ship_p': '운송장 번호로 배송 상황을 확인하실 수 있습니다.',
           'order': '주문번호', 'total': '결제금액', 'acct': '입금계좌', 'holder': '예금주', 'due': '입금기한',
           'track': '운송장', 'cta': '주문 내역 보기', 'items': '주문 상품',
           'foot': '성수동 K-컬처 플래그십 · 서울 성동구 성수이로16길 5 · 매일 11:00–21:00',
           'unsub': '수신거부'},
    'en': {'hi': 'Hi %s,', 'paid_s': 'Your MAPDAL SEOUL order is confirmed (%s)',
           'paid_h': 'Thank you for your order', 'paid_p': 'Your payment went through. We will ship your order as soon as it is packed.',
           'dep_s': 'Payment instructions for your order (%s)', 'dep_h': 'Please complete your bank transfer',
           'dep_p': 'We start packing as soon as the deposit is confirmed.',
           'ship_s': 'Your MAPDAL SEOUL order has shipped (%s)', 'ship_h': 'Your order is on its way',
           'ship_p': 'Use the tracking number below to follow your parcel.',
           'order': 'Order no.', 'total': 'Total', 'acct': 'Account', 'holder': 'Holder', 'due': 'Pay by',
           'track': 'Tracking', 'cta': 'View my order', 'items': 'Items',
           'foot': 'K-culture flagship in Seongsu, Seoul · 5 Seongsui-ro 16-gil · Open daily 11:00–21:00 KST',
           'unsub': 'Unsubscribe'},
    'ja': {'hi': '%s 様', 'paid_s': '【MAPDAL SEOUL】ご注文ありがとうございます（%s）',
           'paid_h': 'ご注文ありがとうございます', 'paid_p': 'お支払いが完了しました。準備ができ次第発送いたします。',
           'dep_s': '【MAPDAL SEOUL】お振込みのご案内（%s）', 'dep_h': '下記口座へお振込みください',
           'dep_p': 'ご入金を確認次第、発送準備を開始します。',
           'ship_s': '【MAPDAL SEOUL】商品を発送しました（%s）', 'ship_h': '商品を発送しました',
           'ship_p': '追跡番号で配送状況をご確認いただけます。',
           'order': '注文番号', 'total': 'お支払い金額', 'acct': '振込先', 'holder': '名義', 'due': '振込期限',
           'track': '追跡番号', 'cta': '注文履歴を見る', 'items': 'ご注文商品',
           'foot': 'ソウル・聖水洞のK-カルチャー旗艦店 · 毎日 11:00–21:00（韓国時間）', 'unsub': '配信停止'},
    'zh': {'hi': '%s 您好，', 'paid_s': '【MAPDAL SEOUL】订单已确认（%s）',
           'paid_h': '感谢您的订购', 'paid_p': '付款已完成，我们将尽快为您发货。',
           'dep_s': '【MAPDAL SEOUL】转账付款说明（%s）', 'dep_h': '请转账至以下账户',
           'dep_p': '确认到账后我们将立即为您备货。',
           'ship_s': '【MAPDAL SEOUL】商品已发货（%s）', 'ship_h': '您的包裹已出发',
           'ship_p': '可通过运单号查询物流状态。',
           'order': '订单号', 'total': '支付金额', 'acct': '收款账户', 'holder': '户名', 'due': '付款期限',
           'track': '运单号', 'cta': '查看订单', 'items': '订购商品',
           'foot': '首尔圣水洞 K-文化旗舰店 · 每天 11:00–21:00（韩国时间）', 'unsub': '退订'},
}


def _mt(lang):
    return _MT.get((lang or 'ko')[:2], _MT['en'])


def mail_layout(lang, heading, intro, rows_html='', cta=('', ''), extra='', unsub_url=''):
    """브랜드 메일 레이아웃 — 테이블 기반(주요 메일 클라이언트 호환), 인라인 스타일."""
    t = _mt(lang)
    btn = ('<tr><td style="padding:8px 32px 28px"><a href="%s" style="display:inline-block;background:#DC2B24;color:#fff;'
           'text-decoration:none;font-weight:700;font-size:14px;padding:14px 22px">%s</a></td></tr>'
           % (_e(cta[0]), _e(cta[1]))) if cta and cta[0] else ''
    un = ('<br><a href="%s" style="color:#87867F">%s</a>' % (_e(unsub_url), t['unsub'])) if unsub_url else ''
    return ('<!doctype html><html lang="%s"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '</head><body style="margin:0;background:#F4F3EF;font-family:-apple-system,BlinkMacSystemFont,\'Apple SD Gothic Neo\','
            '\'Noto Sans KR\',\'Hiragino Sans\',\'PingFang SC\',Arial,sans-serif;color:#141414">'
            '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">'
            '<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%%;background:#fff">'
            '<tr><td style="background:#141414;padding:18px 32px;border-bottom:4px solid #DC2B24">'
            '<span style="color:#fff;font-weight:900;font-size:20px;letter-spacing:.02em">MAPDAL</span>'
            '<span style="color:#DC2B24;font-weight:900;font-size:20px">SEOUL</span></td></tr>'
            '<tr><td style="padding:30px 32px 6px"><h1 style="margin:0 0 10px;font-size:22px;line-height:1.35">%s</h1>'
            '<p style="margin:0;font-size:14.5px;line-height:1.7;color:#3a3a3a">%s</p></td></tr>'
            '<tr><td style="padding:14px 32px">%s</td></tr>%s%s'
            '<tr><td style="padding:18px 32px;background:#FAFAF7;font-size:11.5px;line-height:1.7;color:#87867F">%s%s</td></tr>'
            '</table></td></tr></table></body></html>'
            % (_e(lang or 'ko'), _e(heading), intro, rows_html, extra, btn, _e(t['foot']), un))


def _kv_rows(pairs):
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #E7E5DF">'
            + ''.join('<tr><td style="padding:10px 0;border-bottom:1px solid #E7E5DF;font-size:13px;color:#87867F;width:34%%">%s</td>'
                      '<td style="padding:10px 0;border-bottom:1px solid #E7E5DF;font-size:14px;font-weight:600">%s</td></tr>'
                      % (_e(k), _e(v)) for k, v in pairs if v not in (None, ''))
            + '</table>')


def _items_html(items):
    return ''.join('<div style="font-size:13.5px;line-height:1.6;padding:4px 0">· %s × %d</div>'
                   % (_e(str(i.get('n') or '')[:80]), int(i.get('q') or 1)) for i in (items or [])[:20])


def _won(n):
    return '₩' + format(int(n or 0), ',')


def order_mail(oid, event):
    """주문 상태 메일 (paid · deposit_wait · shipped). 언어 = 주문 시 사이트 언어."""
    if not mail_enabled():
        return
    r = _order(oid)
    if not r:
        return
    buyer = _jl(r.get('buyer'), {}) or {}
    to = str(buyer.get('email') or '').strip()
    if not to:
        return
    at = _jl(r.get('attr'), {}) or {}
    lang = (at.get('lang') or 'ko')[:2]
    t = _mt(lang)
    items = _jl(r.get('items'), []) or []
    name = buyer.get('name') or ''
    hi = (t['hi'] % _e(name)) + '<br>' if name else ''
    try:   # 서명 링크 — 다른 기기·메일 앱에서도 입력 없이 주문 조회(/track)가 열린다
        import globalshop
        link = globalshop.track_link(oid, to)
        if lang in ('en', 'ja', 'zh'):
            link = link.replace(_site() + '/track', _site() + '/' + lang + '/track', 1)
    except Exception:
        link = _site() + '/order-complete?oid=' + urllib.parse.quote(oid)
    if event == 'paid':
        subj, head, intro = t['paid_s'] % oid, t['paid_h'], hi + _e(t['paid_p'])
        rows_ = _kv_rows([(t['order'], oid), (t['total'], _won(r.get('amount')))])
    elif event == 'deposit_wait':
        subj, head, intro = t['dep_s'] % oid, t['dep_h'], hi + _e(t['dep_p'])
        acct = ' '.join(x for x in ((r.get('vbank_name') or '').strip(), (r.get('vbank_num') or '').strip()) if x)
        due = (r.get('vbank_due') or '').strip()
        due_s = ('%s-%s-%s %s:%s' % (due[:4], due[4:6], due[6:8], due[8:10], due[10:12])) if len(due) >= 12 else due
        rows_ = _kv_rows([(t['order'], oid), (t['total'], _won(r.get('amount'))), (t['acct'], acct),
                          (t['holder'], r.get('vbank_holder') or ''), (t['due'], due_s)])
    elif event == 'shipped':
        subj, head, intro = t['ship_s'] % oid, t['ship_h'], hi + _e(t['ship_p'])
        trk = ' '.join(x for x in ((r.get('courier') or '').strip().upper(), (r.get('tracking') or '').strip()) if x)
        rows_ = _kv_rows([(t['order'], oid), (t['track'], trk)])
    else:
        return
    extra = ('<tr><td style="padding:4px 32px 14px"><div style="font-size:12px;color:#87867F;margin-bottom:4px">%s</div>%s</td></tr>'
             % (_e(t['items']), _items_html(items)))
    send_mail(to, subj, mail_layout(lang, head, intro, rows_, (link, t['cta']), extra), 'order_' + event, oid)


def on_order_event(oid, event):
    """admin_v2.order_notify_async 에서 백그라운드로 호출 — 메일·서버전환·쿠폰 확정·이벤트 원장."""
    try:
        ensure()
        if event == 'paid':
            try:
                send_server_purchase(oid)
            except Exception as e:
                print('[growth] capi %s: %s' % (oid, e), flush=True)
            try:
                coupon_finalize(oid)
            except Exception:
                pass
            try:
                _purchase_event_row(oid)
            except Exception:
                pass
            try:
                contact_link_order(oid)
            except Exception:
                pass
        order_mail(oid, event)
    except Exception as e:
        print('[growth] on_order_event %s/%s: %s' % (oid, event, e), flush=True)


def on_order_event_async(oid, event):
    try:
        threading.Thread(target=on_order_event, args=(oid, event), daemon=True).start()
    except Exception:
        pass


def _purchase_event_row(oid):
    """결제완료를 이벤트 원장에도 기록(픽셀 차단과 무관한 권위 있는 purchase)."""
    r = _order(oid)
    if not r or r.get('status') != 'PAID':
        return
    if _one("SELECT id FROM mp_events WHERE name='purchase_server' AND oid=?", (oid,)):
        return
    at = _jl(r.get('attr'), {}) or {}
    lt = at.get('lt') or {}
    items = _jl(r.get('items'), []) or []
    _run('INSERT INTO mp_events(id,ts,day,vid,sid,cid,name,path,value,currency,oid,src,med,cmp,country,lang,dev,props) '
         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
         (secrets.token_hex(8), _iso(), _day(), at.get('vid') or '', at.get('sid') or '', r.get('customer_id') or '',
          'purchase_server', '/order-complete', int(r.get('amount') or 0), 'KRW', oid,
          str(lt.get('s') or '(direct)')[:40], str(lt.get('m') or '(none)')[:30], str(lt.get('c') or '')[:80],
          (r.get('country') or '')[:2], (at.get('lang') or '')[:5], '',
          json.dumps({'it': [[i.get('id'), i.get('q'), i.get('p')] for i in items[:20]]}, ensure_ascii=False)[:1500]))


# ═══════════════════════════ 쿠폰 ══════════════════════════════════════
def _norm_code(code):
    return re.sub(r'[^A-Z0-9-]', '', str(code or '').upper())[:24]


def _email_like(email):
    """orders.buyer(JSON 텍스트)에서 이메일 필드를 찾는 LIKE 패턴 — 와일드카드 문자는 이스케이프."""
    e = str(email or '').lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return '%"email": "' + e + '"%'


def _buyer_has_paid(email='', customer_id='', phone=''):
    """첫 주문 쿠폰 판정 — 이메일·고객ID·전화 중 하나라도 결제완료 이력이 있으면 True."""
    try:
        if customer_id and _one("SELECT order_id FROM orders WHERE customer_id=? AND status='PAID' LIMIT 1", (customer_id,)):
            return True
        if phone and _one("SELECT order_id FROM orders WHERE contact_phone_norm=? AND status='PAID' LIMIT 1", (phone,)):
            return True
        if email:
            if _one("SELECT order_id FROM orders WHERE status='PAID' AND buyer LIKE ? ESCAPE '\\' LIMIT 1",
                    (_email_like(email),)):
                return True
    except Exception:
        return False
    return False


def coupon_check(code, email='', customer_id='', phone='', intl=False):
    """주문 트랜잭션 '밖'에서 호출 — 유효하면 쿠폰 dict, 아니면 HTTPException(400)."""
    code = _norm_code(code)
    if not code:
        return None
    if not ensure():
        raise HTTPException(400, '쿠폰을 확인할 수 없습니다 — 잠시 후 다시 시도해 주세요')
    cp = _one('SELECT * FROM mp_coupons WHERE code=?', (code,))
    if not cp or not int(cp.get('active') or 0):
        raise HTTPException(400, '사용할 수 없는 쿠폰입니다 (Invalid coupon)')
    today = _day()
    if cp.get('starts') and today < str(cp['starts'])[:10]:
        raise HTTPException(400, '아직 사용 기간이 아닌 쿠폰입니다 (Not yet valid)')
    if cp.get('ends') and today > str(cp['ends'])[:10]:
        raise HTTPException(400, '사용 기간이 지난 쿠폰입니다 (Expired)')
    if cp.get('uses_left') is not None and int(cp['uses_left']) <= 0:
        raise HTTPException(400, '이미 사용된 쿠폰입니다 (Already used)')
    sc = cp.get('scope') or 'all'
    if sc == 'first' and _buyer_has_paid(email, customer_id, phone):
        raise HTTPException(400, '첫 주문 전용 쿠폰입니다 (First order only)')
    if sc == 'intl' and not intl:
        raise HTTPException(400, '해외 배송 주문 전용 쿠폰입니다 (International orders only)')
    return cp


def coupon_amount(cp, sub):
    """순수 계산 (DB 접근 없음) — 주문 트랜잭션 안에서 안전하게 호출 가능."""
    if not cp:
        return 0
    sub = int(sub or 0)
    if sub < int(cp.get('min_sub') or 0):
        raise HTTPException(400, '쿠폰 최소 주문금액(%s)에 미달합니다 (Minimum order not met)'
                            % _won(cp.get('min_sub')))
    v = int(cp.get('value') or 0)
    off = (sub * v // 100) if (cp.get('kind') or 'pct') == 'pct' else v
    mx = int(cp.get('max_off') or 0)
    if mx > 0:
        off = min(off, mx)
    return max(0, min(off, sub - 100))      # 결제금액 100원 미만 방지(PG 최소금액)


def coupon_hold(order_id, code, off):
    try:
        _run('INSERT INTO mp_coupon_uses(order_id,code,created,amount_off,status) VALUES(?,?,?,?,?) '
             'ON CONFLICT DO NOTHING', (order_id, _norm_code(code), _iso(), int(off), 'HELD'))
    except Exception:
        pass


def coupon_finalize(oid):
    u = _one("SELECT * FROM mp_coupon_uses WHERE order_id=? AND status='HELD'", (oid,))
    if not u:
        return
    _run("UPDATE mp_coupon_uses SET status='USED' WHERE order_id=?", (oid,))
    _run('UPDATE mp_coupons SET uses_left=uses_left-1 WHERE code=? AND uses_left IS NOT NULL AND uses_left>0', (u['code'],))


@growth_router.post('/api/coupon/check')
async def api_coupon_check(req: Request):
    """체크아웃 미리보기 — 할인액 계산(실제 적용은 /api/orders 에서 서버가 재검증)."""
    d = await req.json()
    sub = int(d.get('sub') or 0)
    email = str(d.get('email') or '').strip().lower()
    cid = ''
    try:
        m = _av().member_of(req)
        cid = (m or {}).get('customer_id') or ''
        email = email or str((m or {}).get('email') or '').lower()
    except Exception:
        pass
    cp = coupon_check(d.get('code'), email, cid, '', bool(d.get('intl')))
    off = coupon_amount(cp, sub)
    return {'code': cp['code'], 'off': off, 'note': cp.get('note') or ''}


def _new_code(prefix):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return prefix + '-' + ''.join(secrets.choice(alphabet) for _ in range(6))


def issue_coupon(prefix, kind, value, min_sub, max_off, days, scope, note, contact_id=''):
    for _ in range(5):
        code = _new_code(prefix)
        try:
            _run('INSERT INTO mp_coupons(code,kind,value,min_sub,max_off,starts,ends,uses_left,scope,note,created,active,contact_id) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)',
                 (code, kind, value, min_sub, max_off, _day(), _day(_now() + datetime.timedelta(days=days)),
                  1, scope, note, _iso(), contact_id))
            return code
        except Exception:
            continue
    return ''


# ═══════════════════════════ [7] O2O — 매장 QR · 연락처 ═════════════════
def _secret():
    s = _env('GROWTH_SECRET') or _env('ADMIN_TOKEN') or _env('DATABASE_URL') or 'mapdal-local'
    return hashlib.sha256(('mpgrowth:' + s).encode()).digest()


def unsub_token(email):
    return hmac.new(_secret(), str(email or '').lower().encode(), hashlib.sha256).hexdigest()[:24]


def unsub_url(email):
    return '%s/api/unsub?e=%s&t=%s' % (_site(), urllib.parse.quote(email), unsub_token(email))


def mkt_ok(email='', customer_id=''):
    """광고성 메일 발송 가능 여부 — 명시적 동의 + 수신거부 없음."""
    email = str(email or '').strip().lower()
    try:
        if email and _one('SELECT id FROM mp_contacts WHERE email=? AND unsub=1 LIMIT 1', (email,)):
            return False
        if email and _one('SELECT id FROM mp_contacts WHERE email=? AND consent=1 AND unsub=0 LIMIT 1', (email,)):
            return True
        if customer_id:
            r = _one("SELECT granted FROM consent_history WHERE customer_id=? AND consent_type='MARKETING' "
                     "ORDER BY created_at DESC LIMIT 1", (customer_id,))
            return bool(r and int(r.get('granted') or 0))
    except Exception:
        return False
    return False


_CH_OK = {'email', 'whatsapp', 'line', 'wechat', 'kakao', 'instagram'}


@growth_router.post('/api/contacts')
async def api_contact(req: Request):
    """매장 QR·푸터·체크아웃 동의 캡처. 이메일 또는 메신저 핸들 1개 필수 + 광고 수신 동의 필수."""
    ensure()
    d = await req.json()
    email = str(d.get('email') or '').strip().lower()[:80]
    ch = str(d.get('channel') or 'email').lower()
    ch = ch if ch in _CH_OK else 'email'
    handle = re.sub(r'[\s<>"\']', '', str(d.get('handle') or ''))[:60]
    if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[A-Za-z]{2,}', email):
        raise HTTPException(400, 'Please check your email address')
    if not email and not handle:
        raise HTTPException(400, 'Email or messenger ID is required')
    if not d.get('consent'):
        raise HTTPException(400, 'Please agree to receive offers')
    lang = re.sub(r'[^a-z]', '', str(d.get('lang') or 'en').lower())[:2] or 'en'
    country = re.sub(r'[^A-Z]', '', str(d.get('country') or _country(req) or '').upper())[:2]
    src = re.sub(r'[^a-z0-9:_-]', '', str(d.get('source') or 'web').lower())[:40]
    vid = _cid(req.cookies.get('mp_vid'))[:24]
    ex = None
    if email:
        ex = _one('SELECT * FROM mp_contacts WHERE email=? ORDER BY created DESC LIMIT 1', (email,))
    if ex and ex.get('coupon'):
        _run('UPDATE mp_contacts SET consent=1, unsub=0 WHERE id=?', (ex['id'],))
        return {'ok': True, 'coupon': ex['coupon'], 'existing': True}
    cid_ = secrets.token_hex(10)
    first = src.startswith('qr') or src.startswith('visit')
    code = issue_coupon('HOME', 'pct', 10, 30000, 30000, 180, 'first',
                        'O2O 웰컴 — 첫 온라인 주문 10% (최대 3만원)' if first else '뉴스레터 웰컴 — 첫 주문 10%', cid_)
    _run('INSERT INTO mp_contacts(id,created,email,phone,channel,handle,country,lang,source,consent,vid,coupon,unsub,mail_step) '
         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
         (cid_, _iso(), email or None, None, ch if handle else 'email', handle or None, country, lang, src, 1, vid, code, ''))
    _EVQ.append((secrets.token_hex(8), _iso(), _day(), vid, _cid(req.cookies.get('mp_sid'))[:16], '',
                 'generate_lead', '/visit', 0, 'KRW', '', str(_touch_from_cookie(req, 'mp_lt').get('s') or '(direct)')[:40],
                 str(_touch_from_cookie(req, 'mp_lt').get('m') or '(none)')[:30], src, country, lang,
                 'm' if _is_mobile(req) else 'd', json.dumps({'ch': ch})))
    _ensure_flusher()
    if email:
        threading.Thread(target=welcome_mail, args=(email, lang, code), daemon=True).start()
    return {'ok': True, 'coupon': code}


def contact_link_order(oid):
    r = _order(oid)
    if not r:
        return
    email = str((_jl(r.get('buyer'), {}) or {}).get('email') or '').lower()
    if email and r.get('customer_id'):
        _run('UPDATE mp_contacts SET customer_id=? WHERE email=? AND (customer_id IS NULL OR customer_id=?)',
             (r['customer_id'], email, ''))


@growth_router.get('/api/unsub', response_class=HTMLResponse)
def api_unsub(e: str = '', t: str = ''):
    e = str(e or '').strip().lower()
    if not e or not hmac.compare_digest(unsub_token(e), str(t or '')):
        return HTMLResponse('<meta charset=utf-8><p style="font-family:sans-serif;padding:40px">Invalid link.</p>', 400)
    ensure()
    try:
        if _one('SELECT id FROM mp_contacts WHERE email=?', (e,)):
            _run('UPDATE mp_contacts SET unsub=1 WHERE email=?', (e,))
        else:
            _run('INSERT INTO mp_contacts(id,created,email,channel,source,consent,unsub) VALUES(?,?,?,?,?,0,1)',
                 (secrets.token_hex(10), _iso(), e, 'email', 'unsub'))
    except Exception:
        pass
    return HTMLResponse('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'
                        '<title>MAPDAL SEOUL</title><body style="font-family:-apple-system,sans-serif;padding:60px 20px;'
                        'text-align:center;color:#141414"><h2>수신거부가 완료되었습니다</h2>'
                        '<p>You have been unsubscribed from MAPDAL SEOUL marketing emails.</p>'
                        '<p><a href="/home" style="color:#DC2B24">mapdal.kr</a></p>')


_WT = {
    'ko': ('[맵달SEOUL] 성수에서 만난 맵달, 이제 집에서도', '방문해 주셔서 감사합니다',
           '맵달SEOUL 온라인 스토어에서 매장에서 본 앨범·굿즈를 그대로 만나보세요. 첫 온라인 주문에 쓰실 수 있는 10% 쿠폰을 드립니다.',
           '쿠폰 코드', '온라인 스토어 둘러보기', '유효기간 180일 · 3만원 이상 주문 시 · 최대 3만원 할인'),
    'en': ('Your MAPDAL SEOUL welcome gift', 'Thanks for visiting us in Seongsu',
           'Keep the Seoul vibe going. Shop the albums and merch you saw in store at mapdal.kr. We ship albums and merch to 50+ countries, tracked from Seoul. Here is 10% off your first online order.',
           'Your code', 'Shop online', 'Valid 180 days · min. order ₩30,000 · up to ₩30,000 off'),
    'ja': ('MAPDAL SEOULからのウェルカムギフト', '聖水店へのご来店ありがとうございます',
           '店舗でご覧になったアルバムやグッズは mapdal.kr でいつでもお買い求めいただけます。ソウルから50か国以上へ追跡付きで発送します。初回オンライン注文で使える10%クーポンをお送りします。',
           'クーポンコード', 'オンラインストアへ', '有効期限180日 · ₩30,000以上のご注文 · 最大₩30,000割引'),
    'zh': ('MAPDAL SEOUL 欢迎礼', '感谢您光临圣水店',
           '店内看到的专辑和周边，回国后也能在 mapdal.kr 购买。从首尔发往50多个国家，全程可追踪。送您首次线上订单 10% 优惠券。',
           '优惠码', '前往线上商店', '有效期180天 · 订单满₩30,000 · 最高减₩30,000'),
}


def welcome_mail(email, lang, code):
    try:
        w = _WT.get((lang or 'en')[:2], _WT['en'])
        box = ('<div style="border:2px dashed #DC2B24;padding:16px;text-align:center;margin:6px 0">'
               '<div style="font-size:12px;color:#87867F">%s</div>'
               '<div style="font-size:26px;font-weight:900;letter-spacing:.08em;color:#DC2B24">%s</div>'
               '<div style="font-size:11.5px;color:#87867F;margin-top:4px">%s</div></div>' % (_e(w[3]), _e(code), _e(w[5])))
        url = _site() + '/shop?utm_source=email&utm_medium=crm&utm_campaign=o2o_welcome'
        send_mail(email, w[0], mail_layout(lang, w[1], _e(w[2]), box, (url, w[4]), unsub_url=unsub_url(email)),
                  'welcome', email)
    except Exception as e:
        print('[growth] welcome mail: %s' % e, flush=True)


# ═══════════════════════════ 라이프사이클 자동화 ═════════════════════════
_LT = {
    'abandon': {
        'ko': ('[맵달SEOUL] 주문을 마치지 못하셨나요?', '장바구니에 담은 상품이 기다리고 있어요',
               '결제 도중 멈춘 주문이 있어요. 한정 수량 상품은 빠르게 품절될 수 있습니다.', '주문 이어하기'),
        'en': ('Did you forget something?', 'Your picks are still waiting',
               'Your checkout was not completed. Limited items sell out fast.', 'Complete my order'),
        'ja': ('ご注文が完了していません', 'カートの商品がお待ちしています',
               'お支払いが完了していないご注文があります。数量限定商品はすぐに売り切れる場合があります。', '注文を続ける'),
        'zh': ('您的订单还未完成', '购物车里的商品还在等您', '您有一笔未完成付款的订单。限量商品可能很快售罄。', '继续下单')},
    'review': {
        'ko': ('[맵달SEOUL] 상품은 마음에 드셨나요?', '구매 후기를 남겨주세요',
               '솔직한 구매인증 리뷰는 다른 팬들에게 큰 도움이 됩니다.', '리뷰 쓰기'),
        'en': ('How was your MAPDAL SEOUL order?', 'Tell other fans what you think',
               'Your verified review helps fans around the world shop with confidence.', 'Write a review'),
        'ja': ('ご購入商品はいかがでしたか？', 'レビューをお寄せください', 'あなたの購入レビューが世界中のファンの参考になります。', 'レビューを書く'),
        'zh': ('您对商品满意吗？', '分享您的使用感受', '您的真实评价能帮助全球粉丝放心购物。', '写评价')},
    'drip7': {
        'ko': ('[맵달SEOUL] 성수가 그리울 때', '쿠폰이 아직 남아 있어요',
               '매장에서 받으신 첫 주문 10% 쿠폰을 아직 사용하지 않으셨어요. 새로 들어온 앨범과 굿즈를 확인해 보세요.', '신상품 보기'),
        'en': ('Missing Seoul already?', 'Your 10% welcome code is still waiting',
               'Bring a piece of Seongsu home. New albums and merch drop every week, shipped worldwide from Seoul.', 'See new arrivals'),
        'ja': ('ソウルが恋しくなったら', 'ウェルカムクーポンがまだ使えます',
               '聖水のひとときをご自宅でも。毎週新しいアルバムとグッズが入荷し、ソウルから海外へ発送しています。', '新着を見る'),
        'zh': ('想念首尔了吗？', '您的 10% 欢迎优惠券还未使用', '把圣水的回忆带回家。每周上新专辑与周边，从首尔发往全球。', '查看新品')},
    'winback': {
        'ko': ('[맵달SEOUL] 오랜만이에요, 다시 만나요', '돌아오신 걸 환영하는 10% 쿠폰',
               '지난 주문 이후 새로운 드롭이 많이 열렸어요. 다음 주문에 쓰실 수 있는 쿠폰을 드립니다.', '쇼핑하러 가기'),
        'en': ('We miss you at MAPDAL SEOUL', 'Here is 10% off to welcome you back',
               'A lot has dropped since your last order. Use this code on your next one.', 'Shop now'),
        'ja': ('お久しぶりです', 'おかえりなさいクーポン10%', '前回のご注文以降、新しいドロップが続々登場しています。次回のご注文にご利用ください。', 'ショップへ'),
        'zh': ('好久不见', '欢迎回来 10% 优惠券', '自您上次下单以来上新了很多商品。下次订单即可使用。', '去购物')},
}


def _pdp_url(item_id):
    s = str(item_id or '')
    if s.startswith('k2g::'):
        return '/album-detail?uid=' + urllib.parse.quote(s[5:])
    if s.startswith('mpd::'):
        return '/new-drops'
    if s.startswith('mp::'):
        return '/p/' + urllib.parse.quote(s[4:].split('::')[0])
    if s.startswith('product-'):
        return '/' + s.split('::')[0].replace('.html', '')
    return '/shop'


def _flow_mail(kind, to, lang, ref, extra_html='', cta_url='', code=''):
    t = _LT[kind].get((lang or 'en')[:2], _LT[kind]['en'])
    sep = '&' if '?' in cta_url else '?'
    url = _site() + (cta_url or '/shop') + sep + 'utm_source=email&utm_medium=crm&utm_campaign=' + kind
    box = ''
    if code:
        box = ('<div style="border:2px dashed #DC2B24;padding:14px;text-align:center;margin:6px 0">'
               '<div style="font-size:24px;font-weight:900;letter-spacing:.08em;color:#DC2B24">%s</div></div>' % _e(code))
    return send_mail(to, t[0], mail_layout(lang, t[1], _e(t[2]), box + extra_html, (url, t[3]),
                                           unsub_url=unsub_url(to)), kind, ref)


def lifecycle_tick():
    """30분 주기 — 메일 미설정이면 아무것도 하지 않는다. 모든 흐름은 (kind, ref) 기준 1회 발송."""
    if not mail_enabled() or not ensure():
        return
    now = _now()
    iso = lambda d: d.isoformat(timespec='seconds')
    # A) 결제 이탈 — 1~26시간 전 미결제 주문 · 이후 결제 없음 · 마케팅 동의
    try:
        for r in _rows("SELECT order_id, created, buyer, items, customer_id, attr FROM orders "
                       "WHERE status IN ('PENDING','FAILED') AND created>=? AND created<=?",
                       (iso(now - datetime.timedelta(hours=26)), iso(now - datetime.timedelta(hours=1)))):
            b = _jl(r.get('buyer'), {}) or {}
            em = str(b.get('email') or '').lower()
            if not em or not mkt_ok(em, r.get('customer_id')):
                continue
            ref = em + ':' + now.strftime('%G-W%V')
            if _mail_sent('abandon', ref):
                continue
            if _buyer_paid_since(em, r.get('created')):
                continue
            items = _jl(r.get('items'), []) or []
            lang = ((_jl(r.get('attr'), {}) or {}).get('lang') or 'ko')[:2]
            _flow_mail('abandon', em, lang, ref, '<div style="padding:4px 0">%s</div>' % _items_html(items),
                       _pdp_url(items[0].get('id') if items else ''))
    except Exception as e:
        print('[growth] abandon: %s' % e, flush=True)
    # B) 리뷰 요청 — 결제 7~14일 경과
    try:
        for r in _rows("SELECT order_id, buyer, items, customer_id, attr FROM orders WHERE status='PAID' "
                       "AND paid_at>=? AND paid_at<=?",
                       (iso(now - datetime.timedelta(days=14)), iso(now - datetime.timedelta(days=7)))):
            b = _jl(r.get('buyer'), {}) or {}
            em = str(b.get('email') or '').lower()
            if not em or not mkt_ok(em, r.get('customer_id')) or _mail_sent('review', r['order_id']):
                continue
            items = _jl(r.get('items'), []) or []
            lang = ((_jl(r.get('attr'), {}) or {}).get('lang') or 'ko')[:2]
            _flow_mail('review', em, lang, r['order_id'], '', (_pdp_url(items[0].get('id')) + '#mpRv') if items else '/account')
    except Exception as e:
        print('[growth] review: %s' % e, flush=True)
    # C) O2O 드립 — 연락처 등록 6~30일, 아직 첫 주문 전
    try:
        for c in _rows("SELECT * FROM mp_contacts WHERE consent=1 AND unsub=0 AND email IS NOT NULL "
                       "AND created>=? AND created<=?",
                       (iso(now - datetime.timedelta(days=30)), iso(now - datetime.timedelta(days=6)))):
            if 'd7' in (c.get('mail_step') or '') or _buyer_has_paid(c['email'], c.get('customer_id') or ''):
                continue
            if _flow_mail('drip7', c['email'], c.get('lang') or 'en', c['id'], '', '/new-drops', c.get('coupon') or ''):
                _run("UPDATE mp_contacts SET mail_step=?, last_mail=? WHERE id=?",
                     ((c.get('mail_step') or '') + ',d7', _iso(), c['id']))
    except Exception as e:
        print('[growth] drip: %s' % e, flush=True)
    # D) 윈백 — 마지막 결제 60~75일 전 · 이후 주문 없음
    try:
        cand = _rows("SELECT customer_id, MAX(paid_at) AS last FROM orders WHERE status='PAID' AND customer_id IS NOT NULL "
                     "GROUP BY customer_id HAVING MAX(paid_at)>=? AND MAX(paid_at)<=?",
                     (iso(now - datetime.timedelta(days=75)), iso(now - datetime.timedelta(days=60))))
        for c in cand[:200]:
            ref = c['customer_id'] + ':' + str(c['last'])[:10]
            if _mail_sent('winback', ref):
                continue
            r = _one("SELECT buyer, attr FROM orders WHERE customer_id=? AND status='PAID' ORDER BY paid_at DESC LIMIT 1",
                     (c['customer_id'],))
            em = str((_jl((r or {}).get('buyer'), {}) or {}).get('email') or '').lower()
            if not em or not mkt_ok(em, c['customer_id']):
                continue
            lang = ((_jl((r or {}).get('attr'), {}) or {}).get('lang') or 'ko')[:2]
            code = issue_coupon('BACK', 'pct', 10, 30000, 20000, 30, 'all', '윈백 10% (최대 2만원)')
            _flow_mail('winback', em, lang, ref, '', '/new-drops', code)
    except Exception as e:
        print('[growth] winback: %s' % e, flush=True)


def _buyer_paid_since(email, since):
    try:
        return bool(_one("SELECT order_id FROM orders WHERE status='PAID' AND created>=? AND buyer LIKE ? ESCAPE '\\' LIMIT 1",
                         (since, _email_like(email))))
    except Exception:
        return False


_SCHED = {'t': None}


def start_scheduler():
    """단일 인스턴스(Render starter) 전제 — 30분마다 라이프사이클 · 광고비 자동수집 · 오래된 이벤트 정리."""
    if _SCHED['t'] and _SCHED['t'].is_alive():
        return

    def loop():
        time.sleep(90)
        n = 0
        while True:
            try:
                lifecycle_tick()
            except Exception as e:
                print('[growth] lifecycle: %s' % e, flush=True)
            if n % 12 == 0:      # 6시간마다
                try:
                    pull_ad_spend()
                except Exception as e:
                    print('[growth] spend pull: %s' % e, flush=True)
                try:
                    _run('DELETE FROM mp_events WHERE day<?', (_day(_now() - datetime.timedelta(days=400)),))
                except Exception:
                    pass
            n += 1
            time.sleep(1800)
    t = threading.Thread(target=loop, daemon=True)
    _SCHED['t'] = t
    t.start()


# ═══════════════════════════ 광고비 수집 ═══════════════════════════════
def _chan(s, m=''):
    """유입 소스/매체 → 광고 채널 버킷 (광고비 원장과 조인되는 키)."""
    s = str(s or '').lower()
    m = str(m or '').lower()
    paid = m in ('cpc', 'ppc', 'paid', 'paid_social', 'paidsocial', 'cpm', 'display', 'ads', 'ad')
    if s == 'homescreen' or m == 'app':
        return 'app'
    if s in ('facebook', 'instagram', 'meta', 'fb', 'ig', 'threads') or s.startswith('facebook') or s.startswith('instagram'):
        return 'meta' if (paid or m in ('social_paid',)) else 'social'
    if s.startswith('google') or s in ('youtube', 'gdn', 'pmax'):
        return 'google' if paid or s in ('gdn', 'pmax') else ('search' if m == 'organic' else 'social' if s == 'youtube' else 'referral')
    if s.startswith('tiktok'):
        return 'tiktok' if paid else 'social'
    if s.startswith('naver'):
        return 'naver' if paid else 'search'
    if s.startswith('kakao'):
        return 'kakao' if paid else 'social'
    if s == 'store_qr' or m == 'offline':
        return 'store'
    if m in ('email', 'crm', 'newsletter', 'sms', 'alimtalk'):
        return 'crm'
    if m == 'organic':
        return 'search'
    if m == 'social':
        return 'social'
    if s in ('(direct)', '', 'direct'):
        return 'direct'
    if m == 'referral':
        return 'referral'
    return 'other' if not paid else s[:20]


def spend_upsert(rows_, src='manual'):
    n = 0
    for r in rows_:
        try:
            day = str(r.get('day') or '')[:10]
            datetime.date.fromisoformat(day)
            ch = re.sub(r'[^a-z0-9_-]', '', str(r.get('channel') or '').lower())[:20]
            if not ch:
                continue
            cmp_ = str(r.get('campaign') or '')[:120]
            vals = (int(float(r.get('spend') or 0)), int(float(r.get('impressions') or 0)),
                    int(float(r.get('clicks') or 0)), src, _iso())
            _run('INSERT INTO mp_ad_spend(day,channel,campaign,spend,impressions,clicks,src,updated) VALUES(?,?,?,?,?,?,?,?) '
                 'ON CONFLICT(day,channel,campaign) DO UPDATE SET spend=excluded.spend, impressions=excluded.impressions, '
                 'clicks=excluded.clicks, src=excluded.src, updated=excluded.updated', (day, ch, cmp_) + vals)
            n += 1
        except Exception:
            continue
    return n


def pull_ad_spend(days=7):
    """Meta Marketing API · TikTok Business API 일별 캠페인 광고비 자동수집 (환경변수 설정 시).
    통화: 광고계정 통화가 KRW 가 아니면 *_SPEND_FX(1단위당 원화)로 환산."""
    out = {}
    since = _day(_now() - datetime.timedelta(days=days))
    until = _day()
    tok, acct = _env('META_ADS_TOKEN'), re.sub(r'[^0-9]', '', _env('META_AD_ACCOUNT'))
    if tok and acct:
        fx = float(_env('META_SPEND_FX', '1') or 1)
        url = ('https://graph.facebook.com/v21.0/act_%s/insights?level=campaign&time_increment=1'
               '&fields=campaign_name,spend,impressions,clicks&limit=500&time_range=%s&access_token=%s'
               % (acct, urllib.parse.quote(json.dumps({'since': since, 'until': until})), urllib.parse.quote(tok)))
        rows_ = []
        try:
            while url and len(rows_) < 5000:
                with urllib.request.urlopen(url, timeout=20) as r:
                    d = json.loads(r.read().decode())
                for x in d.get('data', []):
                    rows_.append({'day': x.get('date_start'), 'channel': 'meta', 'campaign': x.get('campaign_name') or '',
                                  'spend': float(x.get('spend') or 0) * fx, 'impressions': x.get('impressions') or 0,
                                  'clicks': x.get('clicks') or 0})
                url = (d.get('paging') or {}).get('next')
            out['meta'] = spend_upsert(rows_, 'meta_api')
        except Exception as e:
            out['meta'] = 'ERR %s' % str(e)[:120]
    ttok, tadv = _env('TIKTOK_ADS_TOKEN'), re.sub(r'[^0-9]', '', _env('TIKTOK_ADVERTISER_ID'))
    if ttok and tadv:
        fx = float(_env('TIKTOK_SPEND_FX', '1') or 1)
        q = urllib.parse.urlencode({'advertiser_id': tadv, 'report_type': 'BASIC', 'data_level': 'AUCTION_CAMPAIGN',
                                    'dimensions': json.dumps(['campaign_id', 'stat_time_day']),
                                    'metrics': json.dumps(['campaign_name', 'spend', 'impressions', 'clicks']),
                                    'start_date': since, 'end_date': until, 'page_size': 1000})
        try:
            rq = urllib.request.Request('https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/?' + q,
                                        headers={'Access-Token': ttok})
            with urllib.request.urlopen(rq, timeout=20) as r:
                d = json.loads(r.read().decode())
            rows_ = []
            for x in ((d.get('data') or {}).get('list') or []):
                dm, mt = x.get('dimensions') or {}, x.get('metrics') or {}
                rows_.append({'day': str(dm.get('stat_time_day') or '')[:10], 'channel': 'tiktok',
                              'campaign': mt.get('campaign_name') or dm.get('campaign_id') or '',
                              'spend': float(mt.get('spend') or 0) * fx, 'impressions': mt.get('impressions') or 0,
                              'clicks': mt.get('clicks') or 0})
            out['tiktok'] = spend_upsert(rows_, 'tiktok_api')
        except Exception as e:
            out['tiktok'] = 'ERR %s' % str(e)[:120]
    return out


# ═══════════════════════════ [8] 그로스 대시보드 API ═════════════════════
def _admin(req, lvl=0):
    a = _av()
    actor = a.get_actor(req)
    a.need(actor, lvl)
    ensure()
    return actor


def _range(frm, to, default_days=30):
    try:
        t = datetime.date.fromisoformat(str(to)[:10])
    except Exception:
        t = _now().date()
    try:
        f = datetime.date.fromisoformat(str(frm)[:10])
    except Exception:
        f = t - datetime.timedelta(days=default_days - 1)
    if f > t:
        f, t = t, f
    return f.isoformat(), (t + datetime.timedelta(days=1)).isoformat()


def _paid_orders(f, t):
    return _rows("SELECT order_id, created, paid_at, amount, attr, country, customer_id, buyer, contact_phone_norm, ship_method "
                 "FROM orders WHERE status='PAID' AND created>=? AND created<?", (f, t))


def _ckey(r):
    """고객 식별 키 — 고객ID > 이메일 > 전화 (비회원 재구매 추적)."""
    if r.get('customer_id'):
        return 'c:' + r['customer_id']
    b = _jl(r.get('buyer'), {}) or {}
    if b.get('email'):
        return 'e:' + str(b['email']).lower()
    return 'p:' + str(r.get('contact_phone_norm') or b.get('phone') or r.get('order_id'))


@growth_router.get('/admin/api/growth/overview')
def g_overview(request: Request, frm: str = '', to: str = ''):
    _admin(request)
    f, t = _range(frm, to)
    flush_events()
    orders = _paid_orders(f, t)
    # 신규/재구매 판정용: 기간 이전 결제 고객 키
    prev_keys = set(_ckey(r) for r in _rows("SELECT customer_id, buyer, contact_phone_norm, order_id FROM orders "
                                             "WHERE status='PAID' AND created<?", (f,)))
    ch, cmp_, ctry, seen = {}, {}, {}, set()
    rev = new_rev = intl_rev = 0
    for r in orders:
        at = _jl(r.get('attr'), {}) or {}
        lt = at.get('lt') or {}
        amt = int(r.get('amount') or 0)
        rev += amt
        k = _ckey(r)
        is_new = k not in prev_keys and k not in seen
        seen.add(k)
        if is_new:
            new_rev += amt
        c = _chan(lt.get('s') or '(direct)', lt.get('m'))
        x = ch.setdefault(c, {'orders': 0, 'revenue': 0, 'new': 0})
        x['orders'] += 1; x['revenue'] += amt; x['new'] += 1 if is_new else 0
        if lt.get('c'):
            y = cmp_.setdefault((c, lt['c']), {'orders': 0, 'revenue': 0})
            y['orders'] += 1; y['revenue'] += amt
        cc = (r.get('country') or 'KR')[:2] or 'KR'
        z = ctry.setdefault(cc, {'orders': 0, 'revenue': 0})
        z['orders'] += 1; z['revenue'] += amt
        if r.get('ship_method') == 'intl' or cc != 'KR':
            intl_rev += amt
    sp_ch, sp_cmp, spend = {}, {}, 0
    for s in _rows('SELECT channel, campaign, SUM(spend) AS sp, SUM(clicks) AS cl, SUM(impressions) AS im FROM mp_ad_spend '
                   'WHERE day>=? AND day<? GROUP BY channel, campaign', (f, t)):
        v = int(s.get('sp') or 0)
        spend += v
        q = sp_ch.setdefault(s['channel'], {'spend': 0, 'clicks': 0, 'impr': 0})
        q['spend'] += v; q['clicks'] += int(s.get('cl') or 0); q['impr'] += int(s.get('im') or 0)
        sp_cmp[(s['channel'], s.get('campaign') or '')] = v
    sess = {}
    for e in _rows("SELECT src, med, COUNT(DISTINCT sid) AS n FROM mp_events WHERE name='page_view' AND day>=? AND day<? "
                   "GROUP BY src, med", (f, t)):
        c = _chan(e.get('src'), e.get('med'))
        sess[c] = sess.get(c, 0) + int(e.get('n') or 0)
    tot = _one("SELECT COUNT(DISTINCT sid) AS s, COUNT(DISTINCT vid) AS v FROM mp_events WHERE name='page_view' AND day>=? AND day<?",
               (f, t)) or {}
    chans = sorted(set(ch) | set(sp_ch) | set(sess))
    ch_rows = []
    for c in chans:
        o = ch.get(c, {'orders': 0, 'revenue': 0, 'new': 0})
        s = sp_ch.get(c, {'spend': 0, 'clicks': 0, 'impr': 0})
        n_s = sess.get(c, 0)
        ch_rows.append({'channel': c, 'sessions': n_s, 'orders': o['orders'], 'revenue': o['revenue'], 'new': o['new'],
                        'spend': s['spend'], 'clicks': s['clicks'], 'impr': s['impr'],
                        'cvr': round(o['orders'] * 100.0 / n_s, 2) if n_s else None,
                        'roas': round(o['revenue'] * 100.0 / s['spend']) if s['spend'] else None,
                        'cac': round(s['spend'] / o['new']) if (s['spend'] and o['new']) else None})
    ch_rows.sort(key=lambda x: -(x['revenue'] + x['spend']))
    cmp_rows = []
    for (c, name), v in cmp_.items():
        sp = sp_cmp.get((c, name), 0)
        cmp_rows.append({'channel': c, 'campaign': name, 'orders': v['orders'], 'revenue': v['revenue'], 'spend': sp,
                         'roas': round(v['revenue'] * 100.0 / sp) if sp else None})
    for (c, name), sp in sp_cmp.items():
        if (c, name) not in cmp_ and sp:
            cmp_rows.append({'channel': c, 'campaign': name, 'orders': 0, 'revenue': 0, 'spend': sp, 'roas': 0})
    cmp_rows.sort(key=lambda x: -(x['revenue'] + x['spend']))
    n = len(orders)
    return {'from': f, 'to': t, 'kpi': {
        'revenue': rev, 'orders': n, 'aov': round(rev / n) if n else 0, 'spend': spend,
        'roas': round(rev * 100.0 / spend) if spend else None, 'mer': round(spend * 100.0 / rev, 1) if rev else None,
        'sessions': int(tot.get('s') or 0), 'visitors': int(tot.get('v') or 0),
        'cvr': round(n * 100.0 / int(tot['s']), 2) if tot.get('s') else None,
        'new_rev': new_rev, 'repeat_rev': rev - new_rev, 'intl_rev': intl_rev},
        'channels': ch_rows, 'campaigns': cmp_rows[:60],
        'countries': sorted([{'country': k, **v} for k, v in ctry.items()], key=lambda x: -x['revenue'])}


@growth_router.get('/admin/api/growth/funnel')
def g_funnel(request: Request, frm: str = '', to: str = ''):
    _admin(request)
    f, t = _range(frm, to)
    flush_events()
    steps = ['page_view', 'view_item', 'add_to_cart', 'begin_checkout', 'add_payment_info', 'purchase']
    out = []
    by = {}
    for r in _rows("SELECT name, dev, COUNT(DISTINCT sid) AS n FROM mp_events WHERE day>=? AND day<? AND name IN "
                   "('page_view','view_item','add_to_cart','begin_checkout','add_payment_info','purchase') GROUP BY name, dev",
                   (f, t)):
        by.setdefault(r['name'], {})[r.get('dev') or '?'] = int(r.get('n') or 0)
    for s in steps:
        d = by.get(s, {})
        out.append({'step': s, 'all': sum(d.values()), 'mobile': d.get('m', 0), 'desktop': d.get('d', 0)})
    langs = _rows("SELECT lang, COUNT(DISTINCT sid) AS n FROM mp_events WHERE name='page_view' AND day>=? AND day<? "
                  "GROUP BY lang ORDER BY n DESC", (f, t))
    daily = _rows("SELECT day, COUNT(DISTINCT sid) AS s FROM mp_events WHERE name='page_view' AND day>=? AND day<? "
                  "GROUP BY day ORDER BY day", (f, t))
    rev = {}
    for r in _rows("SELECT created, amount FROM orders WHERE status='PAID' AND created>=? AND created<?", (f, t)):
        d = str(r['created'])[:10]
        rev[d] = rev.get(d, 0) + int(r.get('amount') or 0)
    sp = {r['day']: int(r.get('s') or 0) for r in _rows('SELECT day, SUM(spend) AS s FROM mp_ad_spend WHERE day>=? AND day<? GROUP BY day', (f, t))}
    days = sorted(set([x['day'] for x in daily]) | set(rev) | set(sp))
    dmap = {x['day']: int(x.get('s') or 0) for x in daily}
    return {'funnel': out, 'langs': langs,
            'daily': [{'day': d, 'sessions': dmap.get(d, 0), 'revenue': rev.get(d, 0), 'spend': sp.get(d, 0)} for d in days]}


@growth_router.get('/admin/api/growth/cohorts')
def g_cohorts(request: Request, months: int = 9):
    """월별 첫 구매 코호트 → M+n 재구매율 · 누적 LTV. 국가(KR/해외)별 재구매율 포함."""
    _admin(request)
    months = max(3, min(int(months or 9), 18))
    rs = _rows("SELECT order_id, created, amount, customer_id, buyer, contact_phone_norm, country FROM orders "
               "WHERE status='PAID' ORDER BY created")
    first, cust_orders = {}, {}
    for r in rs:
        k = _ckey(r)
        m = str(r['created'])[:7]
        first.setdefault(k, (m, (r.get('country') or 'KR')[:2]))
        cust_orders.setdefault(k, []).append((m, int(r.get('amount') or 0)))

    def mdiff(a, b):
        return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])
    coh = {}
    for k, (m0, cc) in first.items():
        c = coh.setdefault(m0, {'size': 0, 'act': [0] * months, 'rev': [0] * months})
        c['size'] += 1
        hit = set()
        for m, amt in cust_orders[k]:
            d = mdiff(m0, m)
            if 0 <= d < months:
                c['rev'][d] += amt
                if d not in hit:
                    c['act'][d] += 1
                    hit.add(d)
    out = []
    for m0 in sorted(coh)[-months:]:
        c = coh[m0]
        cum, ltv = 0, []
        for v in c['rev']:
            cum += v
            ltv.append(round(cum / c['size']) if c['size'] else 0)
        out.append({'cohort': m0, 'size': c['size'],
                    'retention': [round(a * 100.0 / c['size'], 1) if c['size'] else 0 for a in c['act']], 'ltv': ltv})
    rep = {}
    for k, (m0, cc) in first.items():
        g = 'KR' if cc == 'KR' else 'INTL'
        x = rep.setdefault(g, {'customers': 0, 'repeat': 0, 'revenue': 0})
        x['customers'] += 1
        x['repeat'] += 1 if len(cust_orders[k]) > 1 else 0
        x['revenue'] += sum(a for _, a in cust_orders[k])
    for g, x in rep.items():
        x['repeat_rate'] = round(x['repeat'] * 100.0 / x['customers'], 1) if x['customers'] else 0
        x['ltv'] = round(x['revenue'] / x['customers']) if x['customers'] else 0
    return {'cohorts': out, 'segments': rep}


@growth_router.get('/admin/api/growth/o2o')
def g_o2o(request: Request, frm: str = '', to: str = ''):
    _admin(request)
    f, t = _range(frm, to, 90)
    flush_events()
    scans = {}
    for r in _rows("SELECT props, COUNT(*) AS n FROM mp_events WHERE name='qr_scan' AND day>=? AND day<? GROUP BY props", (f, t)):
        code = (_jl(r.get('props'), {}) or {}).get('m') or '?'
        scans[code] = scans.get(code, 0) + int(r.get('n') or 0)
    qrs = _rows('SELECT code, label, active FROM mp_qr ORDER BY created')
    leads = _rows("SELECT source, channel, country, COUNT(*) AS n FROM mp_contacts WHERE created>=? AND created<? AND consent=1 "
                  "GROUP BY source, channel, country ORDER BY n DESC", (f, t))
    red = _rows("SELECT u.code, u.amount_off, u.status, o.amount, o.country FROM mp_coupon_uses u "
                "LEFT JOIN orders o ON o.order_id=u.order_id WHERE u.created>=? AND u.created<?", (f, t))
    used = [r for r in red if r.get('status') == 'USED']
    store_rev = 0
    store_orders = 0
    for r in _paid_orders(f, t):
        at = _jl(r.get('attr'), {}) or {}
        if _chan((at.get('lt') or {}).get('s'), (at.get('lt') or {}).get('m')) == 'store' or \
           _chan((at.get('ft') or {}).get('s'), (at.get('ft') or {}).get('m')) == 'store':
            store_rev += int(r.get('amount') or 0)
            store_orders += 1
    home = [r for r in used if str(r.get('code') or '').startswith('HOME-')]
    return {'qr': [{'code': q['code'], 'label': q['label'], 'active': q['active'], 'scans': scans.get(q['code'], 0),
                    'url': _site() + '/visit?qr=' + urllib.parse.quote(q['code'])} for q in qrs],
            'leads': leads, 'lead_total': sum(int(x.get('n') or 0) for x in leads),
            'coupon_used': len(used), 'coupon_off': sum(int(r.get('amount_off') or 0) for r in used),
            'home_orders': len(home), 'home_revenue': sum(int(r.get('amount') or 0) for r in home),
            'home_intl': sum(1 for r in home if (r.get('country') or 'KR') != 'KR'),
            'store_orders': store_orders, 'store_revenue': store_rev}


@growth_router.get('/admin/api/growth/status')
def g_status(request: Request):
    _admin(request)
    c = cfg()
    last = {}
    try:
        for r in _rows("SELECT channel, status, MAX(ts) AS ts, COUNT(*) AS n FROM mp_conv_log GROUP BY channel, status"):
            last.setdefault(r['channel'], []).append({'status': r['status'], 'n': r['n'], 'ts': r['ts']})
        mails = _rows("SELECT kind, status, COUNT(*) AS n, MAX(ts) AS ts FROM mp_mail_log GROUP BY kind, status")
    except Exception:
        mails = []
    return {'ga4': bool(_env('GA4_ID')), 'naver': bool(_env('NAVER_SA_ID')),
            'meta_pixel': bool(c['meta']), 'meta_capi': bool(c['meta'] and c['meta_capi']),
            'tiktok_pixel': bool(c['tt']), 'tiktok_api': bool(c['tt'] and c['tt_api']),
            'kakao_pixel': bool(c['kakao']), 'google_ads': bool(c['aw']), 'google_ads_purchase': bool(c['aw'] and c['aw_label']),
            'meta_spend_api': bool(_env('META_ADS_TOKEN') and _env('META_AD_ACCOUNT')),
            'tiktok_spend_api': bool(_env('TIKTOK_ADS_TOKEN') and _env('TIKTOK_ADVERTISER_ID')),
            'mail': ('resend' if _env('RESEND_API_KEY') else 'smtp' if mail_enabled() else ''),
            'conv_log': last, 'mail_log': mails, 'queue': len(_EVQ)}


@growth_router.post('/admin/api/growth/spend')
def g_spend(request: Request, body: dict = Body(...)):
    actor = _admin(request, 1)
    n = spend_upsert(body.get('rows') or [], 'manual')
    try:
        _av().audit(actor, '광고비 입력', '', '%d행' % n)
    except Exception:
        pass
    return {'ok': True, 'rows': n}


@growth_router.post('/admin/api/growth/spend/pull')
def g_spend_pull(request: Request):
    _admin(request, 1)
    return pull_ad_spend(30)


@growth_router.get('/admin/api/growth/spend')
def g_spend_list(request: Request, frm: str = '', to: str = ''):
    _admin(request)
    f, t = _range(frm, to)
    return {'rows': _rows('SELECT * FROM mp_ad_spend WHERE day>=? AND day<? ORDER BY day DESC, channel', (f, t))}


@growth_router.get('/admin/api/growth/coupons')
def g_coupons(request: Request):
    _admin(request)
    cps = _rows("SELECT * FROM mp_coupons WHERE contact_id IS NULL OR contact_id='' ORDER BY created DESC LIMIT 200")
    uses = {r['code']: r for r in _rows("SELECT code, COUNT(*) AS n, SUM(amount_off) AS off FROM mp_coupon_uses "
                                        "WHERE status='USED' GROUP BY code")}
    issued = _one("SELECT COUNT(*) AS n FROM mp_coupons WHERE contact_id IS NOT NULL AND contact_id<>''") or {}
    for c in cps:
        u = uses.get(c['code']) or {}
        c['used'] = int(u.get('n') or 0); c['off'] = int(u.get('off') or 0)
    return {'coupons': cps, 'personal_issued': int(issued.get('n') or 0)}


@growth_router.post('/admin/api/growth/coupons')
def g_coupon_save(request: Request, body: dict = Body(...)):
    actor = _admin(request, 1)
    code = _norm_code(body.get('code'))
    if len(code) < 4:
        raise HTTPException(400, '코드는 영문·숫자 4자 이상')
    kind = 'amt' if body.get('kind') == 'amt' else 'pct'
    val = int(body.get('value') or 0)
    if kind == 'pct' and not (1 <= val <= 90):
        raise HTTPException(400, '할인율은 1~90%')
    if kind == 'amt' and val < 100:
        raise HTTPException(400, '할인액을 확인해 주세요')
    scope = body.get('scope') if body.get('scope') in ('all', 'first', 'intl') else 'all'
    uses = body.get('uses_left')
    uses = int(uses) if str(uses or '').strip().lstrip('-').isdigit() else None
    _run('INSERT INTO mp_coupons(code,kind,value,min_sub,max_off,starts,ends,uses_left,scope,note,created,active) '
         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET kind=excluded.kind, value=excluded.value, '
         'min_sub=excluded.min_sub, max_off=excluded.max_off, starts=excluded.starts, ends=excluded.ends, '
         'uses_left=excluded.uses_left, scope=excluded.scope, note=excluded.note, active=excluded.active',
         (code, kind, val, int(body.get('min_sub') or 0), int(body.get('max_off') or 0),
          str(body.get('starts') or '')[:10], str(body.get('ends') or '')[:10], uses, scope,
          str(body.get('note') or '')[:120], _iso(), 1 if body.get('active', True) else 0))
    try:
        _av().audit(actor, '쿠폰 저장', code, '%s %s' % (kind, val))
    except Exception:
        pass
    return {'ok': True}


@growth_router.post('/admin/api/growth/qr')
def g_qr_save(request: Request, body: dict = Body(...)):
    _admin(request, 1)
    code = re.sub(r'[^a-z0-9-]', '', str(body.get('code') or '').lower())[:30]
    if len(code) < 2:
        raise HTTPException(400, 'QR 코드명은 영문 소문자·숫자 2자 이상')
    _run('INSERT INTO mp_qr(code,label,created,active) VALUES(?,?,?,1) ON CONFLICT(code) DO UPDATE SET label=excluded.label',
         (code, str(body.get('label') or code)[:80], _iso()))
    return {'ok': True, 'url': _site() + '/visit?qr=' + code}


@growth_router.get('/admin/api/growth/contacts.csv')
def g_contacts_csv(request: Request):
    _admin(request, 2)
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['created', 'email', 'channel', 'handle', 'country', 'lang', 'source', 'consent', 'unsub', 'coupon', 'customer_id'])
    for r in _rows('SELECT * FROM mp_contacts ORDER BY created DESC'):
        w.writerow([r.get(k) or '' for k in ('created', 'email', 'channel', 'handle', 'country', 'lang', 'source',
                                              'consent', 'unsub', 'coupon', 'customer_id')])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="mapdal_contacts.csv"'})


@growth_router.get('/admin/api/growth/fx')
def g_fx(request: Request):
    _admin(request)
    return {'rates': _rows('SELECT * FROM mp_fx ORDER BY cur')}


@growth_router.post('/admin/api/growth/fx')
def g_fx_save(request: Request, body: dict = Body(...)):
    _admin(request, 2)
    for cur, v in (body.get('rates') or {}).items():
        cur = re.sub(r'[^A-Z]', '', str(cur).upper())[:3]
        try:
            v = float(v)
        except Exception:
            continue
        if len(cur) == 3 and v > 0:
            _run('INSERT INTO mp_fx(cur,per_krw,updated) VALUES(?,?,?) ON CONFLICT(cur) DO UPDATE SET '
                 'per_krw=excluded.per_krw, updated=excluded.updated', (cur, v, _iso()))
    global _FX_CACHE
    _FX_CACHE = {'t': 0, 'v': {}}
    return {'ok': True}


_FX_CACHE = {'t': 0, 'v': {}}


def fx_rates():
    """표시 환율 (KRW 1원당 외화) — 5분 캐시. 결제는 항상 KRW."""
    if time.time() - _FX_CACHE['t'] < 300 and _FX_CACHE['v']:
        return _FX_CACHE['v']
    v = {}
    try:
        if ensure():
            v = {r['cur']: float(r['per_krw']) for r in _rows('SELECT cur, per_krw FROM mp_fx')}
    except Exception:
        v = {}
    _FX_CACHE.update({'t': time.time(), 'v': v})
    return v


@growth_router.get('/api/fx')
def api_fx():
    return JSONResponse({'base': 'KRW', 'rates': fx_rates()}, headers={'Cache-Control': 'public, max-age=300'})


# ═══════════════════════════ 그로스 대시보드 화면 ════════════════════════
@growth_router.get('/admin/growth', response_class=HTMLResponse)
def growth_page(request: Request):
    try:
        actor = _av().get_actor(request)
    except HTTPException:
        return RedirectResponse('/admin/dashboard', status_code=303)
    ensure()
    return HTMLResponse(_GROWTH_HTML.replace('__ROLE__', _e(actor.get('role') or '')),
                        headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})


_GROWTH_HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>MAPDAL — 그로스 엔진</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root{--ink:#141414;--red:#DC2B24;--amber:#B87400;--line:#E2E0D9;--paper:#F7F6F2;--steel:#5E5D57;--good:#0A7D38;--bad:#B3261E}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 "IBM Plex Sans KR",-apple-system,sans-serif}
header{background:var(--ink);color:#fff;padding:14px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;border-bottom:4px solid var(--red)}
header b{font-size:17px}header a{color:#fff;opacity:.8;font-size:13px}header .sp{flex:1}
header input,header select{font:inherit;padding:6px 8px;border:0}header button{font:700 13px inherit;background:var(--red);color:#fff;border:0;padding:8px 14px;cursor:pointer}
nav{display:flex;gap:2px;padding:0 20px;background:#fff;border-bottom:1px solid var(--line);overflow-x:auto}
nav button{font:600 13.5px inherit;background:none;border:0;border-bottom:3px solid transparent;padding:12px 14px;cursor:pointer;white-space:nowrap;color:var(--steel)}
nav button.on{border-color:var(--red);color:var(--ink)}
main{padding:20px;max-width:1280px;margin:0 auto}section{display:none}section.on{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:#fff;border:1px solid var(--line);padding:14px}.kpi small{display:block;color:var(--steel);font-size:12px}
.kpi b{font:700 22px "IBM Plex Mono",monospace;display:block;margin-top:2px}.kpi em{font-style:normal;font-size:11.5px;color:var(--steel)}
.panel{background:#fff;border:1px solid var(--line);padding:16px;margin-bottom:16px;overflow-x:auto}
.panel h3{margin:0 0 10px;font-size:15px}.panel p.h{margin:-4px 0 10px;color:var(--steel);font-size:12.5px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:11.5px;color:var(--steel);font-weight:600;background:#FAFAF7}th:first-child,td:first-child{text-align:left}
.good{color:var(--good);font-weight:700}.bad{color:var(--bad);font-weight:700}.mut{color:var(--steel)}
.bar{height:26px;background:var(--red);color:#fff;font:600 12px/26px "IBM Plex Mono",monospace;padding:0 8px;min-width:2px;white-space:nowrap}
.frow{display:grid;grid-template-columns:150px 1fr 70px;gap:10px;align-items:center;margin:6px 0;font-size:13px}
.hm td{text-align:center;font:500 12px "IBM Plex Mono",monospace}
textarea{width:100%;min-height:120px;font:12.5px "IBM Plex Mono",monospace;border:1px solid var(--line);padding:10px}
.btn{font:700 13px inherit;background:var(--ink);color:#fff;border:0;padding:9px 14px;cursor:pointer}.btn.red{background:var(--red)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.pill{display:inline-block;padding:2px 8px;font-size:11.5px;font-weight:700}.pill.on{background:#E3F4E9;color:var(--good)}.pill.off{background:#F3F2EE;color:var(--steel)}
.qr{display:inline-block;margin:8px 14px 8px 0;text-align:center;font-size:12px;vertical-align:top;width:170px}.qr div{background:#fff;padding:8px;border:1px solid var(--line)}
.f{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}.f input,.f select{font:inherit;padding:7px 8px;border:1px solid var(--line)}
svg text{font:11px "IBM Plex Mono",monospace;fill:var(--steel)}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;padding:10px 16px;display:none}
</style></head><body>
<header><b>MAPDAL 그로스 엔진</b><a href="/admin/dashboard">← 관리자 홈</a><span class="sp"></span>
<input type="date" id="f"><span>~</span><input type="date" id="t"><button id="go">조회</button></header>
<nav><button data-s="ov" class="on">성과·ROAS</button><button data-s="fn">퍼널·트래픽</button><button data-s="co">코호트·LTV</button>
<button data-s="o2o">O2O·매장</button><button data-s="sp">광고비</button><button data-s="cp">쿠폰</button><button data-s="st">연동 상태</button></nav>
<main>
<section id="ov" class="on"><div class="kpis" id="kpis"></div>
<div class="panel"><h3>채널별 성과</h3><p class="h">주문 기준 = 최근 유입 터치(last touch, 30일). ROAS = 매출 ÷ 광고비. CAC = 광고비 ÷ 신규 고객 수.</p><div id="chT"></div></div>
<div class="panel"><h3>캠페인별 성과</h3><p class="h">utm_campaign(또는 매장 QR 코드)과 광고비 원장의 캠페인명을 매칭합니다 — 광고 링크에 utm_campaign 을 캠페인명과 동일하게 넣어주세요.</p><div id="cmT"></div></div>
<div class="panel"><h3>국가별 매출</h3><div id="ctT"></div></div></section>
<section id="fn"><div class="panel"><h3>일별 세션 · 매출 · 광고비</h3><div id="dChart"></div></div>
<div class="grid2"><div class="panel"><h3>구매 퍼널 (세션 기준)</h3><div id="fnB"></div></div><div class="panel"><h3>언어별 세션</h3><div id="lgT"></div></div></div></section>
<section id="co"><div class="panel"><h3>월별 첫 구매 코호트 — 재구매율(%)</h3><p class="h">M0 = 첫 구매 달. 회원ID → 이메일 → 전화번호 순으로 고객을 묶어 비회원 재구매도 추적합니다.</p><div id="coT"></div></div>
<div class="panel"><h3>코호트 누적 LTV (고객 1인당 ₩)</h3><div id="ltT"></div></div><div class="panel"><h3>국내 vs 해외 고객</h3><div id="sgT"></div></div></section>
<section id="o2o"><div class="kpis" id="oK"></div>
<div class="panel"><h3>매장 QR</h3><p class="h">QR → /visit 랜딩에서 이메일·WhatsApp·LINE 동의 + 귀국 후 첫 주문 10% 쿠폰을 받습니다. 인쇄해서 1F 카운터·4F 계산대·쇼핑백에 부착하세요.</p>
<div id="qrL"></div><div class="f"><input id="qc" placeholder="코드 (예: seongsu-2f)"><input id="ql" placeholder="위치 설명"><button class="btn" id="qAdd">QR 추가</button></div></div>
<div class="panel"><h3>연락처 수집 (소스 · 채널 · 국가)</h3><div id="ldT"></div><p><a href="/admin/api/growth/contacts.csv">연락처 CSV 내려받기</a> (매니저 이상)</p></div></section>
<section id="sp"><div class="panel"><h3>광고비 입력</h3><p class="h">CSV 붙여넣기: <code>날짜,채널,캠페인,광고비,노출,클릭</code> — 채널은 meta · google · tiktok · naver · kakao. Meta/TikTok 은 API 키 설정 시 6시간마다 자동 수집됩니다.</p>
<textarea id="spC" placeholder="2026-10-01,meta,seongsu_tourist_jp,150000,42000,610"></textarea>
<div class="f"><button class="btn red" id="spS">저장</button><button class="btn" id="spP">API 즉시 수집</button></div></div><div class="panel"><h3>최근 광고비 원장</h3><div id="spT"></div></div></section>
<section id="cp"><div class="panel"><h3>쿠폰</h3><p class="h">scope: all=전체 · first=첫 주문 전용 · intl=해외배송 전용. 개인 발급 쿠폰(HOME-·BACK-)은 자동 생성됩니다.</p><div id="cpT"></div>
<div class="f"><input id="cC" placeholder="코드" size="12"><select id="cK"><option value="pct">%</option><option value="amt">원</option></select><input id="cV" placeholder="값" size="6">
<input id="cM" placeholder="최소주문" size="8"><input id="cX" placeholder="최대할인" size="8"><select id="cS"><option>all</option><option>first</option><option>intl</option></select>
<input id="cE" type="date" title="종료일"><input id="cU" placeholder="사용횟수(빈칸=무제한)" size="14"><input id="cN" placeholder="메모"><button class="btn red" id="cSave">저장</button></div></div></section>
<section id="st"><div class="panel"><h3>연동 상태</h3><p class="h">Render 환경변수로 켭니다. 설정 즉시(재배포 후) 전 페이지에 반영됩니다.</p><div id="stT"></div></div>
<div class="panel"><h3>표시 환율 (KRW 1원당)</h3><p class="h">해외 고객에게 보여주는 참고 금액용. 결제는 항상 원화(KRW)로 진행됩니다.</p><div id="fxT"></div><button class="btn" id="fxS">환율 저장</button></div></section>
</main><div id="toast"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
const $=s=>document.querySelector(s),ROLE='__ROLE__';
const won=n=>n==null?'—':'₩'+Math.round(n).toLocaleString('ko-KR'),num=n=>n==null?'—':Number(n).toLocaleString('ko-KR');
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2600)}
async function api(p,o){const r=await fetch(p,o);if(!r.ok){let m='오류';try{m=(await r.json()).detail||m}catch(e){}throw new Error(m)}return r.json()}
const CH={app:'홈 화면 앱(재방문)',meta:'Meta 광고',google:'Google 광고',tiktok:'TikTok 광고',naver:'네이버 광고',kakao:'카카오 광고',store:'매장 QR (O2O)',crm:'이메일·CRM',search:'자연검색',social:'소셜(자연)',direct:'직접 유입',referral:'추천 사이트',other:'기타'};
const td=new Date(),fd=new Date(Date.now()-29*864e5),iso=d=>d.toISOString().slice(0,10);$('#f').value=iso(fd);$('#t').value=iso(td);
const q=()=>'?frm='+$('#f').value+'&to='+$('#t').value;
function tbl(h,rows){return'<table><tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+(rows.length?rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join(''):'<tr><td colspan="'+h.length+'" class="mut">데이터 없음</td></tr>')+'</table>'}
const roas=v=>v==null?'—':'<span class="'+(v>=300?'good':v<100?'bad':'')+'">'+v+'%</span>';
async function ov(){const d=await api('/admin/api/growth/overview'+q()),k=d.kpi;
 $('#kpis').innerHTML=[['매출',won(k.revenue),k.orders+'건 · 객단가 '+won(k.aov)],['광고비',won(k.spend),'MER '+(k.mer==null?'—':k.mer+'%')],
 ['ROAS',k.roas==null?'—':k.roas+'%','매출 ÷ 광고비'],['세션',num(k.sessions),'방문자 '+num(k.visitors)],['구매 전환율',k.cvr==null?'—':k.cvr+'%','주문 ÷ 세션'],
 ['신규 고객 매출',won(k.new_rev),'재구매 '+won(k.repeat_rev)],['해외 매출',won(k.intl_rev),k.revenue?Math.round(k.intl_rev*100/k.revenue)+'%':'—']]
 .map(x=>'<div class="kpi"><small>'+x[0]+'</small><b>'+x[1]+'</b><em>'+x[2]+'</em></div>').join('');
 $('#chT').innerHTML=tbl(['채널','세션','주문','매출','신규','광고비','클릭','전환율','ROAS','CAC'],d.channels.map(c=>[esc(CH[c.channel]||c.channel),num(c.sessions),num(c.orders),won(c.revenue),num(c.new),c.spend?won(c.spend):'—',c.clicks?num(c.clicks):'—',c.cvr==null?'—':c.cvr+'%',roas(c.roas),c.cac==null?'—':won(c.cac)]));
 $('#cmT').innerHTML=tbl(['채널','캠페인','주문','매출','광고비','ROAS'],d.campaigns.map(c=>[esc(CH[c.channel]||c.channel),esc(c.campaign),num(c.orders),won(c.revenue),c.spend?won(c.spend):'—',roas(c.roas)]));
 $('#ctT').innerHTML=tbl(['국가','주문','매출'],d.countries.map(c=>[esc(c.country),num(c.orders),won(c.revenue)]))}
async function fn(){const d=await api('/admin/api/growth/funnel'+q());const N={page_view:'방문',view_item:'상품 조회',add_to_cart:'장바구니',begin_checkout:'주문서 진입',add_payment_info:'결제 시도',purchase:'구매 완료'};
 const mx=Math.max(1,...d.funnel.map(x=>x.all));
 $('#fnB').innerHTML=d.funnel.map((x,i)=>{const p=i?d.funnel[i-1].all:0,r=p?Math.round(x.all*1000/p)/10+'%':'';return'<div class="frow"><span>'+N[x.step]+'<br><small class="mut">M '+num(x.mobile)+' · D '+num(x.desktop)+'</small></span><div><div class="bar" style="width:'+Math.max(1,x.all*100/mx)+'%">'+num(x.all)+'</div></div><span class="mut">'+r+'</span></div>'}).join('');
 $('#lgT').innerHTML=tbl(['언어','세션'],d.langs.map(x=>[esc(x.lang||'?'),num(x.n)]));
 const D=d.daily,W=Math.max(600,D.length*26),H=220,mS=Math.max(1,...D.map(x=>x.sessions)),mR=Math.max(1,...D.map(x=>Math.max(x.revenue,x.spend)));
 let s='<svg width="'+W+'" height="'+(H+30)+'" role="img" aria-label="일별 추이">';D.forEach((x,i)=>{const X=20+i*(W-40)/Math.max(1,D.length);
  s+='<rect x="'+X+'" y="'+(H-x.revenue*H/mR)+'" width="9" height="'+(x.revenue*H/mR)+'" fill="#DC2B24"><title>'+x.day+' 매출 '+won(x.revenue)+'</title></rect>';
  s+='<rect x="'+(X+10)+'" y="'+(H-x.spend*H/mR)+'" width="9" height="'+(x.spend*H/mR)+'" fill="#B87400"><title>'+x.day+' 광고비 '+won(x.spend)+'</title></rect>';
  s+='<circle cx="'+(X+9)+'" cy="'+(H-x.sessions*H/mS)+'" r="3" fill="#141414"><title>'+x.day+' 세션 '+x.sessions+'</title></circle>';
  if(i%Math.ceil(D.length/10)===0)s+='<text x="'+X+'" y="'+(H+16)+'">'+x.day.slice(5)+'</text>'});
 $('#dChart').innerHTML=(D.length?s+'</svg>':'<p class="mut">데이터 없음</p>')+'<p class="mut"><span style="color:#DC2B24">■</span> 매출 <span style="color:#B87400">■</span> 광고비 ● 세션</p>'}
async function co(){const d=await api('/admin/api/growth/cohorts'),n=d.cohorts.length?d.cohorts[0].retention.length:0,H=['코호트','고객']; for(let i=0;i<n;i++)H.push('M'+i);
 const cell=v=>'<span style="display:block;background:rgba(232,51,42,'+Math.min(.85,v/60)+');color:'+(v>35?'#fff':'#141414')+';padding:2px">'+v+'</span>';
 $('#coT').innerHTML='<table class="hm">'+tbl(H,d.cohorts.map(c=>[c.cohort,c.size].concat(c.retention.map((v,i)=>i===0?'100':cell(v))))).slice(7);
 $('#ltT').innerHTML=tbl(H,d.cohorts.map(c=>[c.cohort,c.size].concat(c.ltv.map(won))));
 $('#sgT').innerHTML=tbl(['구분','고객 수','재구매 고객','재구매율','고객당 LTV'],Object.entries(d.segments).map(([g,x])=>[g==='KR'?'국내':'해외',num(x.customers),num(x.repeat),x.repeat_rate+'%',won(x.ltv)]))}
async function o2o(){const d=await api('/admin/api/growth/o2o'+q());
 $('#oK').innerHTML=[['QR 스캔',num(d.qr.reduce((a,x)=>a+x.scans,0))],['연락처 확보',num(d.lead_total)],['웰컴쿠폰 사용 주문',num(d.home_orders)+'건'],['웰컴쿠폰 매출',won(d.home_revenue)],['그중 해외 주문',num(d.home_intl)+'건'],['매장 유입 매출(QR 터치)',won(d.store_revenue)]]
 .map(x=>'<div class="kpi"><small>'+x[0]+'</small><b>'+x[1]+'</b></div>').join('');
 $('#qrL').innerHTML=d.qr.map((x,i)=>'<div class="qr"><div id="qr'+i+'"></div><b>'+esc(x.code)+'</b><br>'+esc(x.label)+'<br><span class="mut">스캔 '+x.scans+'</span><br><a href="'+esc(x.url)+'" target="_blank">링크</a></div>').join('');
 d.qr.forEach((x,i)=>{try{new QRCode(document.getElementById('qr'+i),{text:x.url,width:150,height:150,correctLevel:QRCode.CorrectLevel.M})}catch(e){}});
 $('#ldT').innerHTML=tbl(['소스','채널','국가','건수'],d.leads.map(x=>[esc(x.source),esc(x.channel),esc(x.country||'?'),num(x.n)]))}
async function sp(){const d=await api('/admin/api/growth/spend'+q());$('#spT').innerHTML=tbl(['날짜','채널','캠페인','광고비','노출','클릭','출처'],d.rows.slice(0,300).map(r=>[r.day,esc(r.channel),esc(r.campaign),won(r.spend),num(r.impressions),num(r.clicks),esc(r.src)]))}
async function cp(){const d=await api('/admin/api/growth/coupons');$('#cpT').innerHTML=tbl(['코드','할인','최소주문','최대','범위','기간','잔여','사용','할인액','상태','메모'],
 d.coupons.map(c=>[esc(c.code),c.kind==='pct'?c.value+'%':won(c.value),won(c.min_sub),c.max_off?won(c.max_off):'—',esc(c.scope),esc((c.starts||'')+'~'+(c.ends||'')),c.uses_left==null?'∞':c.uses_left,num(c.used),won(c.off),c.active?'<span class="pill on">ON</span>':'<span class="pill off">OFF</span>',esc(c.note)]))+'<p class="mut">개인 발급(매장 웰컴·윈백) 누적 '+num(d.personal_issued)+'건</p>'}
async function st(){const d=await api('/admin/api/growth/status');const L=[['GA4','ga4','GA4_ID'],['네이버 애널리틱스','naver','NAVER_SA_ID'],['Meta 픽셀','meta_pixel','META_PIXEL_ID'],['Meta 전환 API(CAPI)','meta_capi','META_CAPI_TOKEN'],['TikTok 픽셀','tiktok_pixel','TIKTOK_PIXEL_ID'],['TikTok Events API','tiktok_api','TIKTOK_EVENTS_TOKEN'],['카카오 픽셀','kakao_pixel','KAKAO_PIXEL_ID'],['Google Ads','google_ads','GOOGLE_ADS_ID'],['Google Ads 구매 전환','google_ads_purchase','GOOGLE_ADS_PURCHASE_LABEL'],['Meta 광고비 자동수집','meta_spend_api','META_ADS_TOKEN · META_AD_ACCOUNT'],['TikTok 광고비 자동수집','tiktok_spend_api','TIKTOK_ADS_TOKEN · TIKTOK_ADVERTISER_ID'],['이메일 발송','mail','RESEND_API_KEY 또는 SMTP_HOST·SMTP_USER·SMTP_PASS · MAIL_FROM']];
 $('#stT').innerHTML=tbl(['연동','상태','환경변수'],L.map(x=>[x[0],d[x[1]]?'<span class="pill on">ON'+(typeof d[x[1]]==='string'?' · '+esc(d[x[1]]):'')+'</span>':'<span class="pill off">OFF</span>','<code>'+x[2]+'</code>']))+
 '<h3 style="margin-top:16px">서버 전환 전송</h3>'+tbl(['채널','상태','건수','최근'],Object.entries(d.conv_log||{}).flatMap(([c,a])=>a.map(x=>[c,x.status,x.n,x.ts])))+
 '<h3 style="margin-top:16px">메일 발송</h3>'+tbl(['종류','상태','건수','최근'],(d.mail_log||[]).map(x=>[esc(x.kind),x.status,x.n,x.ts]));
 const fx=await api('/admin/api/growth/fx');$('#fxT').innerHTML='<div class="f">'+fx.rates.map(r=>'<label>'+r.cur+' <input data-c="'+r.cur+'" value="'+r.per_krw+'" size="10"></label>').join('')+'</div>'}
const L={ov,fn,co,o2o,sp,cp,st};let cur='ov';
function load(){L[cur]().catch(e=>toast(e.message))}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.s));cur=b.dataset.s;load()});
$('#go').onclick=load;
$('#spS').onclick=async()=>{const rows=$('#spC').value.split('\n').map(l=>l.split(/[,\t]/).map(x=>x.trim())).filter(a=>a.length>=4&&/^\d{4}-\d{2}-\d{2}$/.test(a[0])).map(a=>({day:a[0],channel:a[1],campaign:a[2],spend:a[3].replace(/[^0-9.]/g,''),impressions:(a[4]||'0').replace(/\D/g,''),clicks:(a[5]||'0').replace(/\D/g,'')}));
 if(!rows.length)return toast('형식을 확인해 주세요');try{const r=await api('/admin/api/growth/spend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows})});toast(r.rows+'행 저장');$('#spC').value='';sp()}catch(e){toast(e.message)}};
$('#spP').onclick=async()=>{try{const r=await api('/admin/api/growth/spend/pull',{method:'POST'});toast(JSON.stringify(r)||'API 미설정');sp()}catch(e){toast(e.message)}};
$('#qAdd').onclick=async()=>{try{await api('/admin/api/growth/qr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('#qc').value,label:$('#ql').value})});o2o()}catch(e){toast(e.message)}};
$('#cSave').onclick=async()=>{try{await api('/admin/api/growth/coupons',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('#cC').value,kind:$('#cK').value,value:$('#cV').value,min_sub:$('#cM').value,max_off:$('#cX').value,scope:$('#cS').value,ends:$('#cE').value,uses_left:$('#cU').value,note:$('#cN').value,active:true})});toast('저장됨');cp()}catch(e){toast(e.message)}};
$('#fxS').onclick=async()=>{const r={};document.querySelectorAll('#fxT input').forEach(i=>r[i.dataset.c]=i.value);try{await api('/admin/api/growth/fx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rates:r})});toast('저장됨')}catch(e){toast(e.message)}};
load();
</script></body></html>'''


# ═══════════════════════════ /visit — 매장 QR 랜딩 ═══════════════════════
#   매장(1F 카운터·4F 계산대·쇼핑백·영수증) QR → 이 페이지. 목적은 단 하나:
#   '귀국 후에도 다시 살 수 있는 연결고리' = 동의 기반 연락처 + 첫 온라인 주문 쿠폰.
@growth_router.get('/visit', response_class=HTMLResponse)
def visit_page(request: Request, qr: str = ''):
    code = re.sub(r'[^a-z0-9-]', '', (qr or '').lower())[:30] or 'store'
    html = _VISIT_HTML.replace('__QR__', code).replace('__SITE__', _e(_site()))
    html = head_apply(html)
    try:
        add = _av()._analytics_snippet()
    except Exception:
        add = ''
    add += _body_js()
    html = html.replace('</body>', add + '</body>', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})


_VISIT_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>MAPDAL SEOUL — Welcome gift</title>
<meta name="description" content="Thanks for visiting MAPDAL SEOUL in Seongsu. Get 10% off your first online order and keep shopping K-pop albums, merch and K-culture goods from home.">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#141414">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root{--ink:#141414;--red:#DC2B24;--amber:#FFB000;--paper:#F7F6F2;--line:#E2E0D9;--steel:#5E5D57}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--ink);color:#fff;font:15px/1.6 "IBM Plex Sans KR",-apple-system,"Hiragino Sans","PingFang SC",sans-serif;min-height:100vh}
.wrap{max-width:480px;margin:0 auto;padding:22px 16px calc(28px + env(safe-area-inset-bottom))}
.top{display:flex;justify-content:space-between;align-items:center}
.logo{font-family:"Black Han Sans",sans-serif;font-size:24px;letter-spacing:.01em}.logo em{font-style:normal;color:var(--red)}
.lang{display:flex;gap:4px}.lang button{font:600 12px "IBM Plex Mono",monospace;background:transparent;color:#bbb;border:1px solid #444;padding:6px 8px;cursor:pointer;min-width:40px;min-height:36px;white-space:nowrap}
.lang button.on{background:#fff;color:var(--ink);border-color:#fff}
.tick{font:500 11.5px "IBM Plex Mono",monospace;color:var(--amber);letter-spacing:.12em;margin:26px 0 8px}
h1{font-family:"Black Han Sans",sans-serif;font-weight:400;font-size:38px;line-height:1.12;margin:0 0 12px}
h1 b{color:var(--red);font-weight:400}
.lead{color:#d6d6d6;margin:0 0 20px}
.perks{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 22px}
.perks div{border:1px solid #333;padding:10px 8px;font-size:12px;line-height:1.4;color:#ddd}.perks b{display:block;color:#fff;font-size:13px;margin-bottom:2px}
.card{background:#fff;color:var(--ink);padding:18px}
label.t{display:block;font-weight:700;font-size:13px;margin:12px 0 6px}
input,select{width:100%;font:inherit;font-size:16px;padding:12px;border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:0}
input:focus,select:focus{outline:2px solid var(--red);outline-offset:-1px}
.seg{display:flex;flex-wrap:wrap;gap:6px}.seg button{flex:1 1 auto;font:600 13px inherit;padding:10px 8px;border:1px solid var(--line);background:#fff;cursor:pointer;min-height:42px}
.seg button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.chk{display:flex;gap:10px;align-items:flex-start;font-size:12.5px;color:var(--steel);margin:14px 0 4px}.chk input{width:20px;height:20px;flex:0 0 20px;margin-top:2px}
.go{width:100%;font:700 16px inherit;background:var(--red);color:#fff;border:0;padding:16px;margin-top:12px;cursor:pointer;min-height:52px}
.go:disabled{opacity:.6}.err{color:var(--red);font-size:13px;min-height:18px;margin-top:8px}
.done{display:none;text-align:center}.code{font:700 30px "IBM Plex Mono",monospace;letter-spacing:.1em;color:var(--red);border:2px dashed var(--red);padding:14px;margin:12px 0;user-select:all}
.small{font-size:12px;color:var(--steel)}.cta{display:block;background:var(--ink);color:#fff;text-decoration:none;font-weight:700;padding:14px;margin-top:10px}
.cta.alt{background:#fff;color:var(--ink);border:1px solid var(--ink)}
.foot{margin-top:22px;font-size:12px;color:#999;line-height:1.7}.foot a{color:#ccc}
</style></head><body>
<div class="wrap">
 <div class="top"><div class="logo">MAPDAL<em>SEOUL</em></div>
  <div class="lang" role="group" aria-label="Language"><button data-l="en">EN</button><button data-l="ja">JA</button><button data-l="zh">中文</button><button data-l="ko">KO</button></div></div>
 <div class="tick" data-i="tick"></div>
 <h1 data-i="h1"></h1>
 <p class="lead" data-i="lead"></p>
 <div class="perks"><div data-i="p1"></div><div data-i="p2"></div><div data-i="p3"></div></div>
 <div class="card">
  <form id="f" novalidate>
   <label class="t" data-i="how"></label>
   <div class="seg" id="seg"><button type="button" data-c="email" class="on">Email</button><button type="button" data-c="whatsapp">WhatsApp</button><button type="button" data-c="line">LINE</button><button type="button" data-c="wechat">WeChat</button><button type="button" data-c="instagram">Instagram</button></div>
   <label class="t" for="v" id="vl" data-i="email"></label>
   <input id="v" name="v" type="email" autocomplete="email" inputmode="email" required>
   <div id="ew" style="display:none"><label class="t" for="em" data-i="email_opt"></label><input id="em" type="email" autocomplete="email" inputmode="email"></div>
   <label class="t" for="ct" data-i="country"></label>
   <select id="ct" autocomplete="country"></select>
   <label class="chk"><input type="checkbox" id="ok"><span data-i="consent"></span></label>
   <button class="go" id="go" type="submit" data-i="submit"></button>
   <div class="err" id="err" role="alert"></div>
  </form>
  <div class="done" id="done" aria-live="polite">
   <div style="font-size:13px;font-weight:700" data-i="done_h"></div>
   <div class="code" id="code"></div>
   <div class="small" data-i="done_p"></div>
   <a class="cta" id="shop" href="/shop?utm_source=store_qr&utm_medium=offline" data-i="shop"></a>
   <a class="cta alt" href="/mapdal-seoul" data-i="store"></a>
  </div>
 </div>
 <div class="foot"><span data-i="addr"></span><br><a href="/privacy" data-i="privacy"></a></div>
</div>
<script>
(function(){
var QR='__QR__';
var T={
en:{tick:'SEONGSU FLAGSHIP · THANK YOU FOR VISITING',h1:'Take Seoul<br><b>home with you.</b>',lead:'Loved what you found in Seongsu? Keep shopping the same K-pop albums, merch and Seoul-made goods online, and get <b>10% off your first online order</b>.',
 p1:'<b>Ships worldwide</b>Albums & merch to 50+ countries',p2:'<b>Official albums</b>Counted toward the charts',p3:'<b>Drop alerts</b>New releases, first',how:'Where should we send your code?',email:'Email',email_opt:'Email (optional, for order updates)',wa:'WhatsApp number (with country code)',line:'LINE ID',wechat:'WeChat ID',ig:'Instagram @handle',
 country:'Where are you from?',consent:'I agree to receive MAPDAL SEOUL offers and new-drop news. You can unsubscribe at any time.',submit:'Get my 10% code',
 done_h:'Your welcome code',done_p:'Use it at checkout on mapdal.kr within 180 days (min. ₩30,000, up to ₩30,000 off). Screenshot this screen to keep it.',shop:'Shop online now',store:'Explore the 4-floor store',
 addr:'MAPDAL SEOUL · 5 Seongsui-ro 16-gil, Seongdong-gu, Seoul · Open daily 11:00–21:00',privacy:'Privacy policy',e_need:'Please enter your contact.',e_mail:'Please check your email address.',e_ok:'Please tick the consent box.',e_net:'Something went wrong. Please try again.'},
ja:{tick:'聖水フラッグシップ · ご来店ありがとうございます',h1:'ソウルを、<br><b>おうちでも。</b>',lead:'聖水で見つけたK-POPアルバムやグッズを、帰国後もオンラインで。<b>初回オンライン注文が10%オフ</b>になるクーポンをお送りします。',
 p1:'<b>海外発送</b>アルバム・グッズを50か国以上へ',p2:'<b>公式アルバム</b>チャートに反映',p3:'<b>新作通知</b>いち早くお届け',how:'クーポンの受け取り方法',email:'メールアドレス',email_opt:'メールアドレス（任意・注文のご案内用）',wa:'WhatsApp番号（国番号から）',line:'LINE ID',wechat:'WeChat ID',ig:'Instagram @アカウント',
 country:'お住まいの国',consent:'MAPDAL SEOULからのお得な情報・新作のお知らせを受け取ることに同意します。いつでも配信停止できます。',submit:'10%クーポンを受け取る',
 done_h:'ウェルカムクーポン',done_p:'mapdal.kr のお支払い画面で180日以内にご利用ください（₩30,000以上・最大₩30,000割引）。この画面をスクリーンショットしてください。',shop:'オンラインストアへ',store:'4フロアの店舗を見る',
 addr:'MAPDAL SEOUL · ソウル市城東区聖水二路16ギル5 · 毎日11:00–21:00',privacy:'プライバシーポリシー',e_need:'連絡先を入力してください。',e_mail:'メールアドレスをご確認ください。',e_ok:'同意にチェックしてください。',e_net:'エラーが発生しました。もう一度お試しください。'},
zh:{tick:'圣水旗舰店 · 感谢光临',h1:'把首尔<br><b>带回家。</b>',lead:'在圣水看中的 K-POP 专辑和周边，回国后也能在线购买。送您<b>首次线上订单 10% 优惠</b>。',
 p1:'<b>全球配送</b>专辑与周边发往50多个国家',p2:'<b>官方专辑</b>计入榜单',p3:'<b>新品提醒</b>第一时间通知',how:'优惠码发送到哪里？',email:'电子邮箱',email_opt:'电子邮箱（选填，用于订单通知）',wa:'WhatsApp 号码（含国家代码）',line:'LINE ID',wechat:'微信号',ig:'Instagram 账号',
 country:'您来自哪里？',consent:'我同意接收 MAPDAL SEOUL 的优惠和新品资讯，可随时退订。',submit:'领取 10% 优惠码',
 done_h:'您的欢迎优惠码',done_p:'请在180天内于 mapdal.kr 结账时使用（满₩30,000，最高减₩30,000）。建议截图保存。',shop:'立即线上购物',store:'了解四层旗舰店',
 addr:'MAPDAL SEOUL · 首尔城东区圣水二路16街5 · 每天 11:00–21:00',privacy:'隐私政策',e_need:'请输入联系方式。',e_mail:'请检查邮箱地址。',e_ok:'请勾选同意。',e_net:'出错了，请重试。'},
ko:{tick:'성수 플래그십 · 방문해 주셔서 감사합니다',h1:'성수의 맵달을,<br><b>집에서도.</b>',lead:'매장에서 본 앨범·굿즈를 온라인에서 그대로. <b>첫 온라인 주문 10% 쿠폰</b>을 드려요.',
 p1:'<b>전국·해외 배송</b>앨범·굿즈 50여 개국',p2:'<b>공식 앨범</b>차트 반영',p3:'<b>드롭 알림</b>신상 가장 먼저',how:'쿠폰을 어디로 보내드릴까요?',email:'이메일',email_opt:'이메일 (선택 · 주문 안내용)',wa:'WhatsApp 번호 (국가번호 포함)',line:'LINE ID',wechat:'WeChat ID',ig:'인스타그램 @계정',
 country:'국가',consent:'맵달SEOUL의 혜택·신상 소식 수신에 동의합니다. 언제든 수신거부할 수 있습니다.',submit:'10% 쿠폰 받기',
 done_h:'웰컴 쿠폰 코드',done_p:'mapdal.kr 결제 단계에서 180일 이내 사용 (3만원 이상 · 최대 3만원 할인). 화면을 캡처해 두세요.',shop:'온라인 스토어 바로가기',store:'4개 층 매장 둘러보기',
 addr:'맵달SEOUL · 서울 성동구 성수이로16길 5 · 매일 11:00–21:00',privacy:'개인정보처리방침',e_need:'연락처를 입력해 주세요.',e_mail:'이메일 주소를 확인해 주세요.',e_ok:'수신 동의에 체크해 주세요.',e_net:'오류가 발생했습니다. 다시 시도해 주세요.'}};
var C=[['US','United States'],['JP','Japan 日本'],['CN','China 中国'],['TW','Taiwan 台灣'],['HK','Hong Kong 香港'],['SG','Singapore'],['TH','Thailand'],['VN','Vietnam'],['PH','Philippines'],['MY','Malaysia'],['ID','Indonesia'],['AU','Australia'],['CA','Canada'],['GB','United Kingdom'],['FR','France'],['DE','Germany'],['ES','Spain'],['IT','Italy'],['NL','Netherlands'],['MX','Mexico'],['BR','Brazil'],['IN','India'],['AE','UAE'],['KR','Korea 한국'],['ZZ','Other']];
var nav=(navigator.language||'en').toLowerCase(),L=(/^ja/.test(nav)?'ja':/^zh/.test(nav)?'zh':/^ko/.test(nav)?'ko':'en'),CH='email';
var tz='';try{tz=Intl.DateTimeFormat().resolvedOptions().timeZone||''}catch(e){}
var guess={'Asia/Tokyo':'JP','Asia/Shanghai':'CN','Asia/Taipei':'TW','Asia/Hong_Kong':'HK','Asia/Singapore':'SG','Asia/Bangkok':'TH','Asia/Ho_Chi_Minh':'VN','Asia/Manila':'PH','Asia/Kuala_Lumpur':'MY','Asia/Jakarta':'ID','Australia/Sydney':'AU','Europe/London':'GB','Europe/Paris':'FR','Europe/Berlin':'DE','Asia/Seoul':'KR'}[tz]||(/^America\//.test(tz)?'US':'');
var sel=document.getElementById('ct');sel.innerHTML='<option value=""></option>'+C.map(function(c){return'<option value="'+c[0]+'"'+(c[0]===guess?' selected':'')+'>'+c[1]+'</option>'}).join('');
function paint(){var t=T[L];document.documentElement.lang=L;document.querySelectorAll('[data-i]').forEach(function(el){var k=el.getAttribute('data-i');if(t[k]!=null)el.innerHTML=t[k]});
 document.querySelectorAll('.lang button').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-l')===L)});setCh(CH,true)}
function setCh(c,keep){CH=c;var t=T[L],v=document.getElementById('v'),lab=document.getElementById('vl');
 document.querySelectorAll('#seg button').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-c')===c)});
 lab.textContent=c==='email'?t.email:c==='whatsapp'?t.wa:c==='line'?t.line:c==='wechat'?t.wechat:t.ig;
 v.type=c==='email'?'email':c==='whatsapp'?'tel':'text';v.setAttribute('inputmode',c==='email'?'email':c==='whatsapp'?'tel':'text');v.setAttribute('autocomplete',c==='email'?'email':c==='whatsapp'?'tel':'off');
 document.getElementById('ew').style.display=c==='email'?'none':'block';if(!keep)v.value=''}
document.querySelectorAll('.lang button').forEach(function(b){b.onclick=function(){L=b.getAttribute('data-l');paint();try{window.mpTrack&&mpTrack('lang_switch',{method:L})}catch(e){}}});
document.querySelectorAll('#seg button').forEach(function(b){b.onclick=function(){setCh(b.getAttribute('data-c'))}});
document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();var t=T[L],err=document.getElementById('err'),v=document.getElementById('v').value.trim(),em=CH==='email'?v:document.getElementById('em').value.trim();
 err.textContent='';if(!v)return err.textContent=t.e_need;if(em&&!/^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$/.test(em))return err.textContent=t.e_mail;if(!document.getElementById('ok').checked)return err.textContent=t.e_ok;
 var btn=document.getElementById('go');btn.disabled=true;
 fetch('/api/contacts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em,channel:CH,handle:CH==='email'?'':v,country:sel.value,lang:L,source:'qr:'+QR,consent:true})})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||t.e_net);return d})})
 .then(function(d){document.getElementById('code').textContent=d.coupon||'';document.getElementById('f').style.display='none';document.getElementById('done').style.display='block';
  try{window.mpTrack&&mpTrack('generate_lead',{method:CH})}catch(e){}
  document.getElementById('shop').href='/shop?utm_source=store_qr&utm_medium=offline&utm_campaign='+encodeURIComponent(QR)})
 .catch(function(x){err.textContent=x.message||t.e_net;btn.disabled=false})});
try{document.cookie='mp_lang='+L+';path=/;max-age=31536000;samesite=lax'}catch(e){}
paint();
})();
</script></body></html>'''


# ═══════════════════════════ 서빙 파이프라인 진입점 ══════════════════════
def html_apply(html, path=''):
    """admin_v2._inject_auth 끝단에서 호출 — 신뢰 정합화 + head 스크립트 (멱등)."""
    try:
        html = trust_apply(html, path)
        html = copy_apply(html, path)
        html = o2o_apply(html, path)
        if not mail_enabled() and '주문 확인 메일을 보내드렸습니다. ' in html:
            # 메일 미연동 상태에서 '메일 보냈다'는 거짓 안내를 하지 않는다.
            html = html.replace('주문 확인 메일을 보내드렸습니다. ', '')
        html = head_apply(html)
    except Exception:
        pass
    return html


def startup():
    """app 기동 시 1회 — 테이블 준비 · 스케줄러 · 이벤트 플러셔."""
    def _go():
        for _ in range(30):
            try:
                if _app().DB_READY and ensure():
                    break
            except Exception:
                pass
            time.sleep(5)
        start_scheduler()
        _ensure_flusher()
    threading.Thread(target=_go, daemon=True).start()


# ═══════════════════════════ 다국어 사전 (브라우저 런타임용) ══════════════
@growth_router.get('/i18n/{lang}.json')
def i18n_dict(lang: str):
    try:
        import i18n
        if lang not in i18n.LANGS:
            raise HTTPException(404)
        return JSONResponse(i18n.load(lang), headers={'Cache-Control': 'public, max-age=86400'})
    except HTTPException:
        raise
    except Exception:
        return JSONResponse({}, headers={'Cache-Control': 'no-cache'})


# ═══════════════════════════ 배송 문구 정합화 (DDP 과장 제거) ═════════════
#   종전 문구는 '전 세계 DDP·추가 결제 없음'을 약속했지만 실제로는 해외 주문이 막혀 있었다.
#   해외 체크아웃(/checkout-global) 오픈에 맞춰 실제 정책과 일치시키고 진입 링크를 붙인다.
_FOOD_PAGE = re.compile(r'^/(product-bowl-|product-kimbap-|product-tteokbokki)')
_COPY_COMMON = (
    ('<td>전 세계 배송 가능 · 관세·세금 선지불(DDP)</td>',
     '<td>전 세계 배송 가능 · 결제 시 배송비 확정 · 관세는 수령 국가 기준</td>'),
    ('<td>식품 수입 규제에 따라 국가별 상이 · 체크아웃 자동 확인 · 관세 선지불(DDP)</td>',
     '<td>현재 국내 배송 전용 (냉동·냉장 식품 해외 배송 준비 중)</td>'),
    ('<tr><th>글로벌배송 (DDP)</th><td><b>관세·세금 선지불</b> — 체크아웃에서 배송지 입력 시 관세 포함 최종 금액이 확정되며 '
     '수령 시 추가 비용이 없습니다. 배송 가능 국가는 품목(특히 식품)에 따라 자동 확인됩니다.</td></tr>',
     '<tr><th>글로벌배송</th><td><b>앨범·굿즈 전 세계 배송</b> — <a href="/checkout-global">해외 배송 주문</a>(PayPal·해외카드)에서 '
     '국가를 선택하면 배송비와 예상 도착일이 확정됩니다. 관세·부가세는 수령 국가 기준에 따라 수령 시 부과될 수 있으며, '
     '관세 선지불(DDP) 가능 국가는 결제 화면에 별도로 표시됩니다. 냉동·냉장 K-FOOD는 국내 배송 전용입니다.</td></tr>'),
    ('해외 주문 반품 시 국제 회수 운임이 발생할 수 있으며, DDP로 선지불된 관세는 국가별 환급 규정에 따릅니다',
     '해외 주문 반품 시 국제 회수 운임이 발생할 수 있으며, 관세·부가세 환급은 수령 국가의 규정에 따릅니다'),
    ('일반·맵달드림 당일배송·성수 픽업·글로벌 DDP, 그리고 냉동식품 콜드체인 4단계까지.',
     '일반·맵달드림 당일배송·성수 픽업·글로벌 배송, 그리고 냉동식품 콜드체인 4단계까지.'),
    ('<li>Worldwide DDP shipping available</li>', '<li>Worldwide shipping · PayPal &amp; cards</li>'),
    ('해외 배송 시 결제 단계에서 관세·세금이 선지불(DDP)로 자동 합산됩니다.',
     '해외 배송은 해외 배송 주문 화면에서 국가별 배송비가 자동 계산되며, 관세는 수령 국가 기준에 따라 부과될 수 있습니다.'),
    ('에서 배송지 입력 시 자동 확인됩니다. 관세·세금은 선지불(DDP)되어 수령 시 추가 비용이 없습니다.',
     '에서 배송지 입력 시 자동 확인됩니다. 관세·부가세는 수령 국가 기준에 따라 수령 시 부과될 수 있습니다.'),
    ('<td>관세·세금 선지불(DDP) · 식품 수입 규제에 따라 국가별 배송 가능 여부 상이 (체크아웃 자동 확인)</td>',
     '<td>현재 국내 배송 전용 (냉동·냉장 식품 해외 배송 준비 중)</td>'),
    ('관세·세금 선지불(DDP) — 받는 분에게 추가 청구 없음', '해외 배송 — 결제 시 배송비 확정 · 관세는 수령 국가 기준'),
    ('배송비·관세(DDP)가 바로 계산됩니다.', '배송비가 바로 계산되며, 관세는 수령 국가 기준에 따라 부과될 수 있습니다.'),
    ('<div class="a">가능합니다. 식품 수입 규제에 따라 배송 가능 국가가 다르며, 체크아웃에서 배송지 입력 시 자동 확인됩니다. '
     '관세·부가세는 수령 국가 기준에 따라 수령 시 부과될 수 있습니다.</div>',
     '<div class="a">냉동·냉장 K-FOOD는 현재 국내 배송만 가능합니다. 앨범·굿즈는 해외 배송 주문에서 전 세계로 보내드립니다.</div>'),
)
_BV_OLD = '<div class="bv">관세·세금 <b>선지불(DDP)</b> · 추가 결제 없음</div>'
_BD_GOODS_OLD = '<div class="bd">전 세계 배송 가능 · 체크아웃에서 배송지 입력 시 최종 금액 자동 확정</div>'
_BD_FOOD_OLD = '<div class="bd">콜드체인 대응 국가에 한함 · 체크아웃에서 자동 확인</div>'


def copy_apply(html, path=''):
    if not isinstance(html, str):
        return html
    try:
        for a, b in _COPY_COMMON:
            if a in html:
                html = html.replace(a, b)
        if _BV_OLD in html:
            if _FOOD_PAGE.match(path or ''):
                html = html.replace(_BV_OLD, '<div class="bv"><b>국내 배송 전용</b> · 해외 배송 준비 중</div>')
            else:
                html = html.replace(_BV_OLD, '<div class="bv"><b>전 세계 배송</b> · 결제 시 배송비 확정</div>')
        if _BD_GOODS_OLD in html:
            html = html.replace(_BD_GOODS_OLD, '<div class="bd">50여 개국 배송 · 해외 배송 주문에서 국가 선택 시 배송비·예상 도착일 자동 계산 · '
                                               '관세는 수령 국가 기준에 따라 부과될 수 있음</div>')
        if _BD_FOOD_OLD in html:
            html = html.replace(_BD_FOOD_OLD, '<div class="bd">냉동·냉장 K-FOOD는 현재 국내 배송만 가능합니다</div>')
        # 홈 히어로 — 관리자 슬라이드를 서버에서 미리 실어 '기본 슬라이드 → API 교체' 레이아웃 이동(CLS 0.24) 제거
        if path == '/home' and 'render(MZH_DEFAULT);' in html and '__MZH=' not in html:
            try:
                import hero_api
                hd = hero_api.load_data()
                if hd and hd.get('slides'):
                    js = json.dumps(hd, ensure_ascii=False).replace('</', '<\\/')
                    html = html.replace('render(MZH_DEFAULT);',
                                        'window.__MZH=' + js + ';render(window.__MZH);', 1)
                    html = html.replace('fetch("/api/hero",{cache:"no-store"})',
                                        '(window.__MZH?Promise.reject(0):fetch("/api/hero",{cache:"no-store"}))', 1)
            except Exception:
                pass
        # NEW/DROPS — 세 화면(목록·상세·당첨)이 모두 숨겨진 채 시작해 JS 가 하나를 띄운다. 그 사이 푸터가
        #   첫 화면에 보였다가 밀려나는 CLS(0.94)를 막도록 한 화면 높이 자리표시를 두고, 화면이 뜨면 제거.
        if path == '/new-drops' and '<div id="vList" style="display:none">' in html and 'mpHold' not in html:
            html = html.replace('<div id="vList" style="display:none">',
                                '<div id="mpHold" aria-hidden="true" style="min-height:calc(100vh - 140px)"></div>'
                                '<div id="vList" style="display:none">', 1)
            i = html.lower().rfind('</body>')
            html = html[:i] + ('<script id="mpHoldJs">(function(){var h=document.getElementById("mpHold");if(!h)return;'
                               'var ids=["vList","vDetail","vWinners"];function chk(){for(var i=0;i<ids.length;i++){var e=document.getElementById(ids[i]);'
                               'if(e&&e.style.display!=="none"){h.remove();return true}}return false}'
                               'if(chk())return;var mo=new MutationObserver(function(){if(chk())mo.disconnect()});'
                               'ids.forEach(function(id){var e=document.getElementById(id);if(e)mo.observe(e,{attributes:true,attributeFilter:["style"]})});'
                               'setTimeout(function(){if(document.getElementById("mpHold"))h.remove()},8000)})();</script>') + html[i:]
        # 접근성: 옵션 셀렉트 레이블
        if '<select class="opt-select" id="optSel">' in html:
            html = html.replace('<select class="opt-select" id="optSel">',
                                '<select class="opt-select" id="optSel" aria-label="옵션 선택">')
        # 실물 사진이 없는 굿즈 상세 — 단일 이미지 페이저(1 | 1) 숨김 + 사실 그대로 안내
        if '<div class="glyph-hero">' in html and '<span class="gal-pager">1 | 1</span>' in html:
            html = html.replace('<span class="gal-pager">1 | 1</span>',
                                '<span class="gal-pager" style="letter-spacing:.06em">실물 사진 준비 중 · 성수 매장에서 실물 확인 가능</span>', 1)
        # 국내 체크아웃·장바구니: 해외 배송 주문 진입점
        if (path in ('/checkout', '/cart')) and 'mpIntlEntry' not in html and '<div class="cart-layout">' in html:
            bar = ('<div id="mpIntlEntry" style="display:flex;gap:10px;align-items:center;justify-content:space-between;'
                   'flex-wrap:wrap;background:#141414;color:#fff;padding:12px 16px;margin:0 0 16px;font-size:13.5px">'
                   '<span>🌏 <b>해외로 받으시나요?</b> 앨범·굿즈 전 세계 배송 · PayPal·해외카드 결제</span>'
                   '<a href="/checkout-global" style="background:#DC2B24;color:#fff;padding:9px 14px;text-decoration:none;'
                   'font-weight:700;white-space:nowrap">해외 배송으로 주문하기 →</a></div>')
            html = html.replace('<div class="cart-layout">', bar + '<div class="cart-layout">', 1)
    except Exception:
        pass
    return html


# ═══════════════════════════ O2O 확장: 국내 체크아웃 쿠폰 · 드롭 알림 구독 ═══════
_CK_SEND_OLD = "client:(window.mpClientHint?window.mpClientHint():null)}));"
_CK_SEND_NEW = ("client:(window.mpClientHint?window.mpClientHint():null),coupon:(window.mpCpCode||''),"
                "lang:(window.MP_LANG||'ko')}));")
_CK_CALC_OLD = "return {sub,ship,total:sub+ship,drop,pts};"
_CK_CALC_NEW = ("var _off=(window.mpCpOff&&sub>=(window.mpCpMin||0))?Math.min(window.mpCpOff,Math.max(0,sub-100)):0;"
                "window.mpCpApplied=_off;return {sub,ship,total:sub+ship-_off,drop,pts};")
_CK_COUPON_UI = r'''<div id="mpCp" style="padding:10px 0;border-bottom:1px solid var(--line)">
<div style="display:flex;gap:6px"><input class="f-input" id="mpCpIn" placeholder="쿠폰 코드 (예: HOME-XXXXXX)" aria-label="쿠폰 코드" style="margin:0;text-transform:uppercase;font-size:14px">
<button type="button" id="mpCpBtn" style="background:var(--ink);color:#fff;border:0;padding:0 14px;font-weight:700;cursor:pointer;white-space:nowrap">적용</button></div>
<div id="mpCpMsg" style="font-size:12px;min-height:16px;margin-top:4px" aria-live="polite"></div></div>
<div class="sum-row" id="mpCpRow" style="display:none"><span>쿠폰 할인</span><span id="mpCpV" style="color:var(--red)"></span></div>'''
_CK_COUPON_JS = r'''<script id="mpCpJs">(function(){try{
var $=function(i){return document.getElementById(i)},fmt=function(n){return '₩'+Math.round(n).toLocaleString('ko-KR')};
function sub(){try{return JSON.parse(localStorage.getItem('mapdal_cart')||'[]').reduce(function(a,i){return a+(Number(i.p)||0)*(Number(i.q)||1)},0)}catch(e){return 0}}
function paint(){var r=$('mpCpRow'),v=$('mpCpV');if(!r)return;var o=window.mpCpApplied||0;r.style.display=o?'':'none';v.textContent='−'+fmt(o)}
var _rs=window.renderSum;if(typeof _rs==='function'){window.renderSum=function(){_rs.apply(this,arguments);paint()}}
function apply(){var c=($('mpCpIn').value||'').trim().toUpperCase(),m=$('mpCpMsg');if(!c){window.mpCpCode='';window.mpCpOff=0;rs();return}
 fetch('/api/coupon/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c,sub:sub(),email:(($('bEmail')||{}).value||'')})})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'쿠폰을 확인할 수 없습니다');return d})})
 .then(function(d){window.mpCpCode=d.code;window.mpCpOff=d.off;window.mpCpMin=0;m.style.color='#0A7D38';m.textContent='적용되었습니다 · '+(d.note||d.code);rs();try{window.mpTrack&&mpTrack('select_promotion',{})}catch(e){}})
 .catch(function(x){window.mpCpCode='';window.mpCpOff=0;m.style.color='var(--red)';m.textContent=x.message;rs()})}
function rs(){try{(window.renderSum||function(){})()}catch(e){}paint()}
var b=$('mpCpBtn');if(b)b.addEventListener('click',apply);
var i=$('mpCpIn');if(i)i.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();apply()}});
try{var q=new URLSearchParams(location.search).get('coupon')||sessionStorage.getItem('mp_cp');if(q&&i){i.value=q;apply()}}catch(e){}
}catch(e){}})();</script>'''

_NL_BAND = r'''<section id="mpNl" aria-label="Drop alerts" style="background:#141414;color:#fff;border-top:4px solid #DC2B24">
<div style="max-width:1180px;margin:0 auto;padding:28px 16px;display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap">
<div style="flex:1 1 320px"><div style="font:500 11px 'IBM Plex Mono',monospace;letter-spacing:.14em;color:#FFB000">DROP ALERTS · WORLDWIDE</div>
<div style="font-family:'Black Han Sans',sans-serif;font-size:26px;line-height:1.2;margin:6px 0 4px">새 드롭·팬사인회 소식을 가장 먼저</div>
<div style="font-size:13px;color:#bbb">구독하면 첫 주문 10% 쿠폰을 바로 드립니다 · 언제든 수신거부</div></div>
<form id="mpNlF" style="flex:1 1 360px;display:flex;flex-wrap:wrap;gap:8px" novalidate>
<input id="mpNlE" type="email" autocomplete="email" inputmode="email" placeholder="이메일 주소" aria-label="이메일 주소" required style="flex:1 1 200px;font:inherit;font-size:16px;padding:12px;border:0;min-width:0">
<button type="submit" style="font:700 14px inherit;background:#DC2B24;color:#fff;border:0;padding:0 18px;min-height:46px;cursor:pointer">구독하기</button>
<label style="flex:1 1 100%;font-size:11.5px;color:#aaa;display:flex;gap:6px;align-items:flex-start"><input type="checkbox" id="mpNlC" style="margin-top:2px">맵달SEOUL의 혜택·신상 소식(광고성 정보) 수신에 동의합니다.</label>
<div id="mpNlM" style="flex:1 1 100%;font-size:13px;min-height:18px" aria-live="polite"></div></form></div></section>
<script id="mpNlJs">(function(){try{var f=document.getElementById('mpNlF');if(!f)return;f.addEventListener('submit',function(e){e.preventDefault();
var em=document.getElementById('mpNlE').value.trim(),m=document.getElementById('mpNlM');
if(!/^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$/.test(em)){m.style.color='#FFB000';m.textContent=(window.mpT||String)('이메일 주소를 확인해 주세요');return}
if(!document.getElementById('mpNlC').checked){m.style.color='#FFB000';m.textContent=(window.mpT||String)('수신 동의에 체크해 주세요');return}
fetch('/api/contacts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em,consent:true,lang:(window.MP_LANG||'ko'),source:'footer'})})
.then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'error');return d})})
.then(function(d){m.style.color='#fff';m.innerHTML=(window.mpT||String)('구독 완료! 첫 주문 쿠폰')+' <b style="color:#FFB000;font-family:monospace;font-size:15px">'+d.coupon+'</b>';
 try{sessionStorage.setItem('mp_cp',d.coupon)}catch(x){}try{window.mpTrack&&mpTrack('generate_lead',{method:'footer'})}catch(x){}})
.catch(function(x){m.style.color='#FFB000';m.textContent=x.message})})}catch(e){}})();</script>'''

_OC_OPTIN = r'''<div id="mpOcOpt" style="max-width:560px;margin:18px auto 0;padding:16px;border:1px solid #E2E0D9;background:#fff;text-align:center;display:none">
<div style="font-weight:700;margin-bottom:4px">다음 드롭 소식을 이메일로 받아보세요</div>
<div style="font-size:12.5px;color:#5E5D57;margin-bottom:10px">새 앨범·팬사인회·한정 굿즈 알림 · 언제든 수신거부</div>
<button type="button" id="mpOcBtn" style="background:#141414;color:#fff;border:0;padding:12px 18px;font-weight:700;cursor:pointer">드롭 알림 받기</button>
<div id="mpOcMsg" style="font-size:13px;margin-top:8px;min-height:16px" aria-live="polite"></div></div>
<script id="mpOcOptJs">(function(){try{var q=new URLSearchParams(location.search),oid=q.get('oid');if(!oid)return;
var box=document.getElementById('mpOcOpt');var hero=document.querySelector('.done-hero');if(hero&&box){hero.appendChild(box)}
fetch('/api/contacts/order-optin?oid='+encodeURIComponent(oid)).then(function(r){return r.json()}).then(function(d){if(d&&d.eligible)box.style.display='block'}).catch(function(){});
document.getElementById('mpOcBtn').addEventListener('click',function(){var b=this;b.disabled=true;
fetch('/api/contacts/order-optin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({oid:oid,lang:(window.MP_LANG||'ko')})})
.then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'error');return d})})
.then(function(){document.getElementById('mpOcMsg').textContent=(window.mpT||String)('구독되었습니다. 다음 드롭에서 만나요!');b.style.display='none'})
.catch(function(x){document.getElementById('mpOcMsg').textContent=x.message;b.disabled=false})})}catch(e){}})();</script>'''


def o2o_apply(html, path=''):
    """국내 체크아웃 쿠폰 · 사이트 공통 드롭 알림 구독 · 주문완료 구독 버튼 (멱등)."""
    if not isinstance(html, str):
        return html
    try:
        if path == '/checkout' and 'id="mpCp"' not in html and '<div class="sum-row total">' in html:
            if _CK_SEND_OLD in html:
                html = html.replace(_CK_SEND_OLD, _CK_SEND_NEW, 1)
            if _CK_CALC_OLD in html:
                html = html.replace(_CK_CALC_OLD, _CK_CALC_NEW, 1)
            html = html.replace('<div class="sum-row total">', _CK_COUPON_UI + '<div class="sum-row total">', 1)
            i = html.lower().rfind('</body>')
            html = html[:i] + _CK_COUPON_JS + html[i:]
        if path == '/order-complete' and 'mpOcOpt' not in html:
            i = html.lower().rfind('</body>')
            html = html[:i] + _OC_OPTIN + html[i:]
        # 드롭 알림 밴드 — 본문이 정적인 페이지에만(본문을 JS 로 늦게 그리는 목록 페이지에 넣으면
        #   밴드가 먼저 보였다가 밀려 내려가 CLS 가 커진다: /new-drops 측정 0.85)
        _nl_ok = (path in ('/home', '/mapdal-seoul', '/seongsu-limited', '/collections', '/journal', '/kfood',
                           '/gift-sets', '/support', '/shipping', '/partnership')
                  or path.startswith('/product-') or path.startswith('/collection-'))
        if _nl_ok and 'id="mpNl"' not in html:
            j = html.find('<footer')
            if j >= 0:
                html = html[:j] + _NL_BAND + html[j:]
    except Exception:
        pass
    return html


@growth_router.get('/api/contacts/order-optin')
def api_order_optin_check(request: Request, oid: str = ''):
    """주문완료 화면 — 이 주문(주문 브라우저·소유 회원) 이메일이 아직 구독 전이면 버튼 노출."""
    try:
        a = _app()
        r = _order(str(oid)[:40])
        if not r or not a._ov_ok(request, r['order_id'], r.get('customer_id') or ''):
            return {'eligible': False}
        em = str((_jl(r.get('buyer'), {}) or {}).get('email') or '').lower()
        return {'eligible': bool(em) and not mkt_ok(em, r.get('customer_id'))}
    except Exception:
        return {'eligible': False}


@growth_router.post('/api/contacts/order-optin')
async def api_order_optin(request: Request):
    d = await request.json()
    a = _app()
    r = _order(str(d.get('oid') or '')[:40])
    if not r or not a._ov_ok(request, r['order_id'], r.get('customer_id') or ''):
        raise HTTPException(403, 'forbidden')
    em = str((_jl(r.get('buyer'), {}) or {}).get('email') or '').lower()
    if not em:
        raise HTTPException(400, 'no email')
    ensure()
    lang = re.sub(r'[^a-z]', '', str(d.get('lang') or 'ko'))[:2] or 'ko'
    if _one('SELECT id FROM mp_contacts WHERE email=?', (em,)):
        _run('UPDATE mp_contacts SET consent=1, unsub=0 WHERE email=?', (em,))
    else:
        _run('INSERT INTO mp_contacts(id,created,email,channel,country,lang,source,consent,customer_id,unsub,mail_step) '
             'VALUES(?,?,?,?,?,?,?,1,?,0,?)',
             (secrets.token_hex(10), _iso(), em, 'email', (r.get('country') or '')[:2], lang, 'order_complete',
              r.get('customer_id') or '', 'd7'))   # 이미 구매 고객 — 웰컴 드립 생략
    return {'ok': True}


# ═══════════════════════════ 홈 화면 추가(PWA 매니페스트) ═══════════════════
#   귀국한 해외 고객이 휴대폰 홈 화면에 맵달을 '앱처럼' 두게 한다 — 재방문 = 재구매 동선.
#   start_url 에 utm 을 붙여 홈 화면 실행 유입을 그로스 대시보드에서 따로 본다.
_HEAD_PWA = ('<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/png" href="/icon-192.png">'
             '<link rel="manifest" href="/manifest.webmanifest">'
             '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
             '<meta name="apple-mobile-web-app-title" content="MAPDAL">')


@growth_router.get('/manifest.webmanifest')
def manifest(request: Request):
    lg = request.cookies.get('mp_lang') or ''
    pre = ('/' + lg) if lg in ('en', 'ja', 'zh') else ''
    m = {'name': 'MAPDAL SEOUL', 'short_name': 'MAPDAL', 'lang': lg or 'ko',
         'description': 'K-POP albums, merch & K-FOOD from Seongsu, Seoul — shipped worldwide.',
         'start_url': pre + '/home?utm_source=homescreen&utm_medium=app', 'scope': '/', 'display': 'standalone',
         'background_color': '#FFFFFF', 'theme_color': '#141414',
         'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                   {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
                   {'src': '/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]}
    return JSONResponse(m, media_type='application/manifest+json', headers={'Cache-Control': 'public, max-age=86400'})
