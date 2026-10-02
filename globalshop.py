"""globalshop.py — 해외 직구 체크아웃 (2026-10)

해외 고객이 '귀국 후에도' 맵달에서 바로 살 수 있게 하는 전용 결제 흐름.
국내 체크아웃(이니시스·다음 우편번호·가상계좌)은 건드리지 않는다 — 별도 경로 /checkout-global.

  · 상품별 해외배송 가능 여부 — 앨범(k2g)·굿즈 가능 / 냉동·냉장 K-FOOD·응모형 드롭 불가(환경변수로 조정)
  · 국가 존(zone)별 배송비 — 첫 상품 기본료 + 추가 상품당 요금 (EMS 프리미엄 기준 근사)
  · 관세: 기본 DAP(수령인 현지 납부) 고지 — 운송사 DDP 계약 국가는 INTL_DDP 로 결제 시 합산
  · 결제: PayPal(계정 없이 해외카드 결제 포함) · USD 청구(KRW 미지원) — 환율은 /admin/growth 표시환율 + 마진
  · PayPal 미설정 시: 주문 접수 후 결제 링크 발송(인보이스) — 주소·상품·금액이 구조화되어 CS 부담 최소
  · 재고 차감·쿠폰·어트리뷰션·주문조회 쿠키·결제완료 알림(메일·CAPI)은 국내 주문과 동일 파이프라인
"""
import os, re, json, time, base64, secrets, datetime, threading, urllib.request, urllib.parse, urllib.error

from fastapi import APIRouter, Request, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response

global_router = APIRouter()


def _app():
    import app as _a
    return _a


def _av():
    import admin_v2 as _a
    return _a


def _g():
    import growth as _x
    return _x


def _env(k, d=''):
    return (os.getenv(k) or d).strip()


# ═══════════════════════════ 국가 · 존 · 배송비 ═══════════════════════════
COUNTRIES = [  # (ISO, English, 日本語, 中文, zone)
    ('JP', 'Japan', '日本', '日本', 1), ('CN', 'China', '中国', '中国', 1), ('TW', 'Taiwan', '台湾', '台湾', 1),
    ('HK', 'Hong Kong', '香港', '香港', 1), ('MO', 'Macau', 'マカオ', '澳门', 1), ('MN', 'Mongolia', 'モンゴル', '蒙古', 1),
    ('SG', 'Singapore', 'シンガポール', '新加坡', 2), ('TH', 'Thailand', 'タイ', '泰国', 2), ('VN', 'Vietnam', 'ベトナム', '越南', 2),
    ('PH', 'Philippines', 'フィリピン', '菲律宾', 2), ('MY', 'Malaysia', 'マレーシア', '马来西亚', 2),
    ('ID', 'Indonesia', 'インドネシア', '印度尼西亚', 2), ('BN', 'Brunei', 'ブルネイ', '文莱', 2),
    ('KH', 'Cambodia', 'カンボジア', '柬埔寨', 2), ('IN', 'India', 'インド', '印度', 2),
    ('US', 'United States', 'アメリカ', '美国', 3), ('CA', 'Canada', 'カナダ', '加拿大', 3),
    ('AU', 'Australia', 'オーストラリア', '澳大利亚', 3), ('NZ', 'New Zealand', 'ニュージーランド', '新西兰', 3),
    ('GB', 'United Kingdom', 'イギリス', '英国', 3), ('IE', 'Ireland', 'アイルランド', '爱尔兰', 3),
    ('FR', 'France', 'フランス', '法国', 3), ('DE', 'Germany', 'ドイツ', '德国', 3), ('ES', 'Spain', 'スペイン', '西班牙', 3),
    ('IT', 'Italy', 'イタリア', '意大利', 3), ('NL', 'Netherlands', 'オランダ', '荷兰', 3), ('BE', 'Belgium', 'ベルギー', '比利时', 3),
    ('AT', 'Austria', 'オーストリア', '奥地利', 3), ('CH', 'Switzerland', 'スイス', '瑞士', 3), ('SE', 'Sweden', 'スウェーデン', '瑞典', 3),
    ('NO', 'Norway', 'ノルウェー', '挪威', 3), ('DK', 'Denmark', 'デンマーク', '丹麦', 3), ('FI', 'Finland', 'フィンランド', '芬兰', 3),
    ('PL', 'Poland', 'ポーランド', '波兰', 3), ('PT', 'Portugal', 'ポルトガル', '葡萄牙', 3), ('CZ', 'Czechia', 'チェコ', '捷克', 3),
    ('HU', 'Hungary', 'ハンガリー', '匈牙利', 3), ('GR', 'Greece', 'ギリシャ', '希腊', 3), ('RO', 'Romania', 'ルーマニア', '罗马尼亚', 3),
    ('MX', 'Mexico', 'メキシコ', '墨西哥', 4), ('BR', 'Brazil', 'ブラジル', '巴西', 4), ('AR', 'Argentina', 'アルゼンチン', '阿根廷', 4),
    ('CL', 'Chile', 'チリ', '智利', 4), ('PE', 'Peru', 'ペルー', '秘鲁', 4), ('CO', 'Colombia', 'コロンビア', '哥伦比亚', 4),
    ('AE', 'United Arab Emirates', 'アラブ首長国連邦', '阿联酋', 4), ('SA', 'Saudi Arabia', 'サウジアラビア', '沙特阿拉伯', 4),
    ('QA', 'Qatar', 'カタール', '卡塔尔', 4), ('TR', 'Türkiye', 'トルコ', '土耳其', 4), ('IL', 'Israel', 'イスラエル', '以色列', 4),
    ('ZA', 'South Africa', '南アフリカ', '南非', 4), ('EG', 'Egypt', 'エジプト', '埃及', 4),
]
_CMAP = {c[0]: c for c in COUNTRIES}
_ZONE_DEFAULT = {1: (16000, 2500), 2: (19000, 3000), 3: (26000, 4000), 4: (33000, 5000)}


