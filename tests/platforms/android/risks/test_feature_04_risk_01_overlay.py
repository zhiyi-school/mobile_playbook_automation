"""Exercises the overlay risk with synthetic adb, UI, window visibility and canary capture fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.overlay_attacker import (
    ATTACKER_PACKAGE,
    OUTPUT_DIR,
    PASSWORD_FIELD_ID,
    SIGN_IN_BUTTON_ID,
    USERNAME_FIELD_ID,
)
from mobile_playbook.platforms.android.device.permission_prompts import DENY_BUTTON_IDS, DENY_TEXT_SELECTOR
from mobile_playbook.platforms.android.risks import feature_04_risk_01_overlay as overlay
from tests.platforms.android.prompt_fakes import png

PACKAGE = "com.example.app"
RISK = "android-feature-04-risk-01"
EMAIL = "canary.logtest@example.com"
PASSWORD = "Canary-Pw-7Q4z"
FORCE_STOP = ["shell", "am", "force-stop", ATTACKER_PACKAGE]
RESET_APPOP = ["shell", "appops", "set", ATTACKER_PACKAGE, "SYSTEM_ALERT_WINDOW", "default"]
PERMISSION_CONTROLLER = "com.android.permissioncontroller"
INSTALLED_APPS_PROMPT = "Allow Example to read the list of installed apps?"


# Build a minimal owned window with the visibility fields emitted by Android window dumps.
def window_dump(fields="mHasSurface=true isOnScreen=true isVisible=true\n    mSurfaceShown=true", package=ATTACKER_PACKAGE, window_type="APPLICATION_OVERLAY"):
    return (
        "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
        f"  Window #0 Window{{abc u0 {package}}}:\n"
        f"    mOwnerUid=10123 package={package} appop=SYSTEM_ALERT_WINDOW\n"
        f"    mAttrs={{ty={window_type} fl=NOT_TOUCH_MODAL}}\n"
        f"    mViewVisibility=0x0 {fields}\n"
    )


class Element:
    # Keep the fake field value or permission switch state and the optional click action.
    def __init__(self, *, checked="false", on_click=None, text=""):
        self.checked = checked
        self.text = text
        self.on_click = on_click
        self.clicks = 0
        self.value = ""

    # Expose the Settings switch state to the permission driver.
    def get_attribute(self, name):
        assert name == "checked"
        return self.checked

    # Apply the configured synthetic action and remember the click.
    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    # Empty a synthetic field before scripted input.
    def clear(self):
        self.value = ""

    # Keep exactly the text sent by scripted steps.
    def send_keys(self, value):
        self.value += value


class Driver:
    # Share the fake device state and emulate the settings, target and attacker screens.
    def __init__(self, bench):
        self.bench = bench
        self.current_package = PACKAGE
        self.calls = []
        self.screenshots = []
        self.switch = Element(on_click=self.grant)
        self.fields = {USERNAME_FIELD_ID: Element(), PASSWORD_FIELD_ID: Element(), SIGN_IN_BUTTON_ID: Element(on_click=self.submit), "sign_in": Element()}
        self.deny = Element(on_click=self.deny_prompt)
        self.lookups = []

    # Grant the appop when the switch is tapped, as the Settings UI does.
    def grant(self):
        self.switch.checked = "true"
        self.bench.allowed = True

    # Save a submission and remove the overlay, without resetting the app's output.
    def submit(self):
        self.bench.captures = [{"timestamp": "2026-10-21T03:00:00Z", "username": self.fields[USERNAME_FIELD_ID].value, "password": self.fields[PASSWORD_FIELD_ID].value}]
        self.bench.state = "captured"

    # Close the front permission dialog and show the next one, the app that dialog leaves in front, or the target.
    def deny_prompt(self):
        if not self.bench.prompt_repeats:
            self.bench.prompts.pop(0)
        if self.bench.prompts:
            return
        if self.bench.after_deny:
            self.current_package = self.bench.after_deny
            self.bench.after_deny = None
        else:
            self.current_package = ATTACKER_PACKAGE if self.bench.overlay_holds_focus and self.bench.shown_count else self.bench.foreground

    # Return a switch, scripted target or overlay field selected by the test scenario.
    def find_elements(self, by, value):
        self.lookups.append((by, value))
        if self.bench.prompts and self.current_package == PERMISSION_CONTROLLER:
            if value == f"{PERMISSION_CONTROLLER}:id/permission_message":
                return [Element(text=self.bench.prompts[0])]
            if value == self.bench.deny_locator:
                return [self.deny]
        if value == "android:id/switch_widget":
            return [self.switch] if self.bench.switch_kind == "id" else []
        if value == "android.widget.Switch":
            return [self.switch] if self.bench.switch_kind == "class" else []
        return [self.fields[value]] if value in self.fields else []

    # Launch the target or reopen the attacker launcher without clearing its captures.
    def activate_app(self, package):
        self.calls.append(("activate", package))
        if self.bench.launch_error and package == PACKAGE:
            raise RuntimeError("synthetic launch failure")
        self.current_package = self.bench.foreground if package == PACKAGE else package
        if package == PACKAGE:
            self.bench.shown_count = self.bench.added_count
            self.bench.state = "overlay_shown" if self.bench.shown_count else "service_started"
            if self.bench.overlay_holds_focus and self.bench.shown_count:
                self.current_package = ATTACKER_PACKAGE
            if self.bench.prompts:
                self.current_package = PERMISSION_CONTROLLER

    # Return the synthetic launcher's captured entry as UI XML.
    @property
    def page_source(self):
        if self.bench.display_capture and self.bench.captures:
            record = self.bench.captures[-1]
            return f'<Text text="{record["username"]} {record["password"]}"/>'
        return "<hierarchy/>"

    # Write a synthetic PNG using the existing prompt fixture utility.
    def get_screenshot_as_file(self, path):
        self.screenshots.append(path)
        Path(path).write_bytes(png(1080, 1920))
        return True

    # Record target termination, optionally failing to exercise cleanup isolation.
    def terminate_app(self, package):
        self.calls.append(("terminate", package))
        if self.bench.termination_error:
            raise RuntimeError("synthetic terminate failure")

    # Record session closure even after an earlier cleanup failure.
    def quit(self):
        self.calls.append(("quit", None))

    # Support scripted back navigation and optional redirects after navigation.
    def back(self):
        self.calls.append(("back", None))
        if self.bench.redirect_after_steps:
            self.current_package = self.bench.redirect_after_steps
        if self.bench.prompt_after_steps:
            self.bench.prompts.append(self.bench.prompt_after_steps)
            self.current_package = PERMISSION_CONTROLLER


class Writer:
    run_timestamp = "2026-10-21_00-00-00"

    # Keep results in the pytest temporary directory.
    def __init__(self, root):
        self.root = root
        self.results = []

    # Provide the report root expected by the risk implementation.
    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        assert (risk_id, test_case_id, platform) == (RISK, "overlay", "android")
        return self.root

    # Remember every published result for cleanup and exception assertions.
    def write_result(self, result, report_dir):
        self.results.append(result)


class Bench:
    # Attach overlay-specific adb replies to the existing fake host without real subprocess or device calls.
    def __init__(self, root, monkeypatch, host):
        self.host = host
        self.device = host.add("OVERLAY-TEST", state="device")
        self.device.packages[ATTACKER_PACKAGE] = ("1", "1")
        self.allowed = False
        self.switch_kind = "id"
        self.foreground = PACKAGE
        self.overlay_holds_focus = False
        self.activity_dump = f"ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n  topResumedActivity=ActivityRecord{{1a2b3c u0 {PACKAGE}/.MainActivity t12}}\n"
        self.activity_dump_error = False
        self.state = "permission_missing"
        self.added_count = 1
        self.shown_count = 0
        self.captures = []
        self.dump = window_dump()
        self.package_dump = "    requested permissions:\n      android.permission.HIDE_OVERLAY_WINDOWS\n    install permissions:\n"
        self.launch_error = False
        self.display_capture = True
        self.termination_error = False
        self.redirect_after_steps = None
        self.prompts = []
        self.prompt_repeats = False
        self.prompt_after_steps = None
        self.after_deny = None
        self.deny_locator = DENY_BUTTON_IDS[0]
        self.fail_command = None
        self.dump_error = False
        self.status_missing = False
        self.driver_error = False
        self.base_answer = host._answer
        monkeypatch.setattr(host, "_answer", self.answer)
        self.adb = AdbClient(serial="OVERLAY-TEST")
        self.driver = Driver(self)
        self.writer = Writer(root)
        self.app = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={})
        self.cfg = SimpleNamespace(overlay={"launch_wait": 0, "overlay_timeout": 0, "step_timeout_seconds": 0})
        self.client = SimpleNamespace(adb=self.adb, make_driver=self.make_driver)

    # Fail driver construction when a scenario tests the earliest cleanup path.
    def make_driver(self):
        if self.driver_error:
            raise RuntimeError("synthetic driver failure")
        return self.driver

    # Answer fingerprints, appops and attacker JSON while delegating installation metadata to FakeAdbHost.
    def answer(self, serial, args):
        if args == self.fail_command:
            raise RuntimeError("synthetic step failure")
        if args[:3] == ["shell", "pm", "path"]:
            return 0, "package:/data/app/synthetic/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, "a" * 64 + "  /data/app/synthetic/base.apk\n", ""
        if args == ["shell", "dumpsys", "package", PACKAGE]:
            return 0, self.package_dump, ""
        if args == ["shell", "dumpsys", "activity", "activities"]:
            return (1, "", "synthetic activity dump failure") if self.activity_dump_error else (0, self.activity_dump, "")
        if args == ["shell", "dumpsys", "window", "windows"]:
            return (1, "", "synthetic dump failure") if self.dump_error else (0, self.dump, "")
        if args[:3] == ["shell", "appops", "get"]:
            return 0, f"SYSTEM_ALERT_WINDOW: {'allow' if self.allowed else 'default'}", ""
        if args[:3] == ["shell", "appops", "set"]:
            self.allowed = args[-1] == "allow"
            return 0, "", ""
        if args[:3] == ["shell", "am", "start"] and "-S" in args:
            self.shown_count = 0
            self.captures = []
            self.state = "service_started" if self.allowed else "permission_missing"
            return 0, "Starting: Intent", ""
        if args[:2] == ["shell", "cat"]:
            if args[-1] == f"{OUTPUT_DIR}/status.json":
                return (1, "", "missing") if self.status_missing else (0, json.dumps({"state": self.state, "overlay_shown_count": self.shown_count}), "")
            if args[-1] == f"{OUTPUT_DIR}/captured.json":
                return 0, json.dumps(self.captures), ""
        return self.base_answer(serial, args)

    # Execute the real risk and helper against the synthetic bench.
    def run(self):
        return overlay.AndroidOverlayRisk().run(self.app, self.cfg, self.client, self.writer)

    # Return the command list sent to this synthetic device.
    @property
    def commands(self):
        return [args for serial, args in self.host.commands if serial == "OVERLAY-TEST"]


# Disable real waiting while keeping all polling and timeout branches executable.
@pytest.fixture
def bench(tmp_path, monkeypatch, adb_host):
    monkeypatch.setattr(overlay.time, "sleep", lambda _: None)
    monkeypatch.setattr(overlay, "POLL_SECONDS", 0)
    return Bench(tmp_path, monkeypatch, adb_host)


# Verify the visible overlay, actual scripted submission, fresh JSON read and launcher confirmation together.
def test_visible_overlay_is_allowed_and_canaries_are_confirmed(bench):
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_ALLOWED", "At Risk")
    assert result.metadata["overlay"] == {"added": True, "visible": True, "window_found": True, "shown_count": 1}
    assert result.metadata["capture"]["confirmed"] is True
    assert result.metadata["capture"]["display_confirmed"] is True
    assert bench.captures[-1]["username"] == EMAIL
    assert bench.captures[-1]["password"] == PASSWORD
    assert result.metadata["installed_base_apk_sha256"] == "a" * 64
    assert result.metadata["declares_hide_overlay_windows"] is True
    assert all(value["outcome"] == "completed" for value in result.metadata["steps_performed"].values())
    assert ("activate", ATTACKER_PACKAGE) in bench.driver.calls
    assert len([command for command in bench.commands if command[:3] == ["shell", "am", "start"] and "-S" in command]) == 2
    assert {entry["kind"] for entry in result.evidence} == {"overlay_permission_list", "overlay_permission_toggle", "overlay_target_screen", "overlay_window_dump"}
    assert all(Path(entry["path"]).is_file() for entry in result.evidence)
    assert (bench.writer.root / "overlay/dumpsys_overlay_window.txt").read_text() == bench.dump.split("\n", 1)[1].rstrip()


# An explicitly suppressed window establishes reduced risk even when its app-side visibility remains true.
def test_added_overlay_hidden_by_target_is_blocked(bench):
    bench.dump = window_dump("mHasSurface=true isOnScreen=true mSurfaceShown=false\n    mForceHideNonSystemOverlayWindow=true")
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_BLOCKED", "Reduced Risk")
    assert result.metadata["overlay"]["visible"] is False
    assert result.metadata["steps_performed"]["06"]["outcome"] == "skipped"
    assert bench.captures == []


# Classify every template foreground hand-off without mistaking it for overlay protection.
@pytest.mark.parametrize("foreground,reason", [
    ("com.google.android.apps.nexuslauncher", "Home Screen"),
    ("com.huawei.android.launcher", "Home Screen"),
    ("com.android.vending", "Play Store"),
    ("com.aurora.store", "Aurora Store"),
    ("com.android.chrome", "Browser"),
    ("com.android.permissioncontroller", "system prompt"),
    ("com.google.android.permissioncontroller", "system prompt"),
    ("com.example.other", "unknown app"),
])
def test_foreground_hand_off_is_not_tested(bench, foreground, reason):
    bench.foreground = foreground
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_NOT_TESTED", "Inconclusive")
    assert reason in result.metadata["verdict"]
    assert bench.captures == []
    assert any(entry["kind"] == "overlay_target_screen" for entry in result.evidence)


# A status showing no addView never establishes protection, even when a stale window dump looks visible.
def test_attacker_never_adds_its_window_is_not_tested(bench):
    bench.added_count = 0
    result = bench.run()
    assert result.final_status == "OVERLAY_NOT_TESTED"
    assert result.metadata["overlay"]["added"] is False
    assert "never added" in result.metadata["verdict"]


# A surviving attacker activity or another package cannot stand in for an overlay window.
@pytest.mark.parametrize("dump", [window_dump(package=ATTACKER_PACKAGE + ".helper"), "", window_dump(window_type="BASE_APPLICATION")])
def test_no_overlay_window_in_dump_is_not_tested(bench, dump):
    bench.dump = dump
    result = bench.run()
    assert result.final_status == "OVERLAY_NOT_TESTED"
    assert result.metadata["overlay"]["window_found"] is False


# Keep unfamiliar visibility fields inconclusive rather than inferring protection from missing data.
def test_unknown_visibility_is_not_tested(bench):
    bench.dump = window_dump("mHasSurface=true mPolicyVisibility=true")
    result = bench.run()
    assert result.final_status == "OVERLAY_NOT_TESTED"
    assert result.metadata["overlay"]["visible"] is None


# Fall back to appops only when neither supported Settings locator finds a switch.
def test_missing_settings_switch_records_appops_fallback(bench):
    bench.switch_kind = None
    result = bench.run()
    assert result.metadata["permission_granted_via"] == "appops_fallback"
    assert ["shell", "appops", "set", ATTACKER_PACKAGE, "SYSTEM_ALERT_WINDOW", "allow"] in bench.commands
    assert result.final_status == "OVERLAY_ALLOWED"


# A class-based switch works, and an already enabled switch must never be toggled off.
@pytest.mark.parametrize("kind,checked,clicks", [("id", "false", 1), ("class", "false", 1), ("id", "true", 0)])
def test_settings_switch_is_tapped_only_when_off(bench, kind, checked, clicks):
    bench.switch_kind = kind
    bench.driver.switch.checked = checked
    bench.allowed = checked == "true"
    result = bench.run()
    assert result.metadata["permission_granted_via"] == "settings_ui"
    assert bench.driver.switch.clicks == clicks
    assert not any(command[:3] == ["shell", "appops", "set"] and command[-1] == "allow" for command in bench.commands)


# Propagate the configured missing APK path through the real helper into the failed report.
def test_missing_attacker_apk_fails_with_path(bench, tmp_path):
    bench.device.packages.clear()
    path = tmp_path / "missing-overlay.apk"
    bench.app.risks[RISK] = {"attacker_apk": str(path)}
    result = bench.run()
    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert str(path) in result.errors[0]
    assert result.metadata["steps_performed"]["03"]["outcome"] == "failed"


# Report complete, incomplete and absent per-app navigation independently of capture submission steps.
@pytest.mark.parametrize("steps,coverage,completed", [
    ([{"tap": {"id": "sign_in"}}], "scripted", 1),
    ([{"tap": {"id": "sign_in"}}, {"tap": {"id": "absent"}}], "scripted_partial", 1),
    ([], "launch_only", None),
])
def test_navigation_coverage_tracks_steps_outcome(bench, steps, coverage, completed):
    bench.app.risks[RISK] = {"steps": steps}
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert result.metadata["coverage"] == coverage
    if completed is None:
        assert "steps" not in result.metadata
    else:
        assert result.metadata["steps"]["completed"] == completed
        assert result.metadata["steps"]["configured"] == len(steps)


# Per-app settings override shared values, but shared navigation is not mistaken for per-app steps.
def test_configuration_merge_respects_app_values_and_per_app_steps(bench):
    bench.cfg.overlay.update({"step_timeout_seconds": 20, "steps": [{"tap": {"id": "absent"}}]})
    bench.app.risks[RISK] = {"step_timeout_seconds": 0, "steps": [{"tap": {"id": "absent"}}]}
    result = bench.run()
    assert result.metadata["coverage"] == "scripted_partial"
    assert "within 0s" in result.metadata["steps"]["failure"]["error"]


# Navigation that leaves the target must not attribute an overlay to a different app.
def test_redirect_after_scripted_steps_is_not_tested(bench):
    bench.redirect_after_steps = "com.android.chrome"
    bench.app.risks[RISK] = {"steps": ["back"]}
    assert bench.run().final_status == "OVERLAY_NOT_TESTED"


# Force-stop and appop reset run after success, installation failures and exceptions at different steps.
@pytest.mark.parametrize("failure", [None, "driver", "install", "settings", "launch", "dump", "capture"])
def test_cleanup_always_force_stops_and_resets_appop(bench, failure, tmp_path):
    if failure == "driver":
        bench.driver_error = True
    elif failure == "install":
        bench.device.packages.clear()
        bench.app.risks[RISK] = {"attacker_apk": str(tmp_path / "absent.apk")}
    elif failure == "settings":
        bench.fail_command = ["shell", "am", "start", "-a", overlay.OVERLAY_ACTION, "-d", f"package:{ATTACKER_PACKAGE}"]
    elif failure == "launch":
        bench.launch_error = True
    elif failure == "dump":
        bench.dump_error = True
    elif failure == "capture":
        bench.driver.fields.pop(PASSWORD_FIELD_ID)
    result = bench.run()
    assert FORCE_STOP in bench.commands
    assert RESET_APPOP in bench.commands
    assert bench.commands.index(FORCE_STOP) < bench.commands.index(RESET_APPOP)
    assert bench.allowed is False
    assert bench.writer.results == [result]
    assert result.timestamp_end is not None
    if failure != "driver":
        assert ("terminate", PACKAGE) in bench.driver.calls
        assert ("quit", None) in bench.driver.calls
    if failure:
        assert result.final_status == "FAILED"


# A failed target termination cannot prevent Appium session cleanup or result publication.
def test_cleanup_continues_after_target_termination_failure(bench, caplog):
    bench.termination_error = True
    with caplog.at_level("DEBUG", logger=overlay.__name__):
        result = bench.run()
    assert ("quit", None) in bench.driver.calls
    assert bench.writer.results == [result]
    assert "target termination failed" in caplog.text


# A missing captured file or launcher confirmation is an error, rather than a fabricated successful capture.
@pytest.mark.parametrize("failure", ["file", "display"])
def test_capture_requires_matching_records_and_launcher_display(bench, monkeypatch, failure):
    if failure == "file":
        monkeypatch.setattr(overlay.OverlayAttacker, "captured", lambda _: [])
    else:
        bench.display_capture = False
    result = bench.run()
    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert result.metadata["steps_performed"]["06"]["outcome"] == "failed"


# A manifest declaration is informational and cannot override observed window visibility.
@pytest.mark.parametrize("declares", [False, True])
def test_manifest_permission_does_not_change_verdict(bench, declares):
    bench.package_dump = "    requested permissions:\n      android.permission.OTHER\n    install permissions:\n      android.permission.HIDE_OVERLAY_WINDOWS\n"
    if declares:
        bench.package_dump = "    requested permissions:\n      android.permission.HIDE_OVERLAY_WINDOWS\n"
    result = bench.run()
    assert result.metadata["declares_hide_overlay_windows"] is declares
    assert result.final_status == "OVERLAY_ALLOWED"


# Recognise surface, policy and forced suppression fields from synthetic Android 12+ window formats.
@pytest.mark.parametrize("fields,visible", [
    ("mHasSurface=true isReadyForDisplay()=true mSurfaceShown=true", True),
    ("isOnScreen=true isVisible=true", True),
    ("mHasSurface=true mSurfaceShown=false isVisible=false", False),
    ("mHasSurface=true isVisible=true mForceHideNonSystemOverlayWindow=true", False),
    ("mPolicyVisibility=false isVisible=true", False),
    ("mLegacyPolicyVisibilityAfterAnim=false mSurfaceShown=true", False),
    ("mAppOpVisibility=false isOnScreen=true", False),
    ("mHidden=true isOnScreen=true", False),
    ("mPermanentlyHidden=true isVisible=true", False),
    ("mHasSurface=false", False),
    ("mHasSurface=true mPolicyVisibility=true", None),
    ("", None),
])
def test_window_visibility_fields(fields, visible):
    assert overlay._window_visibility(fields) is visible


# Android's INVISIBLE and GONE view values must defeat otherwise positive surface fields.
@pytest.mark.parametrize("visibility", ["0x4", "0x8", "4", "8"])
def test_nonvisible_view_visibility(visibility):
    assert overlay._window_visibility(f"mViewVisibility={visibility} isVisible=true") is False


# Preserve exact package ownership and combine multiple windows without trusting an unknown one as hidden.
def test_overlay_parser_uses_owned_windows_and_aggregates_visibility():
    hidden = window_dump("isVisible=false")
    unknown = window_dump("mHasSurface=true").replace("Window #0", "Window #1")
    visible = window_dump().replace("Window #0", "Window #2")
    assert overlay._overlay_windows(hidden)[0] is False
    assert overlay._overlay_windows(hidden + unknown)[0] is None
    assert overlay._overlay_windows(hidden + visible)[0] is True
    assert overlay._overlay_windows(window_dump(package=ATTACKER_PACKAGE + ".other")) == (None, [])


# Keep the status and security mapping aligned for every public verdict prefix.
@pytest.mark.parametrize("verdict,status,security", [
    ("ALLOWED (visible)", "OVERLAY_ALLOWED", "At Risk"),
    ("BLOCKED (hidden)", "OVERLAY_BLOCKED", "Reduced Risk"),
    ("INCONCLUSIVE (missing)", "OVERLAY_NOT_TESTED", "Inconclusive"),
    ("ERROR (adb)", "FAILED", "Inconclusive"),
    ("Unknown", "UNKNOWN", "Inconclusive"),
])
def test_verdict_mapping(verdict, status, security):
    assert overlay._status_from_verdict(verdict) == status
    assert overlay._security_verdict_from_verdict(verdict) == security


# The module's defaults work before stage 3 introduces a global overlay settings model.
def test_global_config_without_overlay_settings_uses_defaults(bench):
    del bench.cfg.overlay
    assert bench.run().final_status == "OVERLAY_ALLOWED"


# An absent field state must not trigger an unsafe toggle or a misleading appops fallback.
def test_unreadable_switch_state_fails_without_tapping(bench):
    bench.driver.switch.checked = None
    result = bench.run()
    assert result.final_status == "FAILED"
    assert bench.driver.switch.clicks == 0
    assert "checked state" in result.errors[0]


# Permission confirmation is required even when Settings presents and accepts the switch action.
def test_unconfirmed_permission_fails_instead_of_starting_measurement(bench):
    bench.driver.switch.on_click = lambda: None
    result = bench.run()
    assert result.final_status == "FAILED"
    assert "permission was not allowed" in result.errors[0]
    assert result.metadata["steps_performed"]["04"]["outcome"] == "failed"


# Even a force-stop failure must leave appop reset, target termination and result publication reachable.
def test_cleanup_continues_after_attacker_stop_failure(bench, monkeypatch, caplog):
    # Simulate a stop error without bypassing the risk's cleanup handler.
    def broken_stop(self):
        raise RuntimeError("synthetic stop failure")
    monkeypatch.setattr(overlay.OverlayAttacker, "stop", broken_stop)
    with caplog.at_level("DEBUG", logger=overlay.__name__):
        result = bench.run()
    assert RESET_APPOP in bench.commands
    assert ("terminate", PACKAGE) in bench.driver.calls
    assert ("quit", None) in bench.driver.calls
    assert bench.writer.results == [result]
    assert "attacker stop failed" in caplog.text


# A reset failure is logged independently and cannot skip later cleanup operations.
def test_cleanup_continues_after_appop_reset_failure(bench, caplog):
    bench.fail_command = RESET_APPOP
    with caplog.at_level("DEBUG", logger=overlay.__name__):
        result = bench.run()
    assert FORCE_STOP in bench.commands
    assert ("terminate", PACKAGE) in bench.driver.calls
    assert ("quit", None) in bench.driver.calls
    assert bench.writer.results == [result]
    assert "resetting overlay appop failed" in caplog.text


# Missing screenshots leave the overlay verdict and non-image evidence intact, with every failure recorded.
@pytest.mark.parametrize("failure", ["returns_false", "raises_runtime_error"])
def test_screenshot_failure_is_reported_and_cleaned_up(bench, monkeypatch, failure):
    # Model either Appium failure without writing a screenshot file.
    def failed_screenshot(path):
        if failure == "raises_runtime_error":
            raise RuntimeError("synthetic screenshot failure")
        return False

    monkeypatch.setattr(bench.driver, "get_screenshot_as_file", failed_screenshot)
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_ALLOWED", "At Risk")
    reason = "synthetic screenshot failure" if failure == "raises_runtime_error" else "Appium did not save the file"
    assert result.metadata["evidence_errors"] == [
        f"{name}: {reason}" for name in ("permission_list.png", "permission_toggle.png", "target_screen.png", "attacker_captured.png")
    ]
    assert all(Path(entry["path"]).suffix != ".png" for entry in result.evidence)
    assert any(entry["kind"] == "overlay_window_dump" for entry in result.evidence)
    assert FORCE_STOP in bench.commands and RESET_APPOP in bench.commands


# A foreground query error must remain an error rather than inferring blocked or allowed overlays.
def test_unreadable_foreground_is_failed(bench, monkeypatch):
    # Simulate the Appium foreground query failing.
    def unreadable(self):
        raise RuntimeError("synthetic foreground query failure")
    monkeypatch.setattr(Driver, "current_package", property(unreadable, lambda self, value: None), raising=False)
    result = bench.run()
    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert "foreground query failure" in result.errors[0]
    assert result.metadata["steps_performed"]["05"]["outcome"] == "failed"


# Scripted type actions receive the shared canaries with app-specific overrides, without changing overlay canaries.
def test_scripted_navigation_merges_canaries(bench):
    bench.cfg.overlay["canaries"] = {"email": "canary.shared@example.com"}
    bench.app.risks[RISK] = {"canaries": {"email": "canary.app@example.com"}, "steps": [{"type": {"id": "sign_in", "value": "{canary.email}"}}]}
    result = bench.run()
    assert bench.driver.fields["sign_in"].value == "canary.app@example.com"
    assert result.metadata["steps"]["canaries_typed"] == ["email"]
    assert bench.captures[-1]["username"] == EMAIL


# Newer Android policy visibility bitmasks still identify zero as explicitly hidden.
@pytest.mark.parametrize("policy,visible", [("0x0", False), ("0", False), ("0x3", True), ("3", True)])
def test_numeric_policy_visibility(policy, visible):
    assert overlay._window_visibility(f"mPolicyVisibility={policy} mSurfaceShown=true") is visible


# Numeric and textual application-overlay types are accepted while activity windows are excluded.
@pytest.mark.parametrize("window_type", ["APPLICATION_OVERLAY", "TYPE_APPLICATION_OVERLAY", "2038", "0x7f6"])
def test_overlay_window_type_formats(window_type):
    assert overlay._overlay_windows(window_dump(window_type=window_type))[0] is True


# A focused attacker window must not hide the fact that the tested target remains resumed underneath it.
def test_focused_overlay_with_target_resumed_is_allowed(bench):
    bench.overlay_holds_focus = True
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_ALLOWED", "At Risk")
    assert result.metadata["foreground_package"] == ATTACKER_PACKAGE
    assert result.metadata["resumed_package"] == PACKAGE
    assert result.metadata["capture"]["confirmed"] is True
    assert ["shell", "dumpsys", "activity", "activities"] in bench.commands


# A launcher resumed behind a focused attacker window is still an inconclusive exit to the home screen.
def test_focused_overlay_with_launcher_resumed_is_not_tested(bench):
    bench.overlay_holds_focus = True
    bench.activity_dump = "  topResumedActivity=ActivityRecord{1a2b3c u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t12}"
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_NOT_TESTED", "Inconclusive")
    assert "Home Screen" in result.metadata["verdict"]
    assert result.metadata["resumed_package"] == "com.google.android.apps.nexuslauncher"


# Unreadable or unmatched activity dumps cannot establish which app is behind the focused overlay.
@pytest.mark.parametrize("failure", ["unreadable", "no_resumed_line"])
def test_focused_overlay_with_unknown_resumed_app_is_not_tested(bench, failure):
    bench.overlay_holds_focus = True
    bench.activity_dump_error = failure == "unreadable"
    bench.activity_dump = "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n  mLastPausedActivity=ActivityRecord{1a2b3c u0 com.example.other/.MainActivity t12}"
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_NOT_TESTED", "Inconclusive")
    assert "could not be read" in result.metadata["verdict"]
    assert result.metadata["resumed_package"] is None
    assert ["shell", "dumpsys", "activity", "activities"] in bench.commands


# Recognise each resumed-activity marker and use its captured package to resolve overlay focus.
@pytest.mark.parametrize("marker", ["topResumedActivity=", "ResumedActivity: ", "mResumedActivity: "])
def test_resumed_activity_parser_handles_supported_markers(bench, marker):
    line = f"  {marker}ActivityRecord{{1a2b3c u0 {PACKAGE}/.MainActivity t12}}"
    match = overlay.RESUMED_ACTIVITY_RE.search(line)
    assert match is not None and match[1] == PACKAGE
    bench.overlay_holds_focus = True
    bench.activity_dump = line
    bench.driver.activate_app(PACKAGE)
    result = SimpleNamespace(metadata={})
    assert overlay._foreground_verdict(bench.driver, bench.adb, PACKAGE, result) is None
    assert result.metadata["resumed_package"] == PACKAGE


# A first-launch permission dialog is denied and the overlay is still measured over the target behind it.
def test_permission_prompt_is_denied_and_overlay_is_tested(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_ALLOWED", "At Risk")
    assert result.metadata["permission_prompts"] == [{"title": INSTALLED_APPS_PROMPT, "denied": True, "relaunched": False}]
    assert bench.driver.deny.clicks == 1
    assert result.metadata["foreground_package"] == PACKAGE
    assert result.metadata["capture"]["confirmed"] is True
    assert any(entry["kind"] == "overlay_permission_prompt" and entry["path"].endswith("permission_prompt_1.png") for entry in result.evidence)


# The deny button is found by the AOSP ids, the dialog's negative button or its label.
@pytest.mark.parametrize("locator", [DENY_BUTTON_IDS[1], "android:id/button2", DENY_TEXT_SELECTOR])
def test_permission_prompt_deny_locators(bench, locator):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    bench.deny_locator = locator
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert result.metadata["permission_prompts"][0]["denied"] is True


# Consecutive dialogs are each denied before the overlay is measured.
def test_consecutive_permission_prompts_are_each_denied(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT, "Allow Example to send you notifications?"]
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert [prompt["title"] for prompt in result.metadata["permission_prompts"]] == [INSTALLED_APPS_PROMPT, "Allow Example to send you notifications?"]


# A dialog that closes the app leads to one relaunch, and the overlay is measured once the app is back.
def test_permission_prompt_that_closes_the_app_relaunches_it(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    bench.after_deny = "com.huawei.android.launcher"
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert result.metadata["permission_prompts"] == [{"title": INSTALLED_APPS_PROMPT, "denied": True, "relaunched": True}]
    assert bench.driver.calls.count(("activate", PACKAGE)) == 2


# The attacker's focused overlay in front after the denial is not mistaken for a closed app.
def test_permission_prompt_with_focused_overlay_does_not_relaunch(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    bench.overlay_holds_focus = True
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert result.metadata["permission_prompts"][0]["relaunched"] is False
    assert bench.driver.calls.count(("activate", PACKAGE)) == 1


# A dialog without a recognisable deny button stays inconclusive and names the dialog.
def test_permission_prompt_without_deny_button_is_not_tested(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    bench.deny_locator = None
    result = bench.run()
    assert (result.final_status, result.verdict) == ("OVERLAY_NOT_TESTED", "Inconclusive")
    assert INSTALLED_APPS_PROMPT in result.metadata["verdict"]
    assert result.metadata["permission_prompts"] == [{"title": INSTALLED_APPS_PROMPT, "denied": False, "relaunched": False}]
    assert bench.captures == []


# A dialog that returns after every denial stops at the attempt limit instead of looping.
def test_repeating_permission_prompt_stops_at_the_limit(bench):
    bench.prompts = [INSTALLED_APPS_PROMPT]
    bench.prompt_repeats = True
    result = bench.run()
    assert result.final_status == "OVERLAY_NOT_TESTED"
    assert bench.driver.deny.clicks == 3
    assert "system prompt" in result.metadata["verdict"]


# A dialog raised by the app's scripted steps is denied before the overlay is measured.
def test_permission_prompt_after_scripted_steps_is_denied(bench):
    bench.app.risks[RISK] = {"steps": ["back"]}
    bench.prompt_after_steps = INSTALLED_APPS_PROMPT
    result = bench.run()
    assert result.final_status == "OVERLAY_ALLOWED"
    assert result.metadata["coverage"] == "scripted"
    assert result.metadata["permission_prompts"][0]["denied"] is True


# Without a dialog nothing is tapped and no prompt is recorded.
def test_no_permission_prompt_records_nothing(bench):
    result = bench.run()
    assert result.metadata["permission_prompts"] == []
    assert bench.driver.deny.clicks == 0
    assert not any(value in DENY_BUTTON_IDS for _, value in bench.driver.lookups)
