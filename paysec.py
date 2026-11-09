"""paysec.py — 결제 노티(입금통보·승인통보) 진위 검증 (2026-11-10 보안 패치)

배경
  /inicis/vbank-noti · /inicis/mobile-noti 는 이니시스 서버가 호출하는 콜백이라 CSRF·로그인 검사가
  없다. 종전에는 '주문 존재 + 금액 일치 + 상태 가드'만으로 PAID 전환을 했는데, 주문번호와 금액은
  주문한 본인이 /api/orders 응답으로 이미 알고 있으므로 위조 방어가 되지 않았다.
  (자기 주문번호·금액으로 노티를 흉내 내면 결제 없이 PAID 가 된다.)

이 모듈의 원칙
  · 노티는 '조회해 보라'는 신호로만 취급한다. PAID 전환은 이니시스 INIAPI 거래조회(V1 Extra/Inquiry)로
    해당 TID 가 실제 승인(status 0) 또는 입금완료(status Y)이고 금액이 주문금액과 같을 때만 한다.
  · 조회 통신 실패·일시 오류는 '보류' — 호출부가 FAIL 을 돌려 이니시스가 재전송(24시간·약 10분 주기)하게 한다.
    입금은 유실되지 않고, 위조 노티는 영원히 PAID 가 되지 못한다.
  · 발신 IP 허용목록은 보조 수단이다(기본 'log' 모드 — 기록만). 로그로 실제 발신 IP 를 확인한 뒤
    INICIS_NOTI_IP_MODE=enforce 로 올린다.
  · FastAPI 의존이 없다 — 순수 함수라 단위 테스트가 쉽다.

환경변수
  INICIS_NOTI_VERIFY     on(기본) | off   — off 는 긴급 되돌림 레버(종전 동작). 운영에서 끄지 말 것.
  INICIS_NOTI_IPS        콤마 구분 허용 IP (기본: 이니시스 공지 노티 IP)
  INICIS_NOTI_IP_MODE    log(기본) | enforce | off
  INICIS_INQUIRY_URL     거래조회 URL (기본 https://iniapi.inicis.com/api/v1/extra)
  INICIS_CLIENT_IP       거래조회 clientIp 필드값 (기본: 서버 호스트 IP)
"""
import hashlib
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

# 이니시스 공지 노티 발신 IP (app.py 주석 기준). 변경 시 INICIS_NOTI_IPS 로 덮어쓴다.
NOTI_IPS_DEFAULT = ('203.238.37.15', '183.109.71.153')

INQUIRY_URL_DEFAULT = 'https://iniapi.inicis.com/api/v1/extra'


class InquiryError(Exception):
    """거래조회 통신·응답 오류 — 일시적일 수 있으므로 '보류(재시도)'로 처리한다."""


def _env(k, d=''):
    return (os.getenv(k) or d).strip()


def verify_enabled():
    return _env('INICIS_NOTI_VERIFY', 'on').lower() not in ('off', '0', 'false', 'no')


# ── 발신 IP ────────────────────────────────────────────────────────────
def noti_ip_mode():
    m = _env('INICIS_NOTI_IP_MODE', 'log').lower()
    return m if m in ('log', 'enforce', 'off') else 'log'


def noti_ips():
    raw = _env('INICIS_NOTI_IPS')
    vals = [x.strip() for x in raw.split(',') if x.strip()] if raw else list(NOTI_IPS_DEFAULT)
    out = []
    for v in vals:
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            continue
    return out


def ip_allowed(ip):
    """허용목록 포함 여부 (모드와 무관한 순수 판정)."""
    try:
        a = ipaddress.ip_address(str(ip or '').strip())
    except ValueError:
        return False
    return any(a in n for n in noti_ips())


def ip_gate(ip):
    """(통과 여부, 기록용 메모). enforce 모드에서만 차단한다."""
    mode = noti_ip_mode()
    if mode == 'off':
        return True, ''
    ok = ip_allowed(ip)
    if ok:
        return True, ''
    note = '허용목록 밖 발신 IP %s (%s 모드)' % (ip or '-', mode)
    return (mode != 'enforce'), note


# ── 거래조회 (INIAPI V1 Extra/Inquiry) ─────────────────────────────────
def _client_ip():
    v = _env('INICIS_CLIENT_IP')
    if v:
        return v
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return '127.0.0.1'


