"""
Installs a configured Android app on the test device with the configured install method while holding the Android device.
"""

from __future__ import annotations

import logging
from functools import partial

from fastapi import HTTPException

from mobile_playbook.api import config_editing
from mobile_playbook.api.config_editing.android_apps import android_package_name
from mobile_playbook.api.config_editing.shared import ENTRY_FILES
from mobile_playbook.api.run_registry import registry
from mobile_playbook.common.storage_paths import android_work_dir, config_path
from mobile_playbook.orchestration.appium_server import ensure_appium_running
from mobile_playbook.platforms.android.acquisition.store import app_store_for
from mobile_playbook.platforms.android.baseline_devices import uses_baselines
from mobile_playbook.platforms.android.device_roles import NON_ROOTED
from mobile_playbook.platforms.android.models import PHYSICAL_PROVIDER
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.device.client import open_device
from mobile_playbook.platforms.android.device.phone_lock import PhoneBusy, acquire_phone_lock
from mobile_playbook.platforms.android.device.sdk import appium_environment
from mobile_playbook.platforms.corellium.client import CorelliumError

logger = logging.getLogger(__name__)


# Install the app if it is missing and return the outcome; 409 while another run holds the Android device. With
# Corellium baselines the role device (non_rooted unless rooted is asked for) is restored, installed on and shut down.
def install_app(platform: str, app_id: str, role: str | None = None) -> dict:
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

    baselines = uses_baselines(config)
    if role is not None and not baselines:
        raise HTTPException(
            status_code=422,
            detail={"blocker_code": "invalid_run_request", "message": "role needs device.corellium.roles in the Android config."},
        )

    if not registry.try_claim_platform("android"):
        logger.debug("api: Android device busy; install of %s refused.", app_id)
        raise HTTPException(
            status_code=409,
            detail={"blocker_code": "platform_busy", "message": "Another run is already using the test device."},
        )
    store = app_store_for(config)
    try:
        work = android_work_dir() / store.key
        if baselines:
            configured = next(item for item in config.apps if item.id == app_id)
            try:
                [(_, outcome)] = AndroidPlatformRunner().install_on_baseline(
                    config, [configured], role or NON_ROOTED, work / "diagnostics"
                )
            except PhoneBusy as exc:
                logger.info("api: install of %s refused: %s", app_id, exc)
                raise HTTPException(
                    status_code=409,
                    detail={"blocker_code": "platform_busy", "message": "Another run is already using the test device."},
                ) from exc
        elif config.device.provider == PHYSICAL_PROVIDER:
            try:
                phone = acquire_phone_lock(f"Android install of {app_id}")
            except PhoneBusy as exc:
                logger.info("api: install of %s refused: %s", app_id, exc)
                raise HTTPException(
                    status_code=409,
                    detail={"blocker_code": "platform_busy", "message": "Another run is already using the test device."},
                ) from exc
            try:
                outcome = _install_on_device(config, store, app_id, package, work)
            finally:
                phone.release()
        else:
            outcome = _install_on_device(config, store, app_id, package, work)
    finally:
        registry.release_platform("android")
    logger.info(
        "%s install of %s (%s): %s %s version %s build %s.",
        store.name,
        app_id,
        package,
        outcome.status,
        outcome.blocker_code or "",
        outcome.version_name or "unknown",
        outcome.version_code or "unknown",
    )
    result = outcome.to_dict()
    result.pop("diagnostics", None)
    return result



# Install the app on the configured physical or single-instance Corellium device, starting Appium when allowed.
def _install_on_device(config, store, app_id: str, package: str, work):
    appium = ensure_appium_running(
        config.device.appium_server_url,
        getattr(config.device, "appium_auto_start", None),
        work / "appium.log",
        environment=partial(appium_environment, config.device.adb_path),
    )
    if appium.status in ("FAILED", "DISABLED"):
        return store.failure("error", f"Appium is not available: {appium.error or appium.status}")
    try:
        client = open_device(config)
    except CorelliumError as exc:
        logger.warning("Corellium device unavailable for install of %s: %s", app_id, exc)
        return store.failure("error", f"The Corellium test device is not available: {exc}")
    return store.install_and_read_version(client.adb, client.make_driver, package, work / "diagnostics")
