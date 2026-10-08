"""
Installs a missing Android app from its Play Store listing, tapping only a free app's Install button.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.permissions import is_installed

logger = logging.getLogger(__name__)

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    logger.debug("android play store: AppiumBy unavailable", exc_info=True)
    AppiumBy = None

PLAY_STORE_PACKAGE = "com.android.vending"
STORE_NAME = "Play Store"
WORK_DIR_NAME = "play_store"
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")

INSTALL_LABEL = "install"
INSTALLED_LABELS = {"open", "play", "uninstall", "update"}
PAID_LABELS = {"buy"}
PRICE_RE = re.compile(
    r"^(?:buy(?: for)? )?(?:(?:[a-z]{1,3}\s?)?[$€£¥₹₩]\s?\d[\d.,]*|(?:rm|usd|sgd|myr|eur|gbp|inr)\s?\d[\d.,]*)$"
)
UNAVAILABLE_PHRASES = (
    "not available in your region",
    "not available in your country",
    "isn't available in your country",
    "item not found",
    "the requested url was not found",
    "not available for any of your devices",
    "not compatible with your device",
    "error retrieving information from server",
    "pre-register",
)
SIGN_IN_PHRASES = ("sign in to google play", "sign in with your google account", "add your google account")
SIGN_IN_LABELS = {"sign in"}
ACCOUNT_SETUP_PHRASES = ("complete account setup", "add payment method", "verify it's you", "confirm your password")

PAGE_TIMEOUT_SECONDS = 20.0
INSTALL_TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 5.0

_NODE_RE = re.compile(r"<[\w.$-]+\s([^<>]*?)/?>")
_ATTRIBUTE_RE = re.compile(r'([\w-]+)="([^"]*)"')

# Listing states that end the attempt, as (blocker code, whether a retry can clear it, message).
FAILURES: dict[str, tuple[str, bool, str]] = {
    "paid": (
        "app_paid",
        False,
        "This app is paid on the Play Store. Someone must verify the purchase and install it on the test "
        "device, then retry.",
    ),
    "unavailable": (
        "app_unavailable",
        False,
        "The Play Store does not offer this app to the test device: it was not found, is not released, is not "
        "available in this region, or is not compatible with the device.",
    ),
    "sign_in_required": (
        "store_account_action_required",
        False,
        "The Play Store on the test device needs a signed-in Google account before apps can be installed.",
    ),
    "account_action_required": (
        "store_account_action_required",
        False,
        "The Play Store asked for account setup or verification before installing. Someone must complete it "
        "on the test device, then retry.",
    ),
    "unrecognised": (
        "store_screen_unrecognised",
        False,
        "The Play Store screen was not recognised, so nothing was tapped. A screenshot and the screen layout "
        "were saved on the automation host.",
    ),
    "timeout": (
        "app_install_timeout",
        True,
        "The Play Store install did not finish in time.",
    ),
    "error": (
        "app_install_failed",
        True,
        "The app could not be installed from the Play Store.",
    ),
}


@dataclass(frozen=True)
class InstallOutcome:
    status: str
    blocker_code: str | None = None
    retryable: bool = True
    detail: str | None = None
    installer: str | None = None
    seconds: float = 0.0
    diagnostics: str | None = None
    version_name: str | None = None
    version_code: str | None = None

    # Return the outcome as a plain dict.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


# Return the visible text and content descriptions in an Appium page source, in document order.
def screen_labels(page_source: str) -> list[str]:
    labels: list[str] = []
    for node in _NODE_RE.finditer(page_source or ""):
        attributes = dict(_ATTRIBUTE_RE.findall(node.group(1)))
        for key in ("text", "content-desc"):
            value = html.unescape(attributes.get(key, "")).strip()
            if value:
                labels.append(value)
    return labels


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


# Classify a Play Store listing from its labels; a price wins over Install so a paid app is never tapped.
def classify_listing(labels: list[str]) -> str:
    lowered = [label.lower() for label in labels]
    if any(label in PAID_LABELS or PRICE_RE.match(label) for label in lowered):
        return "paid"
    prompt = account_prompt(labels)
    if prompt:
        return prompt
    if INSTALL_LABEL in lowered:
        return "installable"
    if any(label in INSTALLED_LABELS for label in lowered):
        return "installed"
    if any(phrase in label for label in lowered for phrase in UNAVAILABLE_PHRASES):
        return "unavailable"
    return "unrecognised"


# Return the package that installed the app, such as com.android.vending, or an empty string.
def installer_of(adb: AdbClient, package: str) -> str:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    match = re.search(r"installerPackageName=(\S+)", out or "") if code == 0 else None
    if match:
        return match.group(1)
    code, out, _ = adb.run(["shell", "pm", "list", "packages", "-i", package])
    for line in (out or "").splitlines():
        if line.startswith(f"package:{package} ") or line.strip() == f"package:{package}":
            found = re.search(r"installer=(\S+)", line)
            if found:
                return found.group(1)
    return ""


# Open the package's listing in the Play Store app itself, never another store that handles market links.
def open_listing(adb: AdbClient, package: str) -> tuple[bool, str]:
    code, out, err = adb.run(
        [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-p",
            PLAY_STORE_PACKAGE,
            # Quoted so the device shell does not treat '?' as a glob.
            "-d",
            f"'market://details?id={package}'",
        ]
    )
    output = (out or err or "").strip()
    if code != 0 or "error" in output.lower():
        return False, output or f"am start exited {code}"
    return True, output


# Save the current screen and its layout for whoever updates the store rules, returning the folder.
def save_diagnostics(driver: Any, diagnostics_root: Path, package: str, page_source: str) -> Path | None:
    folder = diagnostics_root / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{package}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page_source.xml").write_text(page_source or "", encoding="utf-8")
        png = driver.get_screenshot_as_png()
        if png:
            (folder / "screenshot.png").write_bytes(png)
    except Exception:
        logger.debug("android play store: saving diagnostics to %s failed", folder, exc_info=True)
        logger.warning("Could not save app store diagnostics for %s.", package)
        return None
    logger.info("App store diagnostics for %s saved to %s.", package, folder)
    return folder


# Read the screen's page source, or an empty string while it cannot be read.
def _page_source(driver: Any) -> str:
    try:
        return driver.page_source or ""
    except Exception:
        logger.debug("android play store: page source unavailable", exc_info=True)
        return ""


# Poll the listing until it settles into a recognised state or the page timeout passes.
def _read_listing(driver: Any, page_timeout: float, poll: float) -> tuple[str, str]:
    deadline = time.monotonic() + page_timeout
    source = ""
    while True:
        try:
            foreground = driver.current_package == PLAY_STORE_PACKAGE
        except Exception:
            logger.debug("android play store: current package unavailable", exc_info=True)
            foreground = False
        if foreground:
            source = _page_source(driver)
            state = classify_listing(screen_labels(source))
            logger.debug("android play store: listing state %s", state)
            if state != "unrecognised":
                return state, source
        if time.monotonic() >= deadline:
            return "unrecognised", source
        time.sleep(poll)


# Tap the listing's Install button. This is the only element the automation ever taps in the Play Store.
def _tap_install(driver: Any) -> bool:
    for strategy in ('new UiSelector().textMatches("(?i)^install$")', 'new UiSelector().descriptionMatches("(?i)^install$")'):
        try:
            element = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, strategy)
        except Exception:
            logger.debug("android play store: no Install element for %s", strategy)
            continue
        if element.is_displayed() and element.is_enabled():
            element.click()
            return True
    return False


# Install a missing app from the Play Store, refusing paid apps and any screen it does not recognise.
def install_from_play_store(
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
        logger.debug("android play store: %s already installed", package)
        return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
    if AppiumBy is None:
        return failure("error", "The Appium Python client is not installed on the automation host.")

    started = time.monotonic()
    try:
        driver = make_driver()
    except Exception as exc:
        logger.debug("android play store: driver creation failed", exc_info=True)
        return failure("error", f"Appium could not start a session: {exc}")
    try:
        opened, output = open_listing(adb, package)
        if not opened:
            return failure("error", f"The Play Store listing could not be opened: {output}")
        state, source = _read_listing(driver, page_timeout, poll)
        logger.info("Play Store listing for %s reads as %s.", package, state)
        if state == "installed":
            if is_installed(adb, package):
                return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
            return failure("error", "The Play Store lists the app as installed, but the device does not.")
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
                logger.info("Installed %s from the Play Store in %.0fs (installer %s).", package, seconds, installer or "unknown")
                detail = None if installer == PLAY_STORE_PACKAGE else f"Installed, but the installer is {installer or 'unknown'}."
                return InstallOutcome(status="installed", installer=installer or None, detail=detail, seconds=seconds)
            labels = screen_labels(_page_source(driver))
            prompt = account_prompt(labels)
            if prompt:
                return failure(prompt, seconds=time.monotonic() - started)
            if classify_listing(labels) == "paid":
                return failure("paid", seconds=time.monotonic() - started)
        return failure("timeout", f"Waited {install_timeout:.0f}s.", seconds=time.monotonic() - started)
    except Exception as exc:
        logger.debug("android play store: install of %s failed", package, exc_info=True)
        return failure("error", str(exc), seconds=time.monotonic() - started)
    finally:
        try:
            driver.quit()
        except Exception:
            logger.debug("android play store: driver quit failed", exc_info=True)
