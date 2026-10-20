"""
제주·도서산간 지역 추가 배송비 (2026-10-20) — 주소지(우편번호) 기반 자동 부과. 결제(/api/orders)·추가 배송비 청구(shipfee) 공용.

요금 (CJ대한통운 계약 기준, 2026-10-20 CS팀 확인): 제주 +3,000원 / 도서·산간 +5,000원.
  관리자 [추가 배송비 청구] 화면의 '지역 추가 배송비'에서 변경 (site_settings 'ship_region').

지역 판정: 국내 쇼핑몰·택배 업계 공통 '제주 및 도서산간 우편번호 기준표'(5자리 우편번호).
  · 제주 63000~63644 — 단, 제주 부속 섬(추자도 63000~63001 · 우도 63365)은 도서·산간
  · 도서·산간: 인천 중구·강화·옹진 섬, 충남 당진·태안·보령 섬, 울릉도, 부산 강서 섬, 경남 사천·통영 섬,
    전북 부안 섬, 전남 영광·목포·신안·진도·완도·보성·고흥·여수 섬
  · 우편번호가 없을 때만 주소 첫머리('제주')·군 이름(울릉군·옹진군·신안군)으로 보조 판정
"""
import re

REMOTE_ZIPS = [
    (22386, 22388),  # 인천 중구 섬지역
    (23004, 23010),  # 인천 강화 섬지역
    (23100, 23116),  # 인천 옹진 섬지역1
    (23124, 23136),  # 인천 옹진 섬지역2
    (31708, 31708),  # 충남 당진 섬지역
    (32133, 32133),  # 충남 태안 섬지역
    (33411, 33411),  # 충남 보령 섬지역
    (40200, 40240),  # 경북 울릉도 전지역
    (46768, 46771),  # 부산 강서구 섬지역
    (52570, 52571),  # 경남 사천 섬지역
    (53031, 53033),  # 경남 통영 섬지역1
    (53089, 53104),  # 경남 통영 섬지역2
    (54000, 54000),  # 경남 통영 섬지역3
    (56347, 56349),  # 전북 부안 섬지역
    (57068, 57069),  # 전남 영광 섬지역
    (58760, 58762),  # 전남 목포 섬지역
    (58800, 58810),  # 전남 신안 섬지역1
    (58816, 58818),  # 전남 신안 섬지역2
    (58826, 58826),  # 전남 신안 섬지역3
    (58828, 58866),  # 전남 신안 섬지역4
    (58953, 58958),  # 전남 진도 섬지역
    (59102, 59103),  # 전남 완도 섬지역1
    (59106, 59106),  # 전남 완도 섬지역2
    (59127, 59127),  # 전남 완도 섬지역3
    (59129, 59129),  # 전남 완도 섬지역4
    (59137, 59166),  # 전남 완도 섬지역5
    (59421, 59421),  # 전남 보성 섬지역
    (59531, 59531),  # 전남 고흥 섬지역1
    (59551, 59551),  # 전남 고흥 섬지역2
    (59563, 59563),  # 전남 고흥 섬지역3
    (59568, 59568),  # 전남 고흥 섬지역4
    (59650, 59650),  # 전남 여수 섬지역1
    (59766, 59766),  # 전남 여수 섬지역2
    (59781, 59790),  # 전남 여수 섬지역3
    (63000, 63001),  # 제주 추자도
    (63365, 63365),  # 제주 우도
]
JEJU = (63000, 63644)
DEFAULT_RATES = {'jeju': 3000, 'remote': 5000}
LABEL = {'jeju': '제주 지역 추가 배송비', 'remote': '도서·산간 지역 추가 배송비'}

_cache = {'rates': None, 'ts': 0.0}


def rates():
    """관리자 설정 요금 (60초 캐시)."""
    import time
    if _cache['rates'] is not None and time.time() - _cache['ts'] < 60:
        return dict(_cache['rates'])
    r = dict(DEFAULT_RATES)
    try:
        import admin_v2 as av
        row = av.one("SELECT value FROM site_settings WHERE key='ship_region'")
        c = av.jload((row or {}).get('value'), {}) or {}
        for k in ('jeju', 'remote'):
            if k in c:
                r[k] = max(0, int(c[k]))
    except Exception:
        pass
    _cache['rates'], _cache['ts'] = dict(r), time.time()
    return r


def set_rates(jeju, remote, by=''):
    import admin_v2 as av
    v = {'jeju': max(0, int(jeju)), 'remote': max(0, int(remote))}
    av._setting_put('ship_region', v, by)
    _cache['rates'] = None
    return v


def region(zipcode='', addr1=''):
    """→ 'jeju' | 'remote' | ''"""
    z = re.sub(r'\D', '', str(zipcode or ''))
    if len(z) == 5:
        n = int(z)
        if any(lo <= n <= hi for lo, hi in REMOTE_ZIPS):
            return 'remote'
        if JEJU[0] <= n <= JEJU[1]:
            return 'jeju'
        return ''
    a = re.sub(r'\s+', ' ', str(addr1 or '')).strip()          # 우편번호가 없을 때만 보조 판정
    if re.search(r'(^|\s)(울릉군|옹진군|신안군)(\s|$)', a):
        return 'remote'
    if a.startswith('제주'):
        return 'jeju'
    return ''


def extra_for(buyer, rt=None):
    """배송지(buyer: zip·addr1) → (지역, 추가금)."""
    b = buyer or {}
    reg = region(b.get('zip'), b.get('addr1'))
    if not reg:
        return '', 0
    rt = rt or rates()
    return reg, int(rt.get(reg, 0))
