"""
Installs one app's APKs on the device over adb and reports the package manager's failure reason.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

INSTALL_FAILURE_RE = re.compile(r"Failure \[.*\]")


# Install the APKs as one app, replacing any installed copy, and return whether it worked with the failure reason.
# `installer` records that package as the app's installer, as if that store had installed it.
def install_apks(adb: AdbClient, apks: list[Path], *, grant_permissions: bool, installer: str | None = None, timeout: float = 180) -> tuple[bool, str]:
    if not apks:
        return False, "No APKs to install"
    options = ["-r", "-g"] if grant_permissions else ["-r"]
    if installer:
        options += ["-i", installer]
    if len(apks) == 1:
        command = ["install", *options, str(apks[0])]
    else:
        command = ["install-multiple", *options, *[str(apk) for apk in apks]]
    code, out, err = adb.run(command, timeout=timeout)
    output = "\n".join(part for part in (out, err) if part)
    failure = INSTALL_FAILURE_RE.search(output)
    logger.debug("android install: adb %s of %s APKs exit %s (failure=%r)", command[0], len(apks), code, failure.group(0) if failure else None)
    # Older adb versions exit 0 even when the package manager reports Failure [...].
    if code == 0 and not failure:
        return True, ""
    if failure:
        return False, failure.group(0)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return False, lines[-1] if lines else f"adb {command[0]} exited {code}"
