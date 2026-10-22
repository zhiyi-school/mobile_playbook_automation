"""
Checks the tools and services an Android risk requires before it runs.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

from mobile_playbook.orchestration.appium_server import tcp_reachable as _tcp_reachable
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.appium_driver import appium_available
from mobile_playbook.platforms.android.device.capture_helper import BUILD_COMMAND, HELPER_PACKAGE, HELPER_VERSION, helper_apk_path
from mobile_playbook.platforms.android.device.accessibility_attacker import ATTACKER_PACKAGE as ACCESSIBILITY_ATTACKER_PACKAGE
from mobile_playbook.platforms.android.device.accessibility_attacker import ATTACKER_VERSION as ACCESSIBILITY_ATTACKER_VERSION
from mobile_playbook.platforms.android.device.accessibility_attacker import attacker_apk_path as accessibility_attacker_apk_path
from mobile_playbook.platforms.android.device.event_spy import EVENT_SPY_APP
from mobile_playbook.platforms.android.device.overlay_attacker import ATTACKER_PACKAGE, ATTACKER_VERSION, attacker_apk_path
from mobile_playbook.platforms.android.device.root import find_root_shell

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AndroidPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Check each requirement and collect failures as errors and passing checks as warnings.
def check_android_preflight(config, adb: AdbClient | None, requires: list[str]) -> AndroidPreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    logger.debug("android preflight: checking requirements %s", requires)
    for name, ok, message in check_requirements(requires, adb, config):
        logger.debug("android preflight: %s ok=%s (%s)", name, ok, message)
        (warnings if ok else errors).append(f"{name}: {message}")
    logger.debug("android preflight: ok=%s with %s errors and %s passing checks", not errors, len(errors), len(warnings))
    return AndroidPreflightResult(ok=not errors, errors=errors, warnings=warnings)


# Return a (name, ok, message) result for each named requirement, failing unknown names.
def check_requirements(requires: list[str], adb: AdbClient | None, config) -> list[tuple[str, bool, str]]:
    results = []
    for name in requires:
        if name == "adb":
            results.append((name, *_check_adb(adb)))
        elif name == "appium":
            results.append((name, *_check_appium(config)))
        elif name == "root":
            results.append((name, *_check_root(adb)))
        elif name in {"apktool", "zipalign", "apksigner", "keytool"}:
            results.append((name, *_check_executable(name)))
        elif name == "mobsf":
            results.append((name, *_check_tcp_tool("MobSF", config.tools.get("mobsf_url", ""))))
        elif name == "burp":
            results.append((name, *_check_tcp_tool("Burp proxy", config.tools.get("burp_proxy", ""))))
        elif name == "screen_capture_helper":
            results.append((name, *_check_capture_helper(adb, config)))
        elif name == "overlay_attacker":
            results.append((name, *_check_overlay_attacker(adb, config)))
        elif name == "accessibility_attacker":
            results.append((name, *_check_accessibility_attacker(adb, config)))
        elif name == "event_spy_attacker":
            results.append((name, *_check_event_spy(adb, config)))
        else:
            logger.debug("android preflight: unknown requirement %s", name)
            results.append((name, False, f"unknown requirement '{name}'"))
    return results


# Check that adb is on PATH and a device is connected.
def _check_adb(adb: AdbClient) -> tuple[bool, str]:
    logger.debug("android preflight: checking adb (path=%s, serial=%s)", getattr(adb, "adb_path", None), getattr(adb, "serial", None))
    if not adb.is_available():
        return False, "adb not found on PATH. Install Android platform-tools and add it to PATH."
    if not adb.is_device_connected():
        return False, "No Android device connected. Run 'adb devices' and ensure one shows 'device'."
    return True, "adb OK, device connected"


# Check that the device grants root, through a root adbd or the su binary of userdebug and rooted images.
def _check_root(adb: AdbClient | None) -> tuple[bool, str]:
    if adb is None:
        return False, "No Android device connected to check for root."
    if find_root_shell(adb) is not None:
        return True, "device grants root"
    return False, "The device does not grant root; risks that require root need a rooted device."


# Check that the Appium client is installed and the configured server is reachable.
def _check_appium(config) -> tuple[bool, str]:
    if not appium_available():
        logger.debug("android preflight: Appium-Python-Client missing")
        return False, "Appium-Python-Client is not installed."
    logger.debug("android preflight: checking Appium server %s", config.device.appium_server_url)
    if not _tcp_reachable(config.device.appium_server_url):
        return False, f"Appium server not reachable at {config.device.appium_server_url}. Start it with 'appium'."
    return True, "Appium client + server OK"


# Check that an executable is on PATH.
def _check_executable(name: str) -> tuple[bool, str]:
    if shutil.which(name):
        logger.debug("android preflight: executable %s found on PATH", name)
        return True, f"{name} found on PATH"
    logger.debug("android preflight: executable %s not found on PATH", name)
    return False, f"{name} not found on PATH. Install it and ensure it is on PATH."


# Check that the screen-capture helper APK is built, or that the device already has the current helper.
def _check_capture_helper(adb: AdbClient, config) -> tuple[bool, str]:
    apk = helper_apk_path((getattr(config, "screen_capture", None) or {}).get("helper_apk"))
    if apk.is_file():
        return True, f"screen-capture helper APK found at {apk}"
    installed = installed_version(adb, HELPER_PACKAGE)
    if installed is not None and installed.version_name == HELPER_VERSION:
        return True, "screen-capture helper already installed on the device"
    logger.debug("android preflight: screen-capture helper APK missing at %s and not installed", apk)
    return False, f"screen-capture helper APK not found at {apk}. Build it with `{BUILD_COMMAND}`."


# Check that the overlay attacker APK is in place, or that the device already has the current attacker app.
def _check_overlay_attacker(adb: AdbClient | None, config) -> tuple[bool, str]:
    apk = attacker_apk_path((getattr(config, "overlay", None) or {}).get("attacker_apk"))
    if apk.is_file():
        return True, f"overlay attacker APK found at {apk}"
    if adb is not None:
        installed = installed_version(adb, ATTACKER_PACKAGE)
        if installed is not None and installed.version_name == ATTACKER_VERSION:
            return True, "overlay attacker app already installed on the device"
    logger.debug("android preflight: overlay attacker APK missing at %s and not installed", apk)
    return False, f"overlay attacker APK not found at {apk}. Place F4_Overlay_Attacker.apk in the Android intake folder or set overlay.attacker_apk."


# Check that the accessibility attacker APK is in place, or that the device already has the current attacker app.
def _check_accessibility_attacker(adb: AdbClient | None, config) -> tuple[bool, str]:
    apk = accessibility_attacker_apk_path((getattr(config, "accessibility", None) or {}).get("attacker_apk"))
    if apk.is_file():
        return True, f"accessibility attacker APK found at {apk}"
    if adb is not None:
        installed = installed_version(adb, ACCESSIBILITY_ATTACKER_PACKAGE)
        if installed is not None and installed.version_name == ACCESSIBILITY_ATTACKER_VERSION:
            return True, "accessibility attacker app already installed on the device"
    logger.debug("android preflight: accessibility attacker APK missing at %s and not installed", apk)
    return False, f"accessibility attacker APK not found at {apk}. Place F5R1_Accessibility_Attacker.apk in the Android intake folder or set accessibility.attacker_apk."


# Check that the Event Spy APK is in place, or that the device already has its current build.
def _check_event_spy(adb: AdbClient | None, config) -> tuple[bool, str]:
    apk = EVENT_SPY_APP.apk_path((getattr(config, "accessibility", None) or {}).get("event_spy_apk"))
    if apk.is_file():
        return True, f"Event Spy APK found at {apk}"
    if adb is not None:
        installed = installed_version(adb, EVENT_SPY_APP.package)
        if installed is not None and installed.version_name == EVENT_SPY_APP.version:
            return True, "Event Spy already installed on the device"
    logger.debug("android preflight: Event Spy APK missing at %s and not installed", apk)
    return False, f"Event Spy APK not found at {apk}. Place {EVENT_SPY_APP.apk_name} in the Android intake folder or set accessibility.event_spy_apk."


# Check that a configured tool URL is set and accepts TCP connections.
def _check_tcp_tool(label: str, url: str) -> tuple[bool, str]:
    logger.debug("android preflight: checking %s reachability at %s", label, url)
    if not url:
        return False, f"{label} URL is not configured."
    if not _tcp_reachable(url):
        return False, f"{label} not reachable at {url}."
    return True, f"{label} reachable"
