from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient, TextFieldUnavailableError, is_untrusted_developer_cert_error


class FakeElement:
    def __init__(self, attrs: dict, rect: dict | None = None, fail_click: bool = False):
        self.attrs = attrs
        self.rect = rect or {"x": 10, "y": 20, "width": 100, "height": 40}
        self.fail_click = fail_click
        self.clicked = False
        self.sent_keys = []

    def get_attribute(self, name: str):
        return self.attrs.get(name)

    def click(self):
        if self.fail_click:
            raise RuntimeError("not hittable")
        self.clicked = True

    def clear(self):
        self.attrs["value"] = ""

    def send_keys(self, value):
        self.sent_keys.append(value)
        self.attrs["value"] = value


class FakeDriver:
    def __init__(
        self,
        elements_by_class: dict[str, list[FakeElement]] | None = None,
        is_locked: bool = False,
        predicate_element: FakeElement | None = None,
    ):
        self.elements_by_class = elements_by_class or {}
        self.executed: list[tuple[str, dict]] = []
        self._is_locked = is_locked
        self.unlock_calls = 0
        self.predicate_element = predicate_element
        self.predicates = []
        self.find_requests = []
        self.page_source = "<App />"

    def find_elements(self, by, value):
        return self.elements_by_class.get(value, [])

    def find_element(self, by, value):
        self.find_requests.append((by, value))
        self.predicates.append(value)
        if self.predicate_element is None:
            raise RuntimeError("not found")
        return self.predicate_element

    def execute_script(self, command: str, args: dict):
        self.executed.append((command, args))
        return True

    def is_locked(self) -> bool:
        return self._is_locked

    def unlock(self) -> None:
        self.unlock_calls += 1
        self._is_locked = False

    def save_screenshot(self, path):
        Path(path).write_bytes(b"png")
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


def test_connect_gives_actionable_message_when_developer_cert_is_untrusted(monkeypatch):
    import appium.webdriver as appium_webdriver

    def fail_to_connect(*args, **kwargs):
        raise RuntimeError(
            "Unable to launch WebDriverAgent. Original error: xcodebuild failed with code 65. "
            "Testing failed:\n\tThe application could not be launched because the Developer App "
            "Certificate is not trusted."
        )

    monkeypatch.setattr(appium_webdriver, "Remote", fail_to_connect)

    device_config = SimpleNamespace(
        udid="udid",
        team_id="TEAM123",
        appium_server_url="http://127.0.0.1:4723",
        platform_version=None,
        xcode_signing_id="Apple Development",
        keep_wda=True,
        show_xcode_log=False,
        updated_wda_bundle_id=None,
        allow_provisioning_device_registration=False,
    )
    client = AppiumDeviceClient(device_config)

    with pytest.raises(RuntimeError, match="VPN & Device Management"):
        client.connect()


def test_is_untrusted_developer_cert_error_matches_known_markers():
    assert is_untrusted_developer_cert_error("The Developer App Certificate is not trusted.")
    assert is_untrusted_developer_cert_error("...profile has not been explicitly trusted by the user.")
    assert not is_untrusted_developer_cert_error("xcodebuild failed with code 65")


def test_unlock_calls_driver_unlock_when_locked():
    driver = FakeDriver(is_locked=True)
    client = _client(driver)

    result = client.unlock()

    assert result == {"was_locked": True}
    assert driver.unlock_calls == 1


def test_unlock_still_calls_driver_unlock_when_not_locked():
    driver = FakeDriver(is_locked=False)
    client = _client(driver)

    result = client.unlock()

    assert result == {"was_locked": False}
    assert driver.unlock_calls == 1  # driver.unlock() itself no-ops safely; this just reports prior state


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


def test_activate_app_uses_mobile_command():
    driver = FakeDriver()

    result = _client(driver).activate_app("com.apple.Preferences")

    assert result == {"result": True}
    assert driver.executed == [("mobile: activateApp", {"bundleId": "com.apple.Preferences"})]


