"""Exercises the accessibility event capture risk with synthetic adb, Appium, Settings and Event Spy capture fakes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import accessibility_service
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.event_spy import EVENT_SPY_APP, EVENT_SPY_PACKAGE, CapturedEvent, EventSpy
from mobile_playbook.platforms.android.device.scripted_steps import DEFAULT_CANARIES
from mobile_playbook.platforms.android.risks import feature_05_risk_02_activity_capture as risk
from mobile_playbook.platforms.android.risks import get_risk
from tests.platforms.android.prompt_fakes import png

PACKAGE = "com.example.bank"
RISK = "android-feature-05-risk-02"
CARD_ID = f"{PACKAGE}:id/card"
PASSWORD_ID = f"{PACKAGE}:id/password"
TYPE_CARD = {"type": {"id": CARD_ID, "value": "{canary.card}"}}
TYPE_PASSWORD = {"type": {"id": PASSWORD_ID, "value": "{canary.password}"}}

TAP_TARGETS: dict[tuple[int, int], "Element"] = {}


class Element:
    _next = 0

    def __init__(self, *, text="", cls="android.widget.TextView", checked=None, on_click=None, on_type=None, on_tap=None):
        self.text = text
        self.cls = cls
        self.checked = checked
        self.on_click = on_click
        self.on_type = on_type
        self.on_tap = on_tap
        self.value = ""
        Element._next += 20
        self.cx, self.cy = 100, Element._next
        TAP_TARGETS[(self.cx, self.cy)] = self

    def get_attribute(self, name):
        if name == "bounds":
            return f"[{self.cx - 5},{self.cy - 5}][{self.cx + 5},{self.cy + 5}]"
        value = {"checked": self.checked, "enabled": True}[name]
        return None if value is None else str(value).lower()

    def click(self):
        if self.on_tap:
            self.on_tap(self)
        if self.on_click:
            self.on_click()

    def clear(self):
        self.value = ""

    # Setting the text, as Appium's element send_keys does, fires no text-change event on the device.
    def send_keys(self, value):
        self.value += value

    # A key press reaches the field as a keyboard's does, so Event Spy sees each change.
    def press(self, key):
        self.value += key
        if self.on_type:
            self.on_type(self.value)


def _text_pattern(selector):
    literal = re.search(r'textMatches\("((?:[^"\\]|\\.)*)"\)', selector)[1]
    java = literal.replace('\\"', '"').replace("\\\\", "\\")
    python = re.sub(r"\\Q(.*?)\\E", lambda match: re.escape(match[1]), java[4:])
    return re.compile(python, re.IGNORECASE)


class Driver:
    def __init__(self, bench):
        self.bench = bench
        self.calls = []
        self.screenshots = []
        self.focused = None

    # W3C key actions, as ActionChains sends them, pressed into the field tapped last.
    def execute(self, command, params=None):
        assert command == "actions"
        for device in params["actions"]:
            if device["type"] == "key":
                for action in device["actions"]:
                    if action["type"] == "keyDown":
                        self.focused.press(action["value"])
        return {"value": None}

    @property
    def current_package(self):
        return self.bench.foreground

    def find_elements(self, by, value):
        bench = self.bench
        if by == "class name" and value == "android.widget.Switch":
            return [bench.switch]
        if by == "class name":
            return []
        if by == "-android uiautomator" and "textMatches" in value:
            return [bench.service_entry] if _text_pattern(value).fullmatch(EVENT_SPY_APP.label) else []
        if by == "id" and value in bench.fields:
            return [bench.fields[value]]
        return []

    def activate_app(self, package):
        self.calls.append(("activate", package))
        self.bench.foreground = self.bench.foreground_after_launch if package == PACKAGE else package

    def get_screenshot_as_file(self, path):
        self.screenshots.append(Path(path).name)
        Path(path).write_bytes(png(1080, 1920))
        return True

    def terminate_app(self, package):
        self.calls.append(("terminate", package))

    def quit(self):
        self.calls.append(("quit", None))


class Writer:
    run_timestamp = "2026-10-22_00-00-00"

    def __init__(self, root):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        assert (risk_id, test_case_id, platform) == (RISK, "accessibility_activity_capture", "android")
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class Bench:
    """A phone with Event Spy installed, whose service captures the app's fields as each field's mode says."""

    def __init__(self, root, monkeypatch, host):
        self.host = host
        self.device = host.add("SPY-TEST", state="device")
        self.device.packages[EVENT_SPY_PACKAGE] = ("1.0", "1")
        self.settings = {"accessibility_enabled": "0"}
        self.foreground = "com.android.launcher"
        self.foreground_after_launch = PACKAGE
        self.binds = True
        self.clears = 0
        self.captured: list[CapturedEvent] = []
        # How Event Spy sees each field: "plain" text, "masked" password text, or "hidden" from accessibility.
        self.modes = {CARD_ID: "plain", PASSWORD_ID: "masked"}
        self.fields = {CARD_ID: self._field(CARD_ID), PASSWORD_ID: self._field(PASSWORD_ID)}
        self.switch = Element(cls="android.widget.Switch", checked=False, on_click=self._grant)
        self.service_entry = Element(text=EVENT_SPY_APP.label)
        self.base_answer = host._answer
        monkeypatch.setattr(host, "_answer", self.answer)
        monkeypatch.setattr(EventSpy, "events", lambda spy, package=None: [event for event in self.captured if package in (None, event.package)])
        self.adb = AdbClient(serial="SPY-TEST")
        self.driver = Driver(self)
        self.writer = Writer(root)
        self.app = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={RISK: {"steps": [TYPE_CARD]}})
        self.cfg = SimpleNamespace(accessibility={"launch_wait": 0, "step_timeout_seconds": 0, "attack_timeout": 0, "settle_seconds": 0})

    def _field(self, view_id):
        def typed(value):
            if not self.bound():
                return
            self.capture("FOCUS", view_id=view_id)
            mode = self.modes[view_id]
            if mode == "plain":
                self.capture("TEXT_CHANGE", view_id=view_id, text=value)
            elif mode == "masked":
                self.capture("TEXT_CHANGE", view_id=view_id, text="•" * len(value))

        return Element(cls="android.widget.EditText", on_type=typed, on_tap=lambda element: setattr(self.driver, "focused", element))

    def capture(self, event_type, package=PACKAGE, view_id=None, text=None):
        self.captured.append(CapturedEvent(len(self.captured) + 1, 1000 + len(self.captured), event_type, package, None, view_id, text, None, None, None, None))

    def _grant(self):
        self.settings["enabled_accessibility_services"] = EVENT_SPY_APP.component
        self.switch.checked = True
        self.capture("CLICK", package="com.android.settings")

    def enabled(self):
        return EVENT_SPY_APP.names_service(accessibility_service.enabled_components(self.settings.get("enabled_accessibility_services")))

    def bound(self):
        return self.enabled() and self.binds

    def make_driver(self, extra_capabilities=None):
        assert extra_capabilities == {"appium:disableSuppressAccessibilityService": True}
        return self.driver

    def answer(self, serial, args):
        if args[:3] == ["shell", "input", "tap"]:
            target = TAP_TARGETS.get((int(args[3]), int(args[4])))
            if target is not None:
                target.click()
            return 0, "", ""
        if args == ["shell", "getprop", "ro.product.model"]:
            return 0, "SM-A346E\n", ""
        if args == ["shell", "getprop", "ro.build.version.sdk"]:
            return 0, "36\n", ""
        if args[:3] == ["shell", "pm", "path"]:
            return 0, "package:/data/app/synthetic/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, "a" * 64 + "  /data/app/synthetic/base.apk\n", ""
        if args[:3] == ["shell", "pm", "query-services"]:
            return 0, f"packageName={EVENT_SPY_PACKAGE}\n", ""
        if args == ["shell", "pm", "clear", EVENT_SPY_PACKAGE]:
            self.clears += 1
            self.captured = []
            return 0, "Success\n", ""
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, f"{self.settings.get(args[4], 'null')}\n", ""
        if args[:4] == ["shell", "settings", "put", "secure"]:
            self.settings[args[4]] = args[5].strip("'\"")
            return 0, "", ""
        if args[:4] == ["shell", "settings", "delete", "secure"]:
            self.settings.pop(args[4], None)
            return 0, "Deleted 1 rows\n", ""
        if args[:3] == ["shell", "dumpsys", "accessibility"]:
            bound = f"Service[label={EVENT_SPY_APP.label}, capabilities=1]" if self.bound() else ""
            return 0, f"     Bound services:{{{bound}}}\n", ""
        if args == ["shell", "am", "force-stop", EVENT_SPY_PACKAGE]:
            others = [c for c in accessibility_service.enabled_components(self.settings.get("enabled_accessibility_services")) if not EVENT_SPY_APP.names_service([c])]
            self.settings["enabled_accessibility_services"] = ":".join(others)
            return 0, "", ""
        return self.base_answer(serial, args)

    def run(self):
        return risk.AndroidAccessibilityActivityCaptureRisk().run(self.app, self.cfg, SimpleNamespace(adb=self.adb, make_driver=self.make_driver), self.writer)


@pytest.fixture
def bench(tmp_path, monkeypatch, adb_host):
    monkeypatch.setattr(risk.time, "sleep", lambda _: None)
    monkeypatch.setattr(risk, "POLL_SECONDS", 0)
    monkeypatch.setattr(accessibility_service, "POLL_SECONDS", 0)
    monkeypatch.setattr(accessibility_service, "UNBIND_SECONDS", 0)
    TAP_TARGETS.clear()
    return Bench(tmp_path, monkeypatch, adb_host)


def test_a_typed_canary_captured_in_plain_text_is_at_risk(bench):
    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_INPUT_CAPTURED", "At Risk")
    assert result.metadata["verdict_rule"] == "canary_captured"
    assert result.metadata["capture"]["canaries"] == {"card": {"captured": True, "view_id": CARD_ID, "masked": False}}
    assert result.metadata["coverage"] == "scripted"
    assert result.metadata["service_enable"]["method"] == "settings_ui"
    assert result.metadata["device"] == {"model": "SM-A346E", "sdk": "36"}


def test_only_the_apps_events_are_saved_as_evidence(bench):
    result = bench.run()

    [entry] = [entry for entry in result.evidence if entry["kind"] == "accessibility_captured_events"]
    saved = json.loads(Path(entry["path"]).read_text())
    assert {event["package"] for event in saved} == {PACKAGE}
    texts = [event["text"] for event in saved if event["event_type"] == "TEXT_CHANGE"]
    assert texts[0] == DEFAULT_CANARIES["card"][0] and texts[-1] == DEFAULT_CANARIES["card"]
    assert len(texts) == len(DEFAULT_CANARIES["card"])
    assert {entry["kind"] for entry in result.evidence} >= {"accessibility_target_after", "accessibility_event_spy_screen", "accessibility_list_after", "accessibility_service_on"}
    assert ("activate", EVENT_SPY_PACKAGE) in bench.driver.calls


def test_the_steps_follow_the_playbook_and_cleanup_clears_and_uninstalls_event_spy(bench):
    bench.settings["enabled_accessibility_services"] = "com.other/svc"
    bench.settings["accessibility_enabled"] = "1"

    result = bench.run()

    performed = result.metadata["steps_performed"]
    assert [performed[step]["outcome"] for step in risk.STEPS] == ["completed", "provided", "provided", "completed", "completed", "completed"]
    assert result.metadata["cleanup"] == {"settings_restored": True, "service_off": True, "data_cleared": True, "attacker_uninstalled": True, "errors": []}
    assert bench.settings == {"enabled_accessibility_services": "com.other/svc", "accessibility_enabled": "1"}
    assert bench.clears == 2
    assert EVENT_SPY_PACKAGE not in bench.device.packages


def test_a_capture_the_app_reformatted_still_matches(bench):
    bench.modes[CARD_ID] = "plain"
    original = bench.fields[CARD_ID].on_type
    bench.fields[CARD_ID].on_type = lambda value: original(value.replace(" ", ""))

    result = bench.run()

    assert result.verdict == "At Risk"


def test_a_field_hidden_from_accessibility_is_reduced_risk(bench):
    bench.modes[CARD_ID] = "hidden"

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_INPUT_PROTECTED", "Reduced Risk")
    assert result.metadata["verdict_rule"] == "target_events_without_canaries"
    assert result.metadata["capture"]["canaries"]["card"] == {"captured": False, "view_id": None, "masked": False}


def test_a_masked_password_alone_is_inconclusive(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_PASSWORD]}}

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "Only password fields were typed into" in result.metadata["verdict"]
    assert result.metadata["capture"]["canaries"]["password"]["masked"] is True


def test_a_masked_password_beside_a_hidden_field_is_reduced_risk(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_PASSWORD, TYPE_CARD]}}
    bench.modes[CARD_ID] = "hidden"

    result = bench.run()

    assert result.verdict == "Reduced Risk"


def test_a_masked_password_beside_a_captured_field_is_at_risk(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_PASSWORD, TYPE_CARD]}}

    result = bench.run()

    assert result.verdict == "At Risk"
    assert result.metadata["capture"]["canaries"]["password"] == {"captured": False, "view_id": None, "masked": True}


def test_no_typed_canary_is_inconclusive_with_a_summary_of_the_events(bench):
    bench.app.risks = {}
    bench.foreground_after_launch = PACKAGE

    def launch_click(package):
        bench.foreground = PACKAGE
        bench.capture("CLICK")

    bench.driver.activate_app = lambda package: (bench.driver.calls.append(("activate", package)), launch_click(package) if package == PACKAGE else setattr(bench, "foreground", package))

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert result.metadata["coverage"] == "launch_only"
    assert "No canary was typed" in result.metadata["verdict"] and "1 click" in result.metadata["verdict"]


def test_steps_that_stop_early_without_a_capture_are_inconclusive(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_CARD, {"tap": {"id": f"{PACKAGE}:id/missing"}}]}}
    bench.modes[CARD_ID] = "hidden"

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert result.metadata["coverage"] == "scripted_partial"
    assert "stopped early" in result.metadata["verdict"]


def test_no_events_from_the_app_is_inconclusive(bench):
    bench.fields[CARD_ID].on_type = None

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "recorded no events from the app" in result.metadata["verdict"]


def test_a_service_that_is_not_bound_is_inconclusive_and_the_app_is_not_used(bench):
    bench.binds = False

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "did not bind it" in result.metadata["verdict"]
    assert ("activate", PACKAGE) not in bench.driver.calls
    assert result.metadata["cleanup"]["attacker_uninstalled"] is True


def test_an_app_that_redirects_on_launch_is_inconclusive(bench):
    bench.foreground_after_launch = "com.android.vending"

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "Redirected to" in result.metadata["verdict"]


def test_event_spy_is_found_by_its_own_name_whatever_service_label_is_configured(bench):
    bench.cfg.accessibility["settings_labels"] = {"service": "Playbook Accessibility Test"}

    result = bench.run()

    assert result.metadata["service_enable"]["method"] == "settings_ui"


def test_a_stale_event_spy_service_is_dropped_from_the_settings_to_restore(bench):
    bench.settings["enabled_accessibility_services"] = f"com.other/svc:{EVENT_SPY_APP.short_component}"
    bench.settings["accessibility_enabled"] = "1"

    result = bench.run()

    assert result.metadata["stale_attacker_service"] is True
    assert bench.settings["enabled_accessibility_services"] == "com.other/svc"


def test_a_failure_reading_the_capture_fails_the_run_and_still_cleans_up(bench, monkeypatch):
    def unreadable(spy, package=None):
        raise accessibility_service.AccessibilityAttackerError("Event Spy's database cannot be read with run-as: not debuggable")

    monkeypatch.setattr(EventSpy, "events", unreadable)

    result = bench.run()

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert result.metadata["cleanup"]["data_cleared"] is True
    assert EVENT_SPY_PACKAGE not in bench.device.packages


def test_event_spy_that_cannot_be_uninstalled_is_reported_without_changing_the_verdict(bench):
    bench.device.uninstall_fails.add(EVENT_SPY_PACKAGE)

    result = bench.run()

    assert result.verdict == "At Risk"
    assert result.metadata["cleanup"]["attacker_uninstalled"] is False
    assert any(f"remove {EVENT_SPY_PACKAGE} by hand" in error for error in result.errors)


def test_the_risk_requires_event_spy():
    assert get_risk(RISK).requires == ["adb", "appium", "event_spy_attacker"]


@pytest.mark.parametrize("verdict,status,security", [
    ("CAPTURED (x)", "A11Y_INPUT_CAPTURED", "At Risk"),
    ("PROTECTED (x)", "A11Y_INPUT_PROTECTED", "Reduced Risk"),
    ("INCONCLUSIVE (x)", "A11Y_NOT_TESTED", "Inconclusive"),
    ("ERROR (x)", "FAILED", "Inconclusive"),
])
def test_verdict_mapping(verdict, status, security):
    assert (risk._status_from_verdict(verdict), risk._security_verdict_from_verdict(verdict)) == (status, security)


def test_an_app_that_leaves_the_foreground_during_the_input_is_inconclusive_without_a_capture(bench):
    bench.modes[CARD_ID] = "hidden"
    original = bench.fields[CARD_ID].on_type

    def typed_then_left(value):
        original(value)
        bench.foreground = "com.android.chrome"

    bench.fields[CARD_ID].on_type = typed_then_left

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "no longer in front" in result.metadata["verdict"]
