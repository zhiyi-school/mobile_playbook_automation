from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device.capture_helper import CaptureHelperError, Frame
from mobile_playbook.platforms.android.risks import feature_06_risk_01_screen_capture as screen_capture
from mobile_playbook.platforms.android.risks.feature_06_risk_01_screen_capture import (
    AndroidScreenCaptureRisk,
    _is_blank,
    _is_secure,
    _window_flags,
    _security_verdict_from_verdict,
    _status_from_verdict,
)


@pytest.mark.parametrize(
    "verdict,expected_status,expected_security_verdict",
    [
        ("ALLOWED (visible, unprotected)", "SCREEN_CAPTURE_ALLOWED", "At Risk"),
        ("BLOCKED (FLAG_SECURE set)", "SCREEN_CAPTURE_BLOCKED", "Reduced Risk"),
        ("ERROR (driver failure)", "FAILED", "Inconclusive"),
        ("INCONCLUSIVE (Redirected to the Play Store)", "SCREEN_CAPTURE_NOT_TESTED", "Inconclusive"),
        ("SOMETHING_UNEXPECTED", "UNKNOWN", "Inconclusive"),
    ],
)
def test_screen_capture_status_and_security_verdict_stay_in_sync(verdict, expected_status, expected_security_verdict):
    assert _status_from_verdict(verdict) == expected_status
    assert _security_verdict_from_verdict(verdict) == expected_security_verdict


class ForegroundDriver:
    def __init__(self, current_package: str):
        self.current_package = current_package
        self.terminated: list[str] = []

    def terminate_app(self, package: str) -> None:
        self.terminated.append(package)


@pytest.mark.parametrize(
    ("foreground", "expected"),
    [
        ("com.android.vending", "INCONCLUSIVE"),
        ("com.android.chrome", "INCONCLUSIVE (Redirected to Browser"),
        ("com.google.android.apps.nexuslauncher", "INCONCLUSIVE (App exited to Home Screen"),
        ("com.huawei.android.launcher", "INCONCLUSIVE (App exited to Home Screen"),
        ("com.example.other", "INCONCLUSIVE (Switched to unknown app"),
        ("com.android.permissioncontroller", "INCONCLUSIVE (A system prompt covered the app"),
        ("com.google.android.permissioncontroller", "INCONCLUSIVE (A system prompt covered the app"),
        ("com.example.app", "Unknown"),
    ],
)
def test_foreground_hand_offs_do_not_prove_capture_was_blocked(foreground, expected):
    driver = ForegroundDriver(foreground)

    verdict = AndroidScreenCaptureRisk()._check_app_switch(driver, "com.example.app")

    assert verdict.startswith(expected)
    assert driver.terminated == []


def test_a_store_redirect_is_inconclusive_not_reduced_risk():
    verdict = AndroidScreenCaptureRisk()._check_app_switch(ForegroundDriver("com.android.vending"), "com.example.app")

    assert _security_verdict_from_verdict(verdict) == "Inconclusive"
    assert _status_from_verdict(verdict) == "SCREEN_CAPTURE_NOT_TESTED"


@pytest.mark.parametrize(
    ("store_package", "store_name"), [("com.android.vending", "Play Store"), ("com.aurora.store", "Aurora Store")]
)
def test_a_redirect_to_any_store_app_is_inconclusive(store_package, store_name):
    risk = AndroidScreenCaptureRisk()

    verdict = risk._check_app_switch(ForegroundDriver(store_package), "com.example.app")

    assert verdict.startswith(f"INCONCLUSIVE (Redirected to {store_name}")
    assert risk._check_app_switch(ForegroundDriver("com.huawei.appmarket"), "com.example.app").startswith("INCONCLUSIVE (Switched to unknown app")


def test_a_permission_prompt_is_inconclusive_and_left_open():
    driver = ForegroundDriver("com.android.permissioncontroller")

    verdict = AndroidScreenCaptureRisk()._check_app_switch(driver, "com.example.app")

    assert _security_verdict_from_verdict(verdict) == "Inconclusive"
    assert _status_from_verdict(verdict) == "SCREEN_CAPTURE_NOT_TESTED"
    assert driver.terminated == []


