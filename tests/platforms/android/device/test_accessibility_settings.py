from __future__ import annotations

import re

import pytest

from mobile_playbook.platforms.android.device import accessibility_settings
from mobile_playbook.platforms.android.device.accessibility_attacker import (
    ATTACKER_LABEL,
    SERVICE_COMPONENT,
    AccessibilityAttackerError,
    AccessibilitySettings,
)
from mobile_playbook.platforms.android.device.accessibility_settings import (
    ACCESSIBILITY_DETAILS_ACTION,
    ACCESSIBILITY_SETTINGS_ACTION,
    COMPONENT_NAME_EXTRA,
    SettingsLabels,
    _text_matcher,
    enable_service,
    open_service_page,
)

BEFORE = AccessibilitySettings(enabled_services=None, accessibility_enabled="0")
LIST_ACTION = ["shell", "am", "start", "-a", ACCESSIBILITY_SETTINGS_ACTION]
DETAILS_ACTION = ["shell", "am", "start", "-a", ACCESSIBILITY_DETAILS_ACTION, "--es", COMPONENT_NAME_EXTRA, SERVICE_COMPONENT]


class Element:
    def __init__(self, text="", cls="android.widget.TextView", checked=None, enabled=True, offscreen=False, on_click=None):
        self.text = text
        self.cls = cls
        self.checked = checked
        self.enabled = enabled
        self.offscreen = offscreen
        self.on_click = on_click
        self.clicks = 0

    def get_attribute(self, name):
        value = {"checked": self.checked, "enabled": self.enabled}[name]
        return None if value is None else str(value).lower()

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click(self)


class SettingsDriver:
    """Fake Appium driver showing one Settings screen at a time; UiSelector text regexes are evaluated as Java would."""

    def __init__(self, screens, screen, scrollable=True, screenshots_work=True):
        self.screens = screens
        self.screen = screen
        self.scrollable = scrollable
        self.screenshots_work = screenshots_work
        self.screenshots: list[str] = []
        self.polls = 0

    def find_elements(self, by, value):
        self.polls += 1
        elements = self.screens.get(self.screen, [])
        if by == "class name":
            return [element for element in elements if element.cls == value and not element.offscreen]
        assert by == "-android uiautomator"
        scrolling = value.startswith("new UiScrollable")
        if scrolling and not self.scrollable:
            raise RuntimeError("UiScrollable found no scrollable view")
        pattern = _python_pattern(value)
        return [element for element in elements if pattern.fullmatch(element.text) and (scrolling or not element.offscreen)]

    def get_screenshot_as_file(self, path):
        self.screenshots.append(path.rsplit("/", 1)[1])
        return self.screenshots_work


def _python_pattern(selector: str) -> re.Pattern:
    literal = re.search(r'textMatches\("((?:[^"\\]|\\.)*)"\)', selector)[1]
    java = literal.replace('\\"', '"').replace("\\\\", "\\")
    assert java.startswith("(?i)")
    python = re.sub(r"\\Q(.*?)\\E", lambda match: re.escape(match[1]), java[4:])
    return re.compile(python, re.IGNORECASE)


class Adb:
    def __init__(self, details_output="Starting: Intent { act=android.settings.ACCESSIBILITY_DETAILS_SETTINGS }"):
        self.details_output = details_output
        self.details_result = None
        self.commands: list[list[str]] = []
        self.on_details = None

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args == DETAILS_ACTION:
            if self.on_details:
                self.on_details()
            return self.details_result or (0, self.details_output, "")
        return 0, "Starting: Intent", ""


class Attacker:
    def __init__(self, adb, enabled=False, fallback_enables=True):
        self.adb = adb
        self.enabled = enabled
        self.fallback_enables = fallback_enables
        self.fallback_calls: list[AccessibilitySettings] = []

    def is_enabled(self):
        return self.enabled

    def enable_via_secure_settings(self, before):
        self.fallback_calls.append(before)
        self.enabled = self.fallback_enables


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(accessibility_settings, "POLL_SECONDS", 0)


def go(driver, screen):
    return lambda element: setattr(driver, "screen", screen)


