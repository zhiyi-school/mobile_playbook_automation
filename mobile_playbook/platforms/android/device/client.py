"""
Android device client wrapping adb and Appium driver creation.
"""

from __future__ import annotations

import logging

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.appium_driver import create_appium_driver

logger = logging.getLogger(__name__)


class AndroidDeviceClient:
    """Device handle that Android risk checks use for adb access and Appium sessions."""

    # Store the device config and an adb client, creating a default one when omitted.
    def __init__(self, config=None, adb: AdbClient | None = None):
        self.config = config
        self.adb = adb or AdbClient()

    # Return this client; adb needs no explicit connection.
    def connect(self):
        logger.debug("android device: connected (adb serial=%s)", getattr(self.adb, "serial", None))
        return self

    # Create an Appium driver for the configured server, optionally targeting an app package and activity.
    def make_driver(self, app_package: str | None = None, app_activity: str | None = None):
        if self.config is None:
            logger.debug("android device: make_driver called without a device config")
            raise RuntimeError("Android device config is not available")
        logger.debug("android device: making driver (app_package=%s, app_activity=%s)", app_package, app_activity)
        return create_appium_driver(self.config.device.appium_server_url, app_package, app_activity)

    # No-op, since the client holds no session to release.
    def quit(self) -> None:
        logger.debug("android device: quit")
        return None
