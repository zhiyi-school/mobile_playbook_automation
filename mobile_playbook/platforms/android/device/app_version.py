"""
Reads the version name and version code of an app installed on the Android test device.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

VERSION_NAME_RE = re.compile(r"^[ \t]*versionName=(.*\S)[ \t]*$", re.MULTILINE)
VERSION_CODE_RE = re.compile(r"^[ \t]*versionCode=(\d+)", re.MULTILINE)


@dataclass(frozen=True)
class InstalledVersion:
    version_name: str
    version_code: str | None


# Return the installed app's version name and version code, or None when the device does not report a version.
def installed_version(adb: AdbClient, package: str) -> InstalledVersion | None:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    if code != 0 or not out:
        logger.debug("android app version: dumpsys package %s gave exit %s with %s chars", package, code, len(out or ""))
        return None
    version = parse_installed_version(out)
    logger.debug("android app version: %s reports %s", package, version)
    return version


# Parse the active package's version from dumpsys output, or None when it has no versionName.
def parse_installed_version(text: str) -> InstalledVersion | None:
    # An updated system app is listed again under "Hidden system packages:" with its factory version; the active
    # package comes first, so the first match wins.
    name = VERSION_NAME_RE.search(text)
    if name is None or name.group(1) == "null":
        return None
    code = VERSION_CODE_RE.search(text)
    return InstalledVersion(version_name=name.group(1), version_code=code.group(1) if code else None)
