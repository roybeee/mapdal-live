"""ordergate.py — 주문 생성 남용 방지 (2026-11-10)

배경: 재고는 주문 생성(PENDING) 시점에 차감되고, 결제하지 않은 PENDING 은 최소 24시간 뒤에야
정리됐다. 주문 생성에 속도 제한이 없어 한 사람이 결제 없이 한정 재고(예약판매 등)를 하루 동안
묶어 둘 수 있었다.

방어 (모두 환경변수로 조정, 0 = 끔)
  ORDER_RATE_MEMBER    회원당 주문 생성 시도 / ORDER_RATE_WINDOW_S (기본 10회 / 600초)
  ORDER_RATE_IP        IP당 주문 생성 시도 / ORDER_RATE_WINDOW_S (기본 20회)
  ORDER_OPEN_MAX       고객당 미결제(PENDING·WAITING_DEPOSIT, 최근 24시간) 동시 보유 상한 (기본 5)
  ORDER_PROMO_MAX_QTY  행사 상품(배송비 필수부과 판정) 품목당 수량 상한 (기본 0 = 제한 없음
                       — 팬사인회 응모 등 다량 구매가 정상인 상품이 있어 기본은 끈다)
  ORDER_PENDING_PROMO_MIN  행사 상품이 든 미결제 PENDING 을 정리하는 기준(분, 기본 30)
                       — 일반 상품은 종전대로 VBANK_EXPIRE_PENDING_H(기본 24시간)

속도 제한 상태는 프로세스 메모리에 있다(단일 인스턴스 전제 — admin_v2 로그인 제한과 동일).
"""
import collections
import os
import threading
import time


def _int(name, default, lo=0, hi=10 ** 6):
    try:
        v = int(str(os.getenv(name, '')).strip() or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def window_s():
    return _int('ORDER_RATE_WINDOW_S', 600, 10, 86400)


def member_limit():
    return _int('ORDER_RATE_MEMBER', 10)


def ip_limit():
    return _int('ORDER_RATE_IP', 20)


def open_max():
    return _int('ORDER_OPEN_MAX', 5)


def promo_max_qty():
    return _int('ORDER_PROMO_MAX_QTY', 0, 0, 99)


def pending_promo_min():
    return _int('ORDER_PENDING_PROMO_MIN', 30, 0, 1440)


class SlidingWindow:
    """키별 최근 시도 시각 — limit 이상이면 거부. 메모리 상한 초과 시 오래된 키부터 비운다."""

    def __init__(self, max_keys=20000):
        self._hits = collections.OrderedDict()
        self._lock = threading.Lock()
        self._max = max_keys

    def hit(self, key, limit, window, now=None):
        """시도 1회 기록. 반환: 허용 여부(거부된 시도는 기록하지 않는다)."""
        if not key or limit <= 0:
            return True
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits.pop(key, None) or collections.deque()
            while q and now - q[0] >= window:
                q.popleft()
            allowed = len(q) < limit
            if allowed:
                q.append(now)
            self._hits[key] = q
            while len(self._hits) > self._max:
                self._hits.popitem(last=False)
            return allowed

    def clear(self):
        with self._lock:
            self._hits.clear()


_LIMITER = SlidingWindow()


def rate_check(member_id, ip, now=None, limiter=None):
    """주문 생성 시도 속도 제한. 거부 시 사용자 안내 문구, 허용 시 ''."""
    lim = limiter or _LIMITER
    w = window_s()
    if member_id and not lim.hit('m:' + member_id, member_limit(), w, now):
        return '주문 시도가 너무 잦습니다 — 잠시 후 다시 시도해 주세요'
    if ip and not lim.hit('ip:' + ip, ip_limit(), w, now):
        return '주문 시도가 너무 잦습니다 — 잠시 후 다시 시도해 주세요'
    return ''


def open_cap_message(open_count):
    cap = open_max()
    if cap and open_count >= cap:
        return ('결제하지 않은 주문이 %d건 있습니다 — 마이페이지 주문내역에서 결제하거나 취소한 뒤 '
                '다시 주문해 주세요' % open_count)
    return ''


def promo_qty_message(items, promo_ids):
    """행사 상품 품목당 수량 상한 위반 시 안내 문구."""
    cap = promo_max_qty()
    if not cap:
        return ''
    for it in items or []:
        pid = str((it or {}).get('id') or '')
        try:
            q = int((it or {}).get('q') or 1)
        except (TypeError, ValueError):
            q = 1
        if pid in promo_ids and q > cap:
            return '행사 상품은 한 번에 %d개까지 주문할 수 있습니다' % cap
    return ''