def test_label_helpers_use_ios_predicates_and_visible_text_field():
    field = FakeElement(
        {"type": "XCUIElementTypeTextField", "label": "Server", "value": "old", "visible": "true", "enabled": "true"}
    )
    driver = FakeDriver(predicate_element=field)
    client = _client(driver)

    assert client.element_value_by_label(["Server"]) == "old"
    tapped = client.tap_label(["Server"], 1)
    updated = client.set_visible_text_field("Server", "10.0.0.8")

    assert tapped["tapped"] is True
    assert updated == {"label": "Server", "value": "10.0.0.8"}
    assert field.sent_keys == ["10.0.0.8"]
    assert any("label == 'Server'" in predicate for predicate in driver.predicates)


def test_has_label_limits_matches_to_rows_and_static_text():
    driver = FakeDriver(predicate_element=FakeElement({"type": "XCUIElementTypeStaticText"}))

    assert _client(driver).has_label(["General"], 1) is True
    assert "type == 'XCUIElementTypeCell'" in driver.predicates[0]
    assert "type == 'XCUIElementTypeStaticText'" in driver.predicates[0]


def test_tap_navigation_back_selects_a_navigation_bar_button():
    button = FakeElement({"type": "XCUIElementTypeButton", "label": "Back"})
    driver = FakeDriver(predicate_element=button)

    result = _client(driver).tap_navigation_back(1)

    assert result["tapped"] is True
    assert button.clicked is True
    assert "XCUIElementTypeNavigationBar" in driver.find_requests[0][1]
    assert "XCUIElementTypeButton" in driver.find_requests[0][1]


def test_save_diagnostics_writes_screenshot_and_page_source(tmp_path):
    diagnostics = _client(FakeDriver()).save_diagnostics(tmp_path, "settings_failure")

    assert Path(diagnostics["screenshot_path"]).read_bytes() == b"png"
    assert Path(diagnostics["page_source_path"]).read_text() == "<App />"


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

    with pytest.raises(TextFieldUnavailableError, match="No visible enabled text field"):
        _client(driver).tap_text_field()
    assert not static_text_view.clicked


@pytest.mark.parametrize("missing", [True, False])
def test_configured_text_field_absence_is_distinct_from_an_invalid_selector(monkeypatch, missing):
    from selenium.common.exceptions import InvalidSelectorException, NoSuchElementException

    driver = FakeDriver()
    error = NoSuchElementException("field absent") if missing else InvalidSelectorException("bad selector")

    def fail_find(by, value):
        raise error

    monkeypatch.setattr(driver, "find_element", fail_find)
    with pytest.raises(TextFieldUnavailableError if missing else InvalidSelectorException) as caught:
        _client(driver).tap_text_field({"xpath": "//input"})
    if missing:
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error


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


LOCAL_NETWORK_ALERT = '"LocalKeyboard" would like to find and connect to devices on your local network.'


def _allow_or_deny() -> tuple[FakeElement, FakeElement]:
    allow = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Allow", "name": "Allow", "enabled": "true", "visible": "true"}
    )
    deny = FakeElement(
        {
            "type": "XCUIElementTypeButton",
            "label": "Don't Allow",
            "name": "Don't Allow",
            "enabled": "true",
            "visible": "true",
        }
    )
    return allow, deny


def _alert_driver(text: str) -> tuple[FakeDriver, FakeElement, FakeElement, FakeAlert]:
    allow, deny = _allow_or_deny()
    driver = FakeDriver({"XCUIElementTypeButton": [allow, deny]})
    alert = FakeAlert(text)
    driver.switch_to = FakeSwitchTo(alert)
    return driver, allow, deny, alert


def test_a_matching_alert_is_allowed_even_though_the_action_is_dismiss():
    driver, allow, deny, alert = _alert_driver(LOCAL_NETWORK_ALERT)

    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result["status"] == "HANDLED"
    assert result["action"] == "accept"
    assert allow.clicked
    assert not deny.clicked
    assert not alert.dismissed


