from __future__ import annotations

from pathlib import Path

import pytest

import mobile_playbook.platforms.ios.traffic_interception_setup as setup
from mobile_playbook.core import network


class FakeElement:
    def __init__(self, value=None, element_type="XCUIElementTypeSwitch"):
        self.value = value
        self.element_type = element_type

    def get_attribute(self, name):
        if name == "value":
            return self.value
        if name == "type":
            return self.element_type
        return None


class FakeClient:
    def __init__(
        self,
        *,
        ca_present=True,
        ca_trusted=True,
        fail_label=None,
        settings_page="root",
        trust_row_exists=None,
        switch_stays_disabled=False,
    ):
        self.ca_present = ca_present
        self.ca_trusted = ca_trusted
        self.fail_label = fail_label
        self.settings_page = settings_page
        self.trust_row_exists = ca_present if trust_row_exists is None else trust_row_exists
        self.switch_stays_disabled = switch_stays_disabled
        self.ca_cell = FakeElement(element_type="XCUIElementTypeCell")
        self.ca_switch = FakeElement(
            "1" if ca_trusted else "0",
            element_type="XCUIElementTypeSwitch",
        )
        self.calls = []
        self.fields = []
        self.installing_profile = False
        self.original = {
            "Configure Proxy": "Manual",
            "Server": "original.proxy",
            "Port": "8888",
            "URL": None,
        }

    def activate_app(self, bundle_id):
        self.calls.append(("activate", bundle_id))
        return {"result": True}

    def has_label(self, labels, timeout_seconds):
        self.calls.append(("has_label", tuple(labels)))
        if labels == ["Settings"]:
            return self.settings_page == "root"
        if labels == ["General"]:
            return self.settings_page == "root"
        if labels == ["About"]:
            return self.settings_page == "about"
        if labels == ["Certificate Trust Settings"]:
            return self.settings_page == "about" and self.trust_row_exists
        if labels == ["PortSwigger CA"]:
            return self.settings_page == "trust" and self.ca_present
        return False

    def tap_navigation_back(self, timeout_seconds):
        self.calls.append(("back", self.settings_page))
        previous = {
            "wifi": "root",
            "wifi_details": "wifi",
            "proxy": "wifi_details",
            "general": "root",
            "about": "general",
            "trust": "about",
            "profile": "root",
        }
        self.settings_page = previous[self.settings_page]
        return {"tapped": True}

    def tap_label(self, labels, timeout_seconds):
        self.calls.append(("tap", tuple(labels)))
        if self.fail_label and self.fail_label in labels:
            raise RuntimeError(f"could not find {self.fail_label}")
        if labels == ["Wi-Fi"]:
            self.settings_page = "wifi"
        elif labels == ["Test WiFi"]:
            self.settings_page = "wifi_details"
        elif labels == ["Configure Proxy"]:
            self.settings_page = "proxy"
        elif labels == ["General"]:
            self.settings_page = "general"
        elif labels == ["About"]:
            self.settings_page = "about"
        elif labels == ["Certificate Trust Settings"]:
            self.settings_page = "trust"
        if "Profile Downloaded" in labels:
            self.installing_profile = True
            self.settings_page = "profile"
        if labels == ["Done"] and self.installing_profile:
            self.ca_present = True
            self.trust_row_exists = True
            self.settings_page = "root"
        return {"tapped": True}

    def set_visible_text_field(self, label, value):
        self.fields.append((label, value))
        return {"label": label, "value": value}

    def element_value_by_label(self, labels):
        if "PortSwigger CA" in labels:
            return "1" if self.ca_present and self.ca_trusted else "0"
        return self.original.get(labels[0])

    def find_element_by_label(self, labels, timeout_seconds):
        if "PortSwigger CA" in labels:
            if not self.ca_present or self.settings_page != "trust":
                raise RuntimeError("CA not found")
            return self.ca_cell
        return FakeElement()

    def find_switch_by_label(self, labels, timeout_seconds):
        self.calls.append(("find_switch", tuple(labels)))
        if not self.ca_present or self.settings_page != "trust":
            raise RuntimeError("CA switch not found")
        return self.ca_switch

    def tap_switch_by_label(self, labels, timeout_seconds):
        self.find_switch_by_label(labels, timeout_seconds)
        self.calls.append(("tap_switch", tuple(labels)))
        if not self.switch_stays_disabled:
            self.ca_trusted = True
            self.ca_switch.value = "1"
        return "element_click"

    def open_url(self, url, bundle_id="com.apple.mobilesafari"):
        self.calls.append(("open_url", url, bundle_id))
        return {"result": True}

    def save_diagnostics(self, directory: Path, prefix: str):
        self.calls.append(("diagnostics", prefix))
        return {
            "screenshot_path": str(directory / f"{prefix}.png"),
            "page_source_path": str(directory / f"{prefix}.xml"),
        }


def _config(**overrides):
    config = {
        "mode": "appium_ui",
        "wifi_ssid": "Test WiFi",
        "proxy_host": "10.0.0.8",
        "proxy_port": 8081,
        "ca_download_url": "http://burp/cert",
        "ca_display_name": "PortSwigger CA",
        "configure_ca_if_missing": True,
        "proxy_cleanup_mode": "restore",
        "navigation_timeout_seconds": 2,
    }
    config.update(overrides)
    return config


def test_auto_proxy_host_uses_current_lan_address(monkeypatch):
    monkeypatch.setattr(network, "detect_lan_ip", lambda: "10.0.0.8")

    assert setup.resolve_device_proxy_host("auto") == "10.0.0.8"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "0.0.0.0"])
def test_proxy_host_rejects_loopback_or_non_routable_addresses(host):
    with pytest.raises(ValueError, match="loopback|routable"):
        setup.resolve_device_proxy_host(host)


