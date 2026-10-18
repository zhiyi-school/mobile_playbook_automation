"""
Locates the Android SDK from the configured adb so an auto-started Appium can run UiAutomator2 sessions.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from mobile_playbook.orchestration.appium_server import AppiumEnvironmentError

logger = logging.getLogger(__name__)

SDK_VARIABLES = ("ANDROID_HOME", "ANDROID_SDK_ROOT")


# Return the SDK root that holds the adb at adb_path (or found on PATH), following symlinks to the real binary.
def sdk_root_from_adb(adb_path: str) -> Path:
    found = shutil.which(adb_path)
    if found is None:
        raise AppiumEnvironmentError(f"device.adb_path {adb_path!r} was not found")
    adb = Path(found).resolve()
    if adb.parent.name != "platform-tools":
        raise AppiumEnvironmentError(f"device.adb_path {adb_path!r} resolves to {adb}, which is not in an SDK platform-tools directory")
    return adb.parent.parent


# Return a copy of environ for Appium, adding ANDROID_HOME and ANDROID_SDK_ROOT from adb only when neither is set.
def appium_environment(adb_path: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    if any(env.get(name) for name in SDK_VARIABLES):
        return env
    try:
        root = sdk_root_from_adb(adb_path)
    except AppiumEnvironmentError as exc:
        raise AppiumEnvironmentError(
            f"Appium needs ANDROID_HOME or ANDROID_SDK_ROOT and neither is set; {exc}. "
            "Export ANDROID_HOME or point device.adb_path at <sdk>/platform-tools/adb."
        ) from exc
    logger.info("Appium auto-start: ANDROID_HOME and ANDROID_SDK_ROOT are not set; using the SDK at %s from %s.", root, adb_path)
    env.update(dict.fromkeys(SDK_VARIABLES, str(root)))
    return env
