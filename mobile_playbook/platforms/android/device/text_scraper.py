"""
Describes Text Scraper, the accessibility attacker app used for android-feature-05-risk-03, and drives it: starts and
stops its screenshot capture through its own screen, and finds, pulls and deletes the screenshots it saved.
"""

from __future__ import annotations

import logging
import shlex
import time
from pathlib import Path

from mobile_playbook.platforms.android.device.accessibility_service import AccessibilityAttackerError, ServiceApp
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.captured_events import CapturedEventsApp

logger = logging.getLogger(__name__)

TEXT_SCRAPER_PACKAGE = "com.malicious.textscraper"
TEXT_SCRAPER_APP = ServiceApp(
    package=TEXT_SCRAPER_PACKAGE, version="1.0", apk_name="F5R3_Text_Scraper_Attacker.apk", label="Text Scraper",
    service_class=f"{TEXT_SCRAPER_PACKAGE}.TextScraperAccessibilityService",
)
UIAUTOMATOR = "-android uiautomator"
SCREENSHOT_BUTTON = f'new UiSelector().resourceId("{TEXT_SCRAPER_PACKAGE}:id/btn_screenshot")'
START_LABEL = "Start Screenshot Capture (Every 5s)"
STOP_LABEL = "Stop Screenshot Capture"
# The view IDs on Text Scraper's own screen, as its rows record them without the package.
TEXT_SCRAPER_VIEW_IDS = frozenset({
    "btn_clear", "btn_enable_service", "btn_export", "btn_screenshot", "recycler_events", "spinner_filter",
    "tv_details", "tv_event_count", "tv_event_type", "tv_timestamp",
})
# Where the system saves screenshots: Pictures/Screenshots on HarmonyOS and Pixels, DCIM/Screenshots on Samsung.
SCREENSHOT_DIRS = ("/sdcard/Pictures/Screenshots", "/sdcard/DCIM/Screenshots")
MEDIA_IMAGES_URI = "content://media/external/images/media"
PULL_TIMEOUT = 60
POLL_SECONDS = 0.5


class TextScraper(CapturedEventsApp):
    """Installs, switches, clears and reads Text Scraper, the playbook's on-screen text extraction app."""

    # Keep the adb client and the APK to install when the device lacks the current Text Scraper build.
    def __init__(self, adb: AdbClient, apk: Path):
        super().__init__(adb, apk, TEXT_SCRAPER_APP)

    # Open Text Scraper and tap its screenshot button, as a user does, unless capture is already on or off as wanted.
    # While on, its service takes a screenshot every five seconds; it must already be bound, or the tap only records
    # the choice.
    def set_screenshot_capture(self, driver, on: bool, launch_wait: float, timeout: float) -> None:
        driver.activate_app(TEXT_SCRAPER_PACKAGE)
        time.sleep(launch_wait)
        wanted = STOP_LABEL if on else START_LABEL
        button = _screenshot_button(driver, timeout)
        if button.text == wanted:
            return
        button.click()
        if not _wait_for_label(driver, wanted, timeout):
            raise AccessibilityAttackerError(f"Text Scraper's screenshot button did not change to {wanted!r} after the tap")

    # Return the device paths of the screenshots now in the system's screenshot folders; a missing folder is empty,
    # while any other failure raises, so an unreachable phone is never read as one without screenshots.
    def screenshots(self) -> set[str]:
        paths = set()
        for folder in SCREENSHOT_DIRS:
            code, out, err = self.adb.run(["shell", "ls", "-1", folder])
            if code != 0:
                if "No such file or directory" in f"{out}{err}":
                    continue
                raise AccessibilityAttackerError(f"could not list {folder}: {(err or out or f'exit {code}').strip()}")
            # Names starting with "." are files still being written, which MediaStore names .pending-*.
            paths.update(f"{folder}/{name}" for name in (out or "").splitlines() if name.strip() and not name.startswith("."))
        return paths

    # Return the screenshots last modified at or after a time in seconds on the phone's clock; one whose time cannot
    # be read is left out, so it is never treated as taken during the test.
    def taken_since(self, paths: set[str], epoch: int) -> set[str]:
        taken = set()
        for path in paths:
            code, out, _ = self.adb.run(["shell", "stat", "-c", "%Y", shlex.quote(path)])
            if code == 0 and (out or "").strip().isdigit() and int(out.strip()) >= epoch:
                taken.add(path)
        return taken

    # Copy screenshots to a local folder, returning the copies and an error for each one that could not be copied.
    def pull_screenshots(self, paths: set[str], destination: Path) -> tuple[list[Path], list[str]]:
        destination.mkdir(parents=True, exist_ok=True)
        pulled, errors = [], []
        for path in sorted(paths):
            local = destination / Path(path).name
            code, out, err = self.adb.run(["pull", path, str(local)], timeout=PULL_TIMEOUT)
            if code == 0 and local.is_file():
                pulled.append(local)
            else:
                errors.append(f"{path}: {(err or out or f'exit {code}').strip()}")
        return pulled, errors

    # Delete screenshots and their media entries, returning the paths still on the device afterwards.
    def delete_screenshots(self, paths: set[str]) -> set[str]:
        for path in sorted(paths):
            self.adb.run(["shell", "rm", "-f", shlex.quote(path)])
            # Android 11 and later update the gallery when the file goes; older versions keep listing it until the
            # media scanner runs. MediaStore records the path under /storage/emulated/0, so it is matched by its end.
            where = f"_data LIKE '%{_like_literal(path.removeprefix('/sdcard'))}' ESCAPE '\\'"
            code, out, err = self.adb.run(["shell", "content", "delete", "--uri", MEDIA_IMAGES_URI, "--where", shlex.quote(where)])
            if code != 0:
                logger.debug("android text scraper: media entry for %s not deleted: %s", path, err or out)
        return paths & self.screenshots()


# Return text for a SQL LIKE pattern that matches it literally, escaping wildcards with a backslash and quotes by
# doubling them.
def _like_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")


# Find Text Scraper's screenshot button, waiting for its screen to appear.
def _screenshot_button(driver, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        found = driver.find_elements(UIAUTOMATOR, SCREENSHOT_BUTTON)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            raise AccessibilityAttackerError("Text Scraper's screenshot button was not found on its screen")
        time.sleep(POLL_SECONDS)


# Wait until the screenshot button shows the label, returning whether it did before the timeout.
def _wait_for_label(driver, label: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        found = driver.find_elements(UIAUTOMATOR, SCREENSHOT_BUTTON)
        if found and found[0].text == label:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
