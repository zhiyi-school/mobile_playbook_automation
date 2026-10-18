"""
Decides whether an app needs Google Play services and whether a device provides them, so device risks never judge an
app that stopped at a missing-Play-services dialog.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk
from mobile_playbook.platforms.android.apk_tools import manifest_tree
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.static_analysis.manifest import declares_play_services

logger = logging.getLogger(__name__)

PLAY_SERVICES_PACKAGE = "com.google.android.gms"


# Return the app's Play services need: its requires_play_services setting, else what its intake APK's manifest
# declares, or None when neither is known.
def app_needs_play_services(app: Any) -> bool | None:
    configured = getattr(app, "requires_play_services", None)
    if configured is not None:
        return configured
    found = find_intake_apk(app.package_name)
    if found is None:
        logger.debug("android play services: no intake APK of %s; its need is unknown", app.package_name)
        return None
    stat = found.path.stat()
    return _apk_declares_play_services(str(found.path), stat.st_mtime_ns, stat.st_size)


# Read the manifest once per APK file version; None when no SDK tool can read it.
@functools.lru_cache(maxsize=256)
def _apk_declares_play_services(path: str, mtime_ns: int, size: int) -> bool | None:
    try:
        declared = declares_play_services(manifest_tree(Path(path)))
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Could not read the manifest of %s to tell whether it needs Google Play services: %s", path, exc)
        return None
    logger.debug("android play services: %s declares Play services=%s", path, declared)
    return declared


# Return whether the device has Google Play services installed and enabled, or None when adb cannot tell.
def device_has_play_services(adb: AdbClient) -> bool | None:
    code, out, err = adb.run(["shell", "pm", "list", "packages", "-e", PLAY_SERVICES_PACKAGE])
    if code != 0:
        logger.debug("android play services: package query exited %s (%s)", code, (err or out)[:200])
        return None
    return f"package:{PLAY_SERVICES_PACKAGE}" in {line.strip() for line in out.splitlines()}


# Return why a device risk cannot judge the app on this device, or None when nothing is known to block it; role names
# the device in the message.
def play_services_problem(app: Any, adb: AdbClient, role: str | None = None) -> str | None:
    if not app_needs_play_services(app) or device_has_play_services(adb) is not False:
        return None
    device = f"{role or 'test'} device ({adb.target_serial()})"
    return (
        f"Google Play services are not installed and enabled on the {device}, and {app.name} ({app.package_name}) "
        "needs them, so this risk was not run. Use a device with Google Play, or set requires_play_services: false "
        "for this app if it works without them."
    )