# Build a `dumpsys window windows` dump in the format Android 12 prints, one window per (package, fl= flags) pair.
def window_dump(*windows) -> str:
    lines = ["WINDOW MANAGER WINDOWS (dumpsys window windows)"]
    for index, (package, flags) in enumerate(windows):
        lines += [
            f"  Window #{index} Window{{{index:07x} u0 {package}/{package}.MainActivity}}:",
            "    mDisplayId=0 rootTaskId=33 mSession=Session{44db5be 7267:u0a10117}",
            f"    mOwnerUid=10117 showForAllUsers=false package={package} appop=NONE",
            "    mAttrs={(0,0)(fillxfill) sim={adjust=resize} ty=BASE_APPLICATION",
            f"      fl={flags}",
            "      pfl=NO_MOVE_ANIMATION USE_BLAST SECURE_LOOKING",
            "      bhv=DEFAULT",
            "      fitSides=}",
        ]
    return "\n".join(lines) + "\n"


class AppDriver(ForegroundDriver):
    session_id = "session"
    page_source = "<hierarchy/>"

    def activate_app(self, package: str) -> None:
        pass

    def quit(self) -> None:
        self.quit_called = True


class FakeReportWriter:
    run_timestamp = "2026-10-08_00-00-00"

    def __init__(self, root):
        self.root = root

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        pass


CONTENT = Frame(label="", dark_ratio=0.0, distinct_colors=400, png=None)
BLANK = Frame(label="", dark_ratio=1.0, distinct_colors=1, png=None)


class FakeHelper:
    """Stands in for CaptureHelper: hands out the given frames in order and records what it was asked to do."""

    def __init__(self, frames, start_error=None):
        self.frames = list(frames)
        self.start_error = start_error
        self.calls: list[str] = []

    def __call__(self, adb, apk):
        return self

    def ensure_installed(self):
        self.calls.append("install")

    def start(self, driver, timeout):
        self.calls.append("start")
        if self.start_error:
            raise CaptureHelperError(self.start_error)

    def snap(self, label):
        self.calls.append(f"snap:{label}")
        frame = self.frames.pop(0)
        return Frame(label=label, dark_ratio=frame.dark_ratio, distinct_colors=frame.distinct_colors, png=f"{label}.png")

    def pull(self, frame, destination_dir):
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / frame.png
        path.write_bytes(b"png")
        return path

    def stop(self):
        self.calls.append("stop")


def run_with(tmp_path, monkeypatch, helper, *, flags="HARDWARE_ACCELERATED", foreground="com.example.app", driver=None):
    monkeypatch.setattr(screen_capture, "CaptureHelper", helper)

    def adb_run(args, timeout=None):
        if args[:3] == ["shell", "pm", "path"]:
            return 0, "package:/data/app/x/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, f"{'c' * 64}  /data/app/x/base.apk\n", ""
        return 0, window_dump(("com.example.app", flags)), ""

    device_client = SimpleNamespace(make_driver=lambda: driver if driver is not None else AppDriver(foreground), adb=SimpleNamespace(run=adb_run))
    app_config = SimpleNamespace(id="example", name="Example", package_name="com.example.app", risks={})
    global_config = SimpleNamespace(screen_capture={"launch_wait": 0, "record_lead_in": 0})
    return AndroidScreenCaptureRisk().run(app_config, global_config, device_client, FakeReportWriter(tmp_path))


def test_an_app_recorded_with_its_content_is_at_risk(tmp_path, monkeypatch):
    helper = FakeHelper([CONTENT, CONTENT])

    result = run_with(tmp_path, monkeypatch, helper)

    assert result.final_status == "SCREEN_CAPTURE_ALLOWED"
    assert result.verdict == "At Risk"
    assert result.metadata["capture"]["app_open"] == {"dark_ratio": 0.0, "distinct_colors": 400, "blank": False}
    assert result.metadata["flag_secure"] is False
    assert result.metadata["installed_base_apk_sha256"] == "c" * 64
    assert [item["kind"] for item in result.evidence] == ["screen_capture_frame", "screen_capture_frame"]
    assert helper.calls == ["install", "start", "snap:before_launch", "snap:app_open", "stop"]


