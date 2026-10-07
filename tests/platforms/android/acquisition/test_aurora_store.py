from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.acquisition.aurora_store import classify_screen, install_from_aurora_store
from mobile_playbook.platforms.android.acquisition.play_store import screen_labels

PACKAGE = "com.example.app"
AURORA = "com.aurora.store"
LINK_PROMPT = ("Continue to Aurora Store?", "An external app wants to open Aurora Store", "Cancel", "Open")
LISTING = ("Example App", "Example Publisher", "Manual download", "Install")
INSTALLING = ("Example App", "Installing", "Cancel", "Open")


def listing(*labels: str) -> str:
    nodes = [f'<android.widget.TextView text="{label}" content-desc="" />' for label in labels]
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


class FakeAdb:
    def __init__(self, installed_after: int | None = None):
        self.installed_after = installed_after
        self.checks = 0
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:4] == ["shell", "pm", "list", "packages"] and "-i" not in args:
            self.checks += 1
            installed = self.installed_after is not None and self.checks > self.installed_after
            return (0, f"package:{args[4]}", "") if installed else (0, "", "")
        if args[:3] == ["shell", "dumpsys", "package"]:
            return 0, f"  installerPackageName={AURORA}", ""
        return 0, "Starting: Intent", ""


class FakeElement:
    def __init__(self, driver, label):
        self.driver = driver
        self.label = label

    def is_displayed(self):
        return True

    def click(self):
        self.driver.tapped.append(self.label)
        self.driver.advance()


class FakeDriver:
    """Shows each (package, labels) screen in turn, moving on when something is tapped."""

    def __init__(self, *screens):
        self.screens = list(screens)
        self.tapped: list[str] = []
        self.quit_called = False

    def advance(self):
        if len(self.screens) > 1:
            self.screens.pop(0)

    @property
    def current_package(self):
        return self.screens[0][0]

    @property
    def page_source(self):
        return listing(*self.screens[0][1])

    def find_elements(self, by, selector):
        label = selector.split("^")[1].split("$")[0]
        return [FakeElement(self, text) for text in self.screens[0][1] if text.lower() == label]

    def get_screenshot_as_png(self):
        return b"\x89PNG synthetic"

    def quit(self):
        self.quit_called = True


def install(adb, driver, tmp_path, **kwargs):
    options = {"page_timeout": 0.05, "install_timeout": 0.2, "poll": 0}
    options.update(kwargs)
    return install_from_aurora_store(adb, lambda: driver, PACKAGE, tmp_path / "diagnostics", **options)


class TestClassifyingAScreen:
    def test_the_link_prompt_is_recognised(self):
        assert classify_screen(AURORA, screen_labels(listing(*LINK_PROMPT))) == "link_prompt"

    def test_a_free_listing_is_installable(self):
        assert classify_screen(AURORA, screen_labels(listing(*LISTING))) == "installable"

    def test_open_during_an_install_is_not_installed(self):
        assert classify_screen(AURORA, screen_labels(listing(*INSTALLING))) == "in_progress"

    def test_download_progress_is_in_progress(self):
        assert classify_screen(AURORA, screen_labels(listing("Example App", "2 MB/s, 30s left", "Cancel"))) == "in_progress"

    def test_a_signed_out_aurora_needs_a_person(self):
        labels = screen_labels(listing("Aurora Store", "Please log in to your Google Play account", "Google"))
        assert classify_screen(AURORA, labels) == "sign_in_required"

    def test_a_price_is_a_paid_app(self):
        assert classify_screen(AURORA, screen_labels(listing("Example App", "S$4.98"))) == "paid"

    def test_huaweis_risk_notice_is_a_security_check(self):
        labels = screen_labels(listing("Security notice", "App not filed", "VIEW RISKS"))
        assert classify_screen("com.huawei.appmarket", labels) == "security_check"

    @pytest.mark.parametrize("installer", ["com.android.packageinstaller", "com.google.android.packageinstaller"])
    def test_the_systems_install_prompt_needs_a_person(self, installer):
        assert classify_screen(installer, screen_labels(listing("Do you want to install this app?", "Install"))) == "confirmation_required"

    def test_another_app_in_front_is_unrecognised(self):
        assert classify_screen("com.example.other", screen_labels(listing("Install"))) == "unrecognised"


class TestInstalling:
    def test_the_link_prompt_is_opened_and_a_free_app_installed(self, tmp_path):
        adb = FakeAdb(installed_after=1)
        driver = FakeDriver((AURORA, LINK_PROMPT), (AURORA, LISTING), (AURORA, INSTALLING))

        outcome = install(adb, driver, tmp_path)

        assert outcome.status == "installed"
        assert outcome.installer == AURORA
        assert driver.tapped == ["Open", "Install"]
        assert driver.quit_called
        opened = next(command for command in adb.commands if command[:3] == ["shell", "am", "start"])
        assert opened[opened.index("-p") + 1] == AURORA
        assert f"'market://details?id={PACKAGE}'" in opened

    def test_huaweis_security_check_is_left_for_a_person(self, tmp_path):
        driver = FakeDriver((AURORA, LISTING), ("com.huawei.appmarket", ("Security notice", "VIEW RISKS", "INSTALL")))

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_account_action_required"
        assert outcome.retryable is False
        assert "CAPTCHA" in outcome.detail
        assert driver.tapped == ["Install"]

    def test_the_systems_install_prompt_is_never_tapped(self, tmp_path):
        driver = FakeDriver((AURORA, LISTING), ("com.android.packageinstaller", ("Do you want to install this app?", "Install")))

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_account_action_required"
        assert driver.tapped == ["Install"]

    def test_a_signed_out_aurora_taps_nothing(self, tmp_path):
        driver = FakeDriver((AURORA, ("Please log in to your Google Play account", "Google")))

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_account_action_required"
        assert driver.tapped == []

    def test_an_unrecognised_listing_saves_diagnostics(self, tmp_path):
        driver = FakeDriver((AURORA, ("Example App", "Something new")))

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_screen_unrecognised"
        assert driver.tapped == []
        assert outcome.diagnostics is not None

    def test_two_install_buttons_tap_nothing(self, tmp_path):
        driver = FakeDriver((AURORA, ("Example App", "Install", "Install")))

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_screen_unrecognised"
        assert driver.tapped == []

    def test_an_install_already_under_way_is_waited_for(self, tmp_path):
        driver = FakeDriver((AURORA, INSTALLING))

        outcome = install(FakeAdb(installed_after=1), driver, tmp_path)

        assert outcome.status == "installed"
        assert driver.tapped == []

    def test_a_slow_install_is_retryable(self, tmp_path):
        driver = FakeDriver((AURORA, LISTING), (AURORA, INSTALLING))

        outcome = install(FakeAdb(), driver, tmp_path, install_timeout=0.05)

        assert outcome.blocker_code == "app_install_timeout"
        assert outcome.retryable is True
