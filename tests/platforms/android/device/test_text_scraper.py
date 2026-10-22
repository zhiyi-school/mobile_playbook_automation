"""Drives Text Scraper's screenshot button and its saved screenshots through fake Appium and adb clients."""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import text_scraper
from mobile_playbook.platforms.android.device.accessibility_service import AccessibilityAttackerError
from mobile_playbook.platforms.android.device.text_scraper import (
    SCREENSHOT_BUTTON,
    START_LABEL,
    STOP_LABEL,
    TEXT_SCRAPER_APP,
    TEXT_SCRAPER_PACKAGE,
    TextScraper,
)
from mobile_playbook.platforms.android.preflight import check_requirements
from tests.platforms.android.captured_events_fakes import TARGET, CapturedEventsAdb, database_files

PICTURES = "/sdcard/Pictures/Screenshots"
DCIM = "/sdcard/DCIM/Screenshots"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(text_scraper.time, "sleep", lambda seconds: None)


class Button:
    def __init__(self, text, toggles=True):
        self.text = text
        self.toggles = toggles
        self.clicks = 0

    def click(self):
        self.clicks += 1
        if self.toggles:
            self.text = STOP_LABEL if self.text == START_LABEL else START_LABEL


class Driver:
    def __init__(self, button=None):
        self.button = button
        self.activated: list[str] = []

    def activate_app(self, package):
        self.activated.append(package)

    def find_elements(self, by, selector):
        assert (by, selector) == ("-android uiautomator", SCREENSHOT_BUTTON)
        return [self.button] if self.button else []


class ScreenshotAdb:
    """Fake adb with screenshot folders that ls, pull, rm and content delete act on."""

    def __init__(self, folders, pull_fails=(), rm_fails=(), modified=None):
        self.folders = {folder: list(names) for folder, names in folders.items()}
        self.modified = dict(modified or {})
        self.pull_fails = set(pull_fails)
        self.rm_fails = set(rm_fails)
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:3] == ["shell", "ls", "-1"]:
            if args[3] not in self.folders:
                return 1, "", f"ls: {args[3]}: No such file or directory"
            return 0, "".join(f"{name}\n" for name in self.folders[args[3]]), ""
        if args[:4] == ["shell", "stat", "-c", "%Y"]:
            path = shlex.split(args[4])[0]
            return (0, f"{self.modified[path]}\n", "") if path in self.modified else (1, "", "stat: No such file or directory")
        if args[0] == "pull":
            if args[1] in self.pull_fails:
                return 1, "", "adb: error: failed to stat remote object"
            open(args[2], "wb").write(b"jpeg")
            return 0, "1 file pulled", ""
        if args[:3] == ["shell", "rm", "-f"]:
            path = shlex.split(args[3])[0]
            if path not in self.rm_fails:
                folder, name = path.rsplit("/", 1)
                self.folders[folder].remove(name)
            return 0, "", ""
        return 0, "", ""


def scraper(adb, tmp_path):
    return TextScraper(adb, tmp_path / "scraper.apk")


def test_text_scraper_is_described_by_its_manifest():
    assert TEXT_SCRAPER_APP.component == "com.malicious.textscraper/com.malicious.textscraper.TextScraperAccessibilityService"
    assert TEXT_SCRAPER_APP.short_component == "com.malicious.textscraper/.TextScraperAccessibilityService"
    assert (TEXT_SCRAPER_APP.label, TEXT_SCRAPER_APP.version) == ("Text Scraper", "1.0")
    assert TEXT_SCRAPER_APP.apk_path().name == "F5R3_Text_Scraper_Attacker.apk"


def test_text_scraper_reads_its_own_database(tmp_path):
    adb = CapturedEventsAdb(TEXT_SCRAPER_PACKAGE, database_files(tmp_path))

    events = scraper(adb, tmp_path).events(TARGET)

    assert [event.event_type for event in events] == ["FOCUS", "TEXT_CHANGE"]
    assert ["shell", "-T", "run-as", TEXT_SCRAPER_PACKAGE, "cat", "databases/event_database"] in adb.commands


def test_text_scraper_errors_name_text_scraper(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not clear Text Scraper's data: Failed"):
        scraper(CapturedEventsAdb(TEXT_SCRAPER_PACKAGE, clear_output="Failed"), tmp_path).clear()


def test_starting_capture_opens_text_scraper_and_taps_its_button(tmp_path):
    button = Button(START_LABEL)
    driver = Driver(button)

    scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(driver, True, launch_wait=0, timeout=0)

    assert driver.activated == [TEXT_SCRAPER_PACKAGE]
    assert (button.clicks, button.text) == (1, STOP_LABEL)


def test_capture_already_in_the_wanted_state_is_not_tapped(tmp_path):
    running, stopped = Button(STOP_LABEL), Button(START_LABEL)

    scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(Driver(running), True, launch_wait=0, timeout=0)
    scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(Driver(stopped), False, launch_wait=0, timeout=0)

    assert (running.clicks, stopped.clicks) == (0, 0)


def test_stopping_capture_taps_the_button_again(tmp_path):
    button = Button(STOP_LABEL)

    scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(Driver(button), False, launch_wait=0, timeout=0)

    assert (button.clicks, button.text) == (1, START_LABEL)


def test_a_tap_that_does_not_change_the_button_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="did not change to 'Stop Screenshot Capture'"):
        scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(Driver(Button(START_LABEL, toggles=False)), True, launch_wait=0, timeout=0)