def test_an_unrelated_alert_under_the_same_config_is_still_dismissed():
    driver, allow, deny, alert = _alert_driver(
        '"LocalKeyboard" would like to access your photos.'
    )

    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result["status"] == "HANDLED"
    assert result["action"] == "dismiss"
    assert deny.clicked
    assert not allow.clicked


def test_no_accept_list_leaves_the_configured_action_alone():
    driver, allow, deny, _ = _alert_driver(LOCAL_NETWORK_ALERT)

    result = _client(driver)._handle_one_permission_alert("dismiss", None)

    assert result["action"] == "dismiss"
    assert deny.clicked
    assert not allow.clicked


def test_an_empty_accept_list_leaves_the_configured_action_alone():
    driver, allow, deny, _ = _alert_driver(LOCAL_NETWORK_ALERT)

    assert _client(driver)._handle_one_permission_alert("dismiss", [])["action"] == "dismiss"
    assert deny.clicked
    assert not allow.clicked


def test_the_match_ignores_case():
    driver, allow, _, _ = _alert_driver(LOCAL_NETWORK_ALERT.upper())

    assert _client(driver)._handle_one_permission_alert("dismiss", ["local network"])["action"] == "accept"
    assert allow.clicked


def test_a_matching_alert_overrides_alert_only_too():
    driver, allow, _, _ = _alert_driver(LOCAL_NETWORK_ALERT)

    result = _client(driver)._handle_one_permission_alert("alert_only", ["local network"])

    assert result["status"] == "HANDLED"
    assert result["action"] == "accept"
    assert allow.clicked


def test_a_matching_alert_falls_back_to_the_native_accept_when_no_button_is_found():
    driver = FakeDriver({})
    alert = FakeAlert(LOCAL_NETWORK_ALERT)
    driver.switch_to = FakeSwitchTo(alert)

    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result == {
        "status": "HANDLED",
        "action": "accept",
        "text": LOCAL_NETWORK_ALERT,
        "button": "accept",
    }
    assert alert.accepted
    assert not alert.dismissed


def test_the_accept_list_is_read_from_the_configuration():
    driver, allow, deny, _ = _alert_driver(LOCAL_NETWORK_ALERT)

    results = _client(driver).handle_permission_alerts(
        {
            "action": "dismiss",
            "accept_if_text_contains": ["  Local Network  "],
            "max_alerts": 1,
            "wait_seconds": 0.1,
        }
    )

    assert results[0]["action"] == "accept"
    assert allow.clicked
    assert not deny.clicked


def test_a_blank_entry_does_not_turn_the_list_into_a_match_for_everything():
    driver, allow, deny, _ = _alert_driver('"LocalKeyboard" would like to access your photos.')

    results = _client(driver).handle_permission_alerts(
        {
            "action": "dismiss",
            "accept_if_text_contains": ["", "   ", None],
            "max_alerts": 1,
            "wait_seconds": 0.1,
        }
    )

    assert results[0]["action"] == "dismiss"
    assert deny.clicked
    assert not allow.clicked


class PredicateAwareDriver(FakeDriver):
    """Honours the predicate's type clause, so a row-restricted lookup really can miss."""

    def __init__(self, candidates: list[FakeElement]):
        super().__init__()
        self.candidates = candidates

    def find_element(self, by, value):
        self.find_requests.append((by, value))
        self.predicates.append(value)
        for element in self.candidates:
            if f"type == '{element.attrs['type']}'" in value:
                return element
        raise RuntimeError(f"no element matching {value}")


def test_tap_row_label_restricts_the_match_to_a_row_or_button():
    row = FakeElement(
        {"type": "XCUIElementTypeCell", "label": "Keyboards", "name": "Keyboards", "enabled": "true", "visible": "true"}
    )
    driver = PredicateAwareDriver([row])

    result = _client(driver).tap_row_label(["Keyboards"], 1)

    predicate = driver.predicates[0]
    assert "type == 'XCUIElementTypeCell'" in predicate
    assert "type == 'XCUIElementTypeButton'" in predicate
    assert "label == 'Keyboards'" in predicate
    assert result["tapped"] is True
    assert row.clicked


