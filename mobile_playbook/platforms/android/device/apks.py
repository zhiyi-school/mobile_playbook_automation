"""
Lists the APK files of an app installed on the Android device and pulls copies of them to the workstation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)


# Return the on-device paths of the app's base and split APKs, or an empty list when pm cannot list them.
def installed_apk_paths(adb: AdbClient, package: str) -> list[str]:
    code, out, err = adb.run(["shell", "pm", "path", package])
    logger.debug("android apks: pm path %s exit %s, %s chars, err head=%r", package, code, len(out or ""), (err or "")[:200])
    if code != 0:
        return []
    return [line[len("package:") :].strip() for line in (out or "").splitlines() if line.startswith("package:")]


# Pull every installed APK of the app into `destination`, replacing earlier copies there, and return the pulled
# files; an empty list means pm listed nothing or a pull failed.
def pull_installed_apks(adb: AdbClient, package: str, destination: Path) -> list[Path]:
    paths = installed_apk_paths(adb, package)
    if not paths:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("*.apk"):
        logger.debug("android apks: removing stale copy %s", stale)
        stale.unlink()
    pulled = []
    for apk_path in paths:
        target = destination / Path(apk_path).name
        code, _, _ = adb.run(["pull", apk_path, str(target)], timeout=120)
        logger.debug("android apks: pulled %s -> %s (exit %s)", apk_path, target, code)
        if code != 0:
            return []
        pulled.append(target)
    logger.debug("android apks: pulled %s APK(s) of %s into %s", len(pulled), package, destination)
    return pulled
