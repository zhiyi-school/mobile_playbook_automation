from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from mobile_playbook.orchestration.appium_process import tcp_reachable as _tcp_reachable
from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.appium_driver import appium_available


@dataclass(frozen=True)
class AndroidPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_android_preflight(config, adb: AdbClient, requires: list[str]) -> AndroidPreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    for name, ok, message in check_requirements(requires, adb, config):
        (warnings if ok else errors).append(f"{name}: {message}")
    return AndroidPreflightResult(ok=not errors, errors=errors, warnings=warnings)


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
            results.append((name, False, f"unknown requirement '{name}'"))
    return results


def _check_adb(adb: AdbClient) -> tuple[bool, str]:
    if not adb.is_available():
        return False, "adb not found on PATH. Install Android platform-tools and add it to PATH."
    if not adb.is_device_connected():
        return False, "No Android device connected. Run 'adb devices' and ensure one shows 'device'."
    return True, "adb OK, device connected"


def _check_appium(config) -> tuple[bool, str]:
    if not appium_available():
        return False, "Appium-Python-Client is not installed."
    if not _tcp_reachable(config.device.appium_server_url):
        return False, f"Appium server not reachable at {config.device.appium_server_url}. Start it with 'appium'."
    return True, "Appium client + server OK"


def _check_executable(name: str) -> tuple[bool, str]:
    if shutil.which(name):
        return True, f"{name} found on PATH"
    return False, f"{name} not found on PATH. Install it and ensure it is on PATH."


def _check_tcp_tool(label: str, url: str) -> tuple[bool, str]:
    if not url:
        return False, f"{label} URL is not configured."
    if not _tcp_reachable(url):
        return False, f"{label} not reachable at {url}."
    return True, f"{label} reachable"
