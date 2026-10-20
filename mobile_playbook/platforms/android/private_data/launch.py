"""
Opens an app so it writes its data, then stops it before its private data is copied.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)


# Open the app so it writes its data, then stop it so its files do not change while they are archived.
def launch_and_stop(adb: AdbClient, package: str, launch_wait: float) -> dict[str, Any]:
    code, out, err = adb.run(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
    launched = code == 0 and "monkey aborted" not in (out or "").lower()
    logger.debug("android private data: %s monkey launch exit %s launched=%s", package, code, launched)
    if launched:
        time.sleep(launch_wait)
    adb.run(["shell", "am", "force-stop", package])
    if launched:
        return {"launched": True}
    return {"launched": False, "detail": (err or out or f"monkey exited {code}").strip()[:500]}
