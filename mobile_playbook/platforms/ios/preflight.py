from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from mobile_playbook.orchestration.appium_process import tcp_reachable as _tcp_reachable

DEVICES_SECTION = "Devices"


@dataclass(frozen=True)
class IosPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_ios_preflight(config) -> IosPreflightResult:
    errors: list[str] = []
    if not getattr(config.device, "udid", ""):
        errors.append("device.udid is required")
    if not getattr(config.device, "team_id", ""):
        errors.append("device.team_id is required")
    if not getattr(config.device, "appium_server_url", ""):
        errors.append("device.appium_server_url is required")
    if not errors:
        _check_device_connected(config.device.udid, errors)
    if not errors:
        _check_appium_reachable(config.device.appium_server_url, errors)
    return IosPreflightResult(ok=not errors, errors=errors)


def _check_appium_reachable(appium_server_url: str, errors: list[str]) -> None:
    if not _tcp_reachable(appium_server_url):
        errors.append(f"appium: Appium server not reachable at {appium_server_url}. Start it with 'appium'.")


def _check_device_connected(udid: str, errors: list[str]) -> None:
    connected = connected_device_udids()
    # An empty result means `xcrun xctrace` itself is unavailable/unusable, not that
    # zero devices are connected (the Mac's own UDID always appears when it works) —
    # skip this check rather than block a run on a tool we couldn't query.
    if connected and udid not in connected:
        errors.append(
            f"device.udid '{udid}' is not a connected iOS device. "
            "Run 'xcrun xctrace list devices' to see connected devices — "
            "reconnect/unlock/trust the configured device, or update device.udid to match."
        )


def connected_device_udids(timeout: float = 15.0) -> set[str]:
    try:
        result = subprocess.run(
            ["xcrun", "xctrace", "list", "devices"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return _parse_connected_udids(result.stdout)


def _parse_connected_udids(output: str) -> set[str]:
    udids: set[str] = set()
    in_devices_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            in_devices_section = stripped.strip("= ").strip() == DEVICES_SECTION
            continue
        if not in_devices_section or not stripped:
            continue
        match = re.search(r"\(([0-9A-Fa-f-]+)\)\s*$", stripped)
        if match:
            udids.add(match.group(1))
    return udids
