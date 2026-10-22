"""
Checks and runs the configured Appium steps that take an app through a sensitive flow, such as signing in, typing
synthetic canary values into its fields.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    logger.debug("android scripted steps: AppiumBy unavailable; scripted steps cannot find elements", exc_info=True)
    AppiumBy = None

STEP_KINDS = ("tap", "type", "wait", "back")
LOCATOR_KEYS = ("id", "accessibility_id", "text")
PLACEHOLDER_RE = re.compile(r"\{canary\.([A-Za-z0-9_]+)\}")
PLACEHOLDER_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
POLL_SECONDS = 0.5
# Synthetic values in the formats the playbook control redacts: the card number is a published test number and the
# NRIC fails its checksum, so neither belongs to anyone; an app that checks the checksum needs its own value.
DEFAULT_CANARIES = {
    "email": "canary.logtest@example.com",
    "password": "Canary-Pw-7Q4z",
    "card": "4111 1111 1111 1111",
    "nric": "S0000000Z",
}


class StepError(RuntimeError):
    """A scripted step could not be carried out on the device."""


@dataclass
class StepsOutcome:
    """How far the scripted steps got, and why the first failed step failed."""

    configured: int
    completed: int = 0
    failure: dict[str, Any] | None = None
    canaries_typed: list[str] = field(default_factory=list)

    # Describe the outcome for result metadata.
    def to_dict(self) -> dict[str, Any]:
        return {"configured": self.configured, "completed": self.completed, "failure": self.failure, "canaries_typed": self.canaries_typed}


# Return the problem with a canaries setting, which must map names to non-empty text.
def canary_errors(canaries: Any, label: str) -> list[str]:
    if canaries is None:
        return []
    if not isinstance(canaries, dict) or any(not isinstance(name, str) or not PLACEHOLDER_NAME_RE.fullmatch(name) or not isinstance(value, str) or not value.strip() for name, value in canaries.items()):
        return [f"{label}.canaries must map names of letters, digits and underscores to non-empty text"]
    return []


# Return the canary values steps type: the defaults, overridden by the shared and then the app's own canaries.
def merged_canaries(*layers: Any) -> dict[str, str]:
    canaries = dict(DEFAULT_CANARIES)
    for layer in layers:
        if isinstance(layer, dict):
            canaries.update(layer)
    return canaries


# Return the problems with a list of scripted steps, whose placeholders may name only the given canaries.
def step_errors(steps: Any, canary_names: set[str], label: str) -> list[str]:
    if steps is None:
        return []
    if not isinstance(steps, list):
        return [f"{label}.steps must be a list"]
    errors = []
    for index, step in enumerate(steps):
        errors.extend(_one_step_errors(step, canary_names, f"{label}.steps[{index}]"))
    return errors


# Return the problems with one step: one of tap, type, wait or back, with a single locator for tap and type.
def _one_step_errors(step: Any, canary_names: set[str], label: str) -> list[str]:
    if step == "back":
        return []
    if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in STEP_KINDS:
        return [f"{label} must be a mapping with one of {', '.join(STEP_KINDS)}"]
    kind, value = next(iter(step.items()))
    if kind == "back":
        return [] if value in (None, True) else [f"{label}.back takes no value"]
    if kind == "wait":
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else [f"{label}.wait must be a number of seconds"]
    if not isinstance(value, dict):
        return [f"{label}.{kind} must be a mapping with one of {', '.join(LOCATOR_KEYS)}"]
    errors = []
    locators = [key for key in LOCATOR_KEYS if key in value]
    if len(locators) != 1 or not isinstance(value[locators[0]], str) or not value[locators[0]]:
        errors.append(f"{label}.{kind} must name the element with exactly one of {', '.join(LOCATOR_KEYS)}")
    allowed = {*LOCATOR_KEYS, "value"} if kind == "type" else set(LOCATOR_KEYS)
    errors.extend(f"{label}.{kind}.{key} is not a known setting" for key in value if key not in allowed)
    if kind == "type":
        text = value.get("value")
        if not isinstance(text, str) or not text:
            errors.append(f"{label}.type.value must be the text to type, such as {{canary.email}}")
        else:
            errors.extend(f"{label}.type.value names unknown canary {name!r}" for name in PLACEHOLDER_RE.findall(text) if name not in canary_names)
    return errors


# Replace each {canary.<name>} placeholder in text with that canary's value.
def fill_placeholders(text: str, canaries: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda match: canaries[match.group(1)], text)


# Run the steps in order, stopping at the first that fails, and return how far they got.
def run_steps(driver, steps: list, canaries: dict[str, str], timeout: float) -> StepsOutcome:
    outcome = StepsOutcome(configured=len(steps))
    for index, step in enumerate(steps):
        try:
            typed = _run_step(driver, step, canaries, timeout)
        except Exception as exc:
            logger.debug("android scripted steps: step %s %s failed: %s", index, step, exc, exc_info=True)
            outcome.failure = {"index": index, "step": step, "error": str(exc)[:500]}
            return outcome
        outcome.canaries_typed.extend(name for name in typed if name not in outcome.canaries_typed)
        outcome.completed += 1
    return outcome


# Carry out one step and return the names of the canaries it typed.
def _run_step(driver, step: Any, canaries: dict[str, str], timeout: float) -> list[str]:
    kind, value = ("back", None) if step == "back" else next(iter(step.items()))
    if kind == "back":
        driver.back()
        return []
    if kind == "wait":
        time.sleep(float(value))
        return []
    element = find_element(driver, value, timeout)
    if kind == "tap":
        element.click()
        return []
    element.click()
    element.clear()
    element.send_keys(fill_placeholders(value["value"], canaries))
    return PLACEHOLDER_RE.findall(value["value"])


# Wait up to timeout seconds for the element a locator names and return it.
def find_element(driver, locator: dict[str, str], timeout: float):
    if AppiumBy is None:
        raise StepError("the Appium Python client is not installed")
    if "id" in locator:
        by, target = AppiumBy.ID, locator["id"]
    elif "accessibility_id" in locator:
        by, target = AppiumBy.ACCESSIBILITY_ID, locator["accessibility_id"]
    else:
        quoted = locator["text"].replace("\\", "\\\\").replace('"', '\\"')
        by, target = AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{quoted}")'
    deadline = time.monotonic() + timeout
    while True:
        found = driver.find_elements(by, target)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            key = next(key for key in LOCATOR_KEYS if key in locator)
            raise StepError(f"no element matched {key} {locator[key]!r} within {timeout:g}s")
        time.sleep(POLL_SECONDS)