def test_tap_row_label_does_not_match_the_navigation_bar_title():
    title = FakeElement(
        {
            "type": "XCUIElementTypeNavigationBar",
            "label": "Keyboards",
            "name": "Keyboards",
            "enabled": "true",
            "visible": "true",
        }
    )
    driver = PredicateAwareDriver([title])

    with pytest.raises(Exception):
        _client(driver).tap_row_label(["Keyboards"], 1)

    assert "XCUIElementTypeNavigationBar" not in driver.predicates[0]
    assert not title.clicked


def test_tap_row_label_prefers_the_row_over_a_navigation_bar_of_the_same_name():
    title = FakeElement({"type": "XCUIElementTypeNavigationBar", "label": "Keyboards", "enabled": "true"})
    row = FakeElement({"type": "XCUIElementTypeCell", "label": "Keyboards", "enabled": "true", "visible": "true"})
    driver = PredicateAwareDriver([title, row])

    _client(driver).tap_row_label(["Keyboards"], 1)

    assert row.clicked
    assert not title.clicked


def test_tap_row_label_needs_a_connected_session():
    client = AppiumDeviceClient(SimpleNamespace())
    client.driver = None

    with pytest.raises(RuntimeError, match="not connected"):
        client.tap_row_label(["Keyboards"], 1)


def _allow_and_deny_alert() -> tuple[FakeDriver, FakeElement, FakeElement, FakeAlert]:
    allow = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Allow", "name": "Allow", "enabled": "true", "visible": "true"}
    )
    deny = FakeElement(
        {
            "type": "XCUIElementTypeButton",
            "label": "Don't Allow",
            "name": "Don't Allow",
            "enabled": "true",
            "visible": "true",
        }
    )
    # Don't Allow is listed first, so a positive match must skip past it rather than take it.
    driver = FakeDriver({"XCUIElementTypeButton": [deny, allow]})
    alert = FakeAlert('"LocalKeyboard" would like to access your photos.')
    driver.switch_to = FakeSwitchTo(alert)
    return driver, allow, deny, alert


def test_accepting_an_alert_taps_allow_and_never_dont_allow():
    driver, allow, deny, alert = _allow_and_deny_alert()

    result = _client(driver)._handle_one_permission_alert("accept")

    assert result["status"] == "HANDLED"
    assert result["button"] == "Allow Allow"
    assert allow.clicked
    assert not deny.clicked
    assert not alert.accepted


def test_dismissing_the_same_alert_still_taps_dont_allow():
    driver, allow, deny, alert = _allow_and_deny_alert()

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result["status"] == "HANDLED"
    assert deny.clicked
    assert not allow.clicked
    assert not alert.dismissed


def test_a_positive_match_falls_back_to_a_negative_button_when_there_is_no_other():
    deny = FakeElement(
        {
            "type": "XCUIElementTypeButton",
            "label": "Don't Allow",
            "name": "Don't Allow",
            "enabled": "true",
            "visible": "true",
        }
    )
    driver = FakeDriver({"XCUIElementTypeButton": [deny]})

    assert _client(driver)._find_permission_alert_button(prefer_negative=False)["button"] == "Don't Allow Don't Allow"


class StaticText:
    def __init__(self, text: str):
        self.text = text


def test_the_local_network_prompt_is_recognised_from_page_text_when_there_is_no_alert_element():
    allow = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Allow", "name": "Allow", "enabled": "true", "visible": "true"}
    )
    deny = FakeElement(
        {
            "type": "XCUIElementTypeButton",
            "label": "Don't Allow",
            "name": "Don't Allow",
            "enabled": "true",
            "visible": "true",
        }
    )
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": [deny, allow],
            "XCUIElementTypeStaticText": [
                StaticText('"LocalKeyboard" Would Like to'),
                StaticText("find and connect to devices on your local network."),
            ],
        }
    )

    # No switch_to.alert and no XCUIElementTypeAlert: only the page's own static text is left.
    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result["status"] == "HANDLED"
    assert result["action"] == "accept"
    assert "local network" in result["text"].lower()
    assert allow.clicked
    assert not deny.clicked


