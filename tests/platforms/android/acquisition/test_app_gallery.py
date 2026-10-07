from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.platforms.android.acquisition import app_gallery
from mobile_playbook.platforms.android.acquisition.app_gallery import (
    classify_listing,
    install_from_app_gallery,
)
from mobile_playbook.platforms.android.acquisition.play_store import screen_labels

PACKAGE = "com.example.app"
DETAIL_BUTTON_ID = "com.huawei.appmarket:id/hwdetail_download_button"


def listing(*labels: str) -> str:
    nodes = [f'<android.widget.TextView text="{label}" content-desc="" clickable="false" />' for label in labels]
    return '<?xml version="1.0" encoding="UTF-8"?><hierarchy>' + "".join(nodes) + "</hierarchy>"


class FakeAdb:
    def __init__(self, installed: bool = False, installed_after: int | None = None, installer: str = "com.huawei.appmarket"):
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
    def __init__(self, driver: "FakeDriver", element_id: str, resource_id: str = ""):
        self.driver = driver
        self.id = element_id
        self.resource_id = resource_id

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.resource_id if name == "resource-id" else None

    def click(self):
        self.driver.tapped.append(self.id)
        self.driver.page_source = self.driver.after_tap


class FakeDriver:
    def __init__(self, page_source: str, buttons: list[tuple[str, str]] | None = None, after_tap: str | None = None):
        self.current_package = app_gallery.APP_GALLERY_PACKAGE
        self.page_source = page_source
        self.after_tap = after_tap if after_tap is not None else listing("Installing")
        self.elements = [FakeElement(self, element_id, resource_id) for element_id, resource_id in (buttons or [])]
        self.tapped: list[str] = []
        self.quit_called = False

    def find_elements(self, by, selector):
        return list(self.elements) if "install" in selector else []

    def get_screenshot_as_png(self):
        return b"\x89PNG synthetic"

    def quit(self):
        self.quit_called = True


def install(adb: FakeAdb, driver: FakeDriver | None, tmp_path: Path, **kwargs):
    def make_driver():
        assert driver is not None, "no driver should be needed"
        return driver

    options = {"page_timeout": 0.05, "install_timeout": 0.2, "poll": 0}
    options.update(kwargs)
    return install_from_app_gallery(adb, make_driver, PACKAGE, tmp_path / "diagnostics", **options)


class TestClassifyingAListing:
    def test_a_free_app_is_installable(self):
        assert classify_listing(screen_labels(listing("Example App", "INSTALL"))) == "installable"

    @pytest.mark.parametrize("price", ["S$1.48", "$0.99", "Buy", "Purchase"])
    def test_a_priced_button_is_a_paid_app(self, price):
        assert classify_listing(screen_labels(listing("Example App", "Install", price))) == "paid"

    @pytest.mark.parametrize("label", ["OPEN", "Update"])
    def test_an_installed_app_offers_open_or_update(self, label):
        assert classify_listing(screen_labels(listing("Example App", label))) == "installed"

    def test_install_and_open_together_are_ambiguous(self):
        assert classify_listing(screen_labels(listing("Example App", "Open", "You might also like", "Install"))) == "unrecognised"

    @pytest.mark.parametrize("label", ["This app is not available in your country or region.", "App not found", "Reserve"])
    def test_an_unavailable_listing_is_recognised(self, label):
        assert classify_listing(screen_labels(listing("Example App", label))) == "unavailable"

    def test_reserved_inside_other_text_is_not_unavailability(self):
        assert classify_listing(screen_labels(listing("All rights reserved"))) == "unrecognised"

    def test_a_signed_out_store_needs_a_person(self):
        assert classify_listing(screen_labels(listing("Sign in with HUAWEI ID"))) == "sign_in_required"

    def test_an_identity_check_needs_a_person(self):
        assert classify_listing(screen_labels(listing("Verify your HUAWEI ID"))) == "account_action_required"


class TestInstalling:
    def test_a_free_app_is_installed_from_the_app_gallery_listing(self, tmp_path):
        adb = FakeAdb(installed_after=1)
        driver = FakeDriver(listing("Example App", "Install"), buttons=[("one", DETAIL_BUTTON_ID)])

        outcome = install(adb, driver, tmp_path)

        assert outcome.status == "installed"
        assert outcome.installer == "com.huawei.appmarket"
        assert outcome.detail is None
        assert driver.tapped == ["one"]
        assert driver.quit_called
        opened = next(command for command in adb.commands if command[:3] == ["shell", "am", "start"])
        assert opened[opened.index("-p") + 1] == "com.huawei.appmarket"
        assert f"'appmarket://details?id={PACKAGE}'" in opened

    def test_the_detail_bar_button_is_chosen_over_recommended_apps(self, tmp_path):
        driver = FakeDriver(
            listing("Example App", "Install"),
            buttons=[("recommended", "com.huawei.appmarket:id/card_download_button"), ("own", DETAIL_BUTTON_ID)],
        )

        install(FakeAdb(installed_after=1), driver, tmp_path)

        assert driver.tapped == ["own"]

    def test_a_lone_install_button_without_a_known_id_is_tapped(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"), buttons=[("only", "")])

        outcome = install(FakeAdb(installed_after=1), driver, tmp_path)

        assert outcome.status == "installed"
        assert driver.tapped == ["only"]

    def test_several_install_buttons_tap_nothing_and_save_diagnostics(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"), buttons=[("a", ""), ("b", "")])

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_screen_unrecognised"
        assert driver.tapped == []
        assert (Path(outcome.diagnostics) / "page_source.xml").exists()

    def test_a_paid_app_is_never_tapped(self, tmp_path):
        driver = FakeDriver(listing("Example App", "S$1.48"), buttons=[("one", DETAIL_BUTTON_ID)])

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "app_paid"
        assert outcome.retryable is False
        assert driver.tapped == []

    def test_a_sign_in_prompt_after_install_is_left_for_a_person(self, tmp_path):
        driver = FakeDriver(
            listing("Example App", "Install"), buttons=[("one", DETAIL_BUTTON_ID)], after_tap=listing("Sign in with HUAWEI ID")
        )

        outcome = install(FakeAdb(), driver, tmp_path)

        assert outcome.blocker_code == "store_account_action_required"
        assert "HUAWEI ID" in outcome.detail

    def test_a_slow_download_is_retryable(self, tmp_path):
        driver = FakeDriver(listing("Example App", "Install"), buttons=[("one", DETAIL_BUTTON_ID)])

        outcome = install(FakeAdb(), driver, tmp_path, install_timeout=0.05)

        assert outcome.blocker_code == "app_install_timeout"
        assert outcome.retryable is True

    def test_an_installed_app_needs_no_store_session(self, tmp_path):
        outcome = install(FakeAdb(installed=True), None, tmp_path)

        assert outcome.status == "already_installed"

    def test_an_invalid_package_name_never_reaches_the_device(self, tmp_path):
        adb = FakeAdb()

        outcome = install_from_app_gallery(adb, lambda: None, "com.example.app; reboot", tmp_path)

        assert outcome.blocker_code == "app_install_failed"
        assert adb.commands == []
