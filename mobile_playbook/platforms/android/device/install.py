"""
Installs one app's APKs on the device by copying them over adb and installing them with the package manager.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

INSTALL_FAILURE_RE = re.compile(r"Failure \[.*\]")
# Shell-writable on every device without root; the package manager can read APKs staged here.
STAGING_DIR = "/data/local/tmp/mobile_playbook_install"
# A copy may take this long plus one second per PUSH_MIN_BYTES_PER_SECOND bytes; slow links such as a Corellium
# instance over its project VPN copy at about 1 MB/s, so this leaves several times that headroom.
PUSH_BASE_TIMEOUT_SECONDS = 60.0
PUSH_MIN_BYTES_PER_SECOND = 250_000
PUSH_ATTEMPTS = 2


# Copy the APKs to the device, install them as one app replacing any installed copy, and return whether it worked
# with the failure reason. `installer` records that package as the app's installer, as if that store had installed it;
# `timeout` bounds the package manager's install, while each copy gets a timeout that grows with the APK's size.
def install_apks(adb: AdbClient, apks: list[Path], *, grant_permissions: bool, installer: str | None = None, timeout: float = 180) -> tuple[bool, str]:
    if not apks:
        return False, "No APKs to install"
    adb.run(["shell", f"rm -rf {STAGING_DIR} && mkdir -p {STAGING_DIR}"])
    try:
        # Copying first keeps a slow transfer from timing out mid-install, which leaves the package manager busy.
        staged = []
        for index, apk in enumerate(apks):
            remote = f"{STAGING_DIR}/{index}.apk"
            copied, reason = _push_apk(adb, apk, remote)
            if not copied:
                return False, reason
            staged.append(remote)
        return _pm_install(adb, staged, grant_permissions=grant_permissions, installer=installer, timeout=timeout)
    finally:
        adb.run(["shell", "rm", "-rf", STAGING_DIR])


# Copy one APK to the remote path, retrying once, and return whether the device holds all of it with the reason if not.
def _push_apk(adb: AdbClient, apk: Path, remote: str) -> tuple[bool, str]:
    size = apk.stat().st_size
    timeout = PUSH_BASE_TIMEOUT_SECONDS + size / PUSH_MIN_BYTES_PER_SECOND
    reason = ""
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        code, out, err = adb.run(["push", str(apk), remote], timeout=timeout)
        if code == 0:
            remote_size = _remote_size(adb, remote)
            if remote_size == size:
                logger.debug("android install: copied %s (%s bytes) to %s on attempt %s", apk.name, size, remote, attempt)
                return True, ""
            reason = f"the device holds {remote_size} of {size} bytes"
        else:
            lines = [line.strip() for line in "\n".join(part for part in (out, err) if part).splitlines() if line.strip()]
            reason = lines[-1] if lines else f"adb push exited {code}"
        logger.debug("android install: copy of %s attempt %s/%s failed (%s)", apk.name, attempt, PUSH_ATTEMPTS, reason)
    return False, f"Could not copy {apk.name} to the device: {reason}"


# Return the size in bytes of a file on the device, or None when it cannot be read.
def _remote_size(adb: AdbClient, remote: str) -> int | None:
    code, out, _ = adb.run(["shell", "stat", "-c", "%s", remote])
    value = out.strip()
    return int(value) if code == 0 and value.isdigit() else None


# Install APKs already on the device as one app with the package manager and return whether it worked with the reason.
def _pm_install(adb: AdbClient, remotes: list[str], *, grant_permissions: bool, installer: str | None, timeout: float) -> tuple[bool, str]:
    options = ["-r", "-g"] if grant_permissions else ["-r"]
    if installer:
        options += ["-i", installer]
    command = ["shell", "pm", "install", *options, *remotes]
    code, out, err = adb.run(command, timeout=timeout)
    output = "\n".join(part for part in (out, err) if part)
    failure = INSTALL_FAILURE_RE.search(output)
    logger.debug("android install: pm install of %s APKs exit %s (failure=%r)", len(remotes), code, failure.group(0) if failure else None)
    # Devices without adb's shell protocol report exit 0 even when the package manager prints Failure [...].
    if code == 0 and not failure:
        return True, ""
    if failure:
        return False, failure.group(0)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return False, lines[-1] if lines else f"pm install exited {code}"
