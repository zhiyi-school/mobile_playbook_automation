from __future__ import annotations

import json

import pytest

from mobile_playbook.platforms.android.device import capture_helper
from mobile_playbook.platforms.android.device.capture_helper import (
    HELPER_PACKAGE,
    REPOSITORY_ROOT,
    HELPER_VERSION,
    OUTPUT_DIR,
    CaptureHelper,
    CaptureHelperError,
    Frame,
    helper_apk_path,
)
from mobile_playbook.platforms.android.preflight import check_requirements


class HelperAdb:
    """Fake adb backed by a dict of the helper's output files, with an optional hook run on each command."""

    def __init__(self, version=None, files=None, on_command=None, install_result=(0, "Success", "")):
        self.version = version
        self.files = dict(files or {})
        self.on_command = on_command
        self.install_result = install_result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if self.on_command:
            self.on_command(self, args)
        if args[:3] == ["shell", "dumpsys", "package"]:
            return (0, f"  versionCode=1\n  versionName={self.version}\n", "") if self.version else (0, "", "")
        if args[0] in {"install", "install-multiple"}:
            return self.install_result
        if args[:2] == ["shell", "cat"]:
            name = args[2].rsplit("/", 1)[1]
            return (0, json.dumps(self.files[name]), "") if name in self.files else (1, "", "No such file or directory")
        return 0, "", ""


class ConsentDriver:
    def __init__(self, adb, appear_after=0):
        self.adb = adb
        self.appear_after = appear_after
        self.clicked = False

    def find_elements(self, by, value):
        assert value == "android:id/button1"
        if self.appear_after > 0:
            self.appear_after -= 1
            return []
        return [Button(self)]


class Button:
    text = "ALLOW"

    def __init__(self, driver):
        self.driver = driver

    def click(self):
        self.driver.clicked = True
        self.driver.adb.files["status.json"] = {"state": "recording"}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(capture_helper, "POLL_SECONDS", 0)


def opens_prompt(adb, args):
    if args[:3] == ["shell", "am", "start"]:
        adb.files["status.json"] = {"state": "awaiting_consent"}


def test_the_current_helper_is_not_reinstalled(tmp_path):
    adb = HelperAdb(version=HELPER_VERSION)

    CaptureHelper(adb, tmp_path / "missing.apk").ensure_installed()

    assert not any(command[0] == "install" for command in adb.commands)


def test_a_missing_or_old_helper_is_installed_from_the_apk(tmp_path):
    apk = tmp_path / "helper.apk"
    apk.write_bytes(b"apk")
    adb = HelperAdb(version="0")

    CaptureHelper(adb, apk).ensure_installed()

    assert ["install", "-r", "-g", str(apk)] in adb.commands


def test_a_helper_that_is_neither_installed_nor_built_says_how_to_build_it(tmp_path):
    with pytest.raises(CaptureHelperError, match="build.py"):
        CaptureHelper(HelperAdb(), tmp_path / "missing.apk").ensure_installed()


def test_start_taps_the_consent_prompt_and_waits_for_recording(tmp_path):
    adb = HelperAdb(on_command=opens_prompt)
    driver = ConsentDriver(adb, appear_after=2)

    CaptureHelper(adb, tmp_path / "helper.apk").start(driver, timeout=5)

    assert driver.clicked
    assert ["shell", "am", "start", "-S", "-n", f"{HELPER_PACKAGE}/.CaptureActivity"] in adb.commands


def test_a_refused_consent_stops_the_test_with_the_helpers_reason(tmp_path, monkeypatch):
    adb = HelperAdb(on_command=opens_prompt)
    driver = ConsentDriver(adb)
    monkeypatch.setattr(Button, "click", lambda self: adb.files.update({"status.json": {"state": "denied", "detail": "screen capture consent was not given"}}))

    with pytest.raises(CaptureHelperError, match="denied: screen capture consent was not given"):
        CaptureHelper(adb, tmp_path / "helper.apk").start(driver, timeout=5)


def test_a_prompt_that_never_appears_times_out(tmp_path):
    adb = HelperAdb(on_command=opens_prompt)
    driver = ConsentDriver(adb, appear_after=10**9)

    with pytest.raises(CaptureHelperError, match="consent prompt did not appear"):
        CaptureHelper(adb, tmp_path / "helper.apk").start(driver, timeout=0.01)


def test_snap_returns_the_frames_measurements(tmp_path):
    def saves_frame(adb, args):
        if "SNAP" in " ".join(args):
            adb.files["app_open.json"] = {"label": "app_open", "dark_ratio": 1, "distinct_colors": 1, "png": "app_open.png"}

    frame = CaptureHelper(HelperAdb(on_command=saves_frame), tmp_path / "helper.apk").snap("app_open")

    assert frame == Frame(label="app_open", dark_ratio=1.0, distinct_colors=1, png="app_open.png")


def test_a_frame_the_helper_could_not_save_is_an_error(tmp_path):
    adb = HelperAdb(files={"app_open.json": {"label": "app_open", "error": "no frame has been recorded yet"}})

    with pytest.raises(CaptureHelperError, match="no frame has been recorded yet"):
        CaptureHelper(adb, tmp_path / "helper.apk").snap("app_open")


def test_a_frame_that_never_arrives_times_out(tmp_path):
    with pytest.raises(CaptureHelperError, match="did not save frame 'app_open'"):
        CaptureHelper(HelperAdb(), tmp_path / "helper.apk").snap("app_open", timeout=0.01)


def test_a_frame_is_pulled_from_the_helpers_folder(tmp_path):
    adb = HelperAdb()

    def pull(args, timeout=None):
        adb.commands.append(list(args))
        (tmp_path / "out" / "app_open.png").write_bytes(b"png")
        return 0, "", ""

    adb.run = pull

    path = CaptureHelper(adb, tmp_path / "helper.apk").pull(Frame("app_open", 0.0, 400, "app_open.png"), tmp_path / "out")

    assert path == tmp_path / "out" / "app_open.png"
    assert adb.commands[-1][:2] == ["pull", f"{OUTPUT_DIR}/app_open.png"]


def test_preflight_passes_with_a_built_helper_apk(tmp_path):
    apk = tmp_path / "helper.apk"
    apk.write_bytes(b"apk")
    config = type("Config", (), {"screen_capture": {"helper_apk": str(apk)}})()

    [(name, ok, message)] = check_requirements(["screen_capture_helper"], HelperAdb(), config)

    assert ok and str(apk) in message


def test_preflight_passes_when_the_device_already_has_the_helper(tmp_path):
    config = type("Config", (), {"screen_capture": {"helper_apk": str(tmp_path / "missing.apk")}})()

    [(_, ok, message)] = check_requirements(["screen_capture_helper"], HelperAdb(version=HELPER_VERSION), config)

    assert ok and "already installed" in message


def test_preflight_fails_with_the_build_command_when_the_helper_is_missing(tmp_path):
    config = type("Config", (), {"screen_capture": {"helper_apk": str(tmp_path / "missing.apk")}})()

    [(_, ok, message)] = check_requirements(["screen_capture_helper"], HelperAdb(), config)

    assert not ok
    assert "python tools/companion_apps/android_screen_capture/build.py" in message


def test_the_helper_apk_defaults_to_the_companion_apps_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "store"))

    assert helper_apk_path() == (tmp_path / "store").resolve() / "companion" / "android" / "apks" / "screen-capture-helper.apk"


def test_a_relative_configured_helper_apk_resolves_against_the_repository():
    assert helper_apk_path("custom/helper.apk") == REPOSITORY_ROOT / "custom" / "helper.apk"