def test_an_app_recorded_as_a_blank_screen_is_reduced_risk(tmp_path, monkeypatch):
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, BLANK]), flags="SECURE HARDWARE_ACCELERATED")

    assert result.final_status == "SCREEN_CAPTURE_BLOCKED"
    assert result.metadata["verdict"] == "BLOCKED (Screen recording captured a blank screen while the app was open)"
    assert result.metadata["flag_secure"] is True


def test_the_recording_decides_even_when_flag_secure_says_otherwise(tmp_path, monkeypatch):
    # A protected video view inside the screen blanks the recording without the window flag.
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, BLANK]), flags="HARDWARE_ACCELERATED")

    assert result.final_status == "SCREEN_CAPTURE_BLOCKED"
    assert result.metadata["flag_secure"] is False


def test_a_recording_blank_before_launch_cannot_judge_the_app(tmp_path, monkeypatch):
    result = run_with(tmp_path, monkeypatch, FakeHelper([BLANK, BLANK]))

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert "already blank on the home screen" in result.metadata["verdict"]


@pytest.mark.parametrize("foreground", ["com.android.chrome", "com.android.launcher3", "com.example.other"])
def test_a_redirect_is_inconclusive_and_its_frame_kept(tmp_path, monkeypatch, foreground):
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, CONTENT]), foreground=foreground)

    assert result.final_status == "SCREEN_CAPTURE_NOT_TESTED"
    assert result.verdict == "Inconclusive"
    assert result.metadata["verdict"].startswith("INCONCLUSIVE")
    assert "flag_secure" not in result.metadata
    assert len(result.evidence) == 2


@pytest.mark.parametrize("foreground", ["com.example.app", "com.android.chrome"])
def test_a_broken_recording_cannot_be_overridden_by_a_redirect_or_warning(tmp_path, monkeypatch, foreground):
    driver = AppDriver(foreground)
    driver.page_source = '<Text text="USB debugging detected. Screen recording is prohibited."/>'

    result = run_with(tmp_path, monkeypatch, FakeHelper([BLANK, CONTENT]), driver=driver)

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert "already blank on the home screen" in result.metadata["verdict"]
    assert len(result.evidence) == 2


@pytest.mark.parametrize(
    ("source", "observation", "keyword"),
    [
        ('<Text text="USB debugging detected"/>', "debugging_keywords", "usb debugging"),
        ('<Text text="Screen recording detected"/>', "capture_keywords", "screen recording"),
        ('<Text text="Screen sharing is prohibited"/>', "capture_keywords", "screen sharing"),
    ],
)
@pytest.mark.parametrize(("frame", "status", "verdict"), [(CONTENT, "SCREEN_CAPTURE_ALLOWED", "At Risk"), (BLANK, "SCREEN_CAPTURE_BLOCKED", "Reduced Risk")])
def test_warning_keywords_are_observations_and_the_recording_decides(tmp_path, monkeypatch, source, observation, keyword, frame, status, verdict):
    driver = AppDriver("com.example.app")
    driver.page_source = source

    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, frame]), driver=driver)

    assert result.final_status == status
    assert result.verdict == verdict
    assert keyword in result.metadata["ui_observations"][observation]
    assert len(result.evidence) == 2


def test_visible_content_is_at_risk_even_when_flag_secure_is_reported(tmp_path, monkeypatch):
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, CONTENT]), flags="SECURE")

    assert result.final_status == "SCREEN_CAPTURE_ALLOWED"
    assert result.verdict == "At Risk"
    assert result.metadata["flag_secure"] is True


class UnreadableForegroundDriver(AppDriver):
    def __init__(self):
        self.terminated = []

    @property
    def current_package(self):
        raise RuntimeError("foreground query failed")