def inquiry_hash(key, timestamp, client_ip, mid):
    """hashData = SHA512(INIAPIKey + type + paymethod + timestamp + clientIp + mid)
       — manual.inicis.com/pay/etc-inquiry.html (V1)."""
    plain = '%s%s%s%s%s%s' % (key, 'Extra', 'Inquiry', timestamp, client_ip, mid)
    return hashlib.sha512(plain.encode('utf-8')).hexdigest()


def inquiry_v1(mid, key, tid, oid='', timestamp=None, opener=None, timeout=15):
    """TID 거래조회. 반환: 응답 dict (resultCode '00' 일 때만). 그 외는 InquiryError."""
    if not (mid and key and tid):
        raise InquiryError('거래조회 자격정보/TID 없음')
    if timestamp is None:
        import datetime
        timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y%m%d%H%M%S')
    cip = _client_ip()
    form = {'type': 'Extra', 'paymethod': 'Inquiry', 'timestamp': timestamp, 'clientIp': cip,
            'mid': mid, 'originalTid': tid, 'hashData': inquiry_hash(key, timestamp, cip, mid)}
    if oid:
        form['oid'] = oid
    req = urllib.request.Request(_env('INICIS_INQUIRY_URL', INQUIRY_URL_DEFAULT),
                                 data=urllib.parse.urlencode(form).encode('utf-8'),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'},
                                 method='POST')
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=timeout) as r:
            res = json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        raise InquiryError('거래조회 HTTP %s' % getattr(e, 'code', '?'))
    except Exception as e:
        raise InquiryError('거래조회 통신 오류: %s' % type(e).__name__)
    if not isinstance(res, dict):
        raise InquiryError('거래조회 응답 형식 오류')
    if str(res.get('resultCode') or '') != '00':
        raise InquiryError('거래조회 실패 [%s] %s' % (res.get('resultCode') or '', res.get('resultMsg') or ''))
    return res


def _price(v):
    try:
        return int(str(v or '0').replace(',', '').split('.')[0] or 0)
    except ValueError:
        return -1


def _candidates(*tids):
    out = []
    for t in tids:
        t = str(t or '').strip()
        if t and t not in out:
            out.append(t)
    return out


def _verify(kind, order, noti_tid, inquire):
    """kind: 'deposit'(가상계좌 입금완료 Y) | 'approval'(일반 승인 0).

    inquire(tid, oid) -> 응답 dict (InquiryError 가능).
    반환 (verdict, note, tid)
      'ok'      조회로 실제 결제 확인 — PAID 전환 허용
      'reject'  조회는 됐으나 결제가 아님(미입금·취소·거래없음·금액 불일치) — 전환 거부
      'retry'   조회 불가(통신·자격정보) — 전환 보류, 호출부는 FAIL 로 재전송을 유도
    """
    want = 'Y' if kind == 'deposit' else '0'
    amount = int(order.get('amount') or 0)
    oid = order.get('order_id') or ''
    tids = (_candidates(order.get('payment_key'), noti_tid) if kind == 'deposit'
            else _candidates(noti_tid, order.get('payment_key')))
    if not tids:
        return 'reject', 'TID 없음 — 조회 불가한 노티', ''
    errors, seen = [], []
    for t in tids:
        try:
            res = inquire(t, oid)
        except InquiryError as e:
            errors.append(str(e)[:80])
            continue
        st = str(res.get('status') or '').strip()
        pr = _price(res.get('price'))
        seen.append('%s:%s/%s' % (t[-6:], st or '-', pr))
        if st == want and pr == amount:
            return 'ok', '거래조회 확인 (TID …%s · status %s · %s원)' % (t[-6:], st, format(pr, ',')), t
    if seen:
        return 'reject', '거래조회 불일치 — %s (기대 status %s · %s원)' % (', '.join(seen), want, format(amount, ',')), ''
    return 'retry', '거래조회 보류 — ' + '; '.join(errors), ''


def verify_deposit(order, noti_tid, inquire):
    return _verify('deposit', order, noti_tid, inquire)


def verify_approval(order, noti_tid, inquire):
    return _verify('approval', order, noti_tid, inquire)
