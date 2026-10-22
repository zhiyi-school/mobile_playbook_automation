"""Exercises the on-screen text extraction risk with synthetic adb, Appium, Settings and Text Scraper fakes."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import accessibility_service, text_scraper
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.captured_events import CapturedEvent
from mobile_playbook.platforms.android.device.scripted_steps import DEFAULT_CANARIES
from mobile_playbook.platforms.android.device.text_scraper import (
    SCREENSHOT_BUTTON,
    START_LABEL,
    STOP_LABEL,
    TEXT_SCRAPER_APP,
    TEXT_SCRAPER_PACKAGE,
    TextScraper,
)
from mobile_playbook.platforms.android.risks import feature_05_risk_03_screen_text as risk
from mobile_playbook.platforms.android.risks import get_risk
from tests.platforms.android.prompt_fakes import png

PACKAGE = "com.example.bank"
RISK = "android-feature-05-risk-03"
HUAWEI_SCREENSHOT = "com.huawei.HwMultiScreenShot"
SHOTS = "/sdcard/Pictures/Screenshots"
BALANCE = "S$ 12,345.67"
NAME_ID = f"{PACKAGE}:id/name"
TYPE_NAME = {"type": {"id": NAME_ID, "value": "{canary.email}"}}

TAP_TARGETS: dict[tuple[int, int], "Element"] = {}


class Element:
    _next = 0

    def __init__(self, *, text="", cls="android.widget.TextView", checked=None, on_click=None):
        self.text = text
        self.cls = cls
        self.checked = checked
        self.on_click = on_click
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
        if self.on_click:
            self.on_click()

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value += value


def _text_pattern(selector):
    literal = re.search(r'textMatches\("((?:[^"\\]|\\.)*)"\)', selector)[1]
    return re.compile(literal.replace('\\"', '"').replace("\\\\", "\\")[4:], re.IGNORECASE)


class Driver:
    def __init__(self, bench):
        self.bench = bench
        self.calls = []

    @property
    def current_package(self):
        return self.bench.foreground

    def find_elements(self, by, value):
        bench = self.bench
        if by == "class name" and value == "android.widget.Switch":
            return [bench.switch]
        if by == "class name":
            return []
        if by == "-android uiautomator" and value == SCREENSHOT_BUTTON:
            return [bench.button] if bench.foreground == TEXT_SCRAPER_PACKAGE else []
        if by == "-android uiautomator" and "textMatches" in value:
            return [bench.service_entry] if _text_pattern(value).fullmatch(TEXT_SCRAPER_APP.label) else []
        if by == "id" and value == NAME_ID:
            return [bench.name_field]
        return []

    def activate_app(self, package):
        self.calls.append(("activate", package))
        self.bench.open(self.bench.foreground_after_launch if package == PACKAGE else package)

    def get_screenshot_as_file(self, path):
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

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        assert (risk_id, test_case_id, platform) == (RISK, "accessibility_screen_text", "android")
        return self.root

    def write_result(self, result, report_dir):
        pass


class Bench:
    """A phone with Text Scraper installed, whose service walks the app's screen when its window changes."""

    def __init__(self, root, monkeypatch, host):
        self.device = host.add("SCRAPE-TEST", state="device")
        self.device.packages[TEXT_SCRAPER_PACKAGE] = ("1.0", "1")
        self.settings = {"accessibility_enabled": "0"}
        self.foreground = "com.android.launcher"
        self.foreground_after_launch = PACKAGE
        self.binds = True
        self.clears = 0
        self.clock = 1_000
        self.rows: list[CapturedEvent] = []
        # The app's screen as Text Scraper sees it; a hidden view is left out, as noHideDescendants hides it.
        self.screen = {f"{PACKAGE}:id/title": "Accounts", f"{PACKAGE}:id/balance": BALANCE}
        self.hidden: set[str] = set()
        # Text Scraper saves rows under the screenshot window's package on HarmonyOS, and rereads the screen then.
        self.screenshot_package: str | None = None
        self.capturing = False
        self.screenshots = {"old_photo.jpg": 10}
        self.undeletable = False
        self.button = Element(text=START_LABEL, on_click=self._toggle_capture)
        self.switch = Element(cls="android.widget.Switch", checked=False, on_click=self._grant)
        self.service_entry = Element(text=TEXT_SCRAPER_APP.label)
        self.name_field = Element(cls="android.widget.EditText")
        self.base_answer = host._answer
        monkeypatch.setattr(host, "_answer", self.answer)
        monkeypatch.setattr(TextScraper, "events", lambda scraper, package=None: [row for row in self.rows if package in (None, row.package)])
        self.adb = AdbClient(serial="SCRAPE-TEST")
        self.driver = Driver(self)
        self.app = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={RISK: {"on_screen_text": [BALANCE]}})
        self.cfg = SimpleNamespace(accessibility={"launch_wait": 0, "step_timeout_seconds": 0, "attack_timeout": 0, "settle_seconds": 0, "capture_seconds": 12})

    def tick(self):
        self.clock += 1
        return self.clock

    def bound(self):
        return TEXT_SCRAPER_APP.names_service(accessibility_service.enabled_components(self.settings.get("enabled_accessibility_services"))) and self.binds

    def open(self, package):
        self.foreground = package
        if package == PACKAGE:
            self.walk(PACKAGE)

    # Text Scraper's timer takes a screenshot every five seconds while capture is on.
    def sleep(self, seconds):
        for _ in range(int(seconds // 5)):
            if self.capturing and self.bound():
                self.take_screenshot()

    # Save a row for each view on the app's screen, as Text Scraper does on a window state change.
    def walk(self, package):
        if not self.bound():
            return
        stamp = self.tick() * 1000
        for view_id, text in self.screen.items():
            if view_id not in self.hidden:
                self.rows.append(CapturedEvent(len(self.rows) + 1, stamp, "UI_TEXT", package, None, view_id.rsplit("/", 1)[-1], text, None, None, None, f"App: {package}"))
        if self.name_field.value and NAME_ID not in self.hidden:
            self.rows.append(CapturedEvent(len(self.rows) + 1, stamp, "INPUT_FIELD", package, None, "name", self.name_field.value, None, None, None, f"App: {package}"))

    def take_screenshot(self):
        self.screenshots[f"Screenshot_{self.clock}_{PACKAGE}.jpg"] = self.tick()
        if self.screenshot_package:
            self.walk(self.screenshot_package)

    def _toggle_capture(self):
        self.capturing = not self.capturing
        self.button.text = STOP_LABEL if self.capturing else START_LABEL

    def _grant(self):
        self.settings["enabled_accessibility_services"] = TEXT_SCRAPER_APP.component
        self.switch.checked = True

    def make_driver(self, extra_capabilities=None):
        assert extra_capabilities == {"appium:disableSuppressAccessibilityService": True}
        return self.driver

    def answer(self, serial, args):
        if args[:3] == ["shell", "input", "tap"]:
            target = TAP_TARGETS.get((int(args[3]), int(args[4])))
            if target is not None:
                target.click()
            return 0, "", ""
        if args == ["shell", "date", "+%s"]:
            return 0, f"{self.tick()}\n", ""
        if args == ["shell", "getprop", "ro.product.model"]:
            return 0, "JAD-AL80\n", ""
        if args == ["shell", "getprop", "ro.build.version.sdk"]:
            return 0, "31\n", ""
        if args[:3] == ["shell", "pm", "path"]:
            return 0, "package:/data/app/synthetic/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, "a" * 64 + "  /data/app/synthetic/base.apk\n", ""
        if args[:3] == ["shell", "pm", "query-services"]:
            return 0, f"packageName={TEXT_SCRAPER_PACKAGE}\n", ""
        if args == ["shell", "pm", "clear", TEXT_SCRAPER_PACKAGE]:
            self.clears += 1
            self.rows, self.capturing = [], False
            return 0, "Success\n", ""
        if args[:3] == ["shell", "ls", "-1"]:
            if args[3] != SHOTS:
                return 1, "", "No such file or directory"
            return 0, "".join(f"{name}\n" for name in self.screenshots), ""
        if args[:4] == ["shell", "stat", "-c", "%Y"]:
            name = shlex.split(args[4])[0].rsplit("/", 1)[-1]
            return (0, f"{self.screenshots[name]}\n", "") if name in self.screenshots else (1, "", "No such file")
        if args[:1] == ["pull"]:
            Path(args[2]).write_bytes(b"jpeg")
            return 0, "1 file pulled", ""
        if args[:3] == ["shell", "rm", "-f"]:
            name = shlex.split(args[3])[0].rsplit("/", 1)[-1]
            if not self.undeletable:
                self.screenshots.pop(name, None)
            return 0, "", ""
        if args[:3] == ["shell", "content", "delete"]:
            return 0, "", ""
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, f"{self.settings.get(args[4], 'null')}\n", ""
        if args[:4] == ["shell", "settings", "put", "secure"]:
            self.settings[args[4]] = args[5].strip("'\"")
            return 0, "", ""
        if args[:4] == ["shell", "settings", "delete", "secure"]:
            self.settings.pop(args[4], None)
            return 0, "Deleted 1 rows\n", ""
        if args[:3] == ["shell", "dumpsys", "accessibility"]:
            bound = f"Service[label={TEXT_SCRAPER_APP.label}, capabilities=1]" if self.bound() else ""
            return 0, f"     Bound services:{{{bound}}}\n", ""
        if args == ["shell", "am", "force-stop", TEXT_SCRAPER_PACKAGE]:
            self.capturing = False
            others = [c for c in accessibility_service.enabled_components(self.settings.get("enabled_accessibility_services")) if not TEXT_SCRAPER_APP.names_service([c])]
            self.settings["enabled_accessibility_services"] = ":".join(others)
            return 0, "", ""
        return self.base_answer(serial, args)

    def run(self):
        return risk.AndroidAccessibilityScreenTextRisk().run(self.app, self.cfg, SimpleNamespace(adb=self.adb, make_driver=self.make_driver), Writer(self.root))


@pytest.fixture
def bench(tmp_path, monkeypatch, adb_host):
    monkeypatch.setattr(text_scraper.time, "sleep", lambda _: None)
    monkeypatch.setattr(risk, "POLL_SECONDS", 0)
    monkeypatch.setattr(accessibility_service, "POLL_SECONDS", 0)
    monkeypatch.setattr(accessibility_service, "UNBIND_SECONDS", 0)
    TAP_TARGETS.clear()
    bench = Bench(tmp_path, monkeypatch, adb_host)
    bench.root = tmp_path
    monkeypatch.setattr(risk.time, "sleep", bench.sleep)
    return bench


def test_a_configured_value_read_from_the_screen_is_at_risk(bench):
    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_TEXT_CAPTURED", "At Risk")
    assert result.metadata["verdict_rule"] == "value_captured"
    assert result.metadata["capture"]["values"]["on_screen_text[0]"] == {"captured": True, "event_type": "UI_TEXT", "view_id": "balance", "masked": False}
    assert result.metadata["service_enable"]["method"] == "settings_ui"
    assert result.metadata["device"] == {"model": "JAD-AL80", "sdk": "31"}


def test_the_steps_follow_the_playbook_and_cleanup_leaves_nothing_behind(bench):
    bench.settings["enabled_accessibility_services"] = "com.other/svc"
    bench.settings["accessibility_enabled"] = "1"

    result = bench.run()

    performed = result.metadata["steps_performed"]
    assert [performed[step]["outcome"] for step in risk.STEPS] == ["completed", "provided", "provided", "completed", "completed", "completed"]
    assert result.metadata["attacker_service_config"]["event_types"] == ["typeViewFocused", "typeViewTextChanged", "typeWindowStateChanged"]
    assert result.metadata["cleanup"] == {
        "settings_restored": True, "service_off": True, "data_cleared": True, "screenshots_deleted": True,
        "screenshots_left": [], "attacker_uninstalled": True, "errors": [],
    }
    assert bench.settings == {"enabled_accessibility_services": "com.other/svc", "accessibility_enabled": "1"}
    assert bench.clears == 2
    assert TEXT_SCRAPER_PACKAGE not in bench.device.packages
    assert {entry["kind"] for entry in result.evidence} >= {"accessibility_target_screen", "accessibility_captured_text", "accessibility_text_scraper_screen", "accessibility_list_after", "accessibility_service_on"}


def test_screenshot_capture_is_started_and_stopped_and_its_screenshots_moved_to_the_report(bench):
    result = bench.run()

    assert bench.button.text == START_LABEL
    assert result.metadata["screenshots"] == {"capture_started": True, "error": None, "taken": 2, "pulled": 2, "pull_errors": []}
    assert len([entry for entry in result.evidence if entry["kind"] == "accessibility_attacker_screenshot"]) == 2
    assert all(Path(entry["path"]).parent.name == "screenshots" for entry in result.evidence if entry["kind"] == "accessibility_attacker_screenshot")
    assert list(bench.screenshots) == ["old_photo.jpg"]


def test_a_screenshot_that_could_not_be_deleted_is_reported(bench):
    bench.undeletable = True

    result = bench.run()

    assert result.metadata["cleanup"]["screenshots_deleted"] is False
    assert len(result.metadata["cleanup"]["screenshots_left"]) == 2
    assert any("still on the phone" in error for error in result.errors)
    assert result.verdict == "At Risk"


def test_only_the_apps_rows_from_the_test_are_saved_as_evidence(bench):
    original = bench.open

    def open_and_scrape_another_app(package):
        bench.rows.append(CapturedEvent(len(bench.rows) + 1, bench.tick() * 1000, "UI_TEXT", "com.other.app", None, "x", "elsewhere", None, None, None, None))
        original(package)

    bench.open = open_and_scrape_another_app

    result = bench.run()

    [entry] = [entry for entry in result.evidence if entry["kind"] == "accessibility_captured_text"]
    saved = json.loads(Path(entry["path"]).read_text())
    assert {row["package"] for row in saved} == {PACKAGE}
    assert {row["text"] for row in saved} == {"Accounts", BALANCE}


def test_a_value_hidden_from_accessibility_is_reduced_risk(bench):
    bench.hidden = {f"{PACKAGE}:id/balance"}

    result = bench.run()

    assert (result.final_status, result.verdict) == ("A11Y_TEXT_PROTECTED", "Reduced Risk")
    assert result.metadata["verdict_rule"] == "screen_read_without_values"
    assert result.metadata["capture"]["values"]["on_screen_text[0]"]["captured"] is False


def test_a_reformatted_value_still_matches(bench):
    bench.screen[f"{PACKAGE}:id/balance"] = "S$12345.67"

    assert bench.run().verdict == "At Risk"


def test_rows_saved_under_the_huawei_screenshot_window_count_for_the_app(bench):
    bench.hidden = {f"{PACKAGE}:id/balance"}
    bench.screenshot_package = HUAWEI_SCREENSHOT
    original = bench.walk

    def walk(package):
        bench.hidden = set() if package == HUAWEI_SCREENSHOT else {f"{PACKAGE}:id/balance"}
        original(package)

    bench.walk = walk

    result = bench.run()

    assert result.verdict == "At Risk"


def test_rows_read_from_text_scrapers_own_screen_do_not_count(bench):
    bench.hidden = {f"{PACKAGE}:id/balance"}
    original = bench._toggle_capture

    # A screenshot while Text Scraper's screen lists what it captured is saved under the screenshot window's package.
    def toggle_while_listing_the_balance():
        bench.rows.append(CapturedEvent(len(bench.rows) + 1, bench.tick() * 1000, "UI_TEXT", HUAWEI_SCREENSHOT, None, "text", BALANCE, None, None, None, None))
        original()

    bench.button.on_click = toggle_while_listing_the_balance

    result = bench.run()

    assert result.verdict == "Reduced Risk"
    assert result.metadata["screenshots"]["capture_started"] is True


def test_no_value_to_look_for_is_inconclusive_with_a_summary(bench):
    bench.app.risks = {}

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "No on-screen value was configured" in result.metadata["verdict"] and "2 ui text" in result.metadata["verdict"]


def test_a_typed_canary_is_judged_only_when_the_screen_was_read_again(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_NAME]}}

    result = bench.run()

    assert result.metadata["canaries_judged"] is False
    assert result.verdict == "Inconclusive"


