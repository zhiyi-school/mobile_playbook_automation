"""
Finds and taps elements on Android Settings pages through Appium and adb, for the risks that change a setting the way
the playbook does it by hand.
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

UIAUTOMATOR = "-android uiautomator"
POLL_SECONDS = 0.5


class SettingsUiError(Exception):
    """An element on a Settings page could not be tapped."""


# Tap an element's centre through adb, from its reported bounds, bypassing UiAutomator2's per-tap wait for the window
# to be idle, which never ends on HarmonyOS's Settings.
def tap_element(adb, element) -> None:
    bounds = element.get_attribute("bounds")
    numbers = [int(value) for value in re.findall(r"-?\d+", bounds or "")]
    if len(numbers) != 4:
        raise SettingsUiError(f"could not read the element bounds to tap: {bounds!r}")
    x1, y1, x2, y2 = numbers
    code, out, err = adb.run(["shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2)])
    if code != 0:
        raise SettingsUiError(f"adb input tap failed: {err or out}")


# Find the first element whose whole text matches one of the given texts, ignoring case, scrolling the page if allowed.
def find_text(driver, texts: tuple[str, ...], timeout: float, scroll: bool = True):
    matcher = text_matcher(texts)
    selectors = [matcher]
    if scroll:
        selectors.insert(0, f"new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView({matcher})")
    deadline = time.monotonic() + timeout
    while True:
        for selector in selectors:
            try:
                found = driver.find_elements(UIAUTOMATOR, selector)
            except Exception as exc:
                logger.debug("android settings ui: selector %s failed: %s", selector, exc)
                found = []
            if found:
                return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Build a UiSelector matching any of the texts exactly, ignoring case, as a Java expression for UiAutomator.
# Appium's selector parser keeps backslashes, so other characters become "." rather than being escaped.
def text_matcher(texts: tuple[str, ...]) -> str:
    pattern = "(?i)(" + "|".join("".join(char if char.isalnum() or char == " " else "." for char in text) for text in texts) + ")"
    return f'new UiSelector().textMatches("{pattern}")'
