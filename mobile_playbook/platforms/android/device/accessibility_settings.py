"""
Turns on the accessibility attacker's service through Settings with Appium, as playbook feature-05 does, falling back
to its details page and then to secure settings when the Settings pages cannot be driven.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mobile_playbook.platforms.android.device.accessibility_service import (
    AccessibilityAttackerError,
    AccessibilityServiceControl,
    AccessibilitySettings,
)

logger = logging.getLogger(__name__)

ACCESSIBILITY_SETTINGS_ACTION = "android.settings.ACCESSIBILITY_SETTINGS"
SETTINGS_PACKAGE = "com.android.settings"
ACCESSIBILITY_DETAILS_ACTION = "android.settings.ACCESSIBILITY_DETAILS_SETTINGS"
COMPONENT_NAME_EXTRA = "android.intent.extra.COMPONENT_NAME"
UIAUTOMATOR = "-android uiautomator"
SWITCH_CLASS = "android.widget.Switch"
CHECKBOX_CLASS = "android.widget.CheckBox"
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class SettingsLabels:
    """Text that Settings shows for the service, the categories that list downloaded services and the consent button."""

    service: str
    # AOSP lists downloaded services on the main page under "Downloaded apps"; One UI and HarmonyOS list them on a
    # page of their own.
    categories: tuple[str, ...] = ("Installed apps", "Installed services", "Downloaded apps", "Downloaded services")
    allow: tuple[str, ...] = ("Allow", "Grant access")


@dataclass
class EnableOutcome:
    """How the service was turned on, why any Settings path failed, and the screenshots taken on the way."""

    method: str | None = None
    already_enabled: bool = False
    ui_failures: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    evidence_errors: list[str] = field(default_factory=list)

    # Return the outcome for the risk's report metadata; evidence paths are listed with the risk's other evidence.
    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "already_enabled": self.already_enabled,
            "ui_failures": list(self.ui_failures),
            "evidence_errors": list(self.evidence_errors),
        }


# Turn the attacker's service on through Settings, then its details page, then secure settings, confirming each.
def enable_service(driver, attacker: AccessibilityServiceControl, before: AccessibilitySettings, evidence_dir: Path,
                   timeout: float = 20, labels: SettingsLabels | None = None) -> EnableOutcome:
    labels = labels or SettingsLabels(service=attacker.app.label)
    outcome = EnableOutcome()
    adb = attacker.adb
    open_accessibility_settings(adb)
    entry = find_service_entry(driver, labels, timeout)
    _screenshot(driver, evidence_dir / "accessibility_settings.png", "accessibility_settings", "Accessibility settings", outcome)
    if entry is not None:
        _tap(adb, entry)
        _screenshot(driver, evidence_dir / "accessibility_service_page.png", "accessibility_service_page", "Attacker service page", outcome)
        failure = _turn_on(driver, attacker, labels, timeout, evidence_dir, outcome)
        if failure is None:
            outcome.method = "settings_ui"
            return outcome
        outcome.ui_failures.append(f"settings_ui: {failure}")
    else:
        outcome.ui_failures.append(f"settings_ui: {labels.service!r} was not found in Accessibility settings")

    code, out, err = adb.run(["shell", "am", "start", "-a", ACCESSIBILITY_DETAILS_ACTION, "--es", COMPONENT_NAME_EXTRA, attacker.app.component])
    # An intent nothing handles is reported only in the output; HarmonyOS refuses the page to the shell with a non-zero
    # exit and a SecurityException.
    if code != 0 or "Error" in (out or "") or "Exception" in (out or "") + (err or ""):
        outcome.ui_failures.append(f"details_page: {_first_line(err or out)}")
    else:
        failure = _turn_on(driver, attacker, labels, timeout, evidence_dir, outcome)
        if failure is None:
            outcome.method = "details_page"
            return outcome
        outcome.ui_failures.append(f"details_page: {failure}")

    logger.debug("android accessibility settings: falling back to secure settings after %s", outcome.ui_failures)
    attacker.enable_via_secure_settings(before)
    if not attacker.is_enabled():
        raise AccessibilityAttackerError(f"the attacker's service is not enabled after the secure settings fallback ({'; '.join(outcome.ui_failures)})")
    outcome.method = "secure_settings_fallback"
    return outcome


# Open Accessibility settings on its main list. Settings is stopped first because HarmonyOS delivers the intent to a
# running Settings task, which stays on whichever page it was left on, such as another service's page.
def open_accessibility_settings(adb) -> None:
    _shell(adb, ["am", "force-stop", SETTINGS_PACKAGE])
    _shell(adb, ["am", "start", "-a", ACCESSIBILITY_SETTINGS_ACTION])


# Find the service in Accessibility settings, opening a category of downloaded services first when needed, and open it.
def open_service_page(driver, labels: SettingsLabels, timeout: float) -> bool:
    entry = find_service_entry(driver, labels, timeout)
    if entry is None:
        return False
    entry.click()
    return True


# Return the service's entry in Accessibility settings, opening a category of downloaded services first when needed.
def find_service_entry(driver, labels: SettingsLabels, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        entry = _find_text(driver, (labels.service,), 0)
        if entry is not None:
            return entry
        category = _find_text(driver, labels.categories, 0)
        if category is not None:
            category.click()
            return _find_text(driver, (labels.service,), max(0.0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Show the Accessibility list with the service in it and open the service's page, as playbook feature-05 step 02 does;
# screenshot is called once the list is showing. Returns the step's outcome and detail.
def show_service_listed(driver, adb, labels: SettingsLabels, timeout: float, screenshot: Callable[[], None]) -> dict:
    try:
        open_accessibility_settings(adb)
        entry = find_service_entry(driver, labels, timeout)
    except Exception as exc:
        logger.debug("android accessibility settings: showing the list failed: %s", exc, exc_info=True)
        return {"outcome": "failed", "detail": str(exc)}
    screenshot()
    if entry is None:
        return {"outcome": "failed", "detail": f"{labels.service!r} was not found in Accessibility settings."}
    entry.click()
    return {"outcome": "completed", "detail": f"{labels.service!r} is listed in Accessibility settings."}


# Confirm the open service page's switch is on, as playbook feature-05 step 03 shows, without flipping it again;
# screenshot is called once the switch has been read. Returns the step's outcome and detail.
def confirm_switch_on(driver, labels: SettingsLabels, timeout: float, screenshot: Callable[[], None]) -> dict:
    try:
        switch = service_switch(driver, timeout)
        checked = None if switch is None else str(switch.get_attribute("checked")).lower()
    except Exception as exc:
        logger.debug("android accessibility settings: reading the switch failed: %s", exc, exc_info=True)
        return {"outcome": "failed", "detail": str(exc)}
    screenshot()
    if checked == "true":
        return {"outcome": "completed", "detail": f"The {labels.service!r} switch is on."}
    return {"outcome": "failed", "detail": "No service switch was found." if switch is None else f"The {labels.service!r} switch reads {checked!r}."}


# Return the service page's main switch, the topmost on the page, or None when none appears in time.
def service_switch(driver, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        found = driver.find_elements("class name", SWITCH_CLASS)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Flip the service switch and accept the consent screen, returning None once secure settings list the service, or why not.
def _turn_on(driver, attacker: AccessibilityServiceControl, labels: SettingsLabels, timeout: float, evidence_dir: Path, outcome: EnableOutcome) -> str | None:
    adb = attacker.adb
    switch = service_switch(driver, timeout)
    if switch is None:
        return "no switch was found on the service page"
    if str(switch.get_attribute("enabled")).lower() == "false":
        return "the service switch is disabled, as a restricted setting is"
    checked = str(switch.get_attribute("checked")).lower()
    if checked == "true" and attacker.is_enabled():
        outcome.already_enabled = True
        return None
    if checked not in ("true", "false"):
        return "the service switch has no readable checked state"
    _tap(adb, switch)
    consent_seen = False
    deadline = time.monotonic() + timeout
    while True:
        if attacker.is_enabled():
            return None
        boxes = [box for box in driver.find_elements("class name", CHECKBOX_CLASS) if str(box.get_attribute("checked")).lower() == "false"]
        allow = _find_text(driver, labels.allow, 0, scroll=False)
        if (boxes or allow is not None) and not consent_seen:
            consent_seen = True
            _screenshot(driver, evidence_dir / "accessibility_consent.png", "accessibility_consent", "Accessibility consent prompt", outcome)
        # Tap through adb, not Appium: HarmonyOS's Settings window never reports idle, so UiAutomator2 blocks each tap
        # for its whole idle timeout. The checkbox's countdown keeps Grant access disabled for several seconds, so the
        # unticked box is tapped and Grant is tapped once it is enabled, each pass, until the service is listed.
        for box in boxes:
            if str(box.get_attribute("enabled")).lower() != "false":
                _tap(adb, box)
        if allow is not None and str(allow.get_attribute("enabled")).lower() != "false":
            _tap(adb, allow)
        if time.monotonic() >= deadline:
            return f"the service was not enabled within {timeout:g}s of flipping its switch" + ("" if consent_seen else "; no consent prompt was recognised")
        time.sleep(POLL_SECONDS)


# Tap an element's centre through adb, from its reported bounds, bypassing UiAutomator2's per-tap wait for the window
# to be idle, which never ends on HarmonyOS's Settings.
def _tap(adb, element) -> None:
    bounds = element.get_attribute("bounds")
    numbers = [int(value) for value in re.findall(r"-?\d+", bounds or "")]
    if len(numbers) != 4:
        raise AccessibilityAttackerError(f"could not read the element bounds to tap: {bounds!r}")
    x1, y1, x2, y2 = numbers
    _shell(adb, ["input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2)])


# Find the first element whose whole text matches one of the given texts, ignoring case, scrolling the page if allowed.
def _find_text(driver, texts: tuple[str, ...], timeout: float, scroll: bool = True):
    matcher = _text_matcher(texts)
    selectors = [matcher]
    if scroll:
        selectors.insert(0, f"new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView({matcher})")
    deadline = time.monotonic() + timeout
    while True:
        for selector in selectors:
            try:
                found = driver.find_elements(UIAUTOMATOR, selector)
            except Exception as exc:
                logger.debug("android accessibility settings: selector %s failed: %s", selector, exc)
                found = []
            if found:
                return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Build a UiSelector matching any of the texts exactly, ignoring case, as a Java expression for UiAutomator.
# Appium's selector parser keeps backslashes, so other characters become "." rather than being escaped.
def _text_matcher(texts: tuple[str, ...]) -> str:
    pattern = "(?i)(" + "|".join("".join(char if char.isalnum() or char == " " else "." for char in text) for text in texts) + ")"
    return f'new UiSelector().textMatches("{pattern}")'


# Return the first line of a command's output, which for a failed am start names the reason.
def _first_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[0] if lines else "no output"


# Run a device shell command and raise when it fails.
def _shell(adb, args: list[str]) -> str:
    code, out, err = adb.run(["shell", *args])
    if code != 0:
        raise AccessibilityAttackerError(f"adb {' '.join(args)} failed: {err or out}")
    return out or ""


# Add a screenshot to the evidence when Appium saves it, otherwise record why it is missing.
def _screenshot(driver, path: Path, kind: str, label: str, outcome: EnableOutcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        saved = driver.get_screenshot_as_file(str(path))
    except Exception as exc:
        logger.debug("android accessibility settings: screenshot %s failed: %s", path.name, exc, exc_info=True)
        saved, reason = False, str(exc)
    else:
        reason = "Appium did not save the file"
    if saved:
        outcome.evidence.append({"kind": kind, "path": str(path), "label": label})
    else:
        outcome.evidence_errors.append(f"{path.name}: {reason}")
