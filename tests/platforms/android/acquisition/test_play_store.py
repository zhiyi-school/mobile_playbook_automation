from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.platforms.android.acquisition import play_store
from mobile_playbook.platforms.android.acquisition.play_store import (
    classify_listing,
    install_from_play_store,
    screen_labels,
)

PACKAGE = "com.example.app"


def listing(*labels: str, descriptions: tuple[str, ...] = ()) -> str:
    """A synthetic UiAutomator2 page source holding the given text and content descriptions."""
    nodes = [f'<android.widget.TextView text="{label}" content-desc="" clickable="false" />' for label in labels]
    nodes += [f'<android.view.View text="" content-desc="{label}" clickable="true" />' for label in descriptions]
    return '<?xml version="1.0" encoding="UTF-8"?><hierarchy>' + "".join(nodes) + "</hierarchy>"


class FakeAdb:
    """Answers the adb calls the installer makes; the app appears once `installed_after` polls have passed."""

    def __init__(self, installed: bool = False, installed_after: int | None = None, installer: str = "com.android.vending"):
        self.installed = installed
        self.installed_after = installed_after
        self.installer = installer
        self.checks = 0
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:4] == ["shell", "pm", "list", "packages"] and "-i" not in args:
            self.checks += 1
            if self.installed_after is not None and self.checks > self.installed_after:
                self.installed = True
            return (0, f"package:{args[4]}", "") if self.installed else (0, "", "")
        if args[:3] == ["shell", "dumpsys", "package"]:
            return 0, f"  installerPackageName={self.installer}", ""
        if args[:3] == ["shell", "am", "start"]:
            return 0, "Starting: Intent", ""
        return 0, "", ""


class FakeElement:
    def __init__(self, driver: "FakeDriver"):
        self.driver = driver

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.driver.taps += 1
        self.driver.page_source = self.driver.after_tap


class FakeDriver:
    """A Play Store session showing `page_source`, then `after_tap` once Install is tapped."""

    def __init__(self, page_source: str, after_tap: str | None = None):
        self.current_package = play_store.PLAY_STORE_PACKAGE
        self.page_source = page_source
        self.after_tap = after_tap if after_tap is not None else listing("Cancel")
        self.taps = 0
        self.lookups: list[str] = []
        self.quit_called = False

    def find_element(self, by, selector):
        self.lookups.append(selector)
        if "install" in selector and "Install" in self.page_source:
            return FakeElement(self)
        raise LookupError(selector)

    def get_screenshot_as_png(self):
        return b"\x89PNG synthetic"

    def quit(self):
        self.quit_called = True


def install(adb: FakeAdb, driver: FakeDriver | None, tmp_path: Path, **kwargs):
    made: list[FakeDriver] = []

    def make_driver():
        assert driver is not None, "no driver should be needed"
        made.append(driver)
        return driver

    options = {"page_timeout": 0.05, "install_timeout": 0.2, "poll": 0}
    options.update(kwargs)
    return install_from_play_store(adb, make_driver, PACKAGE, tmp_path / "diagnostics", **options), made


class TestClassifyingAListing:
    def test_a_free_app_is_installable(self):
        assert classify_listing(screen_labels(listing("Example App", "Contains ads", "Install"))) == "installable"

    def test_an_install_button_named_only_by_its_description_is_recognised(self):
        assert classify_listing(screen_labels(listing("Example App", descriptions=("Install",)))) == "installable"

    @pytest.mark.parametrize("price", ["$2.99", "S$4.98", "US$ 1.99", "€3,49", "RM 4.90", "₹99", "Buy", "Buy for $2.99"])
    def test_a_priced_button_is_a_paid_app(self, price):
        assert classify_listing(screen_labels(listing("Example App", price))) == "paid"

    def test_a_price_wins_over_an_install_label_so_nothing_paid_is_tapped(self):
        assert classify_listing(screen_labels(listing("Install", "$2.99"))) == "paid"

    def test_in_app_purchase_text_alone_does_not_make_an_app_paid(self):
        labels = screen_labels(listing("In-app purchases", "$0.99 - $49.99 per item", "Install"))
        assert classify_listing(labels) == "installable"

    @pytest.mark.parametrize("label", ["Open", "Play", "Uninstall", "Update"])
    def test_an_installed_app_offers_open_or_update(self, label):
        assert classify_listing(screen_labels(listing("Example App", label))) == "installed"

    @pytest.mark.parametrize(
        "phrase",
        [
            "This item isn't available in your country.",
            "This app is not compatible with your device",
            "Item not found.",
            "Pre-register",
        ],
    )
    def test_an_unavailable_listing_is_recognised(self, phrase):
        assert classify_listing(screen_labels(listing("Example App", phrase))) == "unavailable"

    def test_a_signed_out_store_needs_a_person(self):
        assert classify_listing(screen_labels(listing("Sign in to Google Play", "Sign in"))) == "sign_in_required"

    def test_an_account_setup_prompt_needs_a_person(self):
        labels = screen_labels(listing("Complete account setup", "Continue", "Skip"))
        assert classify_listing(labels) == "account_action_required"

    @pytest.mark.parametrize("source", ["", listing(), listing("Installieren"), "<hierarchy><node/></hierarchy>"])
    def test_anything_else_is_unrecognised(self, source):
        assert classify_listing(screen_labels(source)) == "unrecognised"

    def test_labels_are_unescaped_and_empty_values_skipped(self):
        assert screen_labels(listing("Tom &amp; Jerry", "")) == ["Tom & Jerry"]