def aosp_screens(driver, attacker, switch_enabled=True, consent_works=True):
    def allow(element):
        attacker.enabled = consent_works
        driver.screen = "service"

    driver.screens.update({
        "list": [
            Element("ScreenReader"),
            Element("DOWNLOADED APPS"),
            Element(ATTACKER_LABEL, offscreen=True, on_click=go(driver, "service")),
        ],
        "service": [
            Element(f"Use {ATTACKER_LABEL}"),
            Element(cls="android.widget.Switch", checked=False, enabled=switch_enabled, on_click=go(driver, "consent")),
            Element(f"{ATTACKER_LABEL} shortcut"),
            Element(cls="android.widget.Switch", checked=False),
        ],
        "consent": [Element("Deny"), Element("Allow", cls="android.widget.Button", on_click=allow)],
    })


def harmony_screens(driver, attacker, countdown_polls=3):
    checkbox = Element("I'm aware of and willing to assume these risks (4)", cls="android.widget.CheckBox", checked=False, enabled=False)
    countdown = {"left": countdown_polls}
    original_find = driver.find_elements

    def find(by, value):
        if driver.screen == "consent" and by == "class name" and value == "android.widget.CheckBox":
            countdown["left"] -= 1
            checkbox.enabled = countdown["left"] <= 0
        return original_find(by, value)

    def tick(element):
        element.checked = True

    def grant(element):
        if checkbox.checked:
            attacker.enabled = True

    checkbox.on_click = tick
    driver.find_elements = find
    driver.screens.update({
        "list": [Element("ScreenReader"), Element("Installed services", offscreen=True, on_click=go(driver, "installed"))],
        "installed": [Element("SuperHub"), Element(ATTACKER_LABEL, on_click=go(driver, "service"))],
        "service": [Element(ATTACKER_LABEL), Element(cls="android.widget.Switch", checked=False, on_click=go(driver, "consent"))],
        "consent": [Element("Security alert"), checkbox, Element("CANCEL"), Element("GRANT ACCESS", cls="android.widget.Button", on_click=grant)],
    })
    return checkbox


def setup(screen="list", **driver_options):
    adb = Adb()
    attacker = Attacker(adb)
    driver = SettingsDriver({}, screen, **driver_options)
    return adb, attacker, driver


def test_aosp_settings_scrolls_to_the_service_flips_its_main_switch_and_allows_it(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker)

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=1)

    assert outcome.method == "settings_ui"
    assert outcome.ui_failures == []
    assert adb.commands == [LIST_ACTION]
    assert driver.screens["service"][3].clicks == 0
    assert driver.screenshots == ["accessibility_settings.png", "accessibility_service_page.png", "accessibility_consent.png"]
    assert [item["kind"] for item in outcome.evidence] == ["accessibility_settings", "accessibility_service_page", "accessibility_consent"]
    assert attacker.fallback_calls == []


def test_harmonyos_opens_installed_services_and_ticks_the_risk_box_after_its_countdown(tmp_path):
    adb, attacker, driver = setup()
    checkbox = harmony_screens(driver, attacker)

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=1)

    assert outcome.method == "settings_ui"
    assert checkbox.checked is True
    assert checkbox.clicks == 1
    assert attacker.fallback_calls == []


@pytest.mark.parametrize("category", ["Installed apps", "Downloaded services"])
def test_a_category_of_downloaded_services_is_opened_when_the_service_is_not_on_the_main_page(tmp_path, category):
    _, attacker, driver = setup()
    aosp_screens(driver, attacker)
    driver.screens["list"] = [Element(category, on_click=go(driver, "downloaded"))]
    driver.screens["downloaded"] = [Element(ATTACKER_LABEL, on_click=go(driver, "service"))]

    assert open_service_page(driver, SettingsLabels(), timeout=1)
    assert driver.screen == "service"


def test_a_page_without_a_scrollable_view_is_still_searched(tmp_path):
    _, attacker, driver = setup(scrollable=False)
    aosp_screens(driver, attacker)
    driver.screens["list"][2].offscreen = False

    assert enable_service(driver, attacker, BEFORE, tmp_path, timeout=1).method == "settings_ui"


