"""
Installs a missing Android app from its Huawei AppGallery listing, tapping only a free app's own Install button.
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
    logger.debug("android app gallery: AppiumBy unavailable", exc_info=True)
    AppiumBy = None

APP_GALLERY_PACKAGE = "com.huawei.appmarket"
STORE_NAME = "AppGallery"
WORK_DIR_NAME = "app_gallery"

INSTALL_LABEL = "install"
INSTALLED_LABELS = {"open", "update", "uninstall"}
PAID_LABELS = {"buy", "purchase"}
UNAVAILABLE_PHRASES = (
    "not available in your country",
    "not available in your region",
    "not available in the current country",
    "app not found",
    "app is not available",
    "has been removed",
    "content not found",
    "not compatible with your device",
    "not supported on this device",
)
UNAVAILABLE_LABELS = {"reserve", "pre-order"}
SIGN_IN_PHRASES = ("sign in with huawei id", "sign in to huawei id", "log in with huawei id")
SIGN_IN_LABELS = {"sign in", "log in"}
ACCOUNT_SETUP_PHRASES = ("verify your huawei id", "verify your identity", "enter your password", "add payment method")
DETAIL_BUTTON_RE = re.compile(r"detail.*(download|install)", re.IGNORECASE)

PAGE_TIMEOUT_SECONDS = 20.0
INSTALL_TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 5.0

# Listing states that end the attempt, as (blocker code, whether a retry can clear it, message).
FAILURES: dict[str, tuple[str, bool, str]] = {
    "paid": (
        "app_paid",
        False,
        "This app is paid on AppGallery. Someone must verify the purchase and install it on the test device, "
        "then retry.",
    ),
    "unavailable": (
        "app_unavailable",
        False,
        "AppGallery does not offer this app to the test device: it was not found, is not released, is not "
        "available in this region, or is not compatible with the device.",
    ),
    "sign_in_required": (
        "store_account_action_required",
        False,
        "AppGallery on the test device needs a signed-in HUAWEI ID before apps can be installed.",
    ),
    "account_action_required": (
        "store_account_action_required",
        False,
        "AppGallery asked for account verification before installing. Someone must complete it on the test "
        "device, then retry.",
    ),
    "unrecognised": (
        "store_screen_unrecognised",
        False,
        "The AppGallery screen was not recognised, so nothing was tapped. A screenshot and the screen layout "
        "were saved on the automation host.",
    ),
    "timeout": (
        "app_install_timeout",
        True,
        "The AppGallery install did not finish in time.",
    ),
    "error": (
        "app_install_failed",
        True,
        "The app could not be installed from AppGallery.",
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


# Return the prompt state a screen shows that a person must deal with, or None.
def account_prompt(labels: list[str]) -> str | None:
    lowered = [label.lower() for label in labels]
    if any(phrase in label for label in lowered for phrase in ACCOUNT_SETUP_PHRASES):
        return "account_action_required"
    if any(label in SIGN_IN_LABELS for label in lowered) or any(
        phrase in label for label in lowered for phrase in SIGN_IN_PHRASES
    ):
        return "sign_in_required"
    return None


# Classify an AppGallery listing from its labels; a price wins over Install so a paid app is never tapped.
def classify_listing(labels: list[str]) -> str:
    lowered = [label.lower() for label in labels]
    if any(label in PAID_LABELS or PRICE_RE.match(label) for label in lowered):
        return "paid"
    prompt = account_prompt(labels)
    if prompt:
        return prompt
    installable = INSTALL_LABEL in lowered
    installed = any(label in INSTALLED_LABELS for label in lowered)
    # Recommended-app cards carry their own Install and Open buttons, so a listing showing both is ambiguous.
    if installable and installed:
        return "unrecognised"
    if installable:
        return "installable"
    if installed:
        return "installed"
    if any(label in UNAVAILABLE_LABELS for label in lowered) or any(
        phrase in label for label in lowered for phrase in UNAVAILABLE_PHRASES
    ):
        return "unavailable"
    return "unrecognised"


# Open the package's listing in AppGallery itself, never another store that handles the link.
def open_listing(adb: AdbClient, package: str) -> tuple[bool, str]:
    code, out, err = adb.run(
        [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-p",
            APP_GALLERY_PACKAGE,
            # Quoted so the device shell does not treat '?' as a glob.
            "-d",
            f"'appmarket://details?id={package}'",
        ]
    )
    output = (out or err or "").strip()
    if code != 0 or "error" in output.lower():
        return False, output or f"am start exited {code}"
    return True, output


# Read the screen's page source, or an empty string while it cannot be read.
def _page_source(driver: Any) -> str:
    try:
        return driver.page_source or ""
    except Exception:
        logger.debug("android app gallery: page source unavailable", exc_info=True)
        return ""


# Poll the listing until it settles into a recognised state or the page timeout passes.
def _read_listing(driver: Any, page_timeout: float, poll: float) -> tuple[str, str]:
    deadline = time.monotonic() + page_timeout
    source = ""
    while True:
        try:
            foreground = driver.current_package == APP_GALLERY_PACKAGE
        except Exception:
            logger.debug("android app gallery: current package unavailable", exc_info=True)
            foreground = False
        if foreground:
            source = _page_source(driver)
            state = classify_listing(screen_labels(source))
            logger.debug("android app gallery: listing state %s", state)
            if state != "unrecognised":
                return state, source
        if time.monotonic() >= deadline:
            return "unrecognised", source
        time.sleep(poll)


# Return an element's resource ID, or an empty string when it cannot be read.
def _resource_id(element: Any) -> str:
    try:
        return element.get_attribute("resource-id") or ""
    except Exception:
        logger.debug("android app gallery: resource-id unavailable", exc_info=True)
        return ""


# Tap the listing's own Install button, only when exactly one candidate remains after preferring the detail button.
def _tap_install(driver: Any) -> bool:
    buttons: dict[str, Any] = {}
    for strategy in ('new UiSelector().textMatches("(?i)^install$")', 'new UiSelector().descriptionMatches("(?i)^install$")'):
        try:
            elements = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, strategy)
        except Exception:
            logger.debug("android app gallery: no Install element for %s", strategy)
            continue
        for element in elements:
            if element.is_displayed() and element.is_enabled():
                buttons[element.id] = element
    detail = [element for element in buttons.values() if DETAIL_BUTTON_RE.search(_resource_id(element))]
    candidates = detail or list(buttons.values())
    logger.debug("android app gallery: %d Install buttons, %d on the detail bar", len(buttons), len(detail))
    if len(candidates) != 1:
        return False
    candidates[0].click()
    return True


# Install a missing app from AppGallery, refusing paid apps and any screen it does not recognise.
def install_from_app_gallery(
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
        logger.debug("android app gallery: %s already installed", package)
        return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
    if AppiumBy is None:
        return failure("error", "The Appium Python client is not installed on the automation host.")

    started = time.monotonic()
    try:
        driver = make_driver()
    except Exception as exc:
        logger.debug("android app gallery: driver creation failed", exc_info=True)
        return failure("error", f"Appium could not start a session: {exc}")
    try:
        opened, output = open_listing(adb, package)
        if not opened:
            return failure("error", f"The AppGallery listing could not be opened: {output}")
        state, source = _read_listing(driver, page_timeout, poll)
        logger.info("AppGallery listing for %s reads as %s.", package, state)
        if state == "installed":
            if is_installed(adb, package):
                return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
            return failure("error", "AppGallery lists the app as installed, but the device does not.")
        if state == "unrecognised":
            return failure("unrecognised", diagnostics=save_diagnostics(driver, diagnostics_root, package, source))
        if state != "installable":
            return failure(state)
        if not _tap_install(driver):
            return failure("unrecognised", diagnostics=save_diagnostics(driver, diagnostics_root, package, source))

        deadline = time.monotonic() + install_timeout
        while time.monotonic() < deadline:
            time.sleep(poll)
            if is_installed(adb, package):
                installer = installer_of(adb, package)
                seconds = time.monotonic() - started
                logger.info("Installed %s from AppGallery in %.0fs (installer %s).", package, seconds, installer or "unknown")
                detail = None if installer == APP_GALLERY_PACKAGE else f"Installed, but the installer is {installer or 'unknown'}."
                return InstallOutcome(status="installed", installer=installer or None, detail=detail, seconds=seconds)
            labels = screen_labels(_page_source(driver))
            prompt = account_prompt(labels)
            if prompt:
                return failure(prompt, seconds=time.monotonic() - started)
            if classify_listing(labels) == "paid":
                return failure("paid", seconds=time.monotonic() - started)
        return failure("timeout", f"Waited {install_timeout:.0f}s.", seconds=time.monotonic() - started)
    except Exception as exc:
        logger.debug("android app gallery: install of %s failed", package, exc_info=True)
        return failure("error", str(exc), seconds=time.monotonic() - started)
    finally:
        try:
            driver.quit()
        except Exception:
            logger.debug("android app gallery: driver quit failed", exc_info=True)
