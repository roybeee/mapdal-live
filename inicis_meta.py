"""inicis_meta.py — 이니시스 MID·TID 판별 · 카드/은행 코드표 · 결제 상세 정규화 (순수 로직)

admin_v2.py(2만 줄)에서 분리한 첫 모듈 (2026-11-10). DB·FastAPI 의존이 없어 단독 테스트가 가능하다.
admin_v2 는 기존 이름 그대로 re-export 하므로 호출부 변경은 없다.
분리 기준·다음 후보는 docs/admin_v2_split.md 참고.
"""
import datetime
import os
import re

KST = datetime.timezone(datetime.timedelta(hours=9))


def _from_app(name, default=''):
    try:
        import app as _app
        return getattr(_app, name, default)
    except Exception:
        return default


def inicis_mid():    return os.environ.get('INICIS_MID') or _from_app('INICIS_MID', 'INIpayTest')
def inicis_iniapi(): return os.environ.get('INICIS_INIAPI') or _from_app('INICIS_INIAPI', 'ItEQKi3rY7uvDS8l')


def num(x):
    if x is None: return 0
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return 0


def now_iso():
    return datetime.datetime.now(KST).replace(tzinfo=None).isoformat(timespec='seconds')


# ── 이니시스 MID 교체 대응 ────────────────────────────────────────────────
#   상점아이디(MID)를 교체하면 교체 이전 결제건의 TID 는 '구 MID' 소속이라,
#   현재 MID·INIAPI key 로 환불을 호출하면 해시 불일치/거래없음으로 전부 실패한다.
#   이니시스 TID 에는 결제를 처리한 MID(10자)가 그대로 박혀 있으므로,
#   ① 알고 있는 MID 들을 TID 부분문자열로 대조해 결제 당시 자격증명을 자동 선택하고
#   ② 구 MID 는 Render 환경변수 INICIS_MID_OLD / INICIS_INIAPI_OLD 로 등록한다
#      (콤마 구분 복수 지원 · 순서 1:1 짝 · 코드 재배포 불필요, 저장 시 자동 재시작).
def _inicis_creds():
    """환불용 자격증명 목록 [(mid, iniapi_key, label)] — 현재 MID 우선, 구 MID 후순위.
    key 짝이 없는 구 MID 는 제외한다(빈 key 로 호출해도 해시 불일치 오류만 난다)."""
    creds = []
    cur_mid, cur_key = inicis_mid(), inicis_iniapi()
    if cur_mid and cur_key:
        creds.append((cur_mid, cur_key, ''))
    olds = [s.strip() for s in (os.environ.get('INICIS_MID_OLD') or '').split(',') if s.strip()]
    keys = [s.strip() for s in (os.environ.get('INICIS_INIAPI_OLD') or '').split(',') if s.strip()]
    for i, m in enumerate(olds):
        if i < len(keys) and m != cur_mid:
            creds.append((m, keys[i], '구MID'))
    return creds

# 표준 TID 형식: (Stdpay|INIMX_|INILITE_) + 결제수단토큰 + MID(10자) + yyyymmddHHMMSS + 일련번호
#   ※ 라우팅은 아래 정규식이 아니라 '설정된 MID 부분문자열 대조'로 한다(형식 변형에 안전).
#     정규식은 미설정 MID 를 오류 안내문에 보여주기 위한 추출 전용.
_INICIS_TID_RE = re.compile(r'(?:Stdpay|INIMX_|INILITE_?)(?:CARD|VBNK|BANK|MOBL|VCRD|HPP)([A-Za-z0-9]{10})\d{14}')

def _inicis_mid_of_tid(tid):
    """TID 에 박힌 결제 당시 MID 판별 — 설정된 MID 대조 우선, 실패 시 형식 추출, 그래도 없으면 ''."""
    t = str(tid or '')
    for m, _k, _lbl in _inicis_creds():
        if m and m in t:
            return m
    mt = _INICIS_TID_RE.search(t)
    return mt.group(1) if mt else ''

def _inicis_cred_for_tid(tid):
    """TID 소유 MID 의 (mid, iniapi_key, label) 반환 — 등록된 MID 가 아니면 None."""
    t = str(tid or '')
    for m, k, lbl in _inicis_creds():
        if m and m in t:
            return (m, k, lbl)
    return None
