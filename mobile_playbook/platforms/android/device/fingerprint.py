"""
Identifies the exact build of an app installed on the Android test device, and which app installed it.
"""

from __future__ import annotations

import logging
import re

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.apks import installed_apk_paths

logger = logging.getLogger(__name__)

SHA256_RE = re.compile(r"^([0-9a-f]{64})\s", re.MULTILINE)


# Return the SHA-256 of the installed app's base.apk, hashed on the device, or None when it cannot be read.
def installed_base_apk_sha256(adb: AdbClient, package: str) -> str | None:
    paths = installed_apk_paths(adb, package)
    base = next((path for path in paths if path.endswith("/base.apk")), None)
    if base is None:
        logger.debug("android fingerprint: no base.apk for %s (%s paths)", package, len(paths))
        return None
    code, out, _ = adb.run(["shell", "sha256sum", base])
    match = SHA256_RE.search(out or "") if code == 0 else None
    logger.debug("android fingerprint: %s base.apk sha256=%s (exit %s)", package, match.group(1) if match else None, code)
    return match.group(1) if match else None


# Return the package that installed the app, or None for an adb or sideloaded install with no installer recorded.
def installer_of(adb: AdbClient, package: str) -> str | None:
    code, out, _ = adb.run(["shell", "pm", "list", "packages", "-i", package])
    # pm matches by substring, so pick the exact package's line.
    match = re.search(rf"^package:{re.escape(package)}\s+installer=(\S+)", out or "", re.MULTILINE) if code == 0 else None
    installer = match.group(1) if match and match.group(1) != "null" else None
    logger.debug("android fingerprint: %s installer=%s (pm list exit %s)", package, installer, code)
    return installer
