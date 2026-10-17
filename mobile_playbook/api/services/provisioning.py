"""
Poll-safe readiness checks for configured apps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mobile_playbook.api import config_editing
from mobile_playbook.api.config_editing.shared import ENTRY_FILES
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient, open_device
from mobile_playbook.platforms.android.device.permissions import is_installed as android_is_installed
from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk
from mobile_playbook.platforms.android.baseline_devices import role_readiness, uses_baselines
from mobile_playbook.platforms.android.device_roles import plan_risk_roles, required_roles
from mobile_playbook.api.run_registry import registry
from mobile_playbook.common.storage_paths import config_path, ios_intake_dir
from mobile_playbook.orchestration.platform_runner import iter_enabled_tests, requires_device
from mobile_playbook.platforms.android.risks import get_risk as get_android_risk
from mobile_playbook.platforms.ios.acquisition.intake_ipa import resolve_intake_ipa
from mobile_playbook.platforms.ios.config import load_config as load_ios_config
from mobile_playbook.platforms.ios.preflight import connected_device_udids
from mobile_playbook.platforms.ios.risks import get_risk as get_ios_risk
from mobile_playbook.platforms.corellium.client import CorelliumError

logger = logging.getLogger(__name__)

_LOCAL_IPA_SOURCES = {"local_ipa", "ci_artifact", "vendor_ipa", "xcode_archive_export"}
_DEVICE_PROBE_TIMEOUT_SECONDS = 5.0

Stage = dict[str, Any]


# Build one setup stage dict.
def _stage(stage_id: str, label: str, state: str, detail: str | None = None) -> Stage:
    return {"id": stage_id, "label": label, "state": state, "detail": detail}


# Return the artifact's configured intake directory, or the default iOS one.
def _intake_dir(artifact: dict) -> Path:
    configured = (artifact or {}).get("intake_dir")
    return Path(configured).expanduser() if configured else ios_intake_dir()


# Summarize stages as failed, ready or pending.
def _overall_status(stages: list[Stage]) -> str:
    states = {stage["state"] for stage in stages}
    if "failed" in states:
        return "failed"
    if states <= {"done", "unknown"}:
        return "ready"
    return "pending"


# Return the detail of the first failed stage, or None.
def _first_failure(stages: list[Stage]) -> str | None:
    for stage in stages:
        if stage["state"] == "failed":
            return stage["detail"]
    return None


# Return the state, detail and resolved bundle id of an iOS app's test configuration.
def _ios_configuration(app: dict) -> tuple[str, str | None, str | None]:
    artifact = app.get("artifact") or {}
    source = artifact.get("source") or ""
    bundle_id = app.get("bundle_id") or artifact.get("expected_bundle_id") or None
    logger.debug(
        "api: checking iOS configuration for app %r (source=%r, bundle_id=%r).", app.get("id"), source, bundle_id
    )

    if source == "intake_ipa":
        resolution = resolve_intake_ipa(
            bundle_id=bundle_id,
            app_name=app.get("name"),
            intake_dir=_intake_dir(artifact),
        )
        if resolution.ambiguous:
            logger.warning(
                "Ambiguous intake builds for app %r: %s",
                app.get("id"),
                sorted({build.bundle_id for build in resolution.candidates}),
            )
            return "failed", "More than one app build matches this app.", None
        if resolution.match is None:
            logger.debug("api: no intake build matches app %r yet.", app.get("id"))
            return "in_progress", "Waiting for the app build to be provided.", None
        logger.debug("api: intake build for app %r resolved to bundle %s.", app.get("id"), resolution.match.bundle_id)
        return "done", None, resolution.match.bundle_id

    if source in _LOCAL_IPA_SOURCES:
        ipa = artifact.get("ipa") or artifact.get("path") or ""
        if not ipa or not Path(ipa).expanduser().exists():
            logger.warning("App %r has no readable build at its configured path.", app.get("id"))
            return "in_progress", "Waiting for the app build to be provided.", bundle_id
        logger.debug("api: local build for app %r found at %s.", app.get("id"), ipa)
        return "done", None, bundle_id

    if source == "installed_app_reference":
        if not bundle_id:
            logger.debug("api: installed-app reference for %r has no bundle id.", app.get("id"))
            return "failed", "This app's test configuration is incomplete.", None
        logger.debug("api: installed-app reference for %r uses bundle %s.", app.get("id"), bundle_id)
        return "done", None, bundle_id

    logger.warning("App %r has an unsupported artifact source %r.", app.get("id"), source)
    return "failed", "This app's test configuration is incomplete.", bundle_id


# Return the Android config, or None when it cannot be read.
def _android_config():
    try:
        return load_android_config(config_path(ENTRY_FILES["android"]), dry_run=True)
    except (AndroidConfigError, OSError) as exc:
        logger.debug("api: Android config unavailable (%s).", type(exc).__name__)
        return None


# Report whether the Android config prepares Corellium role devices from baselines inside each run.
def _android_uses_baselines() -> bool:
    config = _android_config()
    return config is not None and uses_baselines(config)


# Open the configured Android device without starting it: a default client when the config is unavailable, or None
# when the configured Corellium device cannot be looked up.
def _android_device_client() -> AndroidDeviceClient | None:
    config = _android_config()
    if config is None:
        return AndroidDeviceClient(None, AdbClient())
    try:
        # Readiness is polled, so it only reads a Corellium instance's state and never powers it on.
        return open_device(config, start=False)
    except CorelliumError as exc:
        logger.warning("The Corellium test device could not be looked up: %s", exc)
        return None


# Return connected, standby, no_device, ambiguous or unreachable for the Android device, never the serial.
def _android_device(device: AndroidDeviceClient | None) -> str:
    if device is None:
        return "unreachable"
    corellium = device.corellium
    if corellium is not None and corellium.instance.state != "on":
        # A run starts an off or paused instance itself when auto_start allows, and waits for one already starting.
        startable = corellium.instance.state not in ("off", "paused") or device.config.device.corellium.get("auto_start", True)
        logger.debug("api: Corellium instance is %s (startable=%s).", corellium.instance.state, startable)
        return "standby" if startable else "no_device"
    adb = device.adb
    if not adb.is_available():
        logger.debug("api: adb is not available; Android device unreachable.")
        return "unreachable"
    state_code, state_out, state_err = adb.run(["get-state"])
    logger.debug("api: adb get-state exited %s (state=%r).", state_code, state_out.strip())
    if state_code == 0 and state_out.strip() == "device":
        return "connected"
    # Without a serial, adb refuses to choose between devices, including one phone attached over USB and Wi-Fi.
    if "more than one device" in (state_err or state_out or ""):
        logger.warning(
            "Several Android devices are connected and device.adb_serial is not set; run 'adb devices' and set "
            "device.adb_serial in the Android config to the one to test, or to wifi for a phone on USB and Wi-Fi."
        )
        return "ambiguous"
    return "no_device"


# Return the state, detail and package name of an Android app's test configuration.
def _android_configuration(app: dict) -> tuple[str, str | None, str | None]:
    package_name = app.get("package_name") or None
    logger.debug("api: checking Android configuration for app %r (package=%r).", app.get("id"), package_name)
    if not package_name:
        return "failed", "This app's test configuration is incomplete.", None
    if _android_uses_baselines():
        # Each run installs the intake APK on its restored devices, so the build on disk is what must be ready.
        if find_intake_apk(package_name) is None:
            logger.debug("api: no intake APK of %s for baseline installs yet.", package_name)
            return "in_progress", "Waiting for the app build to be provided.", package_name
        return "done", None, package_name

    client = _android_device_client()
    device = _android_device(client)
    logger.debug("api: Android device state for app %r is %s.", app.get("id"), device)
    if device == "unreachable":
        return "unknown", "The test device could not be reached.", package_name
    if device == "no_device":
        return "unknown", "No test device is currently connected.", package_name
    if device == "ambiguous":
        return "unknown", AMBIGUOUS_DEVICE_DETAIL, package_name
    if device == "standby":
        return "unknown", "The test device is off; it is started when testing begins.", package_name

    if android_is_installed(client.adb, package_name):
        logger.debug("api: package %s is installed on the device.", package_name)
        return "done", None, package_name
    logger.debug("api: package %s is not installed on the device yet.", package_name)
    return "in_progress", "The app is not installed on the test device yet; it is installed before testing.", package_name


# Count the app's enabled risks.
def _enabled_risk_count(app: dict) -> int:
    risks = app.get("risks") or {}
    return len([r for r, settings in risks.items() if (settings or {}).get("enabled")])


# Build the three independent setup stages and the resolved app id.
def _setup_stages(platform: str, app: dict | None) -> tuple[list[Stage], str | None]:
    registered = app is not None
    stages = [
        _stage(
            "app_registered",
            "Server environment prepared" if registered else "Server environment is being prepared",
            "done" if registered else "in_progress",
        ),
        _stage("service_online", "Assessment service is running", "done"),
    ]

    if not registered:
        logger.debug("api: %s app is not registered yet; configuration stage pending.", platform)
        stages.append(
            _stage("configuration_applied", "Configuration is being applied", "pending")
        )
        return stages, None

    if platform == "ios":
        state, detail, resolved_id = _ios_configuration(app)
    else:
        state, detail, resolved_id = _android_configuration(app)

    if state == "done" and _enabled_risk_count(app) == 0:
        logger.warning("App %r has no enabled risks.", app.get("id"))
        state, detail = "failed", "No security tests are enabled for this app."

    stages.append(
        _stage(
            "configuration_applied",
            "Configuration is being applied"
            if state in ("in_progress", "pending")
            else "Configuration applied",
            state,
            detail,
        )
    )
    logger.debug("api: setup stages for %s app %r: %s.", platform, app.get("id"), [s["state"] for s in stages])
    return stages, resolved_id


BLOCKERS: dict[str, tuple[bool, str]] = {
    "configuration_incomplete": (
        False,
        "This app's test configuration needs attention before it can be tested.",
    ),
    "no_tests_enabled": (False, "No security tests are enabled for this app."),
    "configuration_pending": (True, "The test configuration is still being applied."),
    "app_build_missing": (True, "Waiting for the app build to be provided."),
    "app_not_installed": (
        True,
        "The app is not installed on the test device yet; it is installed before testing.",
    ),
    "no_device": (True, "Waiting for a compatible test device to become available."),
    "device_unreachable": (True, "The test device could not be reached."),
    "platform_busy": (True, "Another run is already using the test device."),
}

# Shown when several devices are attached and none is chosen; it names no device, since dashboards must not.
AMBIGUOUS_DEVICE_DETAIL = (
    "More than one test device is connected, so none could be chosen. The automation host must be set to use one of them."
)

_CONFIG_BLOCKER_BY_STAGE_STATE = {
    "failed": "configuration_incomplete",
    "in_progress": "app_build_missing",
    "pending": "configuration_pending",
}


# Return the device readiness, blocker and detail of the Corellium role devices an Android app's enabled risks need,
# from read-only lookups; a role the app does not need is never checked.
def _baseline_device(app: dict) -> tuple[bool, str | None, str | None]:
    config = _android_config()
    enabled = {risk for risk, settings in (app.get("risks") or {}).items() if (settings or {}).get("enabled")}
    try:
        roles = required_roles(plan_risk_roles(iter_enabled_tests(config, enabled, {app.get("id")}, get_android_risk), get_android_risk))
    except Exception:
        logger.debug("api: device roles for app %r could not be planned.", app.get("id"), exc_info=True)
        return False, "configuration_incomplete", None
    for role in role_readiness(config, roles):
        logger.debug("api: %s device for app %r is %s (%s).", role.role, app.get("id"), role.state, role.detail)
        kind = "rooted" if role.role == "rooted" else "non-rooted"
        if role.state == "unconfigured":
            return False, "configuration_incomplete", None
        if role.state == "unreachable":
            return False, "device_unreachable", None
        if role.state == "busy":
            return False, "platform_busy", None
        if role.state == "unavailable":
            return False, "no_device", f"Waiting for a compatible {kind} test device to become available."
    return True, None, None


# Return whether the configured iOS device is attached, treating an empty probe as ready.
def _ios_device_ready() -> bool:
    try:
        config = load_ios_config(config_path(ENTRY_FILES["ios"]), dry_run=True)
    except Exception:
        logger.debug("api: iOS config unreadable for device probe; treating device as ready.", exc_info=True)
        return True
    udid = getattr(getattr(config, "device", None), "udid", "") or ""
    if not udid:
        logger.debug("api: no iOS device udid configured; treating device as ready.")
        return True
    connected = connected_device_udids(timeout=_DEVICE_PROBE_TIMEOUT_SECONDS)
    logger.debug(
        "api: iOS device probe saw %d device(s); configured device present=%s.", len(connected), udid in connected
    )
    # An empty probe means xctrace is unusable, not that no device is attached; the run fails fast instead.
    return not connected or udid in connected


# Return whether the app's enabled risks need a device, assuming yes when unsure.
def _device_required(platform: str, app: dict | None) -> bool:
    if app is None:
        logger.debug("api: %s app unknown; assuming a device is required.", platform)
        return True
    enabled = [risk for risk, settings in (app.get("risks") or {}).items() if (settings or {}).get("enabled")]
    if not enabled:
        logger.debug("api: app %r has no enabled risks; assuming a device is required.", app.get("id"))
        return True
    logger.debug("api: checking device requirement for app %r with risks %s.", app.get("id"), sorted(enabled))
    try:
        if platform == "ios":
            config = load_ios_config(config_path(ENTRY_FILES["ios"]), dry_run=True)
            return requires_device(config, set(enabled), {app.get("id")}, get_ios_risk)
        config = load_android_config(config_path(ENTRY_FILES["android"]), dry_run=True)
        return requires_device(config, set(enabled), {app.get("id")}, get_android_risk)
    except Exception:
        logger.debug(
            "api: device requirement check failed for app %r; assuming required.", app.get("id"), exc_info=True
        )
        return True


# Compute execution readiness and the blocking reason, separately from configuration readiness.
def _readiness(platform: str, app: dict | None, stages: list[Stage]) -> dict:
    config_stage = next((s for s in stages if s["id"] == "configuration_applied"), None)
    config_state = (config_stage or {}).get("state", "pending")

    configuration_ready = config_state in ("done", "unknown")
    config_blocker = (
        None if configuration_ready else _CONFIG_BLOCKER_BY_STAGE_STATE.get(config_state, "configuration_incomplete")
    )
    if app is not None and _enabled_risk_count(app) == 0:
        configuration_ready, config_blocker = False, "no_tests_enabled"
    baselines = platform == "android" and _android_uses_baselines()
    # An Android app in progress is one not yet installed, which the worker installs; with baselines the run installs
    # the intake APK itself, so a missing build stays a missing build.
    if platform == "android" and config_blocker == "app_build_missing" and not baselines:
        config_blocker = "app_not_installed"

    device_needed = _device_required(platform, app)
    device_detail = None
    if not device_needed:
        device_ready, device_blocker = True, None
    elif baselines and app is not None:
        device_ready, device_blocker, device_detail = _baseline_device(app)
    elif platform == "android":
        state = _android_device(_android_device_client())
        device_ready = state in ("connected", "standby")
        device_blocker = None if device_ready else ("device_unreachable" if state == "unreachable" else "no_device")
        # Several devices keep the no_device code, so consumers are unchanged, with a detail saying why.
        device_detail = AMBIGUOUS_DEVICE_DETAIL if state == "ambiguous" else None
    else:
        device_ready = _ios_device_ready()
        device_blocker = None if device_ready else "no_device"

    # Android installs are only confirmable with a device attached, so the device blocker wins.
    if device_blocker and config_blocker in ("app_build_missing", None) and config_state == "unknown":
        config_blocker = None

    platform_available = not registry.is_platform_busy(platform)

    blocker = config_blocker or device_blocker or (None if platform_available else "platform_busy")
    runnable = configuration_ready and device_ready and platform_available and blocker is None
    retryable, detail = BLOCKERS.get(blocker, (True, None)) if blocker else (True, None)
    if blocker == device_blocker and device_detail:
        detail = device_detail
    logger.debug(
        "api: %s readiness config_ready=%s device_needed=%s device_ready=%s platform_available=%s blocker=%s.",
        platform,
        configuration_ready,
        device_needed,
        device_ready,
        platform_available,
        blocker,
    )
    return {
        "configuration_ready": configuration_ready,
        "device_required": device_needed,
        "device_ready": device_ready,
        "platform_available": platform_available,
        "runnable": runnable,
        "blocker_code": blocker,
        "retryable": retryable,
        "detail": detail,
    }


# Build the failed setup report for an app whose configuration cannot be used.
def _config_failure(platform: str, app_id: str, detail: str) -> dict:
    return {
        "app_id": app_id,
        "platform": platform,
        "bundle_id": None,
        "status": "failed",
        "stages": [_stage("configuration_applied", "Configuration applied", "failed", detail)],
        "error": detail,
        "configuration_ready": False,
        "device_required": True,
        "device_ready": False,
        "platform_available": True,
        "runnable": False,
        "blocker_code": "configuration_incomplete",
        "retryable": BLOCKERS["configuration_incomplete"][0],
        "detail": BLOCKERS["configuration_incomplete"][1],
    }


# Build the setup and readiness report for one app. See docs/api.md#is-an-app-ready-to-test.
def describe(platform: str, app_id: str) -> dict:
    detail = "The test configuration needs attention before this app can be tested."
    logger.debug("api: describing setup for %s app %r.", platform, app_id)
    try:
        apps = config_editing.list_ios_apps() if platform == "ios" else config_editing.list_android_apps()
    except Exception as exc:
        logger.debug("api: listing %s apps failed.", platform, exc_info=True)
        logger.error("Config for platform %s could not be read: %s", platform, exc)
        return _config_failure(platform, app_id, detail)

    errors = config_editing.app_config_errors(platform)
    if errors.get(app_id) or errors.get(""):
        logger.error(
            "App %r cannot be tested: %s",
            app_id,
            "; ".join(errors.get(app_id, []) + errors.get("", [])),
        )
        return _config_failure(platform, app_id, detail)

    app = next((entry for entry in apps if entry.get("id") == app_id), None)
    logger.debug("api: %s app %r found in config=%s.", platform, app_id, app is not None)
    stages, bundle_id = _setup_stages(platform, app)
    status = _overall_status(stages)
    logger.debug("api: %s app %r setup status %s.", platform, app_id, status)
    return {
        "app_id": app_id,
        "platform": platform,
        "bundle_id": bundle_id,
        "status": status,
        "stages": stages,
        "error": _first_failure(stages) if status == "failed" else None,
        **_readiness(platform, app, stages),
    }