def test_an_unrelated_page_read_the_same_way_is_still_dismissed():
    allow = FakeElement(
        {"type": "XCUIElementTypeButton", "label": "Allow", "name": "Allow", "enabled": "true", "visible": "true"}
    )
    deny = FakeElement(
        {
            "type": "XCUIElementTypeButton",
            "label": "Don't Allow",
            "name": "Don't Allow",
            "enabled": "true",
            "visible": "true",
        }
    )
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": [deny, allow],
            "XCUIElementTypeStaticText": [StaticText('"LocalKeyboard" would like to access your photos.')],
        }
    )

    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result["action"] == "dismiss"
    assert deny.clicked
    assert not allow.clicked


def test_the_alert_element_is_preferred_over_the_rest_of_the_page():
    class AlertScopedDriver(FakeDriver):
        def find_element(self, by, value):
            if value == "XCUIElementTypeAlert":
                return SimpleNamespace(
                    find_elements=lambda by, name: [StaticText("Inside the alert.")]
                )
            return super().find_element(by, value)

    driver = AlertScopedDriver({"XCUIElementTypeStaticText": [StaticText("Elsewhere on the page.")]})

    assert _client(driver)._alert_text() == "Inside the alert."


def test_a_page_with_no_text_at_all_reads_as_empty():
    assert _client(FakeDriver({}))._alert_text() == ""


def test_an_alert_element_holding_no_text_falls_through_to_the_page():
    class EmptyAlertDriver(FakeDriver):
        def find_element(self, by, value):
            if value == "XCUIElementTypeAlert":
                return SimpleNamespace(find_elements=lambda by, name: [])
            return super().find_element(by, value)

    driver = EmptyAlertDriver({"XCUIElementTypeStaticText": [StaticText("Only on the page.")]})

    assert _client(driver)._alert_text() == "Only on the page."



# Build an enabled, visible button whose label and name are both the given text.
def _button(label: str) -> FakeElement:
    return FakeElement({"type": "XCUIElementTypeButton", "label": label, "name": label, "enabled": "true", "visible": "true"})


# Expose a real alert element holding the given buttons, as XCUITest does for an in-app alert.
class AlertElementDriver(FakeDriver):
    def __init__(self, buttons: list[FakeElement], text: str = "An alert."):
        super().__init__({"XCUIElementTypeButton": buttons})
        self.alert_element = SimpleNamespace(
            find_elements=lambda by, name: list(buttons) if name == "XCUIElementTypeButton" else [StaticText(text)]
        )

    def find_element(self, by, value):
        if value == "XCUIElementTypeAlert":
            return self.alert_element
        return super().find_element(by, value)


def test_an_update_prompt_on_the_page_is_not_mistaken_for_a_permission_alert():
    update_now = _button("Update Now")
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": [update_now],
            "XCUIElementTypeStaticText": [
                StaticText("Update Available"),
                StaticText("The MyICA Mobile is not the latest version. Please update the app to the latest version."),
            ],
        }
    )

    result = _client(driver)._handle_one_permission_alert("dismiss", ["local network"])

    assert result == {"status": "NO_ALERT"}
    assert not update_now.clicked


def test_a_real_alert_whose_only_button_navigates_away_is_left_alone():
    update_now = _button("Update Now")
    driver = AlertElementDriver([update_now], "Update Available")
    alert = FakeAlert("Update Available")
    driver.switch_to = FakeSwitchTo(alert)

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result["status"] == "ALERT_UNHANDLED"
    assert result["buttons"] == ["Update Now Update Now"]
    assert not update_now.clicked
    assert not alert.dismissed and not alert.accepted