def test_a_service_that_is_already_on_is_left_alone(tmp_path):
    _, attacker, driver = setup()
    aosp_screens(driver, attacker)
    attacker.enabled = True
    driver.screens["service"][1].checked = True

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=1)

    assert outcome.method == "settings_ui"
    assert outcome.already_enabled is True
    assert driver.screens["service"][1].clicks == 0


def test_the_details_page_is_used_when_the_service_is_not_listed(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker)
    driver.screens["list"] = [Element("ScreenReader")]
    adb.on_details = lambda: setattr(driver, "screen", "service")

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.method == "details_page"
    assert outcome.ui_failures == [f"settings_ui: {ATTACKER_LABEL!r} was not found in Accessibility settings"]
    assert adb.commands == [LIST_ACTION, DETAILS_ACTION]


def test_secure_settings_are_used_when_no_details_page_handles_the_intent(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker)
    driver.screens["list"] = []
    adb.details_output = "Error: Activity not started, unable to resolve Intent"

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.method == "secure_settings_fallback"
    assert outcome.ui_failures[1] == "details_page: Error: Activity not started, unable to resolve Intent"
    assert attacker.fallback_calls == [BEFORE]


def test_a_details_page_refused_to_the_shell_falls_back_to_secure_settings(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker)
    driver.screens["list"] = []
    adb.details_result = (255, "", "Exception occurred while executing 'start':\njava.lang.SecurityException: Permission Denial: requires android.permission.OPEN_ACCESSIBILITY_DETAILS_SETTINGS")

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.method == "secure_settings_fallback"
    assert outcome.ui_failures[1] == "details_page: Exception occurred while executing 'start':"


def test_the_text_matcher_has_no_backslashes_for_appiums_parser():
    assert "\\" not in _text_matcher(("Grant access", "Don't allow", 'Say "yes"'))


def test_a_disabled_switch_is_reported_as_a_restricted_setting_and_falls_back(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker, switch_enabled=False)
    adb.on_details = lambda: setattr(driver, "screen", "service")

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.method == "secure_settings_fallback"
    assert outcome.ui_failures == [
        "settings_ui: the service switch is disabled, as a restricted setting is",
        "details_page: the service switch is disabled, as a restricted setting is",
    ]
    assert driver.screens["service"][1].clicks == 0


def test_consent_that_never_enables_the_service_times_out_and_falls_back(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker, consent_works=False)
    adb.details_output = "Error: Activity not started"

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.ui_failures[0] == "settings_ui: the service was not enabled within 0.2s of flipping its switch"
    assert outcome.method == "secure_settings_fallback"


def test_an_unrecognised_consent_screen_is_named_in_the_failure(tmp_path):
    adb, attacker, driver = setup()
    aosp_screens(driver, attacker)
    driver.screens["consent"] = [Element("Turn on?"), Element("Yes", cls="android.widget.Button")]
    adb.details_output = "Error: Activity not started"

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)

    assert outcome.ui_failures[0].endswith("; no consent prompt was recognised")
    assert "accessibility_consent.png" not in driver.screenshots


def test_a_fallback_that_does_not_enable_the_service_is_an_error(tmp_path):
    adb, attacker, driver = setup()
    attacker.fallback_enables = False
    adb.details_output = "Error: Activity not started"

    with pytest.raises(AccessibilityAttackerError, match="not enabled after the secure settings fallback"):
        enable_service(driver, attacker, BEFORE, tmp_path, timeout=0.2)


def test_missing_screenshots_are_recorded_without_stopping(tmp_path):
    _, attacker, driver = setup(screenshots_work=False)
    aosp_screens(driver, attacker)

    outcome = enable_service(driver, attacker, BEFORE, tmp_path, timeout=1)

    assert outcome.method == "settings_ui"
    assert outcome.evidence == []
    assert outcome.evidence_errors[0] == "accessibility_settings.png: Appium did not save the file"


def test_the_text_matcher_matches_whole_texts_ignoring_case_and_escapes_quotes():
    selector = _text_matcher(("Grant access", 'Say "yes" (1)'))
    pattern = _python_pattern(selector)

    assert selector.startswith('new UiSelector().textMatches("(?i)(')
    assert pattern.fullmatch("GRANT ACCESS")
    assert pattern.fullmatch('say "YES" (1)')
    assert not pattern.fullmatch("Grant access now")