def test_existing_trusted_ca_skips_installation(tmp_path):
    client = FakeClient(ca_present=True, ca_trusted=True)

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["status"] == "READY"
    assert state["ca_action"] == "already_trusted"
    assert not any(call[0] == "open_url" for call in client.calls)
    assert not any(call[0] == "tap_switch" for call in client.calls)


def test_disabled_ca_switch_is_tapped_and_verified(tmp_path):
    client = FakeClient(ca_present=True, ca_trusted=False)

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["status"] == "READY"
    assert state["ca_action"] == "trusted"
    assert ("tap_switch", ("PortSwigger CA",)) in client.calls
    assert client.ca_switch.value == "1"
    assert not any(call[0] == "open_url" for call in client.calls)


def test_settings_left_on_wifi_returns_to_root_before_navigation(tmp_path):
    client = FakeClient(settings_page="wifi")

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["status"] == "READY"
    assert ("back", "wifi") in client.calls


def test_settings_left_in_certificate_trust_returns_to_root(tmp_path):
    client = FakeClient(settings_page="trust")

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["status"] == "READY"
    assert ("back", "trust") in client.calls
    assert ("back", "about") in client.calls
    assert ("back", "general") in client.calls


def test_missing_certificate_trust_settings_row_returns_none():
    client = FakeClient(ca_present=False, ca_trusted=False, trust_row_exists=False)

    trust_state = setup._ca_trust_state(client, "PortSwigger CA", 2)

    assert trust_state is None
    assert ("tap", ("Certificate Trust Settings",)) not in client.calls


def test_missing_ca_is_downloaded_installed_and_trusted(tmp_path):
    client = FakeClient(ca_present=False, ca_trusted=False)

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["status"] == "READY"
    assert state["ca_action"] == "installed"
    assert client.ca_present is True
    assert client.ca_trusted is True
    assert ("open_url", "http://burp/cert", setup.SAFARI_BUNDLE_ID) in client.calls


def test_ca_switch_remaining_disabled_reports_trust_failure(tmp_path):
    client = FakeClient(
        ca_present=True,
        ca_trusted=False,
        switch_stays_disabled=True,
    )

    with pytest.raises(setup.TrafficInterceptionSetupError) as exc_info:
        setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert exc_info.value.status == "CA_TRUST_FAILED"
    assert ("tap_switch", ("PortSwigger CA",)) in client.calls


def test_existing_proxy_values_are_preserved(tmp_path):
    client = FakeClient()

    state = setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert state["original_proxy"] == {
        "mode": "Manual",
        "server": "original.proxy",
        "port": "8888",
        "url": None,
    }
    assert ("Server", "10.0.0.8") in client.fields
    assert ("Port", "8081") in client.fields


def test_restore_reinstates_original_proxy_values(tmp_path):
    client = FakeClient()
    state = setup.prepare_traffic_interception(client, _config(), tmp_path)
    client.fields.clear()

    restored = setup.restore_traffic_interception(client, state, tmp_path)

    assert restored["status"] == "RESTORED"
    assert ("tap", ("Manual",)) in client.calls
    assert client.fields == [("Server", "original.proxy"), ("Port", "8888")]


def test_off_cleanup_mode_selects_off(tmp_path):
    client = FakeClient()
    state = setup.prepare_traffic_interception(
        client,
        _config(proxy_cleanup_mode="off"),
        tmp_path,
    )

    restored = setup.restore_traffic_interception(client, state, tmp_path)

    assert restored == {"status": "RESTORED", "proxy_mode": "Off"}
    assert ("tap", ("Off",)) in client.calls


def test_off_cleanup_runs_after_risk_setup_failure(tmp_path):
    client = FakeClient(fail_label="General")

    with pytest.raises(setup.TrafficInterceptionSetupError) as exc_info:
        setup.prepare_traffic_interception(
            client,
            _config(proxy_cleanup_mode="off"),
            tmp_path,
        )

    client.fail_label = None
    restored = setup.restore_traffic_interception(client, exc_info.value.state, tmp_path)

    assert restored == {"status": "RESTORED", "proxy_mode": "Off"}
    assert ("tap", ("Off",)) in client.calls


def test_off_cleanup_failure_reports_restore_failure(tmp_path):
    client = FakeClient()
    state = setup.prepare_traffic_interception(
        client,
        _config(proxy_cleanup_mode="off"),
        tmp_path,
    )
    client.fail_label = "Off"

    with pytest.raises(setup.TrafficInterceptionSetupError) as exc_info:
        setup.restore_traffic_interception(client, state, tmp_path)

    assert exc_info.value.status == "DEVICE_PROXY_RESTORE_FAILED"


def test_navigation_failure_saves_diagnostics(tmp_path):
    client = FakeClient(fail_label="Wi-Fi")

    with pytest.raises(setup.TrafficInterceptionSetupError) as exc_info:
        setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert exc_info.value.status == "DEVICE_PROXY_SETUP_FAILED"
    assert exc_info.value.state["diagnostics"]["screenshot_path"].endswith(".png")
    assert any(call[0] == "diagnostics" for call in client.calls)


def test_proxy_is_restored_when_ca_navigation_fails(tmp_path):
    client = FakeClient(fail_label="General")

    with pytest.raises(setup.TrafficInterceptionSetupError) as exc_info:
        setup.prepare_traffic_interception(client, _config(), tmp_path)

    assert exc_info.value.status == "CA_TRUST_FAILED"
    client.fail_label = None
    client.fields.clear()

    restored = setup.restore_traffic_interception(client, exc_info.value.state, tmp_path)

    assert restored["status"] == "RESTORED"
    assert client.fields == [("Server", "original.proxy"), ("Port", "8888")]
