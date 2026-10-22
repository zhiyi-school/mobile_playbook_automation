from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device import scripted_steps
from mobile_playbook.platforms.android.device.scripted_steps import (
    DEFAULT_CANARIES,
    canary_errors,
    fill_placeholders,
    locator_errors,
    merged_canaries,
    run_steps,
    step_errors,
)

CANARIES = {"email": "canary.logtest@example.com", "password": "Canary-Pw-7Q4z"}


class FakeElement:
    def __init__(self, name, actions):
        self.name = name
        self.actions = actions

    def click(self):
        self.actions.append(f"click:{self.name}")

    def clear(self):
        self.actions.append(f"clear:{self.name}")

    def send_keys(self, text):
        self.actions.append(f"keys:{self.name}:{text}")


class FakeDriver:
    def __init__(self, elements):
        self.actions: list[str] = []
        self.lookups: list[tuple[str, str]] = []
        self.elements = {key: FakeElement(name, self.actions) for key, name in elements.items()}

    def find_elements(self, by, target):
        self.lookups.append((by, target))
        element = self.elements.get((by, target))
        return [element] if element else []

    def back(self):
        self.actions.append("back")

    # W3C actions, as ActionChains sends them; only the key presses are recorded.
    def execute(self, command, params=None):
        assert command == "actions"
        pressed = "".join(action["value"] for device in params["actions"] if device["type"] == "key" for action in device["actions"] if action["type"] == "keyDown")
        self.actions.append(f"pressed:{pressed}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(scripted_steps.time, "sleep", slept.append)
    return slept


def test_valid_steps_have_no_errors():
    steps = [
        {"tap": {"id": "com.example.app:id/login"}},
        {"type": {"accessibility_id": "Email", "value": "{canary.email}"}},
        {"type": {"text": "Password", "value": "{canary.password}"}},
        {"wait": 2.5},
        {"back": None},
        "back",
    ]

    assert step_errors(steps, set(CANARIES), "app") == []
    assert step_errors(None, set(), "app") == []


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ({"tap": {"id": "x"}}, "app.steps must be a list"),
        ([{"swipe": {"id": "x"}}], "app.steps[0] must be a mapping with one of tap, type, wait, back"),
        ([{"tap": {"id": "x"}, "wait": 1}], "app.steps[0] must be a mapping with one of tap, type, wait, back"),
        (["forward"], "app.steps[0] must be a mapping with one of tap, type, wait, back"),
        ([{"tap": "Login"}], "app.steps[0].tap must be a mapping with one of id, accessibility_id, text"),
        ([{"tap": {"id": "x", "text": "Login"}}], "app.steps[0].tap must name the element with exactly one of id, accessibility_id, text"),
        ([{"tap": {"xpath": "//x"}}], "app.steps[0].tap must name the element with exactly one of id, accessibility_id, text"),
        ([{"tap": {"id": "x", "value": "y"}}], "app.steps[0].tap.value is not a known setting"),
        ([{"type": {"id": "x"}}], "app.steps[0].type.value must be the text to type, such as {canary.email}"),
        ([{"type": {"id": "x", "value": "{canary.otp}"}}], "app.steps[0].type.value names unknown canary 'otp'"),
        ([{"wait": "3"}], "app.steps[0].wait must be a number of seconds"),
        ([{"wait": -1}], "app.steps[0].wait must be a number of seconds"),
        ([{"wait": True}], "app.steps[0].wait must be a number of seconds"),
        ([{"back": "now"}], "app.steps[0].back takes no value"),
    ],
)
def test_invalid_steps_are_reported(steps, error):
    assert error in step_errors(steps, set(CANARIES), "app")


@pytest.mark.parametrize("canaries", [["email"], {"email": ""}, {"email": 5}, {"e-mail": "x@example.com"}])
def test_invalid_canaries_are_reported(canaries):
    assert canary_errors(canaries, "runtime_logs") == ["runtime_logs.canaries must map names of letters, digits and underscores to non-empty text"]


