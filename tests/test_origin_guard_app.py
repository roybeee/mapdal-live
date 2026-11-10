"""app 레벨 — 원본 직접 접근 차단 · 프록시 IP 헤더 신뢰 · 결제 설정 fail-closed."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod  # noqa: E402
import admin_v2  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('ORIGIN_SHARED_SECRET', 's3cret')
    monkeypatch.setenv('ORIGIN_GUARD_MODE', 'enforce')
    return TestClient(appmod.app)


def test_direct_origin_request_is_blocked_in_enforce_mode(client):
    assert client.get('/api/config').status_code == 403
    assert client.get('/api/config', headers={'x-origin-auth': 's3cret'}).status_code == 200
    assert client.get('/healthz').status_code == 200


def test_spoofed_cf_header_ignored_when_edge_unverified():
    req = SimpleNamespace(headers={'cf-connecting-ip': '1.2.3.4', 'x-forwarded-for': '5.6.7.8, 9.9.9.9'},
                          state=SimpleNamespace(edge_ok=False), client=None)
    assert appmod.client_ip(req) == '9.9.9.9'
    assert admin_v2.client_ip_of(req) == '9.9.9.9'
    req.state.edge_ok = True
    assert appmod.client_ip(req) == '1.2.3.4'
    assert admin_v2.client_ip_of(req) == '1.2.3.4'


def test_invalid_pay_config_blocks_order_creation_before_stock(monkeypatch):
    monkeypatch.delenv('ORIGIN_SHARED_SECRET', raising=False)
    monkeypatch.setattr(appmod, 'PAY_CONFIG_ERRORS', ['INICIS_MID 테스트 상점'])
    c = TestClient(appmod.app)
    r = c.post('/api/orders', json={'items': [{'id': 'x', 'q': 1}]})
    assert r.status_code == 503
