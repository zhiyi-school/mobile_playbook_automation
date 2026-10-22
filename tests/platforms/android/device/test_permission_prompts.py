from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device import permission_prompts
from mobile_playbook.platforms.android.device.permission_prompts import DENY_BUTTON_IDS, deny_permission_prompts

PACKAGE = "com.example.app"
CONTROLLER = "com.google.android.permissioncontroller"


class FakeButton:
    def __init__(self, driver, name):
        self.driver = driver
        self.name = name

    def click(self):
        self.driver.clicked.append(self.name)
        self.driver.current_package = PACKAGE


class FakeDriver:
    def __init__(self, current, buttons=()):
        self.current_package = current
        self.buttons = set(buttons)
        self.clicked: list[str] = []

    def find_elements(self, by, value):
        return [FakeButton(self, value)] if self.current_package == CONTROLLER and value in self.buttons else []


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(permission_prompts.time, "sleep", lambda _: None)


def test_target_in_front_is_left_alone():
    driver = FakeDriver(PACKAGE, DENY_BUTTON_IDS)
    assert deny_permission_prompts(driver, PACKAGE, 0, 0) == []
    assert driver.clicked == []


def test_plain_deny_is_preferred_over_dont_ask_again():
    driver = FakeDriver(CONTROLLER, [f"{CONTROLLER}:id/permission_deny_and_dont_ask_again_button", f"{CONTROLLER}:id/permission_deny_button"])
    prompts = deny_permission_prompts(driver, PACKAGE, 0, 0)
    assert prompts == [{"title": None, "denied": True, "relaunched": False}]
    assert driver.clicked == [f"{CONTROLLER}:id/permission_deny_button"]


def test_unreadable_foreground_is_left_to_the_caller():
    class Unreadable(FakeDriver):
        @property
        def current_package(self):
            raise RuntimeError("synthetic")

        @current_package.setter
        def current_package(self, value):
            pass

    assert deny_permission_prompts(Unreadable(CONTROLLER), PACKAGE, 0, 0) == []


def test_before_deny_runs_once_per_dialog():
    driver = FakeDriver(CONTROLLER, DENY_BUTTON_IDS)
    seen: list[int] = []
    deny_permission_prompts(driver, PACKAGE, 0, 0, before_deny=seen.append)
    assert seen == [0]
