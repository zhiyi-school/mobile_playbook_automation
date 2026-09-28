from __future__ import annotations

import logging

from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.appium_driver import create_appium_driver

logger = logging.getLogger(__name__)


class AndroidDeviceClient:
    def __init__(self, config=None, adb: AdbClient | None = None):
        self.config = config
        self.adb = adb or AdbClient()

    def connect(self):
        logger.debug("android device: connected (adb serial=%s)", getattr(self.adb, "serial", None))
        return self

    def make_driver(self, app_package: str | None = None, app_activity: str | None = None):
        if self.config is None:
            logger.debug("android device: make_driver called without a device config")
            raise RuntimeError("Android device config is not available")
        logger.debug("android device: making driver (app_package=%s, app_activity=%s)", app_package, app_activity)
        return create_appium_driver(self.config.device.appium_server_url, app_package, app_activity)

    def quit(self) -> None:
        logger.debug("android device: quit")
        return None
