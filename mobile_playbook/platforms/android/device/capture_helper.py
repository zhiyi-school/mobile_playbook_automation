"""
Drives the screen-capture helper app, which records the screen through MediaProjection as the playbook's malicious
app does, and reads the frames it saves.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, artifacts_root
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.install import install_apks

logger = logging.getLogger(__name__)

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    logger.debug("android capture helper: AppiumBy unavailable; the consent prompt cannot be tapped", exc_info=True)
    AppiumBy = None

HELPER_PACKAGE = "com.mobileplaybook.screencapture"
# Matches android:versionName in the helper's manifest; bump both together so devices get the new build.
HELPER_VERSION = "1"
HELPER_APK_NAME = "screen-capture-helper.apk"
BUILD_COMMAND = "python tools/companion_apps/android_screen_capture/build.py"
OUTPUT_DIR = f"/sdcard/Android/data/{HELPER_PACKAGE}/files/capture"
# The consent prompt's positive button: "Start now" on Android 10-14, "ALLOW" on Huawei, and "Share screen" once the
# entire screen is chosen on newer Android; all are button1.
CONSENT_BUTTON_ID = "android:id/button1"
# Newer Android asks whether to share one app or the entire screen, and may default to one app, which would record
# only the helper; the test needs the entire screen. The option is found by its English text.
SHARE_MODE_ID = "com.android.systemui:id/screen_share_mode_options"
ENTIRE_SCREEN_TEXT = "entire screen"
POLL_SECONDS = 0.5


class CaptureHelperError(Exception):
    """The helper could not be installed, was not given consent, or did not answer in time."""


@dataclass(frozen=True)
class Frame:
    """One saved frame and how blank it is."""

    label: str
    dark_ratio: float
    distinct_colors: int
    png: str | None


# Return where the build script writes the helper APK by default, next to the other companion apps.
def default_helper_apk() -> Path:
    return artifacts_root() / "companion" / "android" / "apks" / HELPER_APK_NAME


# Return the helper APK to install: the configured path, or the default build output.
def helper_apk_path(configured: str | None = None) -> Path:
    if not configured:
        return default_helper_apk()
    path = Path(configured)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


class CaptureHelper:
    """Starts, snaps and stops a recording session of the helper app over adb."""

    # Keep the adb client and the APK to install when the device lacks the current helper.
    def __init__(self, adb: AdbClient, apk: Path):
        self.adb = adb
        self.apk = apk

    # Install the helper unless the device already has this version.
    def ensure_installed(self) -> None:
        installed = installed_version(self.adb, HELPER_PACKAGE)
        if installed is not None and installed.version_name == HELPER_VERSION:
            logger.debug("android capture helper: version %s already installed", HELPER_VERSION)
            return
        if not self.apk.is_file():
            raise CaptureHelperError(f"screen-capture helper APK not found at {self.apk}; build it with `{BUILD_COMMAND}`")
        installed_ok, reason = install_apks(self.adb, [self.apk], grant_permissions=True)
        logger.debug("android capture helper: installed %s ok=%s (%s)", self.apk, installed_ok, reason)
        if not installed_ok:
            raise CaptureHelperError(f"could not install the screen-capture helper: {reason}")

    # Open the helper, tap the system consent prompt's allow button with Appium, and wait until it is recording.
    def start(self, driver, timeout: float = 15) -> None:
        # -S stops an earlier session first; otherwise a leftover helper screen swallows the request and no prompt shows.
        self.adb.run(["shell", "am", "start", "-S", "-n", f"{HELPER_PACKAGE}/.CaptureActivity"])
        self._wait_for_state({"awaiting_consent"}, timeout)
        self._allow_consent(driver, timeout)
        status = self._wait_for_state({"recording", "denied", "error"}, timeout)
        if status.get("state") != "recording":
            raise CaptureHelperError(f"screen capture did not start ({status.get('state')}: {status.get('detail')})")

    # Save the current screen as <label>.png on the device and return its blankness measurements.
    def snap(self, label: str, timeout: float = 10) -> Frame:
        self.adb.run(["shell", "am", "broadcast", "-a", f"{HELPER_PACKAGE}.SNAP", "--es", "label", label, "-p", HELPER_PACKAGE])
        deadline = time.monotonic() + timeout
        while True:
            result = self._read_json(f"{label}.json")
            if result is not None:
                break
            if time.monotonic() >= deadline:
                raise CaptureHelperError(f"the screen-capture helper did not save frame '{label}' within {timeout:g}s")
            time.sleep(POLL_SECONDS)
        logger.debug("android capture helper: frame %s -> %s", label, result)
        if result.get("error"):
            raise CaptureHelperError(f"the screen-capture helper could not save frame '{label}': {result['error']}")
        return Frame(label=label, dark_ratio=float(result["dark_ratio"]), distinct_colors=int(result["distinct_colors"]), png=result.get("png"))

    # Copy a saved frame's PNG into a local folder, returning its path or None when it could not be copied.
    def pull(self, frame: Frame, destination_dir: Path) -> Path | None:
        if not frame.png:
            return None
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / frame.png
        code, _, err = self.adb.run(["pull", f"{OUTPUT_DIR}/{frame.png}", str(destination)], timeout=60)
        logger.debug("android capture helper: pull %s exit %s err=%r", frame.png, code, (err or "")[:200])
        return destination if code == 0 and destination.exists() else None

    # Ask the helper to stop recording, ignoring failures; the recording also ends when the helper is stopped.
    def stop(self) -> None:
        self.adb.run(["shell", "am", "broadcast", "-a", f"{HELPER_PACKAGE}.STOP", "-p", HELPER_PACKAGE])

    # Wait for the consent prompt, choose to share the entire screen when it asks, and tap its allow button.
    def _allow_consent(self, driver, timeout: float) -> None:
        if AppiumBy is None:
            raise CaptureHelperError("Appium-Python-Client is not installed, so the consent prompt cannot be tapped")
        deadline = time.monotonic() + timeout
        while not driver.find_elements(AppiumBy.ID, CONSENT_BUTTON_ID):
            if time.monotonic() >= deadline:
                raise CaptureHelperError("the screen-capture consent prompt did not appear; is the device unlocked?")
            time.sleep(POLL_SECONDS)
        self._choose_entire_screen(driver, deadline)
        # Find the button again: choosing the entire screen redraws it as "Share screen".
        button = driver.find_elements(AppiumBy.ID, CONSENT_BUTTON_ID)[0]
        logger.debug("android capture helper: tapping consent button %r", getattr(button, "text", None))
        button.click()

    # When the prompt offers one app or the entire screen, select the entire screen; prompts without the choice
    # already record the entire screen.
    def _choose_entire_screen(self, driver, deadline: float) -> None:
        modes = driver.find_elements(AppiumBy.ID, SHARE_MODE_ID)
        if not modes:
            return
        selected = [getattr(view, "text", "") or "" for view in modes[0].find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")]
        logger.debug("android capture helper: share mode prompt shows %r", selected)
        if any(ENTIRE_SCREEN_TEXT in text.lower() for text in selected):
            return
        modes[0].click()
        option = f'new UiSelector().textContains("{ENTIRE_SCREEN_TEXT}")'
        while True:
            options = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, option)
            if options:
                options[0].click()
                return
            if time.monotonic() >= deadline:
                raise CaptureHelperError(
                    "the screen-capture prompt asks to share one app or the entire screen, and the entire-screen option "
                    "was not found; the device language must be English"
                )
            time.sleep(POLL_SECONDS)

    # Poll the helper's status file until its state is one of the expected ones, and return it.
    def _wait_for_state(self, states: set[str], timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        status: dict | None = None
        while True:
            status = self._read_json("status.json")
            if status is not None and status.get("state") in states:
                return status
            if time.monotonic() >= deadline:
                seen = status.get("state") if status else "no status"
                raise CaptureHelperError(f"the screen-capture helper did not reach {sorted(states)} within {timeout:g}s (last: {seen})")
            time.sleep(POLL_SECONDS)

    # Read one of the helper's JSON output files, or None when it does not exist yet.
    def _read_json(self, name: str) -> dict | None:
        code, out, _ = self.adb.run(["shell", "cat", f"{OUTPUT_DIR}/{name}"])
        if code != 0 or not (out or "").strip().startswith("{"):
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None
