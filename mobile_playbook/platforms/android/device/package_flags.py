"""
Reads whether an installed app is debuggable or allows backups, and the API level it targets, from `dumpsys package`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

# Android 6 and later list them as `flags=[ ... ]` and older releases as `pkgFlags=[ ... ]`; privateFlags is a different set.
FLAGS_RE = re.compile(r"^[ \t]*(?:pkgFlags|flags)=\[([^\]]*)\]", re.MULTILINE)
TARGET_SDK_RE = re.compile(r"\btargetSdk=(\d+)")


@dataclass(frozen=True)
class PackageFlags:
    debuggable: bool
    allow_backup: bool
    target_sdk: int | None
    flags: tuple[str, ...]

    # Describe the flags for result metadata.
    def to_dict(self) -> dict:
        return {"debuggable": self.debuggable, "allow_backup": self.allow_backup, "target_sdk": self.target_sdk, "flags": list(self.flags)}


# Return the installed app's package flags, or None when the device does not report them.
def package_flags(adb: AdbClient, package: str) -> PackageFlags | None:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    if code != 0 or not out:
        logger.debug("android package flags: dumpsys package %s gave exit %s with %s chars", package, code, len(out or ""))
        return None
    flags = parse_package_flags(out)
    logger.debug("android package flags: %s reports %s", package, flags)
    return flags


# Parse the active package's flags and target API level from dumpsys output, or None when it lists no flags.
def parse_package_flags(text: str) -> PackageFlags | None:
    # An updated system app is listed again under "Hidden system packages:"; the active package comes first.
    match = FLAGS_RE.search(text)
    if match is None:
        return None
    flags = tuple(match.group(1).split())
    target = TARGET_SDK_RE.search(text)
    return PackageFlags("DEBUGGABLE" in flags, "ALLOW_BACKUP" in flags, int(target.group(1)) if target else None, flags)