def test_a_typed_canary_read_again_from_the_screen_is_at_risk(bench):
    bench.app.risks = {RISK: {"steps": [TYPE_NAME]}}
    bench.screenshot_package = HUAWEI_SCREENSHOT

    result = bench.run()

    assert result.metadata["canaries_judged"] is True
    assert result.verdict == "At Risk"
    assert result.metadata["capture"]["values"]["canary.email"]["event_type"] == "INPUT_FIELD"
    assert bench.name_field.value == DEFAULT_CANARIES["email"]


def test_a_service_that_is_not_bound_is_inconclusive_and_no_screenshot_is_touched(bench):
    bench.binds = False

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "did not bind it" in result.metadata["verdict"]
    assert ("activate", PACKAGE) not in bench.driver.calls
    assert result.metadata["screenshots"]["capture_started"] is False
    assert result.metadata["cleanup"]["screenshots_deleted"] is True
    assert list(bench.screenshots) == ["old_photo.jpg"]


def test_screenshot_capture_that_cannot_start_does_not_change_the_verdict(bench):
    bench.button.on_click = None

    result = bench.run()

    assert result.verdict == "At Risk"
    assert result.metadata["screenshots"]["capture_started"] is False
    assert "did not change" in result.metadata["screenshots"]["error"]


def test_an_app_that_redirects_on_launch_is_inconclusive(bench):
    bench.foreground_after_launch = "com.android.vending"

    result = bench.run()

    assert result.verdict == "Inconclusive"
    assert "Redirected to" in result.metadata["verdict"]


def test_a_stale_text_scraper_service_is_dropped_from_the_settings_to_restore(bench):
    bench.settings["enabled_accessibility_services"] = f"com.other/svc:{TEXT_SCRAPER_APP.short_component}"
    bench.settings["accessibility_enabled"] = "1"

    result = bench.run()

    assert result.metadata["stale_attacker_service"] is True
    assert bench.settings["enabled_accessibility_services"] == "com.other/svc"


def test_a_failure_reading_the_rows_fails_the_run_and_still_cleans_up(bench, monkeypatch):
    def unreadable(scraper, package=None):
        raise accessibility_service.AccessibilityAttackerError("Text Scraper's database cannot be read with run-as: not debuggable")

    monkeypatch.setattr(TextScraper, "events", unreadable)

    result = bench.run()

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert result.metadata["cleanup"]["data_cleared"] is True
    assert result.metadata["cleanup"]["screenshots_deleted"] is True
    assert TEXT_SCRAPER_PACKAGE not in bench.device.packages


def test_the_risk_requires_text_scraper():
    assert get_risk(RISK).requires == ["adb", "appium", "text_scraper_attacker"]
