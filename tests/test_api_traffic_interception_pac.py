from __future__ import annotations

import pytest
from fastapi import HTTPException

from mobile_playbook.api.routes import catalog as api_catalog
from mobile_playbook.api.services import catalog as catalog_service
from mobile_playbook.core import network


def test_pac_routes_apple_services_direct_and_other_hosts_to_configured_burp(monkeypatch):
    monkeypatch.setattr(
        catalog_service.config_editor,
        "get_risk_settings",
        lambda platform, risk_id: {"burp": {"proxy_url": "http://10.132.0.9:8081"}},
    )

    response = api_catalog.traffic_interception_pac("ios")

    assert response.media_type == "application/x-ns-proxy-autoconfig"
    body = response.body.decode()
    for pattern in catalog_service.PAC_DIRECT_HOST_PATTERNS:
        assert f'shExpMatch(host, "{pattern}")' in body
    assert 'return "DIRECT";' in body
    assert 'return "PROXY 10.132.0.9:8081";' in body


def test_pac_substitutes_lan_ip_when_burp_host_is_loopback(monkeypatch):
    monkeypatch.setattr(
        catalog_service.config_editor,
        "get_risk_settings",
        lambda platform, risk_id: {"burp": {"proxy_url": "http://127.0.0.1:8080"}},
    )
    monkeypatch.setattr(network, "detect_lan_ip", lambda: "10.0.0.5")

    response = api_catalog.traffic_interception_pac("ios")

    assert 'return "PROXY 10.0.0.5:8080";' in response.body.decode()


def test_pac_prefers_explicit_proxy_host_override(monkeypatch):
    monkeypatch.setattr(
        catalog_service.config_editor,
        "get_risk_settings",
        lambda platform, risk_id: {"burp": {"proxy_url": "http://127.0.0.1:8080"}},
    )
    monkeypatch.setattr(network, "detect_lan_ip", lambda: pytest.fail("should not auto-detect when overridden"))

    response = api_catalog.traffic_interception_pac("ios", proxy_host="10.9.8.7")

    assert 'return "PROXY 10.9.8.7:8080";' in response.body.decode()


def test_pac_404_for_platform_without_traffic_interception():
    with pytest.raises(HTTPException) as exc_info:
        api_catalog.traffic_interception_pac("android")
    assert exc_info.value.status_code == 404


def test_pac_400_when_proxy_url_not_configured(monkeypatch):
    monkeypatch.setattr(catalog_service.config_editor, "get_risk_settings", lambda platform, risk_id: {"burp": {}})

    with pytest.raises(HTTPException) as exc_info:
        api_catalog.traffic_interception_pac("ios")
    assert exc_info.value.status_code == 400