def zone_rates():
    """INTL_ZONE_RATES='1:16000/2500,2:19000/3000,…' 로 조정 가능."""
    z = dict(_ZONE_DEFAULT)
    for part in _env('INTL_ZONE_RATES').split(','):
        m = re.fullmatch(r'\s*([1-4])\s*:\s*(\d+)\s*/\s*(\d+)\s*', part)
        if m:
            z[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
    return z


def ddp_rates():
    """INTL_DDP='GB:20,AU:10' — 운송사 DDP 계약이 있는 국가만. 값=상품가 대비 % (관세+부가세 추정)."""
    out = {}
    for part in _env('INTL_DDP').split(','):
        m = re.fullmatch(r'\s*([A-Z]{2})\s*:\s*(\d+(?:\.\d+)?)\s*', part.upper())
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def ship_fee(country, n_items, sub):
    c = _CMAP.get(country)
    if not c:
        return None
    base, add = zone_rates()[c[4]]
    free = int(_env('INTL_FREE_OVER', '0') or 0)
    if free and sub >= free:
        return 0
    return base + add * max(0, min(int(n_items), 10) - 1)


# ═══════════════════════════ 상품별 해외배송 가능 여부 ═══════════════════
_BLOCK_DEFAULT = 'product-bowl-,product-kimbap-,product-tteokbokki,mpd::'
_FOOD_RE = re.compile(r'(food|kfood|k-food|식품|김밥|떡볶이|냉동|냉장|bowl|kimbap|tteok)', re.I)


def intl_ok(pid, name='', category=''):
    p = str(pid or '')
    allow = [x.strip() for x in _env('INTL_ALLOW_PATTERNS').split(',') if x.strip()]
    if any(p.startswith(a) for a in allow):
        return True
    block = [x.strip() for x in (_env('INTL_BLOCK_PATTERNS') or _BLOCK_DEFAULT).split(',') if x.strip()]
    if any(p.startswith(b) for b in block):
        return False
    if p.startswith('mp::') and (_FOOD_RE.search(category or '') or _FOOD_RE.search(name or '')):
        return False
    return True


# ═══════════════════════════ 견적 (서버 단일 계산원) ════════════════════
def _lookup(c, pid, lock=False):
    a = _app()
    for cand in a._product_id_candidates(pid):
        row = c.one('SELECT * FROM products WHERE id=?%s' % (a.LOCK if lock else ''), (cand,))
        if row:
            return row
    return None


def _usd_rate():
    r = 0.0
    try:
        r = float(_g().fx_rates().get('USD') or 0)
    except Exception:
        r = 0.0
    return r if r > 0 else 0.00072


def to_usd(krw):
    margin = float(_env('PAYPAL_FX_MARGIN', '0.02') or 0.02)
    return round(int(krw) * _usd_rate() * (1 + margin) + 1e-9, 2)


def quote(items, country, coupon_code='', email='', customer_id='', lock_c=None):
    """품목 검증·소계·배송비·DDP·쿠폰·합계(KRW/USD). lock_c 가 주어지면 그 트랜잭션에서 재고 잠금 조회."""
    country = str(country or '').upper()[:2]
    if country not in _CMAP:
        raise HTTPException(400, 'Please choose a shipping country')
    if not items:
        raise HTTPException(400, 'Your cart is empty')
    lines, blocked, sub, n = [], [], 0, 0

    def run(c):
        nonlocal sub, n
        for it in items[:50]:
            pid = str(it.get('id', ''))
            q = max(1, min(99, int(it.get('q', 1) or 1)))
            row = _lookup(c, pid, lock=lock_c is not None)
            if not row:
                # 단종·삭제된 상품이 오래된 장바구니에 남은 경우 — 견적 전체를 막지 않고 제외 대상으로 표시
                blocked.append({'id': pid, 'n': str(it.get('n') or pid)[:80], 'gone': True})
                continue
            cat = row.get('category') or row.get('kind') or ''
            if not intl_ok(row['id'], row.get('name') or '', cat):
                blocked.append({'id': row['id'], 'n': row['name']})
                continue
            if row['soldout']:
                raise HTTPException(400, 'Sold out: %s' % str(row['name'])[:40])
            if int(row['price'] or 0) <= 0:
                raise HTTPException(400, 'Price unavailable: %s' % str(row['name'])[:40])
            if row['stock'] is not None and row['stock'] < q:
                raise HTTPException(409, 'Only %d left: %s' % (row['stock'], str(row['name'])[:40]))
            sub += int(row['price']) * q
            n += q
            lines.append({'id': row['id'], 'n': row['name'], 'p': int(row['price']), 'q': q, 'stock': row['stock']})
    if lock_c is not None:
        run(lock_c)
    else:
        with _app().db() as c:
            run(c)
    if blocked and not lines:
        raise HTTPException(400, 'These items can only be shipped within Korea')
    fee = ship_fee(country, n, sub) or 0
    ddp_pct = ddp_rates().get(country, 0)
    duties = int(round(sub * ddp_pct / 100.0, -2)) if ddp_pct else 0
    cp, off = None, 0
    if str(coupon_code or '').strip():
        cp = _g().coupon_check(coupon_code, email, customer_id, '', True)
        off = _g().coupon_amount(cp, sub)
    total = sub + fee + duties - off
    return {'country': country, 'zone': _CMAP[country][4], 'lines': lines, 'blocked': blocked, 'sub': sub,
            'ship': fee, 'duties': duties, 'ddp': bool(ddp_pct), 'discount': off,
            'coupon': (cp or {}).get('code') or '', 'total': total, 'usd': to_usd(total),
            'paypal': paypal_enabled(), 'free_over': int(_env('INTL_FREE_OVER', '0') or 0)}


@global_router.post('/api/intl/quote')
async def api_quote(req: Request):
    _g().rate_limit(req, 'intl_quote', 120, 600)
    d = await req.json()
    cid, email = '', str(d.get('email') or '').strip().lower()
    try:
        m = _av().member_of(req)
        cid = (m or {}).get('customer_id') or ''
    except Exception:
        pass
    q = quote(d.get('items') or [], d.get('country'), d.get('coupon') or '', email, cid)
    for l in q['lines']:
        l.pop('stock', None)
    return q


@global_router.get('/api/intl/eligibility')
def api_elig(ids: str = ''):
    """장바구니·상품 페이지 표시용 — 쉼표 구분 상품 ID → 해외배송 가능 여부."""
    out = {}
    for pid in [x for x in ids.split(',') if x][:60]:
        try:
            with _app().db() as c:
                row = _lookup(c, pid)
            out[pid] = bool(row) and intl_ok(row['id'], row.get('name') or '', row.get('category') or row.get('kind') or '')
        except Exception:
            out[pid] = intl_ok(pid)
    return out


# ═══════════════════════════ PayPal (Orders v2) ═════════════════════════
_PP = {'tok': '', 'exp': 0}


def paypal_enabled():
    return bool(_env('PAYPAL_CLIENT_ID') and _env('PAYPAL_SECRET'))


def _pp_base():
    return 'https://api-m.paypal.com' if _env('PAYPAL_ENV', 'sandbox') == 'live' else 'https://api-m.sandbox.paypal.com'


def _pp_token():
    if _PP['tok'] and time.time() < _PP['exp'] - 60:
        return _PP['tok']
    auth = base64.b64encode(('%s:%s' % (_env('PAYPAL_CLIENT_ID'), _env('PAYPAL_SECRET'))).encode()).decode()
    rq = urllib.request.Request(_pp_base() + '/v1/oauth2/token', data=b'grant_type=client_credentials',
                                headers={'Authorization': 'Basic ' + auth,
                                         'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(rq, timeout=15) as r:
        d = json.loads(r.read().decode())
    _PP['tok'], _PP['exp'] = d['access_token'], time.time() + int(d.get('expires_in') or 3000)
    return _PP['tok']


def _pp(method, path, payload=None, idem=None):
    h = {'Authorization': 'Bearer ' + _pp_token(), 'Content-Type': 'application/json'}
    if idem:
        h['PayPal-Request-Id'] = idem
    rq = urllib.request.Request(_pp_base() + path, data=(json.dumps(payload).encode() if payload is not None else None),
                                headers=h, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=25) as r:
            return r.status, json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or '{}')
        except Exception:
            return e.code, {}


def _money(v):
    return '%.2f' % v


# ═══════════════════════════ 해외 주문 생성 · 결제 ══════════════════════
_DDL = ("""CREATE TABLE IF NOT EXISTS mp_intl_pay(
  order_id TEXT PRIMARY KEY, provider TEXT, provider_order TEXT, currency TEXT, amount TEXT,
  krw INTEGER, status TEXT, created TEXT, captured TEXT, capture_id TEXT, raw TEXT)""",)
_READY = {'ok': False}


def ensure():
    if _READY['ok']:
        return
    for ddl in _DDL:
        try:
            with _app().db() as c:
                c.exec(ddl)
        except Exception:
            pass
    _READY['ok'] = True


def _clean(s, n=120):
    return re.sub(r'[\x00-\x1f<>]', '', str(s or '')).strip()[:n]


@global_router.post('/api/intl/orders')
async def api_intl_order(req: Request, response: Response):
    """해외 주문 생성 — 재고 차감 + PENDING 주문 + (PayPal) 결제 주문 생성. 반환: orderId, paypalOrderId."""
    _g().rate_limit(req, 'intl_order', 8, 3600)   # 미결제 주문으로 재고를 묶는 남용 방지
    ensure()
    a = _app()
    d = await req.json()
    b = d.get('buyer') or {}
    country = str(b.get('country') or '').upper()[:2]
    buyer = {'name': _clean(b.get('name'), 60), 'phone': _clean(b.get('phone'), 30),
             'email': str(b.get('email') or '').strip().lower()[:80], 'country': country,
             'country_name': (_CMAP.get(country) or ('', ''))[1], 'addr1': _clean(b.get('addr1')),
             'addr2': _clean(b.get('addr2')), 'city': _clean(b.get('city'), 60), 'state': _clean(b.get('state'), 60),
             'zip': _clean(b.get('zip'), 20), 'intl': True}
    if len(buyer['name']) < 2:
        raise HTTPException(400, 'Please enter the recipient’s full name')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[A-Za-z]{2,}', buyer['email']):
        raise HTTPException(400, 'Please enter a valid email address')
    if len(re.sub(r'\D', '', buyer['phone'])) < 7:
        raise HTTPException(400, 'Please enter a phone number with country code')
    if not (buyer['addr1'] and buyer['city'] and buyer['zip']):
        raise HTTPException(400, 'Please complete the shipping address')
    if not d.get('agree'):
        raise HTTPException(400, 'Please agree to the terms of purchase')
    items = d.get('items') or []
    member_id = customer_id = ''
    try:
        _av().ensure_ready()
        m = _av().member_of(req)
        if m and (m.get('status') or 'ACTIVE') == 'ACTIVE':
            member_id, customer_id = m.get('id') or '', m.get('customer_id') or ''
    except Exception:
        pass
    phone_norm = re.sub(r'\D', '', buyer['phone'])
    if not customer_id:
        try:
            customer_id = _av().guest_customer_ensure(buyer['name'], phone_norm)
        except Exception:
            customer_id = ''
    try:
        _av().drop_purchase_gate(items)
    except HTTPException:
        raise
    except Exception:
        pass
    # 쿠폰 유효성은 트랜잭션 밖에서(읽기) — quote() 안에서 coupon_check 가 DB 를 읽으므로
    # 트랜잭션 안에서는 쿠폰 없이 계산하고, 할인액만 순수 계산으로 반영한다.
    cp = None
    if str(d.get('coupon') or '').strip():
        cp = _g().coupon_check(d.get('coupon'), buyer['email'], customer_id, phone_norm, True)
    with a.db() as c:
        q = quote(items, country, '', buyer['email'], customer_id, lock_c=c)
        if q['blocked']:
            raise HTTPException(400, 'Some items in your cart can only be shipped within Korea — please remove them first')
        off = _g().coupon_amount(cp, q['sub']) if cp else 0
        total = q['sub'] + q['ship'] + q['duties'] - off
        for l in q['lines']:
            if l['stock'] is not None:
                c.exec('UPDATE products SET stock=stock-? WHERE id=?', (l['q'], l['id']))
                if l['stock'] - l['q'] == 0:
                    c.exec('UPDATE products SET soldout=1 WHERE id=?', (l['id'],))
        oid = 'MD-%s-%s' % (a.kst_naive().strftime('%Y%m%d'), secrets.token_hex(3).upper())
        buyer['ship_fee'], buyer['duties'], buyer['ddp'] = q['ship'], q['duties'], q['ddp']
        lines = [{'id': l['id'], 'n': l['n'], 'p': l['p'], 'q': l['q']} for l in q['lines']]
        c.exec('INSERT INTO orders(order_id,created,status,amount,buyer,items,ship_method,customer_id,member_id,contact_phone_norm) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (oid, a.kst_iso(), 'PENDING', total, json.dumps(buyer, ensure_ascii=False),
                json.dumps(lines, ensure_ascii=False), 'intl', customer_id or None, member_id or None, phone_norm or None))
        if customer_id:
            try:
                c.exec('INSERT INTO account_order_links(order_id,customer_id,member_id,link_source,linked_at,verified_at) '
                       'VALUES(?,?,?,?,?,?)', (oid, customer_id, member_id, 'CHECKOUT_SESSION' if member_id else 'GUEST_CHECKOUT',
                                               a.kst_iso(), a.kst_iso()))
            except Exception:
                pass
    # ── 주문 트랜잭션 밖: 재고 투영 · 국가 · 어트리뷰션 · 쿠폰 보류 · 조회 쿠키 ──
    try:
        for l in q['lines']:
            if l['stock'] is not None:
                _av().catalog_inventory_from_legacy(l['id'])
    except Exception:
        pass
    try:
        with a.db() as c:
            c.exec('UPDATE orders SET country=? WHERE order_id=?', (country, oid))
    except Exception:
        pass
    try:
        _g().order_attr_capture(req, oid, d)
        if cp and off:
            _g().coupon_hold(oid, cp['code'], off)
            with a.db() as c:
                c.exec('UPDATE orders SET coupon=?, discount=? WHERE order_id=?', (cp['code'], str(off), oid))
    except Exception:
        pass
    try:
        response.set_cookie(a._OV_COOKIE, a._ov_cookie_value(req, oid), max_age=90 * 86400, httponly=True,
                            samesite='lax', secure=a.SITE_ORIGIN.startswith('https'))
    except Exception:
        pass
    if d.get('marketing'):
        try:
            _g()._run('INSERT INTO mp_contacts(id,created,email,channel,country,lang,source,consent,unsub,mail_step) '
                      'VALUES(?,?,?,?,?,?,?,1,0,?)',
                      (secrets.token_hex(10), _g()._iso(), buyer['email'], 'email', country,
                       str(d.get('lang') or 'en')[:2], 'checkout_global', ''))
        except Exception:
            pass
    usd = to_usd(total)
    a._pay_log(oid, 'CREATED', '해외 · %s · %s원 (≈US$%s) · %d품목' % (country, format(total, ','), _money(usd), len(lines)))
    out = {'orderId': oid, 'amount': total, 'usd': usd, 'currency': 'USD'}
    if paypal_enabled():
        name = lines[0]['n'][:100] + (' + %d more' % (len(lines) - 1) if len(lines) > 1 else '')
        payload = {'intent': 'CAPTURE', 'purchase_units': [{
            'reference_id': oid, 'custom_id': oid, 'invoice_id': oid, 'description': ('MAPDAL SEOUL — ' + name)[:127],
            'amount': {'currency_code': 'USD', 'value': _money(usd)},
            'shipping': {'name': {'full_name': buyer['name'][:300]},
                         'address': {'address_line_1': buyer['addr1'][:300], 'address_line_2': buyer['addr2'][:300],
                                     'admin_area_2': buyer['city'][:120], 'admin_area_1': buyer['state'][:300],
                                     'postal_code': buyer['zip'][:60], 'country_code': country}}}],
            'application_context': {'brand_name': 'MAPDAL SEOUL', 'shipping_preference': 'SET_PROVIDED_ADDRESS',
                                    'user_action': 'PAY_NOW', 'locale': {'ja': 'ja-JP', 'zh': 'zh-CN'}.get(str(d.get('lang')), 'en-US')}}
        st, res = _pp('POST', '/v2/checkout/orders', payload, idem='mp-' + oid)
        if st not in (200, 201) or not res.get('id'):
            a._pay_log(oid, 'PP_CREATE_FAIL', str(res)[:300])
            raise HTTPException(502, 'PayPal is temporarily unavailable — please try again in a moment')
        with a.db() as c:
            c.exec('INSERT INTO mp_intl_pay(order_id,provider,provider_order,currency,amount,krw,status,created) '
                   'VALUES(?,?,?,?,?,?,?,?)', (oid, 'paypal', res['id'], 'USD', _money(usd), total, 'CREATED', a.kst_iso()))
        out['paypalOrderId'] = res['id']
    else:
        # 결제 링크 발송 방식(인보이스) — 운영팀에 알림 + 고객 접수 메일
        with a.db() as c:
            c.exec('INSERT INTO mp_intl_pay(order_id,provider,currency,amount,krw,status,created) VALUES(?,?,?,?,?,?,?)',
                   (oid, 'invoice', 'USD', _money(usd), total, 'REQUESTED', a.kst_iso()))
        threading.Thread(target=_invoice_notice, args=(oid, buyer, lines, total, usd, str(d.get('lang') or 'en')),
                         daemon=True).start()
        out['invoice'] = True
    return out


@global_router.post('/api/intl/capture')
async def api_intl_capture(req: Request):
    """PayPal 승인 후 캡처 — 금액·통화·주문번호 대조 후 PAID. 멱등(이미 PAID 면 그대로 성공)."""
    ensure()
    a = _app()
    d = await req.json()
    oid = str(d.get('orderId') or '')[:40]
    with a.db() as c:
        row = c.one('SELECT * FROM mp_intl_pay WHERE order_id=?', (oid,))
        o = c.one('SELECT status FROM orders WHERE order_id=?', (oid,))
    if not row or not o or row.get('provider') != 'paypal':
        raise HTTPException(404, 'Order not found')
    if o['status'] == 'PAID':
        return {'ok': True, 'orderId': oid}
    st, res = _pp('POST', '/v2/checkout/orders/%s/capture' % row['provider_order'], {}, idem='cap-' + oid)
    if st == 422 and 'ORDER_ALREADY_CAPTURED' in json.dumps(res):
        st, res = _pp('GET', '/v2/checkout/orders/%s' % row['provider_order'])
    try:
        pu = res['purchase_units'][0]
        cap = pu['payments']['captures'][0]
        ok = (res.get('status') == 'COMPLETED' and cap.get('status') in ('COMPLETED', 'PENDING')
              and cap['amount']['currency_code'] == 'USD' and cap['amount']['value'] == row['amount']
              and (pu.get('custom_id') or cap.get('custom_id') or oid) == oid)
    except Exception:
        ok, cap = False, {}
    if not ok:
        a._pay_log(oid, 'PP_CAPTURE_FAIL', '[%s] %s' % (st, json.dumps(res)[:300]))
        with a.db() as c:
            c.exec("UPDATE mp_intl_pay SET status='FAILED', raw=? WHERE order_id=?", (json.dumps(res)[:3000], oid))
            c.exec("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (oid,))
        raise HTTPException(402, 'Payment was not completed — no charge was made. Please try again.')
    cid = cap.get('id') or ''
    with a.db() as c:
        c.exec("UPDATE mp_intl_pay SET status='CAPTURED', captured=?, capture_id=?, raw=? WHERE order_id=?",
               (a.kst_iso(), cid, json.dumps(res)[:3000], oid))
        c.exec("UPDATE orders SET status='PAID', payment_key=?, pay_method=?, paid_at=? WHERE order_id=? AND status<>'PAID'",
               (cid, 'PayPal', a.kst_iso(), oid))
    a._pay_log(oid, 'PAID', 'PayPal · US$%s · capture …%s%s' % (row['amount'], cid[-6:],
                                                              ' (보류 — PayPal 심사 중)' if cap.get('status') == 'PENDING' else ''))
    try:
        a._award_purchase_points(oid)
    except Exception:
        pass
    try:
        a._ga4_mp_purchase(oid)
    except Exception:
        pass
    try:
        _av().order_notify_async(oid, 'paid')
    except Exception:
        pass
    return {'ok': True, 'orderId': oid}


def paypal_refund(oid):
    """관리자 취소(_order_cancel_core) 에서 호출 — 전액 환불. 실패 시 HTTPException."""
    ensure()
    a = _app()
    with a.db() as c:
        row = c.one('SELECT * FROM mp_intl_pay WHERE order_id=?', (oid,))
    if not row or row.get('provider') != 'paypal' or not row.get('capture_id'):
        raise HTTPException(400, 'PayPal 결제 정보를 찾을 수 없습니다 — PayPal 대시보드에서 환불 후 [수동환불 완료처리]를 사용하세요')
    if not paypal_enabled():
        raise HTTPException(400, 'PAYPAL_CLIENT_ID / PAYPAL_SECRET 미설정 — PayPal 대시보드에서 환불 후 [수동환불 완료처리]')
    st, res = _pp('POST', '/v2/payments/captures/%s/refund' % row['capture_id'], {}, idem='ref-' + oid)
    if st not in (200, 201) or res.get('status') not in ('COMPLETED', 'PENDING'):
        raise HTTPException(502, 'PayPal 환불 실패: %s' % (json.dumps(res)[:200]))
    with a.db() as c:
        c.exec("UPDATE mp_intl_pay SET status='REFUNDED' WHERE order_id=?", (oid,))
    return True


def _invoice_notice(oid, buyer, lines, total, usd, lang):
    """PayPal 미설정 시 — 운영팀 알림 메일 + 고객 접수 메일."""
    try:
        g = _g()
        ops = _env('INTL_ORDER_NOTIFY', _env('MAIL_REPLY_TO', 'cx@mealzip.kr'))
        rows_ = g._kv_rows([('주문번호', oid), ('국가', buyer.get('country_name')), ('금액', '₩%s (≈US$%.2f)' % (format(total, ','), usd)),
                            ('수령인', buyer.get('name')), ('이메일', buyer.get('email')), ('전화', buyer.get('phone')),
                            ('주소', ' '.join(x for x in (buyer.get('addr1'), buyer.get('addr2'), buyer.get('city'),
                                                       buyer.get('state'), buyer.get('zip')) if x))])
        g.send_mail(ops, '[해외주문 결제링크 요청] %s · %s' % (oid, buyer.get('country_name')),
                    g.mail_layout('ko', '해외 주문 — 결제 링크를 발송해 주세요', '고객에게 24시간 내 결제 링크(PayPal 인보이스 등)를 보내주세요.',
                                  rows_ + g._items_html(lines)), 'intl_ops', oid)
        msg = {'en': ('We received your order (%s)' % oid, 'Order received — payment link coming soon',
                      'Thank you! Our team will email you a secure payment link within 24 hours (KST). Your items are reserved for 72 hours.'),
               'ja': ('ご注文を受け付けました（%s）' % oid, 'ご注文を受け付けました', 'ありがとうございます。24時間以内（韓国時間）に決済リンクをメールでお送りします。商品は72時間確保されます。'),
               'zh': ('已收到您的订单（%s）' % oid, '订单已收到', '谢谢！我们将在24小时内（韩国时间）通过邮件发送安全付款链接，商品为您保留72小时。'),
               'ko': ('[맵달SEOUL] 해외 주문 접수 (%s)' % oid, '주문이 접수되었습니다', '24시간 이내에 결제 링크를 이메일로 보내드립니다. 상품은 72시간 동안 확보됩니다.')}
        m = msg.get(lang[:2], msg['en'])
        g.send_mail(buyer.get('email'), m[0], g.mail_layout(lang[:2], m[1], m[2], g._kv_rows([('Order', oid), ('Total', '₩%s (≈US$%.2f)' % (format(total, ','), usd))]) + g._items_html(lines)),
                    'intl_received', oid)
    except Exception as e:
        print('[global] invoice notice: %s' % e, flush=True)


# ═══════════════════════════ /checkout-global 화면 ═══════════════════════
def _lang(req):
    lg = getattr(req.state, 'lang', None)
    if lg in ('en', 'ja', 'zh'):
        return lg
    c = req.cookies.get('mp_lang') or ''
    if c in ('en', 'ja', 'zh', 'ko'):
        return c
    al = (req.headers.get('accept-language') or '').lower()[:2]
    return al if al in ('en', 'ja', 'zh', 'ko') else 'en'


@global_router.get('/checkout-global', response_class=HTMLResponse)
def checkout_global(request: Request):
    lang = _lang(request)
    ctry = []
    li = {'en': 1, 'ja': 2, 'zh': 3}.get(lang, 1)
    for c in sorted(COUNTRIES, key=lambda x: x[li] if lang != 'en' else x[1]):
        ctry.append([c[0], c[li], c[4]])
    cfg = {'lang': lang, 'countries': ctry, 'paypal': paypal_enabled(),
           'ppClient': _env('PAYPAL_CLIENT_ID'), 'ddp': ddp_rates(),
           'prefix': ('/' + lang) if lang in ('en', 'ja', 'zh') else ''}
    html = _CKG_HTML.replace('__CFG__', json.dumps(cfg, ensure_ascii=False)).replace('__LANG__', lang if lang != 'zh' else 'zh-Hans')
    g = _g()
    html = g.head_apply(html)
    add = ''
    try:
        add += _av()._analytics_snippet()
    except Exception:
        pass
    add += g._body_js()
    html = html.replace('</body>', add + '</body>', 1)
    resp = HTMLResponse(html, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})
    if getattr(request.state, 'lang', None) in ('en', 'ja', 'zh'):
        resp.set_cookie('mp_lang', request.state.lang, max_age=31536000, samesite='lax', secure=True)
    return resp


_CKG_HTML = r'''<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Checkout — MAPDAL SEOUL</title><meta name="robots" content="noindex"><meta name="theme-color" content="#141414">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root{--ink:#141414;--red:#DC2B24;--amber:#FFB000;--paper:#F7F6F2;--line:#E2E0D9;--steel:#5E5D57;--good:#0A7D38}
*{box-sizing:border-box}[hidden]{display:none!important}html,body{margin:0}body{background:var(--paper);color:var(--ink);font:15px/1.55 "IBM Plex Sans KR",-apple-system,"Hiragino Sans","PingFang SC",sans-serif}
a{color:inherit}.top{background:var(--ink);color:#fff;border-bottom:4px solid var(--red)}
.top .in{max-width:1180px;margin:0 auto;padding:14px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.logo{font-family:"Black Han Sans",sans-serif;font-size:24px;text-decoration:none;color:#fff}.logo em{font-style:normal;color:var(--red)}
.steps{display:flex;gap:14px;font:500 11.5px "IBM Plex Mono",monospace;letter-spacing:.1em;color:#999}.steps b{color:var(--amber)}
.sp{flex:1}.langs{display:flex;gap:2px}.langs a{font:600 12px "IBM Plex Mono",monospace;color:#bbb;text-decoration:none;padding:6px 8px;border:1px solid #444;min-width:36px;text-align:center}
.langs a.on{background:#fff;color:var(--ink);border-color:#fff}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 80px;display:grid;grid-template-columns:1fr 400px;gap:28px;align-items:start}
@media(max-width:920px){.wrap{grid-template-columns:1fr}.sum{order:-1}}
h1{font-family:"Black Han Sans",sans-serif;font-weight:400;font-size:32px;margin:0 0 4px}.lede{color:var(--steel);margin:0 0 18px}
.sec{background:#fff;border:1px solid var(--line);padding:20px;margin-bottom:14px}.sec h2{font-size:16px;margin:0 0 4px;display:flex;align-items:center;gap:10px}
.sec h2 span{font:500 11px "IBM Plex Mono",monospace;color:var(--red);letter-spacing:.1em}.sec .h{color:var(--steel);font-size:13px;margin:0 0 12px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:560px){.row{grid-template-columns:1fr}}
label.f{display:block;margin-bottom:10px}label.f span{display:block;font-size:12.5px;font-weight:700;margin-bottom:4px}
input,select{width:100%;font:inherit;font-size:16px;padding:12px;border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:0}
input:focus,select:focus{outline:2px solid var(--ink);outline-offset:-1px}input[aria-invalid="true"]{border-color:var(--red)}
.chk{display:flex;gap:10px;align-items:flex-start;font-size:13px;color:var(--steel);margin:8px 0}.chk input{width:20px;height:20px;flex:0 0 20px;margin-top:1px}
.note{font-size:13px;line-height:1.6;background:#FAFAF7;border-left:3px solid var(--amber);padding:10px 12px;margin-top:6px}
.sum{position:sticky;top:16px}.sum .sec{padding:18px}
.it{display:grid;grid-template-columns:1fr auto;gap:8px;padding:9px 0;border-bottom:1px dashed var(--line);font-size:13.5px}
.it .q{color:var(--steel);font-family:"IBM Plex Mono",monospace;font-size:12px}.it.bad{opacity:.55}.it .why{grid-column:1/-1;color:var(--red);font-size:12px}
.it button{border:0;background:none;color:var(--steel);text-decoration:underline;cursor:pointer;font:inherit;font-size:12px;padding:0}
.ln{display:flex;justify-content:space-between;font-size:14px;padding:5px 0}.ln.tot{font-weight:700;font-size:18px;border-top:2px solid var(--ink);margin-top:6px;padding-top:10px}
.ln small{color:var(--steel);font-weight:400}.usd{font:500 12.5px "IBM Plex Mono",monospace;color:var(--steel);text-align:right}
.cp{display:flex;gap:6px;margin:10px 0}.cp input{font-size:14px;padding:10px;text-transform:uppercase}.cp button{font:700 13px inherit;background:var(--ink);color:#fff;border:0;padding:0 14px;cursor:pointer;white-space:nowrap}
.cpm{font-size:12.5px;min-height:16px}.cpm.ok{color:var(--good)}.cpm.err{color:var(--red)}
.go{width:100%;font:700 16px inherit;background:var(--red);color:#fff;border:0;padding:16px;cursor:pointer;min-height:54px;margin-top:6px}.go:disabled{opacity:.5;cursor:not-allowed}
#pp{margin-top:8px;min-height:50px}.err{color:var(--red);font-size:13.5px;margin-top:8px;min-height:18px}
.trust{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.trust div{font-size:12px;color:var(--steel);border:1px solid var(--line);padding:8px;background:#fff}.trust b{display:block;color:var(--ink);font-size:12.5px}
.empty{text-align:center;padding:40px 10px}.empty a{display:inline-block;margin-top:12px;background:var(--ink);color:#fff;padding:12px 18px;text-decoration:none;font-weight:700}
.dom{font-size:13px;margin-top:10px;color:var(--steel)}
</style></head><body>
<header class="top"><div class="in"><a class="logo" id="home" href="/home">MAPDAL<em>SEOUL</em></a>
<div class="steps"><span>01 <b data-t="s1"></b></span><span>02 <b data-t="s2"></b></span><span>03 <span data-t="s3"></span></span></div><span class="sp"></span>
<nav class="langs" aria-label="Language"><a data-l="en" href="/en/checkout-global">EN</a><a data-l="ja" href="/ja/checkout-global">JA</a><a data-l="zh" href="/zh/checkout-global">中文</a><a data-l="ko" href="/checkout-global">KO</a></nav></div></header>
<main class="wrap" id="main">
<div id="left">
 <h1 data-t="h1"></h1><p class="lede" data-t="lede"></p>
 <form id="f" novalidate autocomplete="on">
 <div class="sec"><h2><span>01</span><b data-t="contact"></b></h2>
  <label class="f"><span data-t="email"></span><input id="email" type="email" autocomplete="email" inputmode="email" required></label>
  <label class="chk"><input type="checkbox" id="mkt"><span data-t="mkt"></span></label></div>
 <div class="sec"><h2><span>02</span><b data-t="ship"></b></h2><p class="h" data-t="ship_h"></p>
  <label class="f"><span data-t="country"></span><select id="country" autocomplete="country" required></select></label>
  <label class="f"><span data-t="name"></span><input id="name" autocomplete="name" required></label>
  <label class="f"><span data-t="a1"></span><input id="addr1" autocomplete="address-line1" required></label>
  <label class="f"><span data-t="a2"></span><input id="addr2" autocomplete="address-line2"></label>
  <div class="row"><label class="f"><span data-t="city"></span><input id="city" autocomplete="address-level2" required></label>
  <label class="f"><span data-t="state"></span><input id="state" autocomplete="address-level1"></label></div>
  <div class="row"><label class="f"><span data-t="zip"></span><input id="zip" autocomplete="postal-code" required></label>
  <label class="f"><span data-t="phone"></span><input id="phone" type="tel" autocomplete="tel" inputmode="tel" placeholder="+1 555 123 4567" required></label></div>
  <div class="note" id="eta"></div><div class="note" id="duty"></div>
  <p class="dom"><span data-t="dom"></span> <a id="domLink" href="/checkout?dom=1" data-t="dom_a"></a></p></div>
 <div class="sec"><h2><span>03</span><b data-t="pay"></b></h2>
  <label class="chk"><input type="checkbox" id="agree"><span data-t="agree"></span></label>
  <div id="payArea"></div><div class="err" id="err" role="alert"></div></div>
 </form>
</div>
<aside class="sum"><div class="sec"><h2><b data-t="summary"></b></h2><div id="items"></div>
 <div class="cp"><input id="cpIn" aria-label="Coupon code"><button type="button" id="cpBtn" data-t="apply"></button></div><div class="cpm" id="cpMsg" aria-live="polite"></div>
 <div class="ln"><span data-t="sub"></span><span id="vSub">—</span></div>
 <div class="ln"><span data-t="shipfee"></span><span id="vShip">—</span></div>
 <div class="ln" id="lDuty" hidden><span data-t="duties"></span><span id="vDuty">—</span></div>
 <div class="ln" id="lOff" hidden><span data-t="disc"></span><span id="vOff">—</span></div>
 <div class="ln tot"><span data-t="total"></span><span id="vTot">—</span></div><div class="usd" id="vUsd"></div>
 <div class="trust"><div><b data-t="t1"></b><span data-t="t1p"></span></div><div><b data-t="t2"></b><span data-t="t2p"></span></div>
 <div><b data-t="t3"></b><span data-t="t3p"></span></div><div><b data-t="t4"></b><span data-t="t4p"></span></div></div></div></aside>
</main>
<script>
(function(){
var C=__CFG__,L=C.lang,P=C.prefix,CK='mapdal_cart';
var T={
en:{s1:'Shipping',s2:'Payment',s3:'Done',h1:'International checkout',lede:'Shipping from Seongsu, Seoul to your door. Prices are in Korean won (KRW); you pay the US-dollar amount shown.',
 contact:'Contact',email:'Email (order updates)',mkt:'Email me new drops and member-only offers. Unsubscribe anytime.',ship:'Shipping address',ship_h:'Please write the address in English (Latin letters).',
 country:'Country / region',name:'Full name',a1:'Address line 1',a2:'Apartment, suite, building (optional)',city:'City',state:'State / province / prefecture',zip:'Postal code',phone:'Phone (with country code)',
 dom:'Shipping to an address in Korea?',dom_a:'Use domestic checkout',pay:'Payment',agree:'I agree to the Terms of purchase and Privacy policy, and understand import duties may apply.',
 summary:'Order summary',apply:'Apply',sub:'Subtotal',shipfee:'International shipping',duties:'Duties & taxes (DDP)',disc:'Coupon',total:'Total',
 t1:'Official albums',t1p:'Counted toward Hanteo charts',t2:'Ships from Seoul',t2p:'Tracked EMS / express',t3:'Secure payment',t3p:'PayPal & major cards',t4:'Easy returns',t4p:'Within 7 days of delivery',
 eta:['Estimated delivery: ','3–6','5–8','6–10','8–14',' business days after dispatch (tracked).'],dap:'Import duties and taxes, if any, are set by your country’s customs and paid on delivery.',ddp:'Duties & taxes are prepaid at checkout (DDP) — nothing to pay on delivery.',
 blocked:'Ships within Korea only (frozen/fresh or event item)',remove:'Remove',empty:'Your cart is empty.',shop:'Continue shopping',payusd:'You will be charged ',payusd2:' via PayPal (approx., incl. conversion).',
 place:'Place order — get a payment link',inv_done:'Order received! We will email a secure payment link within 24 hours (KST).',need:'Please complete the highlighted fields.',
 agree_need:'Please agree to the terms.',cp_ok:'Coupon applied: ',noitems:'Remove the Korea-only items to continue.',free:'Free',choose:'Select a country'},
ja:{s1:'お届け先',s2:'お支払い',s3:'完了',h1:'海外配送チェックアウト',lede:'ソウル・聖水からご自宅へお届けします。価格は韓国ウォン（KRW）表示、お支払いは表示の米ドル金額です。',
 contact:'連絡先',email:'メールアドレス（注文のご案内）',mkt:'新作ドロップや会員限定のお知らせをメールで受け取る（いつでも配信停止可）',ship:'お届け先住所',ship_h:'住所はローマ字（英語表記）でご入力ください。',
 country:'国・地域',name:'氏名（ローマ字）',a1:'住所1（番地・町名）',a2:'建物名・部屋番号（任意）',city:'市区町村',state:'都道府県',zip:'郵便番号',phone:'電話番号（国番号から）',
 dom:'韓国国内の住所にお届けですか？',dom_a:'国内チェックアウトへ',pay:'お支払い',agree:'購入規約・プライバシーポリシーに同意し、輸入関税が発生する場合があることを了承します。',
 summary:'ご注文内容',apply:'適用',sub:'小計',shipfee:'海外送料',duties:'関税・税（DDP）',disc:'クーポン',total:'合計',
 t1:'公式アルバム',t1p:'Hanteoチャートに反映',t2:'ソウルから発送',t2p:'追跡可能なEMS',t3:'安全なお支払い',t3p:'PayPal・主要カード',t4:'返品も安心',t4p:'到着後7日以内',
 eta:['お届け目安：発送後 ','3〜6','5〜8','6〜10','8〜14',' 営業日（追跡可能）'],dap:'輸入関税・税金が発生する場合は、受取時にお客様のご負担となります。',ddp:'関税・税はお支払い時に前払い（DDP）— 受取時の追加負担はありません。',
 blocked:'韓国国内配送のみ（冷凍・生鮮またはイベント商品）',remove:'削除',empty:'カートは空です。',shop:'お買い物を続ける',payusd:'PayPalで ',payusd2:' をお支払いいただきます（換算・概算）。',
 place:'注文する — 決済リンクを受け取る',inv_done:'ご注文を受け付けました。24時間以内（韓国時間）に決済リンクをメールでお送りします。',need:'未入力の項目をご確認ください。',
 agree_need:'規約に同意してください。',cp_ok:'クーポン適用：',noitems:'国内配送のみの商品を削除してください。',free:'無料',choose:'国を選択'},
zh:{s1:'收货信息',s2:'付款',s3:'完成',h1:'国际订单结账',lede:'从首尔圣水直送到您家。价格以韩元（KRW）显示，实际以显示的美元金额支付。',
 contact:'联系方式',email:'电子邮箱（订单通知）',mkt:'通过邮件接收新品与会员专属优惠（可随时退订）',ship:'收货地址',ship_h:'请使用英文（拼音）填写地址。',
 country:'国家/地区',name:'收件人姓名（拼音）',a1:'地址第1行（街道门牌）',a2:'公寓、楼层、单元（选填）',city:'城市',state:'省/州',zip:'邮政编码',phone:'电话（含国家区号）',
 dom:'寄往韩国境内地址？',dom_a:'使用国内结账',pay:'付款',agree:'我同意购买条款和隐私政策，并了解可能产生进口关税。',
 summary:'订单摘要',apply:'使用',sub:'商品小计',shipfee:'国际运费',duties:'关税及税费（DDP）',disc:'优惠券',total:'合计',
 t1:'官方专辑',t1p:'计入 Hanteo 榜单',t2:'首尔发货',t2p:'可追踪 EMS',t3:'安全支付',t3p:'PayPal 及主流银行卡',t4:'无忧退货',t4p:'签收后7天内',
 eta:['预计送达：发货后 ','3–6','5–8','6–10','8–14',' 个工作日（可追踪）。'],dap:'如产生进口关税及税费，由您所在国家海关核定并于收货时支付。',ddp:'关税及税费已于结账时预付（DDP），收货时无需另付。',
 blocked:'仅限韩国境内配送（冷冻/生鲜或活动商品）',remove:'移除',empty:'购物车是空的。',shop:'继续购物',payusd:'将通过 PayPal 支付 ',payusd2:'（含换汇，约数）。',
 place:'提交订单 — 获取付款链接',inv_done:'订单已收到！我们将在24小时内（韩国时间）发送安全付款链接。',need:'请填写标记的项目。',
 agree_need:'请同意条款。',cp_ok:'已使用优惠券：',noitems:'请先移除仅限韩国配送的商品。',free:'免费',choose:'选择国家'},
ko:{s1:'배송 정보',s2:'결제',s3:'완료',h1:'해외 배송 주문',lede:'성수에서 전 세계로 보내드립니다. 상품가는 원화(KRW) 기준이며 결제는 표시된 미국 달러 금액으로 진행됩니다.',
 contact:'연락처',email:'이메일 (주문 안내)',mkt:'신상 드롭·회원 혜택 소식을 이메일로 받겠습니다 (언제든 수신거부 가능)',ship:'해외 배송지',ship_h:'주소는 영문으로 입력해 주세요.',
 country:'국가/지역',name:'받는 분 이름 (영문)',a1:'주소 1 (도로명·번지)',a2:'상세 주소 (선택)',city:'도시',state:'주/도',zip:'우편번호',phone:'전화번호 (국가번호 포함)',
 dom:'국내 주소로 받으시나요?',dom_a:'국내 주문하기',pay:'결제',agree:'구매 약관·개인정보처리방침에 동의하며, 수입 관세가 부과될 수 있음을 확인합니다.',
 summary:'주문 요약',apply:'적용',sub:'상품 금액',shipfee:'해외 배송비',duties:'관세·세금 (DDP)',disc:'쿠폰 할인',total:'합계',
 t1:'공식 앨범',t1p:'한터차트 집계',t2:'서울 직배송',t2p:'추적 가능한 EMS',t3:'안전 결제',t3p:'PayPal·해외카드',t4:'반품 안내',t4p:'수령 후 7일 이내',
 eta:['예상 배송: 출고 후 ','3–6','5–8','6–10','8–14',' 영업일 (추적 가능)'],dap:'관세·부가세가 발생하면 수령 시 현지 세관 기준으로 고객님이 납부합니다.',ddp:'관세·세금이 결제 시 선지불(DDP)되어 수령 시 추가 비용이 없습니다.',
 blocked:'국내 배송 전용 (냉동·신선 또는 이벤트 상품)',remove:'삭제',empty:'장바구니가 비어 있습니다.',shop:'쇼핑 계속하기',payusd:'PayPal로 ',payusd2:'가 결제됩니다 (환산 근사값).',
 place:'주문하기 — 결제 링크 받기',inv_done:'주문이 접수되었습니다. 24시간 내에 결제 링크를 이메일로 보내드립니다.',need:'표시된 항목을 입력해 주세요.',
 agree_need:'약관에 동의해 주세요.',cp_ok:'쿠폰 적용: ',noitems:'국내 전용 상품을 삭제해 주세요.',free:'무료',choose:'국가 선택'}};
var t=T[L]||T.en;document.querySelectorAll('[data-t]').forEach(function(e){var k=e.getAttribute('data-t');if(t[k]!=null)e.textContent=t[k]});
document.querySelectorAll('.langs a').forEach(function(a){a.classList.toggle('on',a.getAttribute('data-l')===L);a.addEventListener('click',function(){document.cookie='mp_lang='+a.getAttribute('data-l')+';path=/;max-age=31536000;samesite=lax'})});
document.getElementById('home').href=P+'/home';document.getElementById('domLink').href='/checkout?dom=1';
var $=function(i){return document.getElementById(i)},won=function(n){return'₩'+Math.round(n).toLocaleString('en-US')};
var items=[];try{items=JSON.parse(localStorage.getItem(CK)||'[]')||[]}catch(e){items=[]}
function save(){try{localStorage.setItem(CK,JSON.stringify(items))}catch(e){}}
var sel=$('country');sel.innerHTML='<option value="">'+t.choose+'</option>'+C.countries.map(function(c){return'<option value="'+c[0]+'">'+c[1]+'</option>'}).join('');
var tz='';try{tz=Intl.DateTimeFormat().resolvedOptions().timeZone||''}catch(e){}
var G={'Asia/Tokyo':'JP','Asia/Shanghai':'CN','Asia/Taipei':'TW','Asia/Hong_Kong':'HK','Asia/Singapore':'SG','Asia/Bangkok':'TH','Asia/Ho_Chi_Minh':'VN','Asia/Manila':'PH','Asia/Kuala_Lumpur':'MY','Asia/Jakarta':'ID','Australia/Sydney':'AU','Australia/Melbourne':'AU','Europe/London':'GB','Europe/Paris':'FR','Europe/Berlin':'DE','Europe/Madrid':'ES','Europe/Rome':'IT','America/Toronto':'CA','America/Vancouver':'CA','America/Mexico_City':'MX','America/Sao_Paulo':'BR'}[tz]||(/^America\/(New_York|Chicago|Denver|Los_Angeles|Phoenix|Anchorage)|^Pacific\/Honolulu/.test(tz)?'US':'');
if(G)sel.value=G;
var Q=null,coupon='';
function paintItems(){var bl={};(Q&&Q.blocked||[]).forEach(function(b){bl[b.id]=1});
 if(!items.length){$('main').innerHTML='<div class="sec empty" style="grid-column:1/-1"><p>'+t.empty+'</p><a href="'+P+'/shop">'+t.shop+'</a></div>';return}
 $('items').innerHTML=items.map(function(i,ix){var bad=false;for(var k in bl){if(String(i.id).split('::')[0].replace('.html','')===String(k).split('::')[0].replace('.html',''))bad=true}
  var nm=String(i.n||i.id).replace(/[<>&]/g,'');
  return'<div class="it'+(bad?' bad':'')+'"><div>'+nm+'<div class="q">'+won(i.p)+' × '+i.q+'</div></div><div>'+won(i.p*i.q)+'<br><button type="button" data-rm="'+ix+'">'+t.remove+'</button></div>'+(bad?'<div class="why">'+t.blocked+'</div>':'')+'</div>'}).join('')}
$('items').addEventListener('click',function(e){var ix=e.target.getAttribute&&e.target.getAttribute('data-rm');if(ix!=null){items.splice(+ix,1);save();refresh()}});
function paintTotals(){if(!Q)return;$('vSub').textContent=won(Q.sub);$('vShip').textContent=Q.ship?won(Q.ship):t.free;
 $('lDuty').hidden=!Q.duties;$('vDuty').textContent=won(Q.duties);$('lOff').hidden=!Q.discount;$('vOff').textContent='−'+won(Q.discount);
 $('vTot').textContent=won(Q.total);$('vUsd').textContent='≈ US$'+Q.usd.toFixed(2);
 var e=t.eta;$('eta').textContent=e[0]+e[Q.zone]+e[5];$('duty').textContent=Q.ddp?t.ddp:t.dap;pay()}
var timer=null;
function refresh(){paintItems();if(!items.length)return;clearTimeout(timer);timer=setTimeout(function(){
 if(!sel.value){$('vShip').textContent='—';$('eta').textContent='';$('duty').textContent=t.dap;return}
 fetch('/api/intl/quote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items.map(function(i){return{id:i.id,q:i.q}}),country:sel.value,coupon:coupon,email:$('email').value})})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'Error');return d})})
 .then(function(d){Q=d;$('err').textContent=d.blocked.length?t.noitems:'';paintItems();paintTotals()})
 .catch(function(x){$('err').textContent=x.message;if(coupon){coupon='';$('cpMsg').className='cpm err';$('cpMsg').textContent=x.message}})},250)}
sel.addEventListener('change',function(){refresh();try{mpTrack&&mpTrack('add_shipping_info',{})}catch(e){}});
$('cpBtn').addEventListener('click',function(){coupon=$('cpIn').value.trim().toUpperCase();if(!coupon)return;
 if(!sel.value){$('cpMsg').className='cpm err';$('cpMsg').textContent=t.choose;return}
 fetch('/api/intl/quote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items.map(function(i){return{id:i.id,q:i.q}}),country:sel.value,coupon:coupon,email:$('email').value})})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'Error');return d})})
 .then(function(d){Q=d;$('cpMsg').className='cpm ok';$('cpMsg').textContent=t.cp_ok+d.coupon+' (−'+won(d.discount)+')';paintTotals()})
 .catch(function(x){coupon='';$('cpMsg').className='cpm err';$('cpMsg').textContent=x.message})});
var F=['email','name','addr1','city','zip','phone'];
function valid(){var ok=true;F.forEach(function(k){var v=$(k).value.trim(),bad=!v||(k==='email'&&!/^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$/.test(v));$(k).setAttribute('aria-invalid',bad?'true':'false');if(bad)ok=false});
 if(!sel.value){sel.setAttribute('aria-invalid','true');ok=false}if(!ok){$('err').textContent=t.need;return false}
 if(!$('agree').checked){$('err').textContent=t.agree_need;return false}if(Q&&Q.blocked.length){$('err').textContent=t.noitems;return false}$('err').textContent='';return true}
function body(){return{items:items.map(function(i){return{id:i.id,q:i.q}}),coupon:coupon,agree:$('agree').checked,marketing:$('mkt').checked,lang:L,viewCurrency:'USD',
 buyer:{email:$('email').value.trim(),name:$('name').value.trim(),addr1:$('addr1').value.trim(),addr2:$('addr2').value.trim(),city:$('city').value.trim(),state:$('state').value.trim(),zip:$('zip').value.trim(),phone:$('phone').value.trim(),country:sel.value}}}
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'Error');return d})})}
function done(oid){try{localStorage.setItem(CK,'[]')}catch(e){}location.href=P+'/order-complete?oid='+encodeURIComponent(oid)}
function ga(n){try{var its=items.map(function(i){return{item_id:String(i.id),item_name:String(i.n||''),price:i.p,quantity:i.q}});gtag('event',n,{currency:'KRW',value:Q?Q.total:0,items:its,payment_type:C.paypal?'PayPal':'Invoice'})}catch(e){}}
var ppReady=false;
function pay(){var A=$('payArea');if(!Q||!items.length)return;
 if(C.paypal){if(ppReady){$('ppNote').textContent=t.payusd+'US$'+Q.usd.toFixed(2)+t.payusd2;return}ppReady=true;
  A.innerHTML='<p class="h" id="ppNote" style="font-size:13px;color:#5E5D57">'+t.payusd+'US$'+Q.usd.toFixed(2)+t.payusd2+'</p><div id="pp"></div>';
  var s=document.createElement('script');s.src='https://www.paypal.com/sdk/js?client-id='+encodeURIComponent(C.ppClient)+'&currency=USD&intent=capture&components=buttons&locale='+({ja:'ja_JP',zh:'zh_CN',ko:'ko_KR'}[L]||'en_US');
  s.onload=function(){var oid='';paypal.Buttons({style:{layout:'vertical',color:'black',shape:'rect',label:'pay'},
   onClick:function(d,actions){if(!valid())return actions.reject();ga('add_payment_info');return actions.resolve()},
   createOrder:function(){return post('/api/intl/orders',body()).then(function(d){oid=d.orderId;return d.paypalOrderId})},
   onApprove:function(){return post('/api/intl/capture',{orderId:oid}).then(function(){done(oid)}).catch(function(x){$('err').textContent=x.message})},
   onError:function(e){$('err').textContent=(e&&e.message)||'PayPal error'}}).render('#pp')};document.head.appendChild(s)}
 else if(!A.firstChild){A.innerHTML='<button type="button" class="go" id="place">'+t.place+'</button>';
  $('place').addEventListener('click',function(){if(!valid())return;var b=this;b.disabled=true;ga('add_payment_info');
   post('/api/intl/orders',body()).then(function(d){$('err').style.color='#0A7D38';$('err').textContent=t.inv_done;setTimeout(function(){done(d.orderId)},1600)})
   .catch(function(x){$('err').style.color='';$('err').textContent=x.message;b.disabled=false})})}}
refresh();if(items.length)ga('begin_checkout');
fetch('/api/member/me').then(function(r){return r.json()}).then(function(m){if(m&&m.email&&!$('email').value)$('email').value=m.email;if(m&&m.name&&!$('name').value)$('name').value=m.name}).catch(function(){});
})();
</script></body></html>'''


# ═══════════════════════════ /track — 비회원·해외 주문 조회 ═════════════════
#   주문번호 + 주문 이메일 일치 시 상태·운송장 공개. 메일의 서명 링크(oid+t)는 입력 없이 바로 열린다.
#   성공하면 주문 조회 쿠키(mp_ov)를 붙여 주문완료 화면·리뷰 작성 흐름과도 이어진다.
import hmac as _hmac, hashlib as _hashlib
_TRK_RATE = {}


def track_token(oid, email):
    key = _hashlib.sha256(('mp-track:' + (_env('GROWTH_SECRET') or _env('ADMIN_TOKEN') or _env('DATABASE_URL') or 'local')).encode()).digest()
    return _hmac.new(key, ('%s|%s' % (oid, str(email or '').strip().lower())).encode(), _hashlib.sha256).hexdigest()[:20]


def track_link(oid, email):
    return '%s/track?oid=%s&t=%s' % (_g()._site(), urllib.parse.quote(oid), track_token(oid, email))


def _track_payload(o):
    av = _av()
    buyer = json.loads(o.get('buyer') or '{}')
    items = json.loads(o.get('items') or '[]')
    co, trk = (o.get('courier') or ''), (o.get('tracking') or '')
    url = ''
    try:
        url = av.track_url(co, trk) if trk else ''
    except Exception:
        url = ''
    if trk and (o.get('ship_method') == 'intl' or not url):
        url = 'https://t.17track.net/en#nums=' + urllib.parse.quote(re.sub(r'[^0-9A-Za-z]', '', trk))
    try:
        cname = av.courier_name(co) if co else ''
    except Exception:
        cname = co
    st, ff = o.get('status') or '', (o.get('fulfill') or 'NEW')
    step = 0
    if st in ('PAID',):
        step = 1
        if ff == 'PREPARING': step = 2
        if ff == 'SHIPPED': step = 3
        if ff == 'DONE': step = 4
    return {'oid': o['order_id'], 'created': str(o.get('created') or '')[:16].replace('T', ' '), 'status': st,
            'fulfill': ff, 'step': step, 'amount': int(o.get('amount') or 0), 'ship': o.get('ship_method') or '',
            'courier': cname, 'tracking': trk, 'url': url, 'country': buyer.get('country_name') or buyer.get('country') or '',
            'items': [{'n': i.get('n'), 'q': i.get('q')} for i in items[:20]],
            'cancelled': st == 'CANCELLED' or ff == 'CANCELLED'}


@global_router.post('/api/track')
async def api_track(req: Request, response: Response):
    a = _app()
    ip = (req.headers.get('x-forwarded-for') or '').split(',')[0].strip() or (req.client.host if req.client else '')
    now = time.time()
    b = _TRK_RATE.get(ip)
    if not b or now - b[0] > 600:
        b = [now, 0]
    b[1] += 1
    _TRK_RATE[ip] = b
    if len(_TRK_RATE) > 5000:
        _TRK_RATE.clear()
    if b[1] > 20:
        raise HTTPException(429, 'Too many attempts — please try again in a few minutes')
    d = await req.json()
    oid = re.sub(r'[^A-Za-z0-9-]', '', str(d.get('oid') or '')).upper()[:30]
    email = str(d.get('email') or '').strip().lower()
    tok = str(d.get('t') or '')
    with a.db() as c:
        cols = 'order_id,created,status,amount,buyer,items,ship_method' + (',fulfill,tracking,courier' if a._has_ship_cols() else '')
        o = c.one('SELECT %s FROM orders WHERE order_id=?' % cols, (oid,))
    if not o:
        raise HTTPException(404, 'We couldn’t find an order with these details')
    bem = str(json.loads(o.get('buyer') or '{}').get('email') or '').strip().lower()
    ok = (bool(email) and _hmac.compare_digest(email, bem)) or (bool(tok) and bem and _hmac.compare_digest(tok, track_token(oid, bem)))
    if not ok:
        raise HTTPException(404, 'We couldn’t find an order with these details')
    try:
        response.set_cookie(a._OV_COOKIE, a._ov_cookie_value(req, oid), max_age=90 * 86400, httponly=True,
                            samesite='lax', secure=a.SITE_ORIGIN.startswith('https'))
    except Exception:
        pass
    return _track_payload(o)


@global_router.get('/track', response_class=HTMLResponse)
def track_page(request: Request):
    lang = _lang(request)
    if not request.cookies.get('mp_lang') and not getattr(request.state, 'lang', None):
        lang = 'ko' if (request.headers.get('accept-language') or 'ko').lower().startswith('ko') else lang
    html = _TRACK_HTML.replace('__LANG__', lang if lang != 'zh' else 'zh-Hans').replace('__L__', json.dumps(lang))
    g = _g()
    html = g.head_apply(html)
    add = ''
    try:
        add += _av()._analytics_snippet()
    except Exception:
        pass
    add += g._body_js()
    html = html.replace('</body>', add + '</body>', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})


