from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient


class FakeElement:
    def __init__(self, attrs: dict, rect: dict | None = None, fail_click: bool = False):
        self.attrs = attrs
        self.rect = rect or {"x": 10, "y": 20, "width": 100, "height": 40}
        self.fail_click = fail_click
        self.clicked = False

    def get_attribute(self, name: str):
        return self.attrs.get(name)

    def click(self):
        if self.fail_click:
            raise RuntimeError("not hittable")
        self.clicked = True


class FakeDriver:
    def __init__(self, elements_by_class: dict[str, list[FakeElement]]):
        self.elements_by_class = elements_by_class
        self.executed: list[tuple[str, dict]] = []

    def find_elements(self, by, value):
        return self.elements_by_class.get(value, [])

    def execute_script(self, command: str, args: dict):
        self.executed.append((command, args))
        return True


class FakeAlert:
    def __init__(self, text: str):
        self.text = text
        self.dismissed = False
        self.accepted = False

    def dismiss(self):
        self.dismissed = True

    def accept(self):
        self.accepted = True


class FakeSwitchTo:
    def __init__(self, alert: FakeAlert):
        self.alert = alert


def _client(driver: FakeDriver) -> AppiumDeviceClient:
    client = AppiumDeviceClient(SimpleNamespace())
    client.driver = driver
    return client


def test_connect_wraps_a_failed_appium_session_in_a_clean_runtime_error(monkeypatch):
    import appium.webdriver as appium_webdriver

    def fail_to_connect(*args, **kwargs):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(appium_webdriver, "Remote", fail_to_connect)

    device_config = SimpleNamespace(
        udid="udid",
        team_id="TEAM",
        appium_server_url="http://127.0.0.1:4723",
        platform_version=None,
        xcode_signing_id="Apple Development",
        keep_wda=True,
        show_xcode_log=False,
        updated_wda_bundle_id=None,
        allow_provisioning_device_registration=False,
    )
    client = AppiumDeviceClient(device_config)

    with pytest.raises(RuntimeError, match="failed to start Appium session at http://127.0.0.1:4723"):
        client.connect()


def test_install_app_resolves_a_relative_ipa_path_to_absolute(tmp_path, monkeypatch):
    # A relative IPA path must never reach Appium as-is: the Appium server is a separate
    # process and would resolve it against its own working directory, not ours, which can
    # silently point at the wrong file if that process started somewhere else.
    monkeypatch.chdir(tmp_path)
    driver = FakeDriver({})
    client = _client(driver)

    result = client.install_app(Path("intake/ios/ipas/LocalKeyboard.ipa"), timeout_ms=1000)

    assert result.status == "INSTALLED"
    command, args = driver.executed[0]
    assert command == "mobile: installApp"
    assert Path(args["app"]).is_absolute()
    assert args["app"] == str(tmp_path / "intake/ios/ipas/LocalKeyboard.ipa")


def test_tap_text_field_prefers_visible_enabled_field():
    hidden = FakeElement({"type": "XCUIElementTypeTextField", "enabled": "true", "visible": "false"})
    visible = FakeElement({"type": "XCUIElementTypeTextField", "enabled": "true", "visible": "true"})
    driver = FakeDriver({"XCUIElementTypeTextField": [hidden, visible]})

    result = _client(driver).tap_text_field()

    assert result["tap_method"] == "element_click"
    assert not hidden.clicked
    assert visible.clicked


def test_tap_text_field_treats_empty_selector_values_as_auto_detection():
    field = FakeElement({"type": "XCUIElementTypeTextField", "enabled": "true", "visible": "true"})
    driver = FakeDriver({"XCUIElementTypeTextField": [field]})

    result = _client(driver).tap_text_field({"accessibility_id": None})

    assert result["selector"] == {"auto": True}
    assert field.clicked


def test_tap_text_field_falls_back_to_coordinate_tap_when_click_fails():
    field = FakeElement(
        {"type": "XCUIElementTypeTextField", "enabled": "true", "visible": "true"},
        rect={"x": 20, "y": 30, "width": 200, "height": 50},
        fail_click=True,
    )
    driver = FakeDriver({"XCUIElementTypeTextField": [field]})

    result = _client(driver).tap_text_field()

    assert result["tap_method"] == "coordinate_tap"
    assert driver.executed == [("mobile: tap", {"x": 120, "y": 55})]


def test_tap_text_field_skips_static_text_views():
    static_text_view = FakeElement(
        {
            "type": "XCUIElementTypeTextView",
            "enabled": "true",
            "visible": "true",
            "traits": "StaticText",
            "value": "Location Services uses GPS and Wi-Fi locations to determine your approximate location.",
        }
    )
    driver = FakeDriver({"XCUIElementTypeTextView": [static_text_view]})

    try:
        _client(driver).tap_text_field()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected no input candidate")

    assert "No visible enabled text field" in message
    assert not static_text_view.clicked


def test_tap_first_button_matching_skips_disabled_buttons_and_reports_visible_buttons():
    disabled_login = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Login", "enabled": "false", "visible": "true"}
    )
    forgot_password = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Forgot Password", "enabled": "true", "visible": "true"}
    )
    driver = FakeDriver({"XCUIElementTypeButton": [disabled_login, forgot_password]})

    try:
        _client(driver).tap_first_button_matching(["use password"], ["forgot"], allow_any=False)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected no matching button")

    assert "Forgot Password" in message
    assert not disabled_login.clicked
    assert not forgot_password.clicked


def test_tap_first_element_matching_can_tap_accessible_other_navigation_controls():
    pseudo_field = FakeElement(
        {
            "type": "XCUIElementTypeOther",
            "label": "Select Car Park",
            "name": "btn_select_carpark",
            "enabled": "true",
            "visible": "true",
            "accessible": "true",
        }
    )
    driver = FakeDriver({"XCUIElementTypeOther": [pseudo_field]})

    result = _client(driver).tap_first_element_matching(
        ["select car park"],
        class_names=["XCUIElementTypeButton", "XCUIElementTypeOther"],
        element_label="navigation element",
    )

    assert result["matched_by"] == "label_contains"
    assert result["class_name"] == "XCUIElementTypeOther"
    assert pseudo_field.clicked


def test_permission_alert_dismiss_taps_cancel_before_alert_dismiss():
    cancel = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Cancel", "name": "Cancel", "enabled": "true", "visible": "true"}
    )
    settings = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Settings", "name": "Settings", "enabled": "true", "visible": "true"}
    )
    driver = FakeDriver({"XCUIElementTypeButton": [settings, cancel]})
    alert = FakeAlert('Turn On Location Services to Allow "Parking" to Determine Your Location')
    driver.switch_to = FakeSwitchTo(alert)

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result["status"] == "HANDLED"
    assert result["button"] == "Cancel Cancel"
    assert cancel.clicked
    assert not settings.clicked
    assert not alert.dismissed
