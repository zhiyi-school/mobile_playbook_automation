"""
Installs a missing Android app from its Aurora Store listing, tapping only the link prompt's Open and a free app's Install.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.acquisition.play_store import (
    PACKAGE_RE,
    PRICE_RE,
    InstallOutcome,
    installer_of,
    save_diagnostics,
    screen_labels,
)
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.permissions import is_installed

logger = logging.getLogger(__name__)

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    logger.debug("android aurora store: AppiumBy unavailable", exc_info=True)
    AppiumBy = None

AURORA_STORE_PACKAGE = "com.aurora.store"
STORE_NAME = "Aurora Store"
WORK_DIR_NAME = "aurora_store"
SYSTEM_INSTALLER_PACKAGES = {"com.android.packageinstaller", "com.google.android.packageinstaller"}
HUAWEI_SECURITY_PACKAGE = "com.huawei.appmarket"

LINK_PROMPT_PHRASE = "continue to aurora store?"
SIGN_IN_PHRASES = ("please log in to your google play account",)
INSTALL_LABEL = "install"
OPEN_LABEL = "open"
IN_PROGRESS_LABELS = {"installing", "downloading", "queued", "pending"}
IN_PROGRESS_RE = re.compile(r"\bleft$")
PAID_LABELS = {"buy", "purchase"}
UNAVAILABLE_PHRASES = ("app not found", "not available", "item not found", "not compatible")
HUAWEI_SECURITY_PHRASES = ("security notice", "view risks")

PAGE_TIMEOUT_SECONDS = 20.0
INSTALL_TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 5.0

# Listing states that end the attempt, as (blocker code, whether a retry can clear it, message).
FAILURES: dict[str, tuple[str, bool, str]] = {
    "paid": (
        "app_paid",
        False,
        "This app is paid on Google Play. Someone must verify the purchase and install it on the test device, "
        "then retry.",
    ),
    "unavailable": (
        "app_unavailable",
        False,
        "Aurora Store did not find this app for the test device: it was not found, is not released, is not "
        "available in this region, or is not compatible with the device.",
    ),
    "sign_in_required": (
        "store_account_action_required",
        False,
        "Aurora Store on the test device needs a signed-in Google account before apps can be installed.",
    ),
    "confirmation_required": (
        "store_account_action_required",
        False,
        "The device asked someone to confirm the install. Finish the install on the test device, then retry.",
    ),
    "security_check": (
        "store_account_action_required",
        False,
        "The device's security check stopped the install (Huawei's risk notice and CAPTCHA). Finish the install "
        "on the test device, or turn off the check, then retry.",
    ),
    "unrecognised": (
        "store_screen_unrecognised",
        False,
        "The Aurora Store screen was not recognised, so nothing was tapped. A screenshot and the screen layout "
        "were saved on the automation host.",
    ),
    "timeout": (
        "app_install_timeout",
        True,
        "The Aurora Store install did not finish in time.",
    ),
    "error": (
        "app_install_failed",
        True,
        "The app could not be installed from Aurora Store.",
    ),
}


# Build the failed outcome for a listing state, optionally with extra detail and saved diagnostics.
def failure(state: str, extra: str | None = None, diagnostics: Path | None = None, seconds: float = 0.0) -> InstallOutcome:
    code, retryable, message = FAILURES[state]
    return InstallOutcome(
        status="failed",
        blocker_code=code,
        retryable=retryable,
        detail=f"{message} {extra}" if extra else message,
        seconds=seconds,
        diagnostics=str(diagnostics) if diagnostics else None,
    )


# Classify the foreground screen during an Aurora install from its package and labels.
def classify_screen(foreground: str, labels: list[str]) -> str:
    lowered = [label.lower() for label in labels]
    if foreground in SYSTEM_INSTALLER_PACKAGES:
        return "confirmation_required"
    if foreground == HUAWEI_SECURITY_PACKAGE or any(phrase in label for label in lowered for phrase in HUAWEI_SECURITY_PHRASES):
        return "security_check"
    if foreground != AURORA_STORE_PACKAGE:
        return "unrecognised"
    if LINK_PROMPT_PHRASE in lowered:
        return "link_prompt"
    if any(phrase in label for label in lowered for phrase in SIGN_IN_PHRASES):
        return "sign_in_required"
    if any(label in PAID_LABELS or PRICE_RE.match(label) for label in lowered):
        return "paid"
    # Aurora keeps an Open button on the listing while it installs, so progress wins over Open.
    if any(label in IN_PROGRESS_LABELS or IN_PROGRESS_RE.search(label) for label in lowered):
        return "in_progress"
    if INSTALL_LABEL in lowered:
        return "installable"
    if OPEN_LABEL in lowered:
        return "installed"
    if any(phrase in label for label in lowered for phrase in UNAVAILABLE_PHRASES):
        return "unavailable"
    return "unrecognised"


# Open the package's listing in Aurora Store itself, never another store that handles market links.
def open_listing(adb: AdbClient, package: str) -> tuple[bool, str]:
    code, out, err = adb.run(
        [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-p",
            AURORA_STORE_PACKAGE,
            # Quoted so the device shell does not treat '?' as a glob.
            "-d",
            f"'market://details?id={package}'",
        ]
    )
    output = (out or err or "").strip()
    if code != 0 or "error" in output.lower():
        return False, output or f"am start exited {code}"
    return True, output


# Return the foreground package and page source, with empty strings while either cannot be read.
def _screen(driver: Any) -> tuple[str, str]:
    try:
        foreground = driver.current_package or ""
    except Exception:
        logger.debug("android aurora store: current package unavailable", exc_info=True)
        foreground = ""
    try:
        source = driver.page_source or ""
    except Exception:
        logger.debug("android aurora store: page source unavailable", exc_info=True)
        source = ""
    return foreground, source


# Tap the one displayed element whose text is exactly the label; only the link prompt's Open and the listing's Install are ever tapped.
def _tap_label(driver: Any, label: str) -> bool:
    try:
        elements = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().textMatches("(?i)^{label}$")')
    except Exception:
        logger.debug("android aurora store: no %s element", label, exc_info=True)
        return False
    shown = [element for element in elements if element.is_displayed()]
    logger.debug("android aurora store: %d displayed %s elements", len(shown), label)
    if len(shown) != 1:
        return False
    shown[0].click()
    return True


# Poll until the listing settles into a state other than the link prompt, tapping the prompt's Open once.
def _read_listing(driver: Any, page_timeout: float, poll: float) -> tuple[str, str]:
    deadline = time.monotonic() + page_timeout
    prompt_answered = False
    source = ""
    while True:
        foreground, source = _screen(driver)
        state = classify_screen(foreground, screen_labels(source)) if foreground else "unrecognised"
        logger.debug("android aurora store: listing state %s (foreground %s)", state, foreground)
        if state == "link_prompt" and not prompt_answered:
            prompt_answered = _tap_label(driver, OPEN_LABEL)
        elif state not in ("link_prompt", "unrecognised"):
            return state, source
        if time.monotonic() >= deadline:
            return "unrecognised", source
        time.sleep(poll)


# Install a missing app through Aurora Store, refusing paid apps, device prompts and any screen it does not recognise.
def install_from_aurora_store(
    adb: AdbClient,
    make_driver: Callable[[], Any],
    package: str,
    diagnostics_root: Path,
    *,
    page_timeout: float = PAGE_TIMEOUT_SECONDS,
    install_timeout: float = INSTALL_TIMEOUT_SECONDS,
    poll: float = POLL_SECONDS,
) -> InstallOutcome:
    if not PACKAGE_RE.match(package or ""):
        return failure("error", f"{package!r} is not a valid package name.")
    if is_installed(adb, package):
        logger.debug("android aurora store: %s already installed", package)
        return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
    if AppiumBy is None:
        return failure("error", "The Appium Python client is not installed on the automation host.")

    started = time.monotonic()
    try:
        driver = make_driver()
    except Exception as exc:
        logger.debug("android aurora store: driver creation failed", exc_info=True)
        return failure("error", f"Appium could not start a session: {exc}")
    try:
        opened, output = open_listing(adb, package)
        if not opened:
            return failure("error", f"The Aurora Store listing could not be opened: {output}")
        state, source = _read_listing(driver, page_timeout, poll)
        logger.info("Aurora Store listing for %s reads as %s.", package, state)
        if state == "installed":
            if is_installed(adb, package):
                return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
            return failure("error", "Aurora Store lists the app as installed, but the device does not.")
        if state == "unrecognised":
            return failure("unrecognised", diagnostics=save_diagnostics(driver, diagnostics_root, package, source))
        if state == "installable":
            if not _tap_label(driver, INSTALL_LABEL):
                return failure("unrecognised", diagnostics=save_diagnostics(driver, diagnostics_root, package, source))
        elif state != "in_progress":
            return failure(state)

        deadline = time.monotonic() + install_timeout
        while time.monotonic() < deadline:
            time.sleep(poll)
            if is_installed(adb, package):
                installer = installer_of(adb, package)
                seconds = time.monotonic() - started
                logger.info("Installed %s through Aurora Store in %.0fs (installer %s).", package, seconds, installer or "unknown")
                detail = None if installer == AURORA_STORE_PACKAGE else f"Installed, but the installer is {installer or 'unknown'}."
                return InstallOutcome(status="installed", installer=installer or None, detail=detail, seconds=seconds)
            foreground, source = _screen(driver)
            state = classify_screen(foreground, screen_labels(source))
            if state in ("confirmation_required", "security_check", "sign_in_required", "paid"):
                return failure(state, seconds=time.monotonic() - started)
        return failure("timeout", f"Waited {install_timeout:.0f}s.", seconds=time.monotonic() - started)
    except Exception as exc:
        logger.debug("android aurora store: install of %s failed", package, exc_info=True)
        return failure("error", str(exc), seconds=time.monotonic() - started)
    finally:
        try:
            driver.quit()
        except Exception:
            logger.debug("android aurora store: driver quit failed", exc_info=True)