class TestInstalling:
    def test_an_installed_app_needs_no_store_session(self, tmp_path):
        outcome, made = install(FakeAdb(installed=True), None, tmp_path)

        assert outcome.status == "already_installed"
        assert outcome.installer == "com.android.vending"
        assert made == []

    def test_a_free_app_is_installed_by_tapping_install_once(self, tmp_path):
        adb = FakeAdb(installed_after=1)
        driver = FakeDriver(listing("Example App", "Install"))

        outcome, _ = install(adb, driver, tmp_path)

        assert outcome.status == "installed"
        assert outcome.blocker_code is None
        assert outcome.installer == "com.android.vending"
        assert driver.taps == 1
        assert driver.quit_called
        opened = next(command for command in adb.commands if command[:3] == ["shell", "am", "start"])
        assert opened[opened.index("-p") + 1] == "com.android.vending"
        assert f"'market://details?id={PACKAGE}'" in opened

    def test_a_paid_app_is_never_tapped_and_needs_a_person(self, tmp_path):
        driver = FakeDriver(listing("Example App", "S$4.98"))

        outcome, _ = install(FakeAdb(), driver, tmp_path)

        assert outcome.status == "failed"
        assert outcome.blocker_code == "app_paid"
        assert outcome.retryable is False
        assert driver.taps == 0
        assert driver.lookups == []

    def test_a_price_shown_after_install_stops_the_attempt(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"), after_tap=listing("Buy for $2.99"))

        outcome, _ = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "app_paid"
        assert driver.taps == 1

    def test_an_account_prompt_after_install_is_left_for_a_person(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"), after_tap=listing("Complete account setup", "Continue"))

        outcome, _ = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_account_action_required"
        assert outcome.retryable is False
        assert driver.taps == 1

    def test_an_unrecognised_screen_taps_nothing_and_saves_diagnostics(self, tmp_path):
        driver = FakeDriver(listing("Installieren"))

        outcome, _ = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_screen_unrecognised"
        assert outcome.retryable is False
        assert driver.taps == 0
        saved = Path(outcome.diagnostics)
        assert saved.parent == tmp_path / "diagnostics"
        assert (saved / "page_source.xml").read_text() == listing("Installieren")
        assert (saved / "screenshot.png").read_bytes() == b"\x89PNG synthetic"

    def test_an_unavailable_app_is_not_retried(self, tmp_path):
        outcome, _ = install(FakeAdb(), FakeDriver(listing("Item not found.")), tmp_path)

        assert outcome.blocker_code == "app_unavailable"
        assert outcome.retryable is False

    def test_a_slow_download_is_retryable(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"))

        outcome, _ = install(FakeAdb(), driver, tmp_path, install_timeout=0.05)

        assert outcome.blocker_code == "app_install_timeout"
        assert outcome.retryable is True
        assert driver.quit_called

    def test_another_installer_is_reported_but_still_installed(self, tmp_path):
        outcome, _ = install(
            FakeAdb(installed_after=1, installer="com.example.sideloader"), FakeDriver(listing("Install")), tmp_path
        )

        assert outcome.status == "installed"
        assert "com.example.sideloader" in outcome.detail

    def test_an_invalid_package_name_never_reaches_the_device(self, tmp_path):
        adb = FakeAdb()

        outcome = install_from_play_store(adb, lambda: None, "com.example.app; reboot", tmp_path)

        assert outcome.blocker_code == "app_install_failed"
        assert adb.commands == []

    def test_a_failed_session_is_retryable(self, tmp_path):
        def broken():
            raise RuntimeError("failed to start Appium session")

        outcome = install_from_play_store(FakeAdb(), broken, PACKAGE, tmp_path, page_timeout=0, poll=0)

        assert outcome.blocker_code == "app_install_failed"
        assert outcome.retryable is True
