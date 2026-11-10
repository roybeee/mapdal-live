"""edgeguard — 결제 설정 fail-closed · 원본 직접 접근 차단 (순수 로직)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edgeguard as eg  # noqa: E402

REAL = ('MAPDAL0001', 'real-sign-key', 'real-iniapi', 'hash')


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ('APP_ENV', 'RENDER', 'INICIS_ALLOW_TEST', 'INICIS_REQUIRE_HASHKEY',
              'ORIGIN_SHARED_SECRET', 'ORIGIN_GUARD_MODE'):
        monkeypatch.delenv(k, raising=False)


def test_production_detection(monkeypatch):
    assert eg.is_production(False) is False
    assert eg.is_production(True) is True
    monkeypatch.setenv('RENDER', 'true')
    assert eg.is_production(False) is True
    monkeypatch.setenv('APP_ENV', 'test')
    assert eg.is_production(True) is False


def test_test_merchant_values_block_production_payments():
    errs, _ = eg.inicis_config_check(eg.TEST_MID, eg.TEST_SIGNKEY, eg.TEST_INIAPI, '', True)
    assert len(errs) == 3
    errs, _ = eg.inicis_config_check('', '', '', '', True)
    assert len(errs) == 3
    assert eg.inicis_config_check(*REAL, True) == ([], [])


def test_non_production_and_explicit_staging_are_allowed(monkeypatch):
    assert eg.inicis_config_check('', '', '', '', False) == ([], [])
    monkeypatch.setenv('INICIS_ALLOW_TEST', '1')
    assert eg.inicis_config_check(eg.TEST_MID, '', '', '', True) == ([], [])


def test_missing_mobile_hashkey_warns_or_blocks(monkeypatch):
    errs, warns = eg.inicis_config_check(*REAL[:3], '', True)
    assert errs == [] and len(warns) == 1
    monkeypatch.setenv('INICIS_REQUIRE_HASHKEY', '1')
    errs, warns = eg.inicis_config_check(*REAL[:3], '', True)
    assert len(errs) == 1 and warns == []


def test_origin_guard_disabled_by_default():
    assert eg.origin_decision('/api/orders', {}) == (True, None)
    assert eg.trust_proxy_headers(SimpleNamespace(edge_ok=None)) is True


def test_origin_guard_log_and_enforce(monkeypatch):
    monkeypatch.setenv('ORIGIN_SHARED_SECRET', 's3cret')
    assert eg.origin_decision('/api/orders', {'x-origin-auth': 's3cret'}) == (True, True)
    assert eg.origin_decision('/api/orders', {}) == (True, False)          # log 모드: 통과 + 미경유 표시
    monkeypatch.setenv('ORIGIN_GUARD_MODE', 'enforce')
    assert eg.origin_decision('/api/orders', {'x-origin-auth': 'nope'}) == (False, False)
    assert eg.origin_decision('/healthz', {}) == (True, False)             # Render 헬스체크 예외
    assert eg.trust_proxy_headers(SimpleNamespace(edge_ok=False)) is False
    assert eg.trust_proxy_headers(SimpleNamespace()) is True
