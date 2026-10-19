"""Product page rendering with isolated product reads and response transforms."""
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_v2 as admin


@pytest.fixture
def render(monkeypatch):
    product = {'id': 'mp::test', 'name': '예약 음반', 'category': 'album',
               'price': 25200, 'stock': None, 'soldout': 0, 'badge': '예약판매'}
    monkeypatch.setattr(admin, 'ensure_ready', lambda: None)
    monkeypatch.setattr(admin, 'one', lambda *args: dict(product))
    monkeypatch.setattr(admin, '_state', {'pcols': set(product), 'pname': 'name', 'pprice': 'price'})
    monkeypatch.setattr(admin, '_track_apply', lambda html, *args: html)
    monkeypatch.setattr(admin, '_brand_apply', lambda html: html)
    monkeypatch.setattr(admin, '_pdp_resp', lambda html, *args: html)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/p/mp::test',
                       'headers': [], 'query_string': b''})
    def page(**changes):
        product.update(changes)
        return admin.pdp(product['id'], request)
    return page


def test_preorder_page_uses_saved_badge_and_unlimited_is_not_sold_out(render):
    html = render()
    assert '<span class="bdg dream">예약판매</span>' in html
    assert '<span class="bdg dream">영상통화</span>' not in html
    assert 'SOLD=false' in html
    assert '구매 가능 · 재고 0' not in html


@pytest.mark.parametrize('badge', ['', None, '  '])
def test_album_without_event_does_not_claim_video_call(render, badge):
    html = render(badge=badge)
    assert '<span class="bdg dream">영상통화</span>' not in html
    assert '<span class="bdg best">한터차트</span>' in html


def test_explicit_video_call_and_manual_sold_out_are_preserved(render):
    html = render(badge='영상통화', soldout=1)
    assert '<span class="bdg dream">영상통화</span>' in html
    assert 'SOLD=true' in html


def test_badge_is_escaped_and_finite_zero_remains_sold_out(render):
    html = render(badge='<img src=x onerror=alert(1)>', stock=0)
    assert '<img src=x onerror=alert(1)>' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    assert 'SOLD=true' in html


def test_chart_badge_is_not_duplicated(render):
    assert render(badge='한터차트').count('>한터차트</span>') == 1


def test_other_categories_keep_their_existing_badges(render):
    html = render(category='kfood', badge='예약판매')
    assert '<span class="bdg dream">콜드체인</span>' in html
    assert '<span class="bdg dream">예약판매</span>' not in html
