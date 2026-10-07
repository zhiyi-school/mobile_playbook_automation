"""
Installs a configured Android app on the test device from the configured app store while holding the Android device.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from mobile_playbook.api import config_editing
from mobile_playbook.api.config_editing.android_apps import android_package_name
from mobile_playbook.api.config_editing.shared import ENTRY_FILES
from mobile_playbook.api.run_registry import registry
from mobile_playbook.common.storage_paths import android_work_dir, config_path
from mobile_playbook.orchestration.appium_server import ensure_appium_running
from mobile_playbook.platforms.android.acquisition.store import WORK_DIR_NAME, failure, install_missing_app
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient

logger = logging.getLogger(__name__)


# Install the app if it is missing and return the outcome; 409 while another run holds the Android device.
def install_app(platform: str, app_id: str) -> dict:
    if platform != "android":
        raise HTTPException(status_code=422, detail="Only Android apps are installed by the automation host")
    app = next((entry for entry in config_editing.list_android_apps() if entry.get("id") == app_id), None)
    if app is None:
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
    package = android_package_name(app)
    if not package:
        raise HTTPException(
            status_code=422,
            detail={"blocker_code": "configuration_incomplete", "message": "This app has no package name configured."},
        )
    try:
        config = load_android_config(config_path(ENTRY_FILES["android"]), dry_run=True)
    except AndroidConfigError as exc:
        logger.debug("api: Android config invalid for install: %s", exc.errors)
        raise HTTPException(
            status_code=422,
            detail={"blocker_code": "configuration_incomplete", "message": "The Android configuration is invalid."},
        ) from exc

    if not registry.try_claim_platform("android"):
        logger.debug("api: Android device busy; install of %s refused.", app_id)
        raise HTTPException(
            status_code=409,
            detail={"blocker_code": "platform_busy", "message": "Another run is already using the test device."},
        )
    try:
        work = android_work_dir() / WORK_DIR_NAME
        appium = ensure_appium_running(
            config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), work / "appium.log"
        )
        if appium.status in ("FAILED", "DISABLED"):
            outcome = failure("error", f"Appium is not available: {appium.error or appium.status}")
        else:
            adb = AdbClient(config.device.adb_path, config.device.adb_serial)
            client = AndroidDeviceClient(config, adb)
            outcome = install_missing_app(adb, client.make_driver, package, work / "diagnostics")
    finally:
        registry.release_platform("android")
    logger.info("App store install of %s (%s): %s %s.", app_id, package, outcome.status, outcome.blocker_code or "")
    result = outcome.to_dict()
    result.pop("diagnostics", None)
    return result
