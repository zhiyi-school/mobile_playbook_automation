"""
Poll-safe readiness checks for configured apps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mobile_playbook.api import config_editor
from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.permissions import is_installed as android_is_installed
from mobile_playbook.api.job_registry import registry
from mobile_playbook.storage import ios_intake_dir
from mobile_playbook.orchestration.platform_runner import requires_device
from mobile_playbook.platforms.android.risks import get_risk as get_android_risk
from mobile_playbook.platforms.ios.artifacts.intake_ipa import resolve_intake_ipa
from mobile_playbook.platforms.ios.config import load_config as load_ios_config
from mobile_playbook.platforms.ios.preflight import connected_device_udids
from mobile_playbook.platforms.ios.risks import get_risk as get_ios_risk

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


# Build an adb client from the Android config, or a default client when it is unavailable.
def _android_adb() -> AdbClient:
    try:
        config = load_android_config(config_editor.ENTRY_FILES["android"], dry_run=True)
        return AdbClient(adb_path=config.device.adb_path, serial=config.device.adb_serial)
    except (AndroidConfigError, OSError) as exc:
        logger.debug("api: Android config unavailable for adb (%s); using the default client.", type(exc).__name__)
        return AdbClient()


# Return connected, no_device or unreachable for the Android device, never the serial.
def _android_device(adb: AdbClient) -> str:
    if not adb.is_available():
        logger.debug("api: adb is not available; Android device unreachable.")
        return "unreachable"
    state_code, state_out, _ = adb.run(["get-state"])
    logger.debug("api: adb get-state exited %s (state=%r).", state_code, state_out.strip())
    return "connected" if state_code == 0 and state_out.strip() == "device" else "no_device"


# Return the state, detail and package name of an Android app's test configuration.
def _android_configuration(app: dict) -> tuple[str, str | None, str | None]:
    package_name = app.get("package_name") or None
    logger.debug("api: checking Android configuration for app %r (package=%r).", app.get("id"), package_name)
    if not package_name:
        return "failed", "This app's test configuration is incomplete.", None

    adb = _android_adb()
    device = _android_device(adb)
    logger.debug("api: Android device state for app %r is %s.", app.get("id"), device)
    if device == "unreachable":
        return "unknown", "The test device could not be reached.", package_name
    if device == "no_device":
        return "unknown", "No test device is currently connected.", package_name

    if android_is_installed(adb, package_name):
        logger.debug("api: package %s is installed on the device.", package_name)
        return "done", None, package_name
    logger.debug("api: package %s is not installed on the device yet.", package_name)
    return "in_progress", "Waiting for the app to be installed on the test device.", package_name


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
    "app_not_installed": (True, "Waiting for the app to be installed on the test device."),
    "no_device": (True, "Waiting for a compatible test device to become available."),
    "device_unreachable": (True, "The test device could not be reached."),
    "platform_busy": (True, "Another run is already using the test device."),
}

_CONFIG_BLOCKER_BY_STAGE_STATE = {
    "failed": "configuration_incomplete",
    "in_progress": "app_build_missing",
    "pending": "configuration_pending",
}


# Return whether the configured iOS device is attached, treating an empty probe as ready.
def _ios_device_ready() -> bool:
    try:
        config = load_ios_config(config_editor.ENTRY_FILES["ios"], dry_run=True)
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
            config = load_ios_config(config_editor.ENTRY_FILES["ios"], dry_run=True)
            return requires_device(config, set(enabled), {app.get("id")}, get_ios_risk)
        config = load_android_config(config_editor.ENTRY_FILES["android"], dry_run=True)
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

    device_needed = _device_required(platform, app)
    if not device_needed:
        device_ready, device_blocker = True, None
    elif platform == "android":
        state = _android_device(_android_adb())
        device_ready = state == "connected"
        device_blocker = None if device_ready else ("device_unreachable" if state == "unreachable" else "no_device")
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
        apps = config_editor.list_ios_apps() if platform == "ios" else config_editor.list_android_apps()
    except Exception as exc:
        logger.debug("api: listing %s apps failed.", platform, exc_info=True)
        logger.error("Config for platform %s could not be read: %s", platform, exc)
        return _config_failure(platform, app_id, detail)

    errors = config_editor.app_config_errors(platform)
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
