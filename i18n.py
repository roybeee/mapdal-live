"""i18n.py — MAPDAL SEOUL 다국어 레이어 (EN · JA · ZH)

원칙
  · 원본(한국어) 페이지·관리자 편집본은 그대로 두고, 서빙 직전에 사전(i18n/<lang>.json)으로 치환한다.
    → 관리자가 한국어로 고친 문구도 사전에 있으면 즉시 번역되고, 없으면 한국어로 남는다(깨지지 않음).
  · URL: /en/… · /ja/… · /zh/… (검색엔진용 hreflang·canonical) + 쿠키 mp_lang(접두사 없는 이동 시 유지)
  · 서버 치환 범위: 텍스트 노드 · 표시 속성(placeholder·alt·title·aria-label·meta content·value)
                    · <script> 안 문자열 리터럴('…' "…" `…` 정적 조각)
  · 동적 렌더(장바구니·API 응답·alert)는 브라우저 런타임(MutationObserver)이 같은 사전으로 치환
  · 결제는 KRW 고정. 표시 통화(USD·JPY·CNY·EUR·TWD)는 참고 금액으로 병기(≈)
"""
import os, re, json, threading

LANGS = ('en', 'ja', 'zh')
LANG_NAMES = {'ko': '한국어', 'en': 'English', 'ja': '日本語', 'zh': '中文'}
HTML_LANG = {'ko': 'ko', 'en': 'en', 'ja': 'ja', 'zh': 'zh-Hans'}
DEFAULT_CUR = {'ko': 'KRW', 'en': 'USD', 'ja': 'JPY', 'zh': 'CNY'}

_BASE = os.path.dirname(os.path.abspath(__file__))
_DIR = os.path.join(_BASE, 'i18n')
_HANGUL = re.compile(r'[가-힣]')
_WS = re.compile(r'\s+')
_LOCK = threading.Lock()
_CACHE = {}


def norm(s):
    return _WS.sub(' ', s or '').strip()


def load(lang):
    """사전 로드(프로세스 캐시). 파일 수정 시각이 바뀌면 다시 읽는다."""
    if lang not in LANGS:
        return {}
    fp = os.path.join(_DIR, lang + '.json')
    try:
        mt = os.path.getmtime(fp)
    except OSError:
        return {}
    c = _CACHE.get(lang)
    if c and c[0] == mt:
        return c[1]
    with _LOCK:
        try:
            d = json.load(open(fp, encoding='utf-8'))
            d = {norm(k): v for k, v in d.items() if isinstance(v, str) and v.strip()}
        except Exception:
            d = {}
        _CACHE[lang] = (mt, d)
        _CACHE.pop(lang + ':ver', None)
    return d