def test_a_short_negative_label_does_not_match_inside_a_longer_word():
    later_now = _button("Update Now")
    not_now = _button("Not Now")
    driver = AlertElementDriver([later_now, not_now])

    assert _client(driver)._find_permission_alert_button(prefer_negative=True)["button"] == "Not Now Not Now"


def test_dismiss_never_falls_back_to_granting_a_permission():
    allow = _button("Allow")
    driver = AlertElementDriver([allow], '"Example" Would Like to Access Your Photos')
    driver.switch_to = FakeSwitchTo(FakeAlert('"Example" Would Like to Access Your Photos'))

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result["status"] == "ALERT_UNHANDLED"
    assert not allow.clicked


def test_dismiss_can_still_acknowledge_a_single_button_alert():
    ok = _button("OK")
    driver = AlertElementDriver([ok], "Your session has expired.")
    driver.switch_to = FakeSwitchTo(FakeAlert("Your session has expired."))

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result["status"] == "HANDLED"
    assert ok.clicked


def test_an_app_store_page_is_not_read_as_an_alert():
    chart_badge = _button("CHART, 7, No., Productivity")
    get_button = _button("Get")
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": [chart_badge, get_button],
            "XCUIElementTypeStaticText": [StaticText("What's New"), StaticText("Version History Version 2.0.0")],
        }
    )

    assert _client(driver)._handle_one_permission_alert("dismiss")["status"] == "NO_ALERT"
    assert not chart_badge.clicked and not get_button.clicked


# Count every attribute read so a test can show which buttons were never inspected.
class CountingElement(FakeElement):
    reads = 0

    def get_attribute(self, name):
        CountingElement.reads += 1
        return super().get_attribute(name)


def test_a_page_with_more_buttons_than_an_alert_is_not_scanned():
    CountingElement.reads = 0
    buttons = [CountingElement({"label": f"App {index}", "name": f"App {index}"}) for index in range(40)]
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": buttons,
            "XCUIElementTypeStaticText": [StaticText('"Example" Would Like to Send You Notifications')],
        }
    )

    result = _client(driver)._handle_one_permission_alert("dismiss")

    assert result == {"status": "NO_ALERT"}
    assert CountingElement.reads == 0
    assert not any(button.clicked for button in buttons)


def test_a_button_that_goes_stale_stops_the_scan():
    class StaleElement(FakeElement):
        def get_attribute(self, name):
            raise RuntimeError("The previously found element is not present in the current view anymore")

    deny = _button("Don't Allow")
    driver = FakeDriver(
        {
            "XCUIElementTypeButton": [StaleElement({}), deny],
            "XCUIElementTypeStaticText": [StaticText('"Example" Would Like to Use Your Location')],
        }
    )

    assert _client(driver)._handle_one_permission_alert("dismiss")["status"] == "NO_ALERT"
    assert not deny.clicked


def test_the_button_scan_stops_when_its_time_limit_has_passed():
    import time

    from mobile_playbook.platforms.ios.device_client import AlertScanAborted

    driver = AlertElementDriver([_button("Don't Allow")])

    with pytest.raises(AlertScanAborted):
        _client(driver)._find_permission_alert_button(prefer_negative=True, deadline=time.monotonic() - 1)


def test_alerts_are_left_alone_when_the_target_app_is_not_in_front():
    class BackgroundDriver(AlertElementDriver):
        def execute_script(self, command, args):
            self.executed.append((command, args))
            return 3 if command == "mobile: queryAppState" else True

    deny = _button("Don't Allow")
    driver = BackgroundDriver([deny], '"Example" Would Like to Use Your Location')
    driver.switch_to = FakeSwitchTo(FakeAlert('"Example" Would Like to Use Your Location'))

    results = _client(driver).handle_permission_alerts(
        {"action": "dismiss", "wait_seconds": 0.1, "foreground_bundle_id": "com.example.target"}
    )

    assert results == [{"status": "TARGET_NOT_FOREGROUND", "bundle_id": "com.example.target", "app_state": 3}]
    assert not deny.clicked