def test_canaries_are_layered_over_the_defaults():
    merged = merged_canaries({"email": "shared@example.com"}, {"otp": "000000"}, None)

    assert merged == {**DEFAULT_CANARIES, "email": "shared@example.com", "otp": "000000"}
    assert canary_errors(None, "x") == []


def test_placeholders_are_filled_from_the_canaries():
    assert fill_placeholders("{canary.email} / {canary.password}", CANARIES) == "canary.logtest@example.com / Canary-Pw-7Q4z"


def test_steps_tap_type_wait_and_go_back_in_order(no_sleep):
    driver = FakeDriver({("id", "com.example.app:id/login"): "login", ("accessibility id", "Email"): "email", ("-android uiautomator", 'new UiSelector().text("Pass \\"word\\"")'): "password"})
    steps = [
        {"tap": {"id": "com.example.app:id/login"}},
        {"type": {"accessibility_id": "Email", "value": "{canary.email}"}},
        {"type": {"text": 'Pass "word"', "value": "{canary.password}"}},
        {"wait": 3},
        "back",
    ]

    outcome = run_steps(driver, steps, CANARIES, timeout=1)

    assert outcome.to_dict() == {"configured": 5, "completed": 5, "failure": None, "canaries_typed": ["email", "password"]}
    assert driver.actions == [
        "click:login",
        "click:email",
        "clear:email",
        "keys:email:canary.logtest@example.com",
        "click:password",
        "clear:password",
        "keys:password:Canary-Pw-7Q4z",
        "back",
    ]
    assert 3.0 in no_sleep


def test_keyboard_steps_press_each_key_into_the_tapped_field_instead_of_setting_its_text(no_sleep):
    driver = FakeDriver({("id", "com.example.app:id/email"): "email"})

    outcome = run_steps(driver, [{"type": {"id": "com.example.app:id/email", "value": "{canary.email}"}}], CANARIES, timeout=1, keyboard=True)

    assert outcome.canaries_typed == ["email"]
    assert driver.actions == ["click:email", "clear:email", "pressed:canary.logtest@example.com"]


def test_a_missing_element_stops_the_steps_at_that_step(monkeypatch):
    clock = iter([0.0, 0.0, 0.6, 1.2])
    monkeypatch.setattr(scripted_steps.time, "monotonic", lambda: next(clock))
    driver = FakeDriver({("id", "com.example.app:id/login"): "login"})
    steps = [{"tap": {"id": "com.example.app:id/login"}}, {"tap": {"text": "Sign in"}}, {"tap": {"id": "com.example.app:id/login"}}]

    outcome = run_steps(driver, steps, CANARIES, timeout=1)

    assert outcome.completed == 1
    assert outcome.failure == {"index": 1, "step": {"tap": {"text": "Sign in"}}, "error": "no element matched text 'Sign in' within 1s"}
    assert driver.actions == ["click:login"]


def test_an_element_that_appears_late_is_waited_for():
    driver = FakeDriver({})
    lookups = []

    def find_elements(by, target):
        lookups.append(target)
        return [FakeElement("late", driver.actions)] if len(lookups) == 3 else []

    driver.find_elements = find_elements

    outcome = run_steps(driver, [{"tap": {"id": "late"}}], CANARIES, timeout=10)

    assert outcome.completed == 1
    assert driver.actions == ["click:late"]


@pytest.mark.parametrize("value,expected", [
    (None, []),
    ({"id": "com.example.app:id/username"}, []),
    ({"accessibility_id": "Username"}, []),
    ("x", ["logged_out must be a mapping with one of id, accessibility_id, text"]),
    ({"id": "a", "text": "b"}, ["logged_out must name the element with exactly one of id, accessibility_id, text"]),
    ({}, ["logged_out must name the element with exactly one of id, accessibility_id, text"]),
    ({"id": "a", "xpath": "//x"}, ["logged_out.xpath is not a known setting"]),
])
def test_locator_errors(value, expected):
    assert locator_errors(value, "logged_out") == expected