_TRACK_HTML = r'''<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Order tracking — MAPDAL SEOUL</title><meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>:root{--ink:#141414;--red:#DC2B24;--line:#E2E0D9;--steel:#5E5D57;--paper:#F7F6F2}*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "IBM Plex Sans KR",-apple-system,"Hiragino Sans","PingFang SC",sans-serif}
.top{background:var(--ink);border-bottom:4px solid var(--red)}.top a{display:inline-block;padding:14px 16px;font-family:"Black Han Sans",sans-serif;font-size:22px;color:#fff;text-decoration:none}.top em{font-style:normal;color:#EE3532}
.w{max-width:620px;margin:0 auto;padding:28px 16px 60px}h1{font-family:"Black Han Sans",sans-serif;font-weight:400;font-size:30px;margin:0 0 6px}
.card{background:#fff;border:1px solid var(--line);padding:20px;margin-top:16px}label{display:block;font-size:13px;font-weight:700;margin:10px 0 4px}
input{width:100%;font:inherit;font-size:16px;padding:12px;border:1px solid var(--line)}input:focus{outline:2px solid var(--ink);outline-offset:-1px}
button{width:100%;margin-top:14px;font:700 15px inherit;background:var(--red);color:#fff;border:0;padding:14px;cursor:pointer;min-height:50px}
.err{color:var(--red);font-size:13.5px;min-height:18px;margin-top:8px}.mono{font-family:"IBM Plex Mono",monospace}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin:16px 0}.steps div{text-align:center;font-size:12px;color:var(--steel);padding-top:10px;border-top:4px solid var(--line)}
.steps div.on{border-color:var(--red);color:var(--ink);font-weight:700}.kv{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.kv span:first-child{color:var(--steel)}.trk{display:inline-block;margin-top:12px;background:var(--ink);color:#fff;text-decoration:none;padding:12px 16px;font-weight:700}
.it{font-size:13.5px;padding:3px 0}.note{font-size:12.5px;color:var(--steel);margin-top:14px}</style></head><body>
<header class="top"><a id="home" href="/home">MAPDAL<em>SEOUL</em></a></header>
<main class="w"><h1 data-t="h"></h1><p data-t="p" style="color:var(--steel);margin:0"></p>
<form class="card" id="f" novalidate><label for="oid" data-t="oid"></label><input id="oid" class="mono" autocomplete="off" placeholder="MD-20261002-ABC123" required>
<label for="em" data-t="em"></label><input id="em" type="email" autocomplete="email" inputmode="email" required>
<button type="submit" data-t="go"></button><div class="err" id="err" role="alert"></div></form>
<section class="card" id="res" hidden aria-live="polite"></section>
<p class="note" data-t="help"></p></main>
<script>(function(){var L=__L__,P=(L==='ko'?'':'/'+L);
var T={ko:{h:'주문 조회',p:'주문번호와 주문 시 입력한 이메일로 배송 상황을 확인하세요.',oid:'주문번호',em:'주문 이메일',go:'조회하기',s:['결제 완료','상품 준비중','발송 완료','배송 완료'],
 ono:'주문번호',date:'주문일시',amt:'결제금액',stat:'상태',courier:'택배사',trk:'운송장',track:'배송 조회하기',pending:'결제 대기',cancel:'취소된 주문입니다',dest:'배송 국가',
 help:'문의: cx@mealzip.kr · 회원은 마이페이지에서 전체 주문 내역을 볼 수 있습니다.'},
en:{h:'Track your order',p:'Enter your order number and the email you used at checkout.',oid:'Order number',em:'Email',go:'Track order',s:['Paid','Packing','Shipped','Delivered'],
 ono:'Order no.',date:'Ordered',amt:'Total',stat:'Status',courier:'Carrier',trk:'Tracking no.',track:'Track parcel',pending:'Awaiting payment',cancel:'This order was cancelled',dest:'Destination',
 help:'Need help? cx@mealzip.kr (EN/JP/CN)'},
ja:{h:'注文状況の確認',p:'注文番号とご注文時のメールアドレスを入力してください。',oid:'注文番号',em:'メールアドレス',go:'確認する',s:['決済完了','準備中','発送済み','配達完了'],
 ono:'注文番号',date:'注文日時',amt:'お支払い金額',stat:'状況',courier:'配送業者',trk:'追跡番号',track:'配送状況を見る',pending:'お支払い待ち',cancel:'キャンセルされたご注文です',dest:'お届け先の国',
 help:'お問い合わせ：cx@mealzip.kr（日本語対応）'},
zh:{h:'订单查询',p:'请输入订单号和下单时使用的电子邮箱。',oid:'订单号',em:'电子邮箱',go:'查询',s:['已付款','备货中','已发货','已送达'],
 ono:'订单号',date:'下单时间',amt:'支付金额',stat:'状态',courier:'快递公司',trk:'运单号',track:'查看物流',pending:'待付款',cancel:'该订单已取消',dest:'收货国家',
 help:'咨询：cx@mealzip.kr（支持中文）'}};
var t=T[L]||T.en,$=function(i){return document.getElementById(i)};document.querySelectorAll('[data-t]').forEach(function(e){e.textContent=t[e.getAttribute('data-t')]});
$('home').href=P+'/home';
var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})};
function show(d){var r=$('res');r.hidden=false;var st=d.cancelled?t.cancel:(d.step?t.s[d.step-1]:t.pending);
 r.innerHTML=(d.cancelled?'':'<div class="steps">'+t.s.map(function(x,i){return'<div class="'+(d.step>i?'on':'')+'">'+x+'</div>'}).join('')+'</div>')
 +'<div class="kv"><span>'+t.ono+'</span><span class="mono">'+esc(d.oid)+'</span></div><div class="kv"><span>'+t.date+'</span><span>'+esc(d.created)+'</span></div>'
 +'<div class="kv"><span>'+t.amt+'</span><span>₩'+Number(d.amount).toLocaleString('en-US')+'</span></div><div class="kv"><span>'+t.stat+'</span><b>'+esc(st)+'</b></div>'
 +(d.country?'<div class="kv"><span>'+t.dest+'</span><span>'+esc(d.country)+'</span></div>':'')
 +(d.tracking?'<div class="kv"><span>'+t.courier+'</span><span>'+esc(d.courier)+'</span></div><div class="kv"><span>'+t.trk+'</span><span class="mono">'+esc(d.tracking)+'</span></div>':'')
 +'<div style="margin-top:10px">'+d.items.map(function(i){return'<div class="it">· '+esc(i.n)+' × '+(i.q||1)+'</div>'}).join('')+'</div>'
 +(d.url?'<a class="trk" href="'+esc(d.url)+'" target="_blank" rel="noopener">'+t.track+' →</a>':'');
 try{window.mpTrack&&mpTrack('share',{method:'track_order'})}catch(e){}}
function go(b){$('err').textContent='';fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||'Error');return d})}).then(show).catch(function(x){$('err').textContent=x.message})}
$('f').addEventListener('submit',function(e){e.preventDefault();var o=$('oid').value.trim(),m=$('em').value.trim();if(!o||!m)return;go({oid:o,email:m})});
var q=new URLSearchParams(location.search);if(q.get('oid')){$('oid').value=q.get('oid');if(q.get('t'))go({oid:q.get('oid'),t:q.get('t')})}
})();</script></body></html>'''
