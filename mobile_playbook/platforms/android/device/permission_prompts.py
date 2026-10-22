"""Denies system runtime-permission dialogs that cover an app, such as first-launch prompts the adb grant cannot reach."""

from __future__ import annotations

import logging
import time
from typing import Callable

from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
MAX_PROMPTS = 3
LEAVE_SECONDS = 3.0
DENY_BUTTON_IDS = tuple(
    f"{owner}:id/{name}"
    for owner in PERMISSION_CONTROLLER_PACKAGES
    for name in ("permission_deny_button", "permission_deny_and_dont_ask_again_button")
) + ("android:id/button2",)
DENY_TEXT_SELECTOR = 'new UiSelector().clickable(true).textMatches("(?i)^(deny|don.t allow)$")'
TITLE_IDS = tuple(f"{owner}:id/permission_message" for owner in PERMISSION_CONTROLLER_PACKAGES) + ("android:id/alertTitle", "android:id/message")


# Deny each permission dialog in front, up to max_prompts, relaunching the app when a dialog closed it; returns one
# record per dialog with its title, whether it was denied and whether the app was relaunched.
def deny_permission_prompts(
    driver,
    package: str,
    timeout: float,
    launch_wait: float,
    *,
    in_front: tuple[str, ...] = (),
    before_deny: Callable[[int], None] | None = None,
    max_prompts: int = MAX_PROMPTS,
) -> list[dict]:
    prompts: list[dict] = []
    while len(prompts) < max_prompts and _current_package(driver) in PERMISSION_CONTROLLER_PACKAGES:
        if before_deny is not None:
            before_deny(len(prompts))
        prompt = {"title": _prompt_title(driver), "denied": False, "relaunched": False}
        prompts.append(prompt)
        button = _deny_button(driver, timeout)
        if button is None:
            logger.debug("android permission prompts: no deny button on %r", prompt["title"])
            break
        button.click()
        prompt["denied"] = True
        current = _wait_to_leave(driver, min(timeout, LEAVE_SECONDS))
        # Some OEM prompts restart the app once answered.
        if current is not None and current not in PERMISSION_CONTROLLER_PACKAGES and current not in (package, *in_front):
            driver.activate_app(package)
            time.sleep(launch_wait)
            prompt["relaunched"] = True
        logger.debug("android permission prompts: denied %r; foreground now %s relaunched=%s", prompt["title"], current, prompt["relaunched"])
    return prompts


# Return the foreground package, or None when Appium cannot report it so the caller's own check reports the error.
def _current_package(driver) -> str | None:
    try:
        return driver.current_package
    except Exception as exc:
        logger.debug("android permission prompts: foreground unreadable: %s", exc, exc_info=True)
        return None


# Return the dialog's question text, or None when no known text element is present.
def _prompt_title(driver) -> str | None:
    for element_id in TITLE_IDS:
        for element in driver.find_elements("id", element_id):
            text = (getattr(element, "text", None) or "").strip()
            if text:
                return text
    return None


# Find the deny button by its resource id, then by its label, allowing the dialog time to draw.
def _deny_button(driver, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        for by, value in (*(("id", element_id) for element_id in DENY_BUTTON_IDS), ("-android uiautomator", DENY_TEXT_SELECTOR)):
            found = driver.find_elements(by, value)
            if found:
                return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Wait for the dialog to close and return the package then in front.
def _wait_to_leave(driver, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while True:
        current = _current_package(driver)
        if current not in PERMISSION_CONTROLLER_PACKAGES or time.monotonic() >= deadline:
            return current
        time.sleep(POLL_SECONDS)