def test_a_missing_button_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="screenshot button was not found"):
        scraper(ScreenshotAdb({}), tmp_path).set_screenshot_capture(Driver(), True, launch_wait=0, timeout=0)


def test_screenshots_are_listed_from_every_folder_skipping_pending_files(tmp_path):
    adb = ScreenshotAdb({PICTURES: ["Screenshot_1.jpg", ".pending-1730000000-Screenshot_2.jpg"], DCIM: ["Screenshot_3.png"]})

    assert scraper(adb, tmp_path).screenshots() == {f"{PICTURES}/Screenshot_1.jpg", f"{DCIM}/Screenshot_3.png"}


def test_missing_screenshot_folders_are_empty(tmp_path):
    assert scraper(ScreenshotAdb({}), tmp_path).screenshots() == set()


def test_only_screenshots_modified_since_the_start_count_as_taken(tmp_path):
    paths = {f"{PICTURES}/new.jpg", f"{PICTURES}/at_start.jpg", f"{PICTURES}/old.jpg", f"{PICTURES}/unreadable.jpg"}
    adb = ScreenshotAdb({}, modified={f"{PICTURES}/new.jpg": 2000, f"{PICTURES}/at_start.jpg": 1000, f"{PICTURES}/old.jpg": 999})

    assert scraper(adb, tmp_path).taken_since(paths, 1000) == {f"{PICTURES}/new.jpg", f"{PICTURES}/at_start.jpg"}


def test_screenshots_are_pulled_with_an_error_for_each_failure(tmp_path):
    taken = {f"{PICTURES}/Screenshot_1.jpg", f"{PICTURES}/Screenshot_2.jpg"}
    adb = ScreenshotAdb({PICTURES: ["Screenshot_1.jpg", "Screenshot_2.jpg"]}, pull_fails={f"{PICTURES}/Screenshot_2.jpg"})

    pulled, errors = scraper(adb, tmp_path).pull_screenshots(taken, tmp_path / "screenshots")

    assert pulled == [tmp_path / "screenshots" / "Screenshot_1.jpg"]
    assert errors == [f"{PICTURES}/Screenshot_2.jpg: adb: error: failed to stat remote object"]


def test_only_the_given_screenshots_are_deleted_with_their_media_entries(tmp_path):
    adb = ScreenshotAdb({PICTURES: ["mine.jpg", "Owner's photo.jpg", "theirs.jpg"]})
    taken = {f"{PICTURES}/mine.jpg", f"{PICTURES}/Owner's photo.jpg"}

    remaining = scraper(adb, tmp_path).delete_screenshots(taken)

    assert remaining == set()
    assert adb.folders[PICTURES] == ["theirs.jpg"]
    media = [command for command in adb.commands if command[1:3] == ["content", "delete"]]
    assert [shlex.split(command[-1])[0] for command in media] == ["_data='/sdcard/Pictures/Screenshots/Owner''s photo.jpg'", "_data='/sdcard/Pictures/Screenshots/mine.jpg'"]


def test_screenshots_left_after_deletion_are_returned(tmp_path):
    adb = ScreenshotAdb({PICTURES: ["mine.jpg"]}, rm_fails={f"{PICTURES}/mine.jpg"})

    assert scraper(adb, tmp_path).delete_screenshots({f"{PICTURES}/mine.jpg"}) == {f"{PICTURES}/mine.jpg"}


def _config(apk):
    return SimpleNamespace(accessibility={"text_scraper_apk": str(apk)})


def test_preflight_passes_with_the_text_scraper_apk_in_place(tmp_path):
    apk = tmp_path / "scraper.apk"
    apk.write_bytes(b"apk")

    [(name, ok, message)] = check_requirements(["text_scraper_attacker"], CapturedEventsAdb(TEXT_SCRAPER_PACKAGE), _config(apk))

    assert (name, ok) == ("text_scraper_attacker", True)
    assert str(apk) in message


def test_preflight_passes_when_the_device_already_has_text_scraper(tmp_path):
    adb = CapturedEventsAdb(TEXT_SCRAPER_PACKAGE, version="1.0")

    [(_, ok, message)] = check_requirements(["text_scraper_attacker"], adb, _config(tmp_path / "missing.apk"))

    assert ok and "already installed" in message


@pytest.mark.parametrize("version", [None, "0.9"])
def test_preflight_fails_with_the_apk_path_when_missing_or_old(tmp_path, version):
    apk = tmp_path / "missing.apk"

    [(_, ok, message)] = check_requirements(["text_scraper_attacker"], CapturedEventsAdb(TEXT_SCRAPER_PACKAGE, version=version), _config(apk))

    assert not ok
    assert str(apk) in message and "F5R3_Text_Scraper_Attacker.apk" in message


def test_preflight_defaults_to_the_intake_copy():
    [(_, ok, message)] = check_requirements(["text_scraper_attacker"], None, SimpleNamespace(accessibility={}))

    assert str(TEXT_SCRAPER_APP.apk_path()) in message