def test_an_unhandled_alert_is_reported_once_rather_than_retried():
    update_now = _button("Update Now")
    driver = AlertElementDriver([update_now], "Update Available")
    driver.switch_to = FakeSwitchTo(FakeAlert("Update Available"))

    results = _client(driver).handle_permission_alerts({"action": "dismiss", "wait_seconds": 1, "max_alerts": 3})

    assert [result["status"] for result in results] == ["ALERT_UNHANDLED"]
    assert not update_now.clicked

def _zipped(entries: dict[str, bytes]) -> str:
    import base64
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.parametrize("prefix", ["", "Evidence/"])
def test_pull_app_documents_extracts_the_folder_with_or_without_its_own_name(tmp_path, prefix):
    pulled: list[str] = []
    encoded = _zipped({f"{prefix}done.json": b"{}", f"{prefix}broadcast-1.mp4": b"mp4"})
    driver = SimpleNamespace(pull_folder=lambda remote: pulled.append(remote) or encoded)

    files = _client(driver).pull_app_documents("com.example.recorder", "/Evidence/", tmp_path / "pulled", max_bytes=10_000)

    assert pulled == ["@com.example.recorder:documents/Evidence"]
    assert [path.name for path in files] == ["broadcast-1.mp4", "done.json"]
    assert (tmp_path / "pulled" / "done.json").read_bytes() == b"{}"


def test_pull_app_documents_refuses_a_folder_above_the_size_limit(tmp_path):
    driver = SimpleNamespace(pull_folder=lambda remote: _zipped({"big.bin": b"x" * 5000}))

    with pytest.raises(RuntimeError, match="max_pull_bytes=100"):
        _client(driver).pull_app_documents("com.example.recorder", "Evidence", tmp_path / "pulled", max_bytes=100)

    assert not (tmp_path / "pulled").exists()


# Keep device time timezone-aware and reject responses that cannot establish an epoch.
@pytest.mark.parametrize("raw, expected", [("2026-10-01T08:00:00+08:00", 1790812800000), ("2026-10-01T00:00:00Z", 1790812800000), ("2026-10-01T00:00:00.999Z", 1790812800000), ("2026-10-01T00:00:00", None), ("unavailable", None), (None, None)])
def test_device_time_is_a_conservative_whole_second_epoch(raw, expected):
    client = AppiumDeviceClient(None)
    client.driver = SimpleNamespace(get_device_time=lambda: raw)
    if expected is None:
        with pytest.raises(RuntimeError, match="invalid device time"):
            client.device_time_ms()
    else:
        assert client.device_time_ms() == expected


NOTIFICATION_PROMPT_TEXT = "“App” Would Like to Send You Notifications"


class ClearableSwitchTo:
    def __init__(self, alert: FakeAlert, cleared_by: FakeElement):
        self._alert = alert
        self._cleared_by = cleared_by

    @property
    def alert(self):
        if self._cleared_by.clicked:
            raise RuntimeError("no alert open")
        return self._alert


class PromptBlockedDriver(FakeDriver):
    def __init__(self, alert_text: str, buttons: list[FakeElement], cleared_by: FakeElement):
        super().__init__({"XCUIElementTypeButton": buttons})
        self.cleared_by = cleared_by
        self.target = FakeElement({"label": "Next", "name": "Next", "enabled": "true", "visible": "true"})
        self.switch_to = ClearableSwitchTo(FakeAlert(alert_text), cleared_by)

    def find_element(self, by, value):
        self.find_requests.append((by, value))
        if value == "XCUIElementTypeAlert" or not self.cleared_by.clicked:
            raise RuntimeError("not found")
        return self.target


def _prompt_blocked_client(alert_text: str = NOTIFICATION_PROMPT_TEXT, **config) -> tuple[AppiumDeviceClient, PromptBlockedDriver]:
    allow, deny = _allow_or_deny()
    driver = PromptBlockedDriver(alert_text, [allow, deny], deny)
    client = AppiumDeviceClient(SimpleNamespace(), {"action": "dismiss", **config})
    client.driver = driver
    return client, driver


