"""
Android device client wrapping adb and Appium driver creation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.appium_driver import create_appium_driver
from mobile_playbook.platforms.android.device.corellium import CorelliumConnection, connect_corellium, stop_corellium
from mobile_playbook.platforms.android.models import CORELLIUM_PROVIDER
from mobile_playbook.platforms.corellium.client import CorelliumError
from mobile_playbook.platforms.corellium.profile import is_baseline_profile

logger = logging.getLogger(__name__)


# Return a device client for the configured device: the physical phone's serial, or the selected Corellium instance.
def open_device(config, *, start: bool = True) -> AndroidDeviceClient:
    device = config.device
    if device.provider == CORELLIUM_PROVIDER and is_baseline_profile(device.corellium):
        raise CorelliumError("device.corellium.roles selects role devices that each run restores from its baseline; "
                             "they are prepared by the run or install, never opened as one device.")
    if device.provider == CORELLIUM_PROVIDER:
        # start=False only reads the instance's state, for checks that must not start a virtual device.
        corellium = connect_corellium(device.adb_path, device.corellium, start=start)
        return AndroidDeviceClient(config, AdbClient(device.adb_path, corellium.serial), corellium=corellium)
    return AndroidDeviceClient(config, AdbClient(device.adb_path, device.adb_serial))


class AndroidDeviceClient:
    """Device handle that Android risk checks use for adb access and Appium sessions."""

    # Store the device config, an adb client (a default one when omitted) and the Corellium instance behind it, if any.
    def __init__(self, config=None, adb: AdbClient | None = None, corellium: CorelliumConnection | None = None):
        self.config = config
        self.adb = adb or AdbClient()
        self.corellium = corellium

    # Return this client; adb needs no explicit connection.
    def connect(self):
        logger.debug("android device: connected (adb serial=%s)", getattr(self.adb, "serial", None))
        return self

    # Create an Appium driver for the configured server and adb serial, optionally targeting an app package and activity
    # and adding extra capabilities.
    def make_driver(self, app_package: str | None = None, app_activity: str | None = None, extra_capabilities: Mapping[str, Any] | None = None):
        if self.config is None:
            logger.debug("android device: make_driver called without a device config")
            raise RuntimeError("Android device config is not available")
        # The adb client's serial, so Appium uses the same connection as adb, including a resolved `adb_serial: wifi`.
        udid = self.adb.target_serial()
        logger.debug("android device: making driver (app_package=%s, app_activity=%s, udid=%s)", app_package, app_activity, udid)
        return create_appium_driver(self.config.device.appium_server_url, app_package, app_activity, udid=udid, extra_capabilities=extra_capabilities)

    # No-op, since the client holds no session to release.
    def quit(self) -> None:
        logger.debug("android device: quit")
        return None

    # Release the device once testing is done, powering off its Corellium instance when auto_stop is set.
    def release(self) -> None:
        self.quit()
        if self.corellium is not None:
            stop_corellium(self.corellium)
