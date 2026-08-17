from __future__ import annotations

from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.appium_driver import create_appium_driver


class AndroidDeviceClient:
    def __init__(self, config=None, adb: AdbClient | None = None):
        self.config = config
        self.adb = adb or AdbClient()

    def connect(self):
        return self

    def make_driver(self, app_package: str | None = None, app_activity: str | None = None):
        if self.config is None:
            raise RuntimeError("Android device config is not available")
        return create_appium_driver(self.config.device.appium_server_url, app_package, app_activity)

    def quit(self) -> None:
        return None