@pytest.fixture
def short_prompt_wait(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.ios.device_client.BLOCKING_PROMPT_WAIT_SECONDS", 0.1)


def test_a_lookup_blocked_by_a_permission_prompt_answers_it_and_retries_once(short_prompt_wait):
    client, driver = _prompt_blocked_client()

    result = client.tap_label(["Next"], 0)

    assert result["tapped"] is True
    assert driver.target.clicked
    assert driver.cleared_by.clicked
    recovered = client.drain_recovered_prompts()
    assert [(item["operation"], item["status"], item["text"]) for item in recovered] == [
        ("tap_label", "HANDLED", NOTIFICATION_PROMPT_TEXT)
    ]
    assert client.drain_recovered_prompts() == []


def test_the_local_network_prompt_is_still_allowed_when_it_blocks_a_lookup(short_prompt_wait):
    allow, deny = _allow_or_deny()
    driver = PromptBlockedDriver(LOCAL_NETWORK_ALERT, [allow, deny], allow)
    client = AppiumDeviceClient(
        SimpleNamespace(), {"action": "dismiss", "accept_if_text_contains": ["find and connect to devices on your local network"]}
    )
    client.driver = driver

    client.tap_label(["Next"], 0)

    assert allow.clicked
    assert not deny.clicked


def test_a_failed_lookup_with_no_prompt_raises_the_original_error(short_prompt_wait):
    client, driver = _prompt_blocked_client()
    driver.switch_to = SimpleNamespace()

    with pytest.raises(RuntimeError, match="not found"):
        client.tap_label(["Next"], 0)

    assert client.drain_recovered_prompts() == []


def test_an_alert_that_is_not_a_permission_prompt_is_left_for_the_test(short_prompt_wait):
    ok = FakeElement({"type": "XCUIElementTypeButton", "label": "OK", "name": "OK", "enabled": "true", "visible": "true"})
    driver = PromptBlockedDriver("Unable to Connect to Server", [ok], ok)
    client = AppiumDeviceClient(SimpleNamespace(), {"action": "dismiss"})
    client.driver = driver

    with pytest.raises(RuntimeError, match="not found"):
        client.tap_label(["Next"], 0)

    assert not ok.clicked


def test_recovery_can_be_paused_for_a_step_that_inspects_the_prompt(short_prompt_wait):
    client, driver = _prompt_blocked_client()

    with client.without_prompt_recovery():
        with pytest.raises(RuntimeError, match="not found"):
            client.tap_label(["Next"], 0)

    assert not driver.cleared_by.clicked
    assert client.tap_label(["Next"], 0)["tapped"] is True


def test_reading_an_absent_value_does_not_wait_for_prompts(short_prompt_wait):
    client, driver = _prompt_blocked_client()

    assert client.element_value_by_label(["Next"]) is None
    assert not driver.cleared_by.clicked


def test_a_client_without_prompt_rules_does_not_recover(short_prompt_wait):
    allow, deny = _allow_or_deny()
    driver = PromptBlockedDriver(NOTIFICATION_PROMPT_TEXT, [allow, deny], deny)
    client = _client(driver)

    with pytest.raises(RuntimeError, match="not found"):
        client.tap_label(["Next"], 0)

    assert not deny.clicked


def test_a_nested_lookup_recovers_only_once(short_prompt_wait, monkeypatch):
    client, driver = _prompt_blocked_client()
    driver.switch_to = SimpleNamespace()
    checks = []
    original = client.handle_permission_alerts
    monkeypatch.setattr(client, "handle_permission_alerts", lambda config=None: checks.append(config) or original(config))

    with pytest.raises(RuntimeError, match="not found"):
        client.tap_label(["Next"], 0)

    assert len(checks) == 1
    assert checks[0]["permission_prompts_only"] is True
    assert checks[0]["wait_seconds"] == 0.1
