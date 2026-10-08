from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.risks.feature_06_risk_01_screen_capture import (
    AndroidScreenCaptureRisk,
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
        ("com.android.chrome", "BLOCKED (Redirected to Browser"),
        ("com.google.android.apps.nexuslauncher", "BLOCKED (App exited to Home Screen"),
        ("com.huawei.android.launcher", "BLOCKED (App exited to Home Screen"),
        ("com.example.other", "BLOCKED (Switched to unknown app"),
        ("com.android.permissioncontroller", "INCONCLUSIVE (A system prompt covered the app"),
        ("com.google.android.permissioncontroller", "INCONCLUSIVE (A system prompt covered the app"),
        ("com.example.app", "Unknown"),
    ],
)
def test_only_a_real_hand_off_counts_as_blocking_screen_capture(foreground, expected):
    driver = ForegroundDriver(foreground)

    verdict = AndroidScreenCaptureRisk()._check_app_switch(driver, "com.example.app")

    assert verdict.startswith(expected)


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
    assert risk._check_app_switch(ForegroundDriver("com.huawei.appmarket"), "com.example.app").startswith("BLOCKED (Switched to unknown app")


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


class NoRecordingDriver(ForegroundDriver):
    session_id = "session"
    page_source = "<hierarchy/>"

    def start_recording_screen(self) -> None:
        raise RuntimeError("screenrecord not found\nStacktrace:\nUnknownError: screenrecord not found\n    at scheduleScreenRecord")

    def activate_app(self, package: str) -> None:
        pass

    def quit(self) -> None:
        pass


class FakeReportWriter:
    run_timestamp = "2026-10-08_00-00-00"

    def __init__(self, root):
        self.root = root

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        pass


def test_a_recording_that_fails_to_start_is_reported_without_changing_the_verdict(tmp_path):
    def adb_run(args):
        return 0, window_dump(("com.example.app", "SECURE HARDWARE_ACCELERATED")), ""

    adb = SimpleNamespace(run=adb_run)
    device_client = SimpleNamespace(make_driver=lambda: NoRecordingDriver("com.example.app"), adb=adb)
    app_config = SimpleNamespace(id="example", name="Example", package_name="com.example.app", risks={})
    global_config = SimpleNamespace(screen_capture={"launch_wait": 0})

    result = AndroidScreenCaptureRisk().run(app_config, global_config, device_client, FakeReportWriter(tmp_path))

    assert result.metadata["recording"] == "not started: screenrecord not found"
    assert result.final_status == "SCREEN_CAPTURE_BLOCKED"
    assert result.errors == []
    assert result.evidence == []


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
