"""
Wakes and unlocks the Android test device's screen before a test, and can keep it on while the device charges.
"""

from __future__ import annotations

import logging
import re
import time

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

WAKEFULNESS_RE = re.compile(r"mWakefulness=(\w+)")
KEYGUARD_RE = re.compile(r"isKeyguardShowing=(true|false)")
POLL_SECONDS = 0.5


class DeviceLockedError(RuntimeError):
    """The lock screen stayed up after waking the device, which happens when a PIN, password or pattern is set."""


# Report whether the screen is on, or None when dumpsys does not say.
def screen_awake(adb: AdbClient) -> bool | None:
    code, out, _ = adb.run(["shell", "dumpsys", "power"])
    match = WAKEFULNESS_RE.search(out or "") if code == 0 else None
    return match.group(1) == "Awake" if match else None


# Report whether the lock screen is showing, or None when dumpsys does not say.
def keyguard_showing(adb: AdbClient) -> bool | None:
    code, out, _ = adb.run(["shell", "dumpsys", "window"])
    match = KEYGUARD_RE.search(out or "") if code == 0 else None
    return match.group(1) == "true" if match else None


# Turn the screen on and dismiss a lock screen that has no PIN, password or pattern, so a test can drive the UI.
def wake_and_unlock(adb: AdbClient, timeout: float = 5) -> None:
    awake = screen_awake(adb)
    if awake is not True:
        logger.debug("android screen: screen awake=%s; waking it", awake)
        adb.run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    showing = keyguard_showing(adb)
    if showing is None:
        logger.debug("android screen: lock screen state not reported; continuing")
        return
    if not showing:
        logger.debug("android screen: already unlocked")
        return
    adb.run(["shell", "wm", "dismiss-keyguard"])
    # The lock screen takes a moment to go away after the dismiss request.
    deadline = time.monotonic() + timeout
    while keyguard_showing(adb):
        if time.monotonic() >= deadline:
            raise DeviceLockedError(
                "the device is still locked after waking it; remove its PIN, password or pattern, or unlock it by hand"
            )
        time.sleep(POLL_SECONDS)
    logger.debug("android screen: lock screen dismissed")


# Keep the screen on whenever the device is charging, from any power source; return whether the device accepted it.
def keep_screen_on_while_charging(adb: AdbClient) -> bool:
    code, out, err = adb.run(["shell", "svc", "power", "stayon", "true"])
    logger.debug("android screen: svc power stayon true exit %s (out=%r err=%r)", code, (out or "")[:200], (err or "")[:200])
    return code == 0
