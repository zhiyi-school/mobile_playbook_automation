"""Whether a configured app is ready to be tested. See docs/api.md#is-an-app-ready-to-test.

Stage text is deliberately free of paths, filenames, config field names and
identifiers: it is rendered directly in a dashboard. Specifics go to this
module's logger instead.
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
from mobile_playbook.platforms.ios.artifacts.intake_ipa import (
    DEFAULT_INTAKE_DIR,
    resolve_intake_ipa,
)

logger = logging.getLogger(__name__)

_LOCAL_IPA_SOURCES = {"local_ipa", "ci_artifact", "vendor_ipa", "xcode_archive_export"}

Stage = dict[str, Any]


def _stage(stage_id: str, label: str, state: str, detail: str | None = None) -> Stage:
    return {"id": stage_id, "label": label, "state": state, "detail": detail}


def _intake_dir(artifact: dict) -> Path:
    configured = (artifact or {}).get("intake_dir")
    return Path(configured).expanduser() if configured else DEFAULT_INTAKE_DIR


def _overall_status(stages: list[Stage]) -> str:
    states = {stage["state"] for stage in stages}
    if "failed" in states:
        return "failed"
    if states <= {"done", "unknown"}:
        return "ready"
    return "pending"


def _first_failure(stages: list[Stage]) -> str | None:
    for stage in stages:
        if stage["state"] == "failed":
            return stage["detail"]
    return None


def _ios_configuration(app: dict) -> tuple[str, str | None, str | None]:
    """(state, detail, resolved bundle id) for an iOS app's test configuration."""
    artifact = app.get("artifact") or {}
    source = artifact.get("source") or ""
    bundle_id = app.get("bundle_id") or artifact.get("expected_bundle_id") or None

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
            return "in_progress", "Waiting for the app build to be provided.", None
        return "done", None, resolution.match.bundle_id

    if source in _LOCAL_IPA_SOURCES:
        ipa = artifact.get("ipa") or artifact.get("path") or ""
        if not ipa or not Path(ipa).expanduser().exists():
            logger.warning("App %r has no readable build at its configured path.", app.get("id"))
            return "in_progress", "Waiting for the app build to be provided.", bundle_id
        return "done", None, bundle_id

    if source == "installed_app_reference":
        if not bundle_id:
            return "failed", "This app's test configuration is incomplete.", None
        return "done", None, bundle_id

    logger.warning("App %r has an unsupported artifact source %r.", app.get("id"), source)
    return "failed", "This app's test configuration is incomplete.", bundle_id


def _android_configuration(app: dict) -> tuple[str, str | None, str | None]:
    package_name = app.get("package_name") or None
    if not package_name:
        return "failed", "This app's test configuration is incomplete.", None

    try:
        config = load_android_config(config_editor.ENTRY_FILES["android"], dry_run=True)
        adb = AdbClient(adb_path=config.device.adb_path, serial=config.device.adb_serial)
    except (AndroidConfigError, OSError):
        adb = AdbClient()

    if not adb.is_available():
        return "unknown", "The test device could not be reached.", package_name

    # A missing device and a missing app both just exit non-zero.
    state_code, state_out, _ = adb.run(["get-state"])
    if state_code != 0 or state_out.strip() != "device":
        return "unknown", "No test device is currently connected.", package_name

    if android_is_installed(adb, package_name):
        return "done", None, package_name
    return "in_progress", "Waiting for the app to be installed on the test device.", package_name


def _enabled_risk_count(app: dict) -> int:
    risks = app.get("risks") or {}
    return len([r for r, settings in risks.items() if (settings or {}).get("enabled")])


def _setup_stages(platform: str, app: dict | None) -> tuple[list[Stage], str | None]:
    """The three setup stages, which complete independently of one another."""
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
    return stages, resolved_id


def _config_failure(platform: str, app_id: str, detail: str) -> dict:
    return {
        "app_id": app_id,
        "platform": platform,
        "bundle_id": None,
        "status": "failed",
        "stages": [_stage("configuration_applied", "Configuration applied", "failed", detail)],
        "error": detail,
    }


def describe(platform: str, app_id: str) -> dict:
    """Setup report for one app. See docs/api.md#is-an-app-ready-to-test."""
    detail = "The test configuration needs attention before this app can be tested."
    try:
        apps = config_editor.list_ios_apps() if platform == "ios" else config_editor.list_android_apps()
    except Exception as exc:
        logger.error("Config for platform %s could not be read: %s", platform, exc)
        return _config_failure(platform, app_id, detail)

    # Only this app's own problems stop it being reported on; another app's
    # broken entry must not make every app look unusable.
    errors = config_editor.app_config_errors(platform)
    if errors.get(app_id) or errors.get(""):
        logger.error(
            "App %r cannot be tested: %s",
            app_id,
            "; ".join(errors.get(app_id, []) + errors.get("", [])),
        )
        return _config_failure(platform, app_id, detail)

    app = next((entry for entry in apps if entry.get("id") == app_id), None)
    stages, bundle_id = _setup_stages(platform, app)
    status = _overall_status(stages)
    return {
        "app_id": app_id,
        "platform": platform,
        "bundle_id": bundle_id,
        "status": status,
        "stages": stages,
        "error": _first_failure(stages) if status == "failed" else None,
    }
