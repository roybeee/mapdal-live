"""edgeguard.py — 운영 설정 fail-closed + Cloudflare 경유 강제 (2026-11-10 보안 패치)

1) 결제 설정 fail-closed
   종전에는 INICIS_MID/SIGNKEY/INIAPI 가 비면 이니시스 '테스트 상점' 값으로 조용히 대체됐다.
   운영에서 환경변수 하나만 빠져도 돈이 들어오지 않는 결제가 성공 처리되고, 환불·노티 검증이 깨진다.
   운영(Render·PostgreSQL)에서는 테스트/빈 값이면 결제 생성을 503 으로 막고 기동 로그에 크게 남긴다.
   스테이징에서 테스트 상점을 쓰려면 INICIS_ALLOW_TEST=1.

2) 원본(Render) 직접 접근 차단
   Cloudflare 를 거치지 않고 *.onrender.com 으로 바로 들어오면 WAF 가 무력화되고,
   CF-Connecting-IP 같은 헤더를 마음대로 넣어 로그인 시도 제한(IP 기준)을 우회할 수 있다.
   Cloudflare Transform Rule 로 모든 요청에 비밀 헤더(X-Origin-Auth)를 붙이고,
   원본은 그 헤더가 있는 요청만 '엣지 경유'로 인정한다.
     ORIGIN_SHARED_SECRET   비밀 헤더 값 (미설정 = 기능 꺼짐, 종전 동작)
     ORIGIN_GUARD_MODE      log(기본) | enforce — log 는 미경유 요청을 기록만, enforce 는 403
   엣지 경유가 확인되지 않은 요청에서는 프록시 IP 헤더(CF-Connecting-IP 등)를 믿지 않는다.
"""
import hmac
import os

TEST_MID = 'INIpayTest'
TEST_SIGNKEY = 'SU5JTElURV9UUklQTEVERVNfS0VZU1RS'
TEST_INIAPI = 'ItEQKi3rY7uvDS8l'

ORIGIN_HEADER = 'x-origin-auth'
# Render 헬스체크·모니터링은 Cloudflare 를 거치지 않는다.
ORIGIN_EXEMPT = ('/healthz', '/readyz')


def _env(k, d=''):
    return (os.getenv(k) or d).strip()


def _truthy(v):
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def is_production(is_pg=False):
    if _env('APP_ENV').lower() in ('dev', 'development', 'test', 'local'):
        return False
    return bool(_env('RENDER') or is_pg or _env('APP_ENV').lower() in ('prod', 'production'))


def inicis_config_check(mid, signkey, iniapi, hashkey, production):
    """(errors, warnings). errors 가 있으면 운영 결제를 막는다."""
    errors, warnings = [], []
    if not production or _truthy(_env('INICIS_ALLOW_TEST')):
        return errors, warnings
    if not mid or mid == TEST_MID:
        errors.append('INICIS_MID 가 비었거나 테스트 상점(INIpayTest)입니다')
    if not signkey or signkey == TEST_SIGNKEY:
        errors.append('INICIS_SIGNKEY 가 비었거나 테스트 키입니다')
    if not iniapi or iniapi == TEST_INIAPI:
        errors.append('INICIS_INIAPI 가 비었거나 테스트 키입니다 (환불·노티 검증 불가)')
    if not hashkey:
        msg = 'INICIS_MOBILE_HASHKEY 미설정 — 모바일 금액위변조 검증(P_CHKFAKE) 생략 중'
        (errors if _truthy(_env('INICIS_REQUIRE_HASHKEY')) else warnings).append(msg)
    return errors, warnings


# ── 원본 직접 접근 ─────────────────────────────────────────────────────
def origin_secret():
    return _env('ORIGIN_SHARED_SECRET')


def origin_mode():
    m = _env('ORIGIN_GUARD_MODE', 'log').lower()
    return m if m in ('log', 'enforce') else 'log'


def edge_verified(headers):
    """비밀 헤더 일치 여부. 기능이 꺼져 있으면 None(판정 불가 — 종전처럼 헤더 신뢰)."""
    sec = origin_secret()
    if not sec:
        return None
    got = str(headers.get(ORIGIN_HEADER) or '')
    return hmac.compare_digest(got.encode('utf-8', 'replace'), sec.encode('utf-8'))


def origin_decision(path, headers):
    """(허용 여부, edge_ok). edge_ok: True/False/None(기능 꺼짐)."""
    ok = edge_verified(headers)
    if ok is None or ok:
        return True, ok
    if path in ORIGIN_EXEMPT:
        return True, False
    return origin_mode() != 'enforce', False


def trust_proxy_headers(state):
    """request.state.edge_ok 기준 — 미경유가 확인된 요청은 CF-Connecting-IP 등을 믿지 않는다."""
    return getattr(state, 'edge_ok', None) is not False
