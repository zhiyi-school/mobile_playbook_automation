"""
Checks that Burp decrypts the device's HTTPS before an app is tested, by opening a known HTTPS page in the device's
browser and looking for it in Burp's capture file, so a broken setup is not mistaken for an app resisting interception.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mobile_playbook.platforms.android.traffic_interception.device_proxy import TrafficInterceptionSetupError
from mobile_playbook.platforms.traffic_interception.burp_capture import poll_capture, snapshot_capture

logger = logging.getLogger(__name__)

CHECK_URL = "https://example.com"
# Chrome trusts user CA certificates, unlike most apps, so it shows whether the proxy and the CA work at all.
BROWSER_PACKAGE = "com.android.chrome"
POLL_SECONDS = 1.0


# Open the check URL in the browser and wait for a decrypted HTTPS exchange with its host in the capture file, raising
# INTERCEPTION_NOT_VERIFIED when none arrives within the timeout. The browser is stopped afterwards either way.
def check_interception(adb, capture_path: Path, *, url: str = CHECK_URL, browser_package: str = BROWSER_PACKAGE,
                       timeout: float = 20) -> dict[str, Any]:
    host = urlsplit(url).hostname or url
    cursor = snapshot_capture(capture_path)
    state: dict[str, Any] = {"url": url, "browser_package": browser_package, "capture_path": str(cursor.path)}
    try:
        code, out, err = adb.run(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", browser_package])
        if code != 0 or "Error" in (out or ""):
            raise TrafficInterceptionSetupError("INTERCEPTION_NOT_VERIFIED", f"opening {url} in {browser_package} failed: {err or out}", state)
        deadline = time.monotonic() + timeout
        while True:
            cursor, observed = poll_capture(cursor, [host])
            if observed.matched_entries:
                state.update({"status": "VERIFIED", "host": host})
                logger.debug("android traffic check: Burp decrypted %s", host)
                return state
            if observed.source_changed or observed.source_unavailable:
                raise TrafficInterceptionSetupError("INTERCEPTION_NOT_VERIFIED", f"Burp's capture file {cursor.path} was replaced or became unreadable", state)
            if time.monotonic() >= deadline:
                raise TrafficInterceptionSetupError(
                    "INTERCEPTION_NOT_VERIFIED",
                    f"no decrypted HTTPS for {host} reached {cursor.path} within {timeout:g}s; check that Burp's listener is "
                    f"bound to all interfaces, the capture extension is loaded and {browser_package} is past its first-run screens",
                    state,
                )
            time.sleep(POLL_SECONDS)
    finally:
        adb.run(["shell", "am", "force-stop", browser_package])