# ── 결제 상세(어떤 카드 · 어떤 페이) ─────────────────────────────────────
#   이니시스 승인응답에는 카드사·카드번호(마스킹)·할부·승인번호·간편결제 구분이
#   전부 들어오는데, 종전에는 payMethod('Card') 와 TID 만 저장하고 나머지를 버렸다.
#   그래서 주문 상세에 '카드'까지만 보이고 어느 카드사·어느 페이인지 알 수 없어
#   CS 응대와 카드사 정산 대조를 상점관리자(iniweb)에서 따로 해야 했다.
#     ① 승인 시점: app.py 의 /inicis/return · /inicis/mobile-return 이
#        pay_detail_save(oid, 승인응답) 을 호출해 orders.pay_detail 에 적재한다.
#        (돈이 움직이는 경로를 건드리지 않도록 전부 try 안에서 호출 — 실패해도 승인은 유지)
#     ② 과거 주문: INIAPI 거래조회(POST https://iniapi.inicis.com/v2/pg/inquiry)로
#        뒤늦게 채운다. 주문 상세를 열 때 자동 1회 조회하고 결과를 캐시한다.
#   코드표 출처: manual.inicis.com/pay/code.html (승인응답 시 카드코드 / 발급사코드)
_INI_CARD_NAME = {
    '01': '하나카드', '02': '우리카드', '03': '롯데카드', '04': '현대카드', '06': '국민카드',
    '11': '비씨카드', '12': '삼성카드', '14': '신한카드', '15': '한미카드', '16': 'NH카드',
    '17': '하나SK카드', '21': 'VISA', '22': 'MASTER', '23': 'JCB', '24': 'AMEX',
    '25': '다이너스', '26': '은련카드',
    '91': '네이버포인트', '93': '토스머니', '94': 'SSG머니', '96': '엘포인트',
    '97': '카카오머니', '98': '페이코포인트',
}
# 카드가 아니라 간편결제 머니·포인트로 결제된 코드 — 어떤 '페이'인지가 여기서 확정된다.
_INI_EASY_NAME = {
    '91': '네이버페이', '93': '토스페이', '94': 'SSGPAY', '96': 'L.POINT',
    '97': '카카오페이', '98': 'PAYCO',
}
_INI_BANK_NAME = {
    '02': '한국산업은행', '03': '기업은행', '04': '국민은행', '05': '하나은행(구 외환)',
    '06': '국민은행(구 주택)', '07': '수협중앙회', '11': '농협중앙회', '12': '단위농협',
    '16': '축협중앙회', '20': '우리은행', '23': 'SC제일은행', '27': '한국씨티은행',
    '31': '대구은행', '32': '부산은행', '34': '광주은행', '35': '제주은행', '37': '전북은행',
    '39': '경남은행', '45': '새마을금고', '48': '신협중앙회', '50': '상호저축은행',
    '53': '한국씨티은행', '54': '홍콩상하이은행', '64': '산림조합', '71': '우체국',
    '81': '하나은행', '87': '신세계', '88': '신한은행', '89': '케이뱅크', '90': '카카오뱅크',
    '91': '네이버포인트', '92': '토스뱅크', '93': '토스머니', '94': 'SSG머니',
    '96': '엘포인트', '97': '카카오머니', '98': '페이코포인트',
}
_PD_CHECK = {'0': '신용카드', '1': '체크카드', '2': '기프트카드'}
_PD_CORP = {'0': '개인', '1': '법인'}
# 승인응답 원문 보관 화이트리스트 접두 — 라벨 매핑이 못 따라가는 신규 코드가 와도
# 관리자가 원문에서 직접 확인할 수 있어야 한다(간편결제 코드는 수시로 추가된다).
_PD_RAW_PREFIX = ('CARD_', 'P_CARD_', 'P_FN_', 'P_SRC', 'P_APPL', 'P_TYPE', 'applNum',
                  'payMethod', 'EventCode', 'P_EVENT', 'VACT_', 'P_VACT_')

def _pd_get(src, *names):
    for n in names:
        v = src.get(n)
        if v not in (None, ''):
            return str(v).strip()
    return ''

def _pd_quota(v):
    q = re.sub(r'\D', '', str(v or ''))
    if q == '':
        return ''
    n = int(q)
    return '일시불' if n <= 1 else ('%d개월' % n)

