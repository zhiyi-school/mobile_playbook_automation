"""
Checks the tools and services an Android risk requires before it runs.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

from mobile_playbook.orchestration.appium_server import tcp_reachable as _tcp_reachable
from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.appium_driver import appium_available

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AndroidPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Check each requirement and collect failures as errors and passing checks as warnings.
def check_android_preflight(config, adb: AdbClient, requires: list[str]) -> AndroidPreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    logger.debug("android preflight: checking requirements %s", requires)
    for name, ok, message in check_requirements(requires, adb, config):
        logger.debug("android preflight: %s ok=%s (%s)", name, ok, message)
        (warnings if ok else errors).append(f"{name}: {message}")
    logger.debug("android preflight: ok=%s with %s errors and %s passing checks", not errors, len(errors), len(warnings))
    return AndroidPreflightResult(ok=not errors, errors=errors, warnings=warnings)


# Return a (name, ok, message) result for each named requirement, failing unknown names.
def check_requirements(requires: list[str], adb: AdbClient, config) -> list[tuple[str, bool, str]]:
    results = []
    for name in requires:
        if name == "adb":
            results.append((name, *_check_adb(adb)))
        elif name == "appium":
            results.append((name, *_check_appium(config)))
        elif name in {"apktool", "apksigner", "keytool"}:
            results.append((name, *_check_executable(name)))
        elif name == "mobsf":
            results.append((name, *_check_tcp_tool("MobSF", config.tools.get("mobsf_url", ""))))
        elif name == "burp":
            results.append((name, *_check_tcp_tool("Burp proxy", config.tools.get("burp_proxy", ""))))
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


# Check that a configured tool URL is set and accepts TCP connections.
def _check_tcp_tool(label: str, url: str) -> tuple[bool, str]:
    logger.debug("android preflight: checking %s reachability at %s", label, url)
    if not url:
        return False, f"{label} URL is not configured."
    if not _tcp_reachable(url):
        return False, f"{label} not reachable at {url}."
    return True, f"{label} reachable"