def version(lang):
    """런타임 사전 캐시 버스터."""
    k = lang + ':ver'
    if k not in _CACHE:
        d = load(lang)
        import hashlib
        _CACHE[k] = hashlib.md5(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
    return _CACHE[k]


# ── HTML 토크나이저 ──────────────────────────────────────────────────────
_TOKEN = re.compile(
    r'(<!--.*?-->)'                                   # 1 주석
    r'|(<script\b[^>]*>)(.*?)(</script\s*>)'          # 2,3,4 스크립트
    r'|(<style\b[^>]*>.*?</style\s*>)'                # 5 스타일
    r'|(<textarea\b[^>]*>)(.*?)(</textarea\s*>)'      # 6,7,8 textarea (내용 보존)
    r'|(<[A-Za-z!/][^>]*>)',                          # 9 일반 태그
    re.S | re.I)
_ATTR = re.compile(r'''(\s(?:placeholder|alt|title|aria-label|content|value|data-label|label)\s*=\s*)(["'])(.*?)\2''', re.S | re.I)
_SCRIPT_TYPE = re.compile(r'type\s*=\s*["\']?([^"\'\s>]+)', re.I)


def _tr_text(s, d):
    """텍스트 조각 치환 — 앞뒤 공백 보존, 정규화 키 정확 일치."""
    if not _HANGUL.search(s):
        return s
    k = norm(s)
    v = d.get(k)
    if v is None:
        return s
    lead = s[:len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return lead + v + trail


def _tr_attr_tag(tag, d):
    if not _HANGUL.search(tag):
        return tag

    def rep(m):
        val = m.group(3)
        if not _HANGUL.search(val):
            return m.group(0)
        k = norm(_unent(val))
        v = d.get(k)
        if v is None:
            return m.group(0)
        q = m.group(2)
        v = v.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        v = v.replace('"', '&quot;') if q == '"' else v.replace("'", '&#39;')
        return m.group(1) + q + v + q
    return _ATTR.sub(rep, tag)


def _unent(s):
    return s.replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'").replace('&lt;', '<').replace('&gt;', '>')


def _html_text_tr(s, d):
    """텍스트 노드(엔티티 포함) 치환 — 사전 키는 엔티티 해제 기준."""
    if not _HANGUL.search(s):
        return s
    k = norm(_unent(s))
    v = d.get(k)
    if v is None:
        return s
    lead = s[:len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return lead + v.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + trail


# ── JS 문자열 리터럴 토크나이저 ──────────────────────────────────────────
def js_literals(src):
    """(start, end, quote, body) 목록 — 정규식 리터럴·주석은 건너뛴다(근사).
    템플릿 리터럴은 ${…} 사이의 정적 조각만 돌려준다."""
    out = []
    i, n = 0, len(src)
    prev_sig = ''
    while i < n:
        ch = src[i]
        if ch == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            i = n if j < 0 else j
            continue
        if ch == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == '/' and (prev_sig == '' or prev_sig in '(,=:[!&|?{};+-*%<>~^'):
            # 정규식 리터럴
            j = i + 1
            inclass = False
            while j < n:
                c = src[j]
                if c == '\\':
                    j += 2
                    continue
                if c == '[':
                    inclass = True
                elif c == ']':
                    inclass = False
                elif c == '/' and not inclass:
                    break
                elif c == '\n':
                    break
                j += 1
            i = j + 1
            prev_sig = 'r'
            continue
        if ch in ('"', "'"):
            j = i + 1
            while j < n:
                c = src[j]
                if c == '\\':
                    j += 2
                    continue
                if c == ch or c == '\n':
                    break
                j += 1
            out.append((i + 1, j, ch, src[i + 1:j]))
            i = j + 1
            prev_sig = 's'
            continue
        if ch == '`':
            j = i + 1
            seg = j
            while j < n:
                c = src[j]
                if c == '\\':
                    j += 2
                    continue
                if c == '$' and j + 1 < n and src[j + 1] == '{':
                    out.append((seg, j, '`', src[seg:j]))
                    depth, j = 1, j + 2
                    while j < n and depth:
                        if src[j] == '{':
                            depth += 1
                        elif src[j] == '}':
                            depth -= 1
                        j += 1
                    seg = j
                    continue
                if c == '`':
                    break
                j += 1
            out.append((seg, j, '`', src[seg:j]))
            i = j + 1
            prev_sig = 's'
            continue
        if not ch.isspace():
            prev_sig = ch if not (ch.isalnum() or ch in '_$') else 'a'
        i += 1
    return out


def _js_unescape(s):
    if '\\' not in s:
        return s
    try:
        return json.loads('"' + s.replace('"', '\\"').replace("\\'", "'") + '"')
    except Exception:
        return s


def _js_escape(v, q):
    v = v.replace('\\', '\\\\').replace('\n', '\\n')
    if q == '`':
        return v.replace('`', '\\`').replace('${', '\\${')
    return v.replace(q, '\\' + q)


def tr_script(src, d):
    if not _HANGUL.search(src):
        return src
    lits = js_literals(src)
    if not lits:
        return src
    out, pos = [], 0
    for a, b, q, body in lits:
        if not _HANGUL.search(body):
            continue
        raw = _js_unescape(body)
        k = norm(raw)
        v = d.get(k)
        if v is None:
            continue
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        out.append(src[pos:a])
        out.append(_js_escape(lead + v + trail, q))
        pos = b
    if not out:
        return src
    out.append(src[pos:])
    return ''.join(out)


def translate_html(html, lang):
    d = load(lang)
    if not d or not isinstance(html, str):
        return html
    out, pos = [], 0
    for m in _TOKEN.finditer(html):
        if m.start() > pos:
            out.append(_html_text_tr(html[pos:m.start()], d))
        if m.group(1):
            out.append(m.group(1))
        elif m.group(2):
            t = _SCRIPT_TYPE.search(m.group(2))
            typ = (t.group(1).lower() if t else 'text/javascript')
            body = m.group(3)
            if 'json' in typ and 'ld+json' not in typ:
                body = tr_script(body, d)       # 데이터 블록(JSON) — 문자열만 치환
            elif 'ld+json' in typ:
                pass                            # 구조화 데이터는 원문 유지(상품명 일관성)
            else:
                body = tr_script(body, d)
            out.append(m.group(2) + body + m.group(4))
        elif m.group(5):
            out.append(m.group(5))
        elif m.group(6):
            out.append(_tr_attr_tag(m.group(6), d) + m.group(7) + m.group(8))
        else:
            out.append(_tr_attr_tag(m.group(9), d))
        pos = m.end()
    if pos < len(html):
        out.append(_html_text_tr(html[pos:], d))
    return ''.join(out)


# ── 사전 키 추출 (번역 작업용) ────────────────────────────────────────────
def extract(html):
    """페이지에서 번역 대상 한국어 조각을 등장 순서대로 추출."""
    keys = []
    seen = set()

    def add(s):
        k = norm(s)
        if k and _HANGUL.search(k) and k not in seen and len(k) <= 600:
            seen.add(k)
            keys.append(k)
    pos = 0
    for m in _TOKEN.finditer(html):
        if m.start() > pos:
            add(_unent(html[pos:m.start()]))
        if m.group(2):
            t = _SCRIPT_TYPE.search(m.group(2))
            typ = (t.group(1).lower() if t else 'text/javascript')
            if 'ld+json' not in typ:
                for a, b, q, body in js_literals(m.group(3)):
                    if _HANGUL.search(body) and '<' not in body[:1]:
                        raw = _js_unescape(body)
                        # HTML 조각이 든 리터럴은 태그 사이 텍스트를 따로 뽑는다
                        if '<' in raw and '>' in raw:
                            for part in re.split(r'<[^>]*>', raw):
                                add(_unent(part))
                        else:
                            add(raw)
                    elif _HANGUL.search(body):
                        for part in re.split(r'<[^>]*>', _js_unescape(body)):
                            add(_unent(part))
        elif m.group(9) or m.group(6):
            tag = m.group(9) or m.group(6)
            for am in _ATTR.finditer(tag):
                add(_unent(am.group(3)))
        pos = m.end()
    if pos < len(html):
        add(_unent(html[pos:]))
    return keys


# ── 런타임(브라우저) 스크립트 ────────────────────────────────────────────
def runtime_js(lang):
    """동적 DOM·alert/confirm 번역 + 언어/통화 스위처 + 참고 통화 병기."""
    if lang not in LANGS:
        lang_js = 'ko'
    else:
        lang_js = lang
    return ('<script id="mpI18nRt">(function(){try{var L=%s,V=%s,CUR0=%s;' % (
        json.dumps(lang_js), json.dumps(version(lang_js) if lang_js != 'ko' else ''), json.dumps(DEFAULT_CUR.get(lang_js, 'KRW')))
        + _RUNTIME_BODY + '}catch(e){}})();</script>')


_RUNTIME_BODY = r"""
var D=document,W=window,H=/[가-힣]/,dict=null,queue=[];
function gc(n){var m=D.cookie.match('(?:^|; )'+n+'=([^;]*)');return m?decodeURIComponent(m[1]):''}
function sc(n,v){D.cookie=n+'='+encodeURIComponent(v)+';path=/;max-age=31536000;samesite=lax'+(location.protocol==='https:'?';secure':'')}
W.MP_LANG=L;
function nz(s){return String(s).replace(/\s+/g,' ').trim()}
function tx(s){if(!dict||!s||!H.test(s))return s;var k=nz(s),v=dict[k];if(v==null)return s;var a=s.match(/^\s*/)[0],b=s.match(/\s*$/)[0];return a+v+b}
W.mpT=function(s){return tx(String(s==null?'':s))};
var SKIP={SCRIPT:1,STYLE:1,TEXTAREA:1,CODE:1,NOSCRIPT:1};
function walk(n){if(!dict)return;if(n.nodeType===3){var t=n.nodeValue;if(H.test(t)){var r=tx(t);if(r!==t)n.nodeValue=r}return}
 if(n.nodeType!==1||SKIP[n.nodeName])return;
 if(n.hasAttribute){['placeholder','title','aria-label','alt'].forEach(function(a){var v=n.getAttribute(a);if(v&&H.test(v)){var r=tx(v);if(r!==v)n.setAttribute(a,r)}});
  if((n.nodeName==='INPUT'&&(n.type==='button'||n.type==='submit'))&&H.test(n.value))n.value=tx(n.value)}
 for(var c=n.firstChild;c;c=c.nextSibling)walk(c)}
function fx(){}
if(L!=='ko'){
 var oa=W.alert,oc=W.confirm,op=W.prompt;W.alert=function(m){return oa.call(W,tx(String(m==null?'':m)))};
 W.confirm=function(m){return oc.call(W,tx(String(m==null?'':m)))};W.prompt=function(m,d){return op.call(W,tx(String(m==null?'':m)),d)};
 fetch('/i18n/'+L+'.json?v='+V).then(function(r){return r.json()}).then(function(d){dict=d;walk(D.body);
  try{new MutationObserver(function(ms){ms.forEach(function(m){if(m.type==='characterData'){var t=m.target;if(H.test(t.nodeValue)){var r=tx(t.nodeValue);if(r!==t.nodeValue)t.nodeValue=r}}else m.addedNodes.forEach(walk)})})
   .observe(D.body,{childList:true,subtree:true,characterData:true})}catch(e){}}).catch(function(){});
}
/* 표시 통화 — 결제는 KRW. 상품가 옆에 ≈ 참고 금액 */
var CUR=gc('mp_cur')||CUR0,RATES=null,SYM={USD:'US$',JPY:'¥',CNY:'CN¥',EUR:'€',TWD:'NT$',KRW:'₩'};
W.MP_CUR=CUR;
var PR=/₩\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,})(?![0-9,])/g;
function conv(n){var r=RATES&&RATES[CUR];if(!r)return'';var v=n*r;
 if(CUR==='JPY'||CUR==='TWD')return SYM[CUR]+Math.round(v).toLocaleString('en-US');
 return SYM[CUR]+(v>=100?Math.round(v).toLocaleString('en-US'):v.toFixed(2))}
function fxNode(t){var s=t.nodeValue;if(s.indexOf('₩')<0||/≈/.test(s))return;var p=t.parentNode;if(!p||SKIP[p.nodeName]||p.closest&&p.closest('#mpLangBar,.mpfx,input,select,option'))return;
 PR.lastIndex=0;if(!PR.test(s))return;PR.lastIndex=0;
 var out=s.replace(PR,function(m,g){var c=conv(Number(g.replace(/,/g,'')));return c?m+' (≈'+c+')':m});if(out!==s)t.nodeValue=out}
function fxWalk(n){if(CUR==='KRW'||!RATES)return;if(n.nodeType===3){fxNode(n);return}if(n.nodeType!==1||SKIP[n.nodeName])return;for(var c=n.firstChild;c;c=c.nextSibling)fxWalk(c)}
if(CUR!=='KRW'){fetch('/api/fx').then(function(r){return r.json()}).then(function(d){RATES=d.rates||{};
 var go=function(){fxWalk(D.body);try{new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(fxWalk);if(m.type==='characterData')fxNode(m.target)})}).observe(D.body,{childList:true,subtree:true,characterData:true})}catch(e){}};
 if(D.readyState==='loading')D.addEventListener('DOMContentLoaded',go);else go()}).catch(function(){})}
/* 언어·통화 스위처 — 헤더 유틸 영역에 🌐 버튼 1개(공간 최소) + 드롭다운. 헤더 없는 화면은 좌하단 고정 */
function bar(){if(D.getElementById('mpLangBar'))return;
 var path=location.pathname.replace(/^\/(en|ja|zh)(?=\/|$)/,'')||'/home';
 var L4=[['ko','한국어'],['en','English'],['ja','日本語'],['zh','简体中文']],LB={ko:'KO',en:'EN',ja:'JA',zh:'中文'};
 var b=D.createElement('span');b.id='mpLangBar';
 b.innerHTML='<button type="button" aria-haspopup="true" aria-expanded="false" aria-label="Language & currency">🌐 '+LB[L]+'</button>'
  +'<div class="pop" role="menu" hidden>'+L4.map(function(x){return'<a role="menuitem" hreflang="'+x[0]+'" href="'+(x[0]==='ko'?'':'/'+x[0])+path+location.search+'" data-l="'+x[0]+'"'+(x[0]===L?' aria-current="true"':'')+'>'+x[1]+'</a>'}).join('')
  +'<label>Currency <select aria-label="Currency">'+['KRW','USD','JPY','CNY','EUR','TWD'].map(function(c){return'<option'+(c===CUR?' selected':'')+'>'+c+'</option>'}).join('')+'</select></label></div>';
 var btn=b.querySelector('button'),pop=b.querySelector('.pop');
 btn.addEventListener('click',function(e){e.stopPropagation();var o=pop.hidden;pop.hidden=!o;btn.setAttribute('aria-expanded',o?'true':'false')});
 D.addEventListener('click',function(e){if(!b.contains(e.target)){pop.hidden=true;btn.setAttribute('aria-expanded','false')}});
 D.addEventListener('keydown',function(e){if(e.key==='Escape'){pop.hidden=true;btn.setAttribute('aria-expanded','false')}});
 pop.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[data-l]');if(a){sc('mp_lang',a.getAttribute('data-l'));try{W.mpTrack&&mpTrack('lang_switch',{method:a.getAttribute('data-l')})}catch(x){}}});
 pop.querySelector('select').addEventListener('change',function(){sc('mp_cur',this.value);location.reload()});
 var host=D.querySelector('header .util')||D.querySelector('.util');
 if(host){b.className='inhdr';host.insertBefore(b,host.firstChild)}else{b.className='fixed';D.body.appendChild(b)}}
var st=D.createElement('style');st.textContent='#mpLangBar{position:relative;display:inline-flex;align-items:center;margin-right:8px}'
 +'#mpLangBar>button{font:600 12px/1 "IBM Plex Mono",monospace;background:transparent;color:inherit;border:1px solid currentColor;border-radius:999px;padding:6px 9px;cursor:pointer;white-space:nowrap;min-height:30px}'
 +'#mpLangBar>button:focus-visible,#mpLangBar a:focus-visible{outline:2px solid #E8332A;outline-offset:2px}'
 +'#mpLangBar .pop{position:absolute;right:0;top:calc(100% + 8px);z-index:9990;background:#fff;color:#141414;border:1px solid #E2E0D9;box-shadow:0 10px 30px rgba(0,0,0,.15);min-width:170px;padding:6px;text-align:left}'
 +'#mpLangBar .pop a{display:block;padding:9px 10px;color:#141414;text-decoration:none;font:500 13.5px/1.2 "IBM Plex Sans KR",sans-serif;letter-spacing:0}'
 +'#mpLangBar .pop a:hover,#mpLangBar .pop a[aria-current]{background:#F4F3EF}#mpLangBar .pop a[aria-current]{font-weight:700;color:#E8332A}'
 +'#mpLangBar .pop label{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid #E2E0D9;margin-top:4px;padding:9px 10px 4px;font:500 12px "IBM Plex Mono",monospace;color:#5E5D57}'
 +'#mpLangBar .pop select{font:inherit;border:1px solid #E2E0D9;padding:4px}'
 +'#mpLangBar.fixed{position:fixed;z-index:9980;left:12px;bottom:12px;background:#141414;color:#fff;padding:4px;margin:0}#mpLangBar.fixed .pop{top:auto;bottom:calc(100% + 8px);left:0;right:auto}'
 +'@media(max-width:768px){#mpLangBar>button{padding:5px 7px;font-size:11px}#mpLangBar.fixed{bottom:84px}'
 +'header .util{gap:10px!important;font-size:12px!important;flex-shrink:0}header .util>a,header .util>span{white-space:nowrap}'
 +'header .logo,header .logo img,header .logo svg{max-width:38vw}}';
D.head.appendChild(st);
if(D.readyState==='loading')D.addEventListener('DOMContentLoaded',bar);else bar();
"""


# ── URL 접두사 처리 ───────────────────────────────────────────────────────
_PREFIX = re.compile(r'^/(en|ja|zh)(/.*)?$')


def split_path(path):
    """'/en/shop' → ('en', '/shop'), '/en' → ('en', '/home'), 기타 → (None, path)."""
    m = _PREFIX.match(path or '')
    if not m:
        return None, path
    return m.group(1), (m.group(2) or '/home')


_HREF = re.compile(r'''(\shref=)(["'])(/(?!/)[^"']*)\2''', re.I)
_NO_PREFIX = re.compile(r'^/(?:api/|admin|auth/|inicis/|static/|img/|space/|hero/|i18n/|og-image|robots\.txt|sitemap\.xml|favicon|p/|en/|ja/|zh/|en$|ja$|zh$|visit|[^?#]*\.(?:png|jpe?g|webp|svg|gif|css|js|json|xml|txt|pdf|ico|avif|woff2?)(?:[?#]|$))', re.I)


def prefix_links(html, lang):
    """내부 링크에 언어 접두사 부여 (정적 자산·API·관리자 제외). /p/ 동적 상세도 접두사 대상."""
    if lang not in LANGS:
        return html

    def rep(m):
        url = m.group(3)
        if url.startswith('/p/'):
            return m.group(1) + m.group(2) + '/' + lang + url + m.group(2)
        if _NO_PREFIX.match(url):
            return m.group(0)
        return m.group(1) + m.group(2) + '/' + lang + url + m.group(2)
    return _HREF.sub(rep, html)


_SEO_ORIGIN = 'https://mapdal.kr'


def seo_alternates(html, path, lang, origin=None):
    """hreflang 대안 링크 + canonical 언어별 교체 + og:locale."""
    origin = (origin or _SEO_ORIGIN).rstrip('/')
    if 'hreflang="x-default"' in html:
        return html
    clean = path or '/home'
    links = ['<link rel="alternate" hreflang="ko" href="%s%s">' % (origin, clean)]
    for l in LANGS:
        links.append('<link rel="alternate" hreflang="%s" href="%s/%s%s">' % (HTML_LANG[l] if l != 'zh' else 'zh-Hans', origin, l, clean))
    links.append('<link rel="alternate" hreflang="x-default" href="%s/en%s">' % (origin, clean))
    alt = ''.join(links)
    if lang in LANGS:
        html = re.sub(r'<link rel="canonical" href="[^"]*">',
                      '<link rel="canonical" href="%s/%s%s">' % (origin, lang, clean), html, count=1)
        html = re.sub(r'<meta property="og:url" content="[^"]*">',
                      '<meta property="og:url" content="%s/%s%s">' % (origin, lang, clean), html, count=1)
        loc = {'en': 'en_US', 'ja': 'ja_JP', 'zh': 'zh_CN'}[lang]
        html = html.replace('<meta property="og:locale" content="ko_KR">',
                            '<meta property="og:locale" content="%s">' % loc, 1)
    i = html.lower().find('</head>')
    return (html[:i] + alt + html[i:]) if i >= 0 else html


def set_html_lang(html, lang):
    return re.sub(r'<html\b([^>]*?)\blang="[^"]*"', lambda m: '<html%slang="%s"' % (m.group(1), HTML_LANG.get(lang, 'ko')),
                  html, count=1)


def localize(html, path, lang, origin=None):
    """최종 단계 — 번역 + 링크 접두사 + hreflang + lang 속성 + 런타임 스크립트 (lang='ko' 는 hreflang·런타임만)."""
    try:
        if lang in LANGS:
            html = translate_html(html, lang)
            html = prefix_links(html, lang)
            html = set_html_lang(html, lang)
        html = seo_alternates(html, path, lang, origin)
        if 'mpI18nRt' not in html:
            i = html.lower().rfind('</body>')
            rt = runtime_js(lang if lang in LANGS else 'ko')
            html = (html[:i] + rt + html[i:]) if i >= 0 else html + rt
    except Exception as e:
        print('[i18n] localize 실패: %s' % e, flush=True)
    return html