def pay_detail_norm(src, origin='STEP3'):
    """이니시스 응답(PC JSON · 모바일 NVP · 거래조회 v2) → 화면용 결제 상세 dict.

    세 규격이 필드명만 다르고 의미는 같아 하나로 흡수한다. 카드 정보가 전혀
    없으면 {} 를 돌려주고 저장하지 않는다(가상계좌 채번 등)."""
    if not isinstance(src, dict):
        return {}
    ci = src.get('cardInfo') if isinstance(src.get('cardInfo'), dict) else {}
    ai = src.get('acctInfo') if isinstance(src.get('acctInfo'), dict) else {}
    vi = src.get('vacctInfo') if isinstance(src.get('vacctInfo'), dict) else {}
    flat = dict(src)
    for sub in (ci, ai, vi):
        for k, v in sub.items():
            flat.setdefault('_' + k, v)
    code = _pd_get(flat, 'CARD_Code', 'P_FN_CD1', '_cardCode')
    brand = _pd_get(flat, 'P_FN_NM', '_cardName') or _INI_CARD_NAME.get(code, '')
    issuer = _pd_get(flat, 'P_CARD_ISSUER_NAME', '_issuerName')
    icode = _pd_get(flat, 'CARD_BankCode', 'P_CARD_ISSUER_CODE', '_issuerCode')
    if not issuer and icode not in ('', '00'):
        issuer = _INI_BANK_NAME.get(icode, '')
    d = {
        'origin': origin,
        'method': _pd_get(flat, 'payMethod', 'P_TYPE', 'paymethod'),
        'code': code, 'brand': brand, 'issuer': issuer,
        'num': _pd_get(flat, 'CARD_Num', 'P_CARD_NUM', '_cardNumber'),
        'quota': _pd_quota(_pd_get(flat, 'CARD_Quota', 'P_CARD_QUOTA', 'P_QUOTABASE', '_cardQuota')),
        'kind': _PD_CHECK.get(_pd_get(flat, 'CARD_CheckFlag', 'P_CARD_CHECKFLAG'), ''),
        'corp': _PD_CORP.get(_pd_get(flat, 'CARD_CorpFlag'), ''),
        'appl': _pd_get(flat, 'applNum', 'P_APPL_NO', '_approvedNumber'),
        'amount': num(re.sub(r'[^\d]', '', _pd_get(flat, 'CARD_ApplPrice', 'P_CARD_APPLPRICE',
                                                   '_approvedAmount', 'TotPrice', 'P_AMT', 'price')) or 0),
        'point': num(re.sub(r'[^\d]', '', _pd_get(flat, 'CARD_UsePoint', 'P_CARD_USEPOINT', '_pointAmount')) or 0),
        'discount': num(re.sub(r'[^\d]', '', _pd_get(flat, 'CARD_CouponDiscount', 'P_CARD_COUPON_DISCOUNT',
                                                     '_instantDiscountAmount')) or 0),
        'bank': _pd_get(flat, '_bankName') or _INI_BANK_NAME.get(_pd_get(flat, '_bankCode'), ''),
        'at': now_iso(),
    }
    # 간편결제 판정 — ① 머니·포인트 코드는 그 자체가 페이 브랜드,
    #                ② 카드를 태운 간편결제는 CARD_SrcCode/P_SRC_CODE 로만 구분된다.
    src_code = _pd_get(flat, 'CARD_SrcCode', 'P_SRC_CODE')
    if code in _INI_EASY_NAME:
        d['easy'] = _INI_EASY_NAME[code]
    elif src_code:
        d['easy'] = '간편결제(앱)'
        d['easy_code'] = src_code
    if _pd_get(flat, 'CARD_Interest', '') == '1' or flat.get('_isInterestFree') is True:
        d['interest'] = '무이자'
    raw = {}
    for k, v in src.items():
        if isinstance(v, (str, int, float)) and str(k).startswith(_PD_RAW_PREFIX):
            raw[str(k)[:40]] = str(v)[:80]
    for k, v in (ci or {}).items():
        if isinstance(v, (str, int, float, bool)):
            raw[('cardInfo.' + str(k))[:40]] = str(v)[:80]
    d['raw'] = dict(list(raw.items())[:40])
    if not (d['brand'] or d['num'] or d['appl'] or d.get('easy') or d['bank']):
        return {}
    return d

def pay_detail_text(d):
    """결제 상세 dict → 주문 상세 한 줄 표기."""
    if not isinstance(d, dict) or not d:
        return ''
    p = []
    if d.get('easy'):
        p.append(d['easy'])
    if d.get('brand'):
        p.append(d['brand'] + (('(%s)' % d['kind']) if d.get('kind') else ''))
    elif d.get('bank'):
        p.append(d['bank'])
    if d.get('issuer') and d.get('issuer') != d.get('brand'):
        p.append('발급 ' + d['issuer'])
    if d.get('num'):
        p.append(d['num'])
    if d.get('quota'):
        p.append(d['quota'] + (' · ' + d['interest'] if d.get('interest') else ''))
    if d.get('corp') == '법인':
        p.append('법인카드')
    if d.get('appl'):
        p.append('승인 ' + d['appl'])
    if num(d.get('point')):
        p.append('포인트 %s원' % format(num(d['point']), ','))
    if num(d.get('discount')):
        p.append('즉시할인 %s원' % format(num(d['discount']), ','))
    return ' · '.join(x for x in p if x)
