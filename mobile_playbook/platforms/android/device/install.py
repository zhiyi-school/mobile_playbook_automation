"""
Installs one app's APKs on the device by copying them over adb and installing them with the package manager.
"""

from __future__ import annotations

import logging
import re
import time
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
# ART's compiler and its planner; the package manager runs one job at a time, so an install waits behind them.
DEXOPT_PROCESS_RE = re.compile(r"^(?:\S*/)?(dex2oat\w*|dexoptanalyzer)(?:\s+(.*))?$")
DEXOPT_LOCATION_RE = re.compile(r"--(?:zip|dex)-location=(\S+)")
# A VM restored from a Google apps snapshot has compiled Play services for more than six minutes.
DEXOPT_WAIT_SECONDS = 900.0
DEXOPT_POLL_SECONDS = 5.0


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


# Return the app compilations running on the device, each as its process and the APK it compiles when shown; empty
# when none run or the process list cannot be read.
def running_dexopt(adb: AdbClient) -> list[str]:
    code, out, _ = adb.run(["shell", "ps", "-A", "-o", "ARGS"])
    if code != 0:
        return []
    running = []
    for line in out.splitlines():
        match = DEXOPT_PROCESS_RE.match(line.strip())
        if match:
            location = DEXOPT_LOCATION_RE.search(match.group(2) or "")
            running.append(f"{match.group(1)} on {location.group(1)}" if location else match.group(1))
    return running


# Wait until no app compilation runs on the device, within `timeout` seconds polled every `poll` seconds, and return
# the compilations still running when the budget ran out, or an empty list once the device is idle.
def wait_for_dexopt(adb: AdbClient, *, timeout: float | None = None, poll: float | None = None) -> list[str]:
    timeout = DEXOPT_WAIT_SECONDS if timeout is None else timeout
    poll = DEXOPT_POLL_SECONDS if poll is None else poll
    started = time.monotonic()
    running = running_dexopt(adb)
    if not running:
        return []
    logger.info("Waiting up to %.0f s for the device to finish compiling before the install: %s", timeout, "; ".join(running))
    idle = 0
    while time.monotonic() - started < timeout:
        time.sleep(poll)
        current = running_dexopt(adb)
        if current:
            if current != running:
                logger.info("The device is still compiling after %.0f s: %s", time.monotonic() - started, "; ".join(current))
            running, idle = current, 0
            continue
        idle += 1
        # dex2oat runs once per APK, so one idle sample between two compilations does not yet mean the work is done.
        if idle == 2:
            break
    if idle:
        logger.info("The device finished compiling after %.0f s.", time.monotonic() - started)
        return []
    return running