def test_an_unreadable_foreground_cannot_classify_an_unattributed_frame(tmp_path, monkeypatch):
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, CONTENT]), driver=UnreadableForegroundDriver())

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert "foreground query failed" in result.metadata["verdict"]
    assert len(result.evidence) == 2


class UnreadableSourceDriver(AppDriver):
    @property
    def page_source(self):
        raise RuntimeError("UI query failed")


def test_unavailable_ui_keywords_do_not_override_capture_evidence(tmp_path, monkeypatch):
    result = run_with(tmp_path, monkeypatch, FakeHelper([CONTENT, CONTENT]), driver=UnreadableSourceDriver("com.example.app"))

    assert result.final_status == "SCREEN_CAPTURE_ALLOWED"
    assert result.verdict == "At Risk"
    assert "UI query failed" in result.metadata["ui_observations"]["error"]


def test_a_helper_that_cannot_start_fails_the_run_and_stops_nothing(tmp_path, monkeypatch):
    helper = FakeHelper([], start_error="the screen-capture consent prompt did not appear; is the device unlocked?")

    result = run_with(tmp_path, monkeypatch, helper)

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert result.errors == ["the screen-capture consent prompt did not appear; is the device unlocked?"]
    assert "stop" not in helper.calls


@pytest.mark.parametrize(
    ("dark_ratio", "distinct_colors", "blank"),
    [(1.0, 1, True), (0.985, 12, True), (0.99, 300, False), (0.5, 3, False)],
)
def test_only_a_flat_black_frame_counts_as_blank(dark_ratio, distinct_colors, blank):
    # A dark-themed screen can be nearly all black but still has text and icons in many colours.
    assert _is_blank(Frame(label="x", dark_ratio=dark_ratio, distinct_colors=distinct_colors, png=None)) is blank


class DumpAdb:
    def __init__(self, dump: str, code: int = 0):
        self.dump = dump
        self.code = code

    def run(self, args):
        return self.code, self.dump, "" if self.code == 0 else "error: closed"


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        ([("com.example.app", "LAYOUT_IN_SCREEN SECURE HARDWARE_ACCELERATED")], "BLOCKED (FLAG_SECURE detected via ADB)"),
        ([("com.example.app", "LAYOUT_IN_SCREEN HARDWARE_ACCELERATED")], "ALLOWED (UI visible and no security flags)"),
        # Another app's secure window, such as a password manager overlay, says nothing about the tested app.
        ([("com.other.vault", "SECURE"), ("com.example.app", "HARDWARE_ACCELERATED")], "ALLOWED (UI visible and no security flags)"),
        # A secure dialog window of the app is enough, even when its main window is not.
        ([("com.example.app", "HARDWARE_ACCELERATED"), ("com.example.app", "SECURE DIM_BEHIND")], "BLOCKED (FLAG_SECURE detected via ADB)"),
        ([("com.other.app", "SECURE")], "ERROR (no window found for com.example.app in dumpsys window)"),
    ],
)
def test_flag_secure_is_read_from_the_apps_own_windows(windows, expected):
    device_client = SimpleNamespace(adb=DumpAdb(window_dump(*windows)))

    assert AndroidScreenCaptureRisk()._check_flag_secure(device_client, "com.example.app") == expected


def test_a_package_name_prefix_does_not_count_as_the_app():
    dump = window_dump(("com.example.app.helper", "SECURE"))

    assert _window_flags(dump, "com.example.app") == []


@pytest.mark.parametrize(("flags", "secure"), [("#2000", True), ("#81810100", False), ("SECURE", True), ("SECURE_LOOKING", False)])
def test_flag_secure_is_recognised_by_name_or_hex_value(flags, secure):
    assert _is_secure(flags) is secure


def test_a_failed_window_dump_is_an_adb_error():
    device_client = SimpleNamespace(adb=DumpAdb("", code=1))

    assert AndroidScreenCaptureRisk()._check_flag_secure(device_client, "com.example.app").startswith("ERROR (ADB check failed")
