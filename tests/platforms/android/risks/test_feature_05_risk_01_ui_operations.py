"""Exercises the accessibility UI operations risk with synthetic adb, Appium, attacker output and settings fakes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device.accessibility_attacker import (
    ATTACKER_PACKAGE,
    OUTPUT_DIR,
    SERVICE_COMPONENT,
    SERVICE_SHORT_COMPONENT,
)
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.risks import feature_05_risk_01_ui_operations as risk
from tests.platforms.android.prompt_fakes import png

PACKAGE = "com.example.bank"
RISK = "android-feature-05-risk-01"
SERVICE_LABEL = "Playbook Accessibility Test"
LOGOUT_ID = f"{PACKAGE}:id/btn_logout"
LOGGED_OUT = {"id": f"{PACKAGE}:id/username"}
PERMISSION_CONTROLLER = "com.android.permissioncontroller"
BIOMETRIC_WINDOW = "  Window{abc123 u0 BiometricPrompt}:\n"


def events(*names, package=PACKAGE):
    built = []
    for name in names:
        record = {"timestamp": "t", "event": name, "package": package, "message": name}
        if name == "click":
            record |= {"view_id": LOGOUT_ID, "class_name": "android.widget.Button", "text": "LOGOUT"}
        if name == "attack_successful":
            record |= {"strategy": "resource_id", "pattern": "btn_logout"}
        built.append(record)
    return built


LOGOUT_EVENTS = events("service_connected", "scan", "search", "click", "attack_successful", "scan", "no_logout_found")


# Elements register their centre so an adb `input tap` at that point reaches the element enable_service tapped.
TAP_TARGETS: dict[tuple[int, int], "Element"] = {}


class Element:
    _next = 0

    def __init__(self, *, text="", cls="android.widget.TextView", checked=None, enabled=True, on_click=None):
        self.text = text
        self.cls = cls
        self.checked = checked
        self.enabled = enabled
        self.on_click = on_click
        self.clicks = 0
        self.value = ""
        Element._next += 20
        self.cx, self.cy = 100, Element._next
        TAP_TARGETS[(self.cx, self.cy)] = self

    def get_attribute(self, name):
        if name == "bounds":
            return f"[{self.cx - 5},{self.cy - 5}][{self.cx + 5},{self.cy + 5}]"
        value = {"checked": self.checked, "enabled": self.enabled}[name]
        return None if value is None else str(value).lower()

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value += value


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

    @property
    def current_package(self):
        return self.bench.foreground

    def make(self):
        return self

    def find_elements(self, by, value):
        bench = self.bench
        if by == "class name" and value == "android.widget.Switch":
            return [bench.switch]
        if by == "class name" and value == "android.widget.CheckBox":
            return []
        if by == "-android uiautomator" and "textMatches" in value:
            pattern = _text_pattern(value)
            if pattern.fullmatch(SERVICE_LABEL):
                return [bench.service_entry]
            return []
        if value == LOGGED_OUT["id"]:
            return [Element(text="Username")] if bench.logged_out_always or (bench.attacked and bench.signed_out_after) else []
        if by in ("id", "accessibility id") and value in bench.present:
            return [bench.present[value]]
        if by == "-android uiautomator":
            match = re.search(r'\.text\("((?:[^"\\]|\\.)*)"\)', value)
            text = match[1] if match else None
            return [Element(text=text)] if text in bench.present_texts else []
        return []

    def activate_app(self, package):
        self.calls.append(("activate", package))
        self.bench.foreground = self.bench.foreground_after_launch if package == PACKAGE else self.bench.after_activate.get(package, package)
        if package == PACKAGE and self.bench.bound():
            self.bench.attacked = True

    @property
    def page_source(self):
        return self.bench.page_source

    def get_screenshot_as_file(self, path):
        if not self.bench.screenshots_work:
            return False
        self.screenshots.append(path)
        Path(path).write_bytes(png(1080, 1920))
        return True

    def terminate_app(self, package):
        self.calls.append(("terminate", package))
        if self.bench.termination_error:
            raise RuntimeError("synthetic terminate failure")

    def quit(self):
        self.calls.append(("quit", None))

    def back(self):
        self.calls.append(("back", None))


class Writer:
    run_timestamp = "2026-10-22_00-00-00"

    def __init__(self, root):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        assert (risk_id, test_case_id, platform) == (RISK, "accessibility_ui_operations", "android")
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class Bench:
    def __init__(self, root, monkeypatch, host):
        self.host = host
        self.device = host.add("A11Y-TEST", state="device")
        self.device.packages[ATTACKER_PACKAGE] = ("1", "1")
        self.settings = {"accessibility_enabled": "0"}
        self.foreground = PACKAGE
        self.foreground_after_launch = PACKAGE
        self.after_activate = {}
        self.attacked = False
        self.logged_out_always = False
        self.signed_out_after = False
        self.attack_events = LOGOUT_EVENTS
        self.binds = True
        self.target = None
        self.auth = False
        self.service_declared = True
        self.screenshots_work = True
        self.termination_error = False
        self.restore_fails = False
        self.page_source = f'<hierarchy><node package="{PACKAGE}" resource-id="{LOGOUT_ID}" text="LOGOUT" content-desc=""/></hierarchy>'
        self.present_texts = set()
        self.present = {}
        self.switch = Element(cls="android.widget.Switch", checked=False, on_click=self._grant)
        self.service_entry = Element(text=SERVICE_LABEL)
        self.base_answer = host._answer
        monkeypatch.setattr(host, "_answer", self.answer)
        self.adb = AdbClient(serial="A11Y-TEST")
        self.driver = Driver(self)
        self.writer = Writer(root)
        self.app = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={})
        self.cfg = SimpleNamespace(accessibility={"launch_wait": 0, "step_timeout_seconds": 0, "attack_timeout": 0, "settle_seconds": 0})

    def _grant(self):
        self.settings["enabled_accessibility_services"] = SERVICE_COMPONENT

    def enabled(self):
        from mobile_playbook.platforms.android.device.accessibility_attacker import enabled_components, names_attacker_service
        return names_attacker_service(enabled_components(self.settings.get("enabled_accessibility_services")))

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
        if args[:3] == ["shell", "pm", "path"]:
            return 0, "package:/data/app/synthetic/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, "a" * 64 + "  /data/app/synthetic/base.apk\n", ""
        if args[:3] == ["shell", "pm", "query-services"]:
            return (0, f"packageName={ATTACKER_PACKAGE}\n", "") if self.service_declared else (1, "", "err")
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, f"{self.settings.get(args[4], 'null')}\n", ""
        if args[:4] == ["shell", "settings", "put", "secure"]:
            if self.restore_fails:
                return 0, "", ""
            self.settings[args[4]] = args[5].strip("'\"")
            return 0, "", ""
        if args[:4] == ["shell", "settings", "delete", "secure"]:
            if not self.restore_fails:
                self.settings.pop(args[4], None)
            return 0, "Deleted 1 rows\n", ""
        if args[:3] == ["shell", "dumpsys", "accessibility"]:
            bound = "Service[label=Playbook Accessibility Test, capabilities=1]" if self.bound() else ""
            return 0, f"     Bound services:{{{bound}}}\n", ""
        if args[:3] == ["shell", "am", "force-stop"] and args[3] == ATTACKER_PACKAGE:
            from mobile_playbook.platforms.android.device.accessibility_attacker import enabled_components, names_attacker_service
            others = [c for c in enabled_components(self.settings.get("enabled_accessibility_services")) if not names_attacker_service([c])]
            self.settings["enabled_accessibility_services"] = ":".join(others)
            return 0, "", ""
        if args[:3] == ["shell", "am", "start"] and f"{ATTACKER_PACKAGE}/.MainActivity" in args:
            self.target = PACKAGE
            self.attacked = False
            return 0, "Starting", ""
        if args[:3] == ["shell", "dumpsys", "window"]:
            return 0, (BIOMETRIC_WINDOW if self.auth else "no overlay windows\n"), ""
        if args[:3] == ["shell", "dumpsys", "activity"]:
            return 0, f"  topResumedActivity=ActivityRecord{{1 u0 {self.foreground}/.Main t1}}\n", ""
        if args[:2] == ["shell", "cat"]:
            if args[-1] == f"{OUTPUT_DIR}/status.json":
                if self.target is None:
                    return 1, "", "missing"
                return 0, json.dumps({"state": "prepared", "target_package": self.target, "service_connected": self.bound()}), ""
            if args[-1] == f"{OUTPUT_DIR}/events.json":
                return 0, json.dumps(self.attack_events if self.attacked else []), ""
        return self.base_answer(serial, args)

    def run(self):
        return risk.AndroidAccessibilityUiOperationsRisk().run(self.app, self.cfg, SimpleNamespace(adb=self.adb, make_driver=self.make_driver), self.writer)

    @property
    def commands(self):
        return [args for serial, args in self.host.commands if serial == "A11Y-TEST"]


@pytest.fixture
def bench(tmp_path, monkeypatch, adb_host):
    monkeypatch.setattr(risk.time, "sleep", lambda _: None)
    monkeypatch.setattr(risk, "POLL_SECONDS", 0)
    monkeypatch.setattr(risk, "UNBIND_SECONDS", 0)
    TAP_TARGETS.clear()
    return Bench(tmp_path, monkeypatch, adb_host)


def with_logged_out(bench):
    bench.app.risks = {RISK: {"logged_out": LOGGED_OUT}}
    bench.present = {}


def test_logout_click_with_logged_out_locator_is_at_risk(bench):
    with_logged_out(bench)
    bench.signed_out_after = True

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_ACTION_PERFORMED", "At Risk")
    assert result.metadata["verdict_rule"] == "logged_out_locator"
    assert result.metadata["attack"]["successes"] == [{"strategy": "resource_id", "pattern": "btn_logout"}]
    assert result.metadata["cleanup"] == {"settings_restored": True, "service_off": True, "attacker_uninstalled": True, "errors": []}
    assert not bench.enabled()
    assert ATTACKER_PACKAGE not in bench.device.packages
    assert {entry["kind"] for entry in result.evidence} >= {"accessibility_attacker_events", "accessibility_target_before", "accessibility_target_after"}
    assert all(Path(entry["path"]).is_file() for entry in result.evidence if entry["path"].endswith((".png", ".json")))


def test_logout_click_and_window_change_without_a_locator_is_at_risk(bench):
    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_ACTION_PERFORMED", "At Risk")
    assert result.metadata["verdict_rule"] == "click_and_window_change"


def test_click_with_no_window_change_without_a_locator_is_inconclusive(bench):
    bench.attack_events = events("service_connected", "scan", "search", "click", "attack_successful")

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert result.metadata["verdict_rule"] == "click_and_window_change"
    assert "windows did not change" in result.metadata["verdict"]


def test_click_that_opens_an_auth_prompt_is_reduced_risk(bench):
    with_logged_out(bench)
    bench.auth = True

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_ACTION_BLOCKED", "Reduced Risk")
    assert result.metadata["verdict_rule"] == "auth_prompt"


def test_click_that_leaves_the_app_signed_in_without_a_prompt_is_inconclusive(bench):
    with_logged_out(bench)

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert result.metadata["verdict_rule"] == "logged_out_locator"
    assert "stayed signed in" in result.metadata["verdict"]


def test_a_visible_control_the_attacker_cannot_find_is_reduced_risk(bench):
    bench.attack_events = events("service_connected", "scan", "search", "no_logout_found")

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_ACTION_BLOCKED", "Reduced Risk")
    assert result.metadata["verdict_rule"] == "hidden_logout_control"


def test_a_screen_without_a_logout_control_is_inconclusive(bench):
    bench.page_source = f'<hierarchy><node package="{PACKAGE}" resource-id="{PACKAGE}:id/home" text="Home"/></hierarchy>'
    bench.attack_events = events("service_connected", "scan", "search", "no_logout_found")

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert result.metadata["verdict_rule"] is None
    assert result.metadata["logout_control"]["visible"] is False


def test_a_service_that_is_not_bound_is_inconclusive(bench):
    bench.binds = False

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert "did not bind" in result.metadata["verdict"]
    assert result.metadata["service_bound"] is False


def test_no_scan_of_the_app_is_inconclusive(bench):
    bench.attack_events = events("service_connected")

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert "never scanned" in result.metadata["verdict"]


def test_a_hand_off_before_the_attack_is_inconclusive_and_does_not_enable_the_service(bench):
    bench.foreground_after_launch = "com.android.chrome"

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert "Browser" in result.metadata["verdict"]
    assert not bench.enabled()
    assert bench.switch.clicks == 0


def test_an_app_already_signed_out_is_inconclusive(bench):
    with_logged_out(bench)
    bench.logged_out_always = True

    result = bench.run()

    assert result.final_status == "A11Y_NOT_TESTED"
    assert "already signed out" in result.metadata["verdict"]


def test_cleanup_always_restores_settings_when_a_step_fails(bench):
    bench.settings["enabled_accessibility_services"] = "com.other/svc"
    bench.settings["accessibility_enabled"] = "1"

    def fail(serial, args):
        if args[:3] == ["shell", "dumpsys", "window"]:
            raise RuntimeError("synthetic step failure")
        return Bench.answer(bench, serial, args)

    bench.host._answer = fail

    result = bench.run()

    assert result.final_status == "FAILED"
    assert bench.settings == {"enabled_accessibility_services": "com.other/svc", "accessibility_enabled": "1"}
    assert result.metadata["cleanup"]["settings_restored"] is True
    assert result.metadata["cleanup"]["attacker_uninstalled"] is True
    assert ATTACKER_PACKAGE not in bench.device.packages


def test_a_service_that_cannot_be_turned_off_fails_the_run(bench):
    with_logged_out(bench)
    bench.signed_out_after = True
    bench.restore_fails = True

    def never_stops(serial, args):
        if args[:3] == ["shell", "am", "force-stop"]:
            bench.host.commands.append((serial, args))
            return 0, "", ""
        return Bench.answer(bench, serial, args)

    bench.host._answer = never_stops

    result = bench.run()

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert result.metadata["cleanup"]["service_off"] is False
    assert any("could not be confirmed off" in error for error in result.errors)


def test_an_attacker_app_that_cannot_be_uninstalled_is_reported_without_changing_the_verdict(bench):
    with_logged_out(bench)
    bench.signed_out_after = True
    bench.device.uninstall_fails.add(ATTACKER_PACKAGE)

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_ACTION_PERFORMED", "At Risk")
    assert result.metadata["cleanup"]["attacker_uninstalled"] is False
    assert any("uninstalling the attacker failed" in error for error in result.metadata["cleanup"]["errors"])
    assert any(f"remove {ATTACKER_PACKAGE} by hand" in error for error in result.errors)


def test_a_stale_attacker_service_is_dropped_from_the_settings_to_restore(bench):
    bench.settings["enabled_accessibility_services"] = f"com.other/svc:{SERVICE_SHORT_COMPONENT}"
    bench.settings["accessibility_enabled"] = "1"

    result = bench.run()

    assert result.metadata["stale_attacker_service"] is True
    assert bench.settings["enabled_accessibility_services"] == "com.other/svc"


def test_steps_05_and_06_confirm_the_service_is_listed_and_on(bench):
    bench.switch.checked = True

    result = bench.run()

    performed = result.metadata["steps_performed"]
    assert performed["05"]["outcome"] == "completed"
    assert performed["06"]["outcome"] == "completed"
    assert performed["02"]["outcome"] == "provided"


def test_scripted_steps_sign_the_app_in_before_the_attack(bench):
    bench.app.risks = {RISK: {"steps": [{"tap": {"id": f"{PACKAGE}:id/login"}}], "logged_out": LOGGED_OUT}}
    bench.present = {f"{PACKAGE}:id/login": Element()}
    bench.signed_out_after = True

    result = bench.run()

    assert result.metadata["coverage"] == "scripted"
    assert result.final_status == "A11Y_ACTION_PERFORMED"


def test_a_missing_attacker_apk_fails(bench, monkeypatch):
    bench.device.packages.pop(ATTACKER_PACKAGE)
    monkeypatch.setattr(risk, "attacker_apk_path", lambda configured=None: Path("/nonexistent/F5.apk"))

    result = bench.run()

    assert result.final_status == "FAILED"
    assert any("accessibility attacker APK not found" in error for error in result.errors)


@pytest.mark.parametrize("verdict,status,security", [
    ("ALLOWED (x)", "A11Y_ACTION_PERFORMED", "At Risk"),
    ("BLOCKED (x)", "A11Y_ACTION_BLOCKED", "Reduced Risk"),
    ("INCONCLUSIVE (x)", "A11Y_NOT_TESTED", "Inconclusive"),
    ("ERROR (x)", "FAILED", "Inconclusive"),
])
def test_verdict_mapping(verdict, status, security):
    assert risk._status_from_verdict(verdict) == status
    assert risk._security_verdict_from_verdict(verdict) == security
