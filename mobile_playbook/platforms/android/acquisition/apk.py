"""
Installs a missing Android app over adb from the app's APK in the intake folder.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk
from mobile_playbook.platforms.android.acquisition.play_store import PACKAGE_RE, InstallOutcome, installer_of
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.install import install_apks
from mobile_playbook.platforms.android.device.permissions import is_installed

logger = logging.getLogger(__name__)

STORE_NAME = "APK intake"
WORK_DIR_NAME = "apk"
INSTALL_TIMEOUT_SECONDS = 300.0

# Install states that end the attempt, as (blocker code, whether a retry can clear it, message).
FAILURES: dict[str, tuple[str, bool, str]] = {
    "missing": (
        "app_unavailable",
        False,
        "No APK of this app is in the intake folder. Put the app's installable APK in "
        "artifacts/intake/android/apks/, named <App_Name>_<version>.apk, or add a complete split set "
        "with apk-set.json (see docs/android/README.md), then retry.",
    ),
    "confirmation_required": (
        "app_install_failed",
        False,
        "The test device asks someone to confirm every adb install. Turn off Developer options > Monitor apps "
        "installed via ADB on the device, then retry.",
    ),
    "rejected": (
        "app_install_failed",
        False,
        "The device refused the app's APK in the intake folder. Replace it with a build for this device, then retry.",
    ),
    "wrong_app": (
        "app_install_failed",
        False,
        "The app's intake APK installed a different app. Replace it with this app's APK, then retry.",
    ),
    "error": (
        "app_install_failed",
        True,
        "The app could not be installed from its intake APK.",
    ),
}


# Build the failed outcome for an install state, optionally with extra detail.
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


# Report whether the device holds every adb install until someone confirms it on screen.
def adb_installs_need_confirmation(adb: AdbClient) -> bool:
    code, out, _ = adb.run(["shell", "settings", "get", "secure", "adb_install_need_confirm"])
    return code == 0 and (out or "").strip() == "1"


# Install a missing app from its intake APK over adb; the Appium driver factory is unused.
def install_from_apk(
    adb: AdbClient,
    make_driver: Callable[[], Any],
    package: str,
    diagnostics_root: Path,
    *,
    intake_dir: Path | None = None,
    install_timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> InstallOutcome:
    if not PACKAGE_RE.match(package or ""):
        return failure("error", f"{package!r} is not a valid package name.")
    if is_installed(adb, package):
        logger.debug("android apk: %s already installed", package)
        return InstallOutcome(status="already_installed", installer=installer_of(adb, package) or None)
    apk = find_intake_apk(package, intake_dir)
    if apk is None:
        return failure("missing")
    if adb_installs_need_confirmation(adb):
        return failure("confirmation_required")

    started = time.monotonic()
    logger.info("Installing %s from intake APK %s.", package, apk.path.name)
    installed, reason = install_apks(adb, apk.paths, grant_permissions=False, timeout=install_timeout)
    seconds = time.monotonic() - started
    if not installed:
        state = "rejected" if reason.startswith("Failure [") else "error"
        return failure(state, reason, seconds=seconds)
    if not is_installed(adb, package):
        return failure("wrong_app", seconds=seconds)
    logger.info("Installed %s from intake APK in %.0fs.", package, seconds)
    return InstallOutcome(status="installed", installer=installer_of(adb, package) or None, seconds=seconds)
