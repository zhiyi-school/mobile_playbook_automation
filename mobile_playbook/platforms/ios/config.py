"""
Loads, parses and validates the iOS run configuration, inferring bundle IDs from IPAs where possible.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.common.storage_paths import ios_work_dir
import re
from typing import Any

from mobile_playbook.common.config_loader import load_yaml_config, merge_dicts
from mobile_playbook.platforms.ios.acquisition.registry import known_sources
from mobile_playbook.platforms.ios.ipa.plist import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import (
    AppConfig,
    CisoConfig,
    DeviceConfig,
    ExpectedBehaviorConfig,
    GlobalConfig,
    RunnerConfig,
)
from mobile_playbook.platforms.ios.risks.registry import get_risk, known_risks

logger = logging.getLogger(__name__)

LOCAL_IPA_SOURCES = {"local_ipa", "ci_artifact", "vendor_ipa", "xcode_archive_export"}

# Risk ID to the GlobalConfig field whose defaults the app's risks.<risk_id> entry overrides.
RISK_GLOBAL_SETTINGS_FIELD = {
    "ios-feature-01-risk-01": "ipa_static_analysis",
    "ios-feature-01-risk-02": "repackaging",
    "ios-feature-02-risk-01": "traffic_interception",
    "ios-feature-03-risk-01": "screen_capture",
    "ios-feature-04-risk-01": "keystroke_collection",
}


# Merge a risk's shared global defaults with the app's own risk settings.
def effective_risk_config(config: GlobalConfig, risk_id: str, risk_config: dict[str, Any] | None) -> dict[str, Any]:
    field_name = RISK_GLOBAL_SETTINGS_FIELD.get(risk_id)
    global_defaults = getattr(config, field_name, {}) if field_name else {}
    merged = merge_dicts(global_defaults or {}, risk_config or {})
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "ios config: effective config for %s from global %s: global keys %s, app keys %s, merged keys %s",
            risk_id,
            field_name,
            list(global_defaults or {}),
            list(risk_config or {}),
            list(merged),
        )
    return merged


class ConfigError(Exception):
    """Configuration failure carrying every validation error found."""

    # Store the errors and join them into the exception message.
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


# Turn a name into a lowercase underscore slug, defaulting to 'app'.
def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "app"


# Return a mapping value, recording an error when it is missing or empty.
def _require(mapping: dict[str, Any], key: str, label: str, errors: list[str]) -> Any:
    value = mapping.get(key)
    if value in (None, ""):
        errors.append(f"{label}.{key} is required")
    return value


# Load, parse and validate a YAML config file, raising ConfigError on any problem.
def load_config(path: Path, dry_run: bool = False) -> GlobalConfig:
    path = Path(path)
    logger.debug("ios config: loading %s (dry_run=%s)", path, dry_run)
    try:
        raw = load_yaml_config(path)
    except ValueError as exc:
        logger.debug("ios config: could not load %s: %s", path, exc, exc_info=True)
        raise ConfigError([str(exc)]) from exc
    logger.debug("ios config: loaded top-level sections %s", list(raw) if isinstance(raw, dict) else type(raw).__name__)
    config = parse_config(raw, path)
    validate_config(config, dry_run=dry_run)
    logger.debug("ios config: %s loaded and validated with %s app(s)", path, len(config.apps))
    return config


# Build a GlobalConfig from raw config data, applying defaults.
def parse_config(raw: dict[str, Any], config_path: Path | None = None) -> GlobalConfig:
    device_raw = raw.get("device") or {}
    runner_raw = raw.get("runner") or {}
    apps = []
    for app_raw in raw.get("apps") or []:
        behavior_raw = app_raw.get("expected_behavior") or {}
        behavior = ExpectedBehaviorConfig(
            app_state_must_be_foreground=behavior_raw.get("app_state_must_be_foreground", True),
            source_contains=list(behavior_raw.get("source_contains") or []),
            source_not_contains=list(behavior_raw.get("source_not_contains") or []),
            app_specific_check=behavior_raw.get("app_specific_check"),
        )
        bundle_id = app_raw.get("bundle_id", "")
        app_name = app_raw.get("name", "")
        cisos = [
            CisoConfig(name=c.get("name", ""), email=c.get("email", ""))
            for c in (app_raw.get("cisos") or [])
        ]
        apps.append(
            AppConfig(
                id=app_raw.get("id") or _slugify(app_name),
                name=app_name,
                bundle_id=bundle_id,
                test_bundle_id=app_raw.get("test_bundle_id") or bundle_id,
                artifact=app_raw.get("artifact") or {},
                expected_behavior=behavior,
                risks=app_raw.get("risks") or {},
                sector=app_raw.get("sector", ""),
                agency=app_raw.get("agency", ""),
                version=app_raw.get("version", ""),
                cisos=cisos,
            )
        )
        logger.debug(
            "ios config: parsed app %s bundle_id=%s test_bundle_id=%s source=%s risks=%s",
            apps[-1].id,
            apps[-1].bundle_id,
            apps[-1].test_bundle_id,
            apps[-1].artifact.get("source") if isinstance(apps[-1].artifact, dict) else None,
            list(apps[-1].risks) if isinstance(apps[-1].risks, dict) else None,
        )
    logger.debug(
        "ios config: parsed %s app(s); device udid=%s appium=%s",
        len(apps),
        device_raw.get("udid", ""),
        device_raw.get("appium_server_url", ""),
    )
    return GlobalConfig(
        device=DeviceConfig(
            udid=device_raw.get("udid", ""),
            team_id=device_raw.get("team_id", ""),
            appium_server_url=device_raw.get("appium_server_url", ""),
            platform_version=device_raw.get("platform_version"),
            xcode_signing_id=device_raw.get("xcode_signing_id", "Apple Development"),
            keep_wda=bool(device_raw.get("keep_wda", True)),
            show_xcode_log=bool(device_raw.get("show_xcode_log", False)),
            updated_wda_bundle_id=device_raw.get("updated_wda_bundle_id"),
            allow_provisioning_device_registration=bool(device_raw.get("allow_provisioning_device_registration", False)),
            appium_auto_start=device_raw.get("appium_auto_start") or {},
        ),
        runner=RunnerConfig(
            sequential=bool(runner_raw.get("sequential", True)),
            uninstall_after_each_test=bool(runner_raw.get("uninstall_after_each_test", True)),
            stop_on_first_failure=bool(runner_raw.get("stop_on_first_failure", False)),
            app_install_timeout_ms=int(runner_raw.get("app_install_timeout_ms", 480000)),
            launch_wait_seconds=int(runner_raw.get("launch_wait_seconds", 5)),
            work_dir=Path(runner_raw.get("work_dir") or ios_work_dir()),
            permission_alerts=runner_raw.get("permission_alerts") or {},
        ),
        apps=apps,
        ipa_static_analysis=raw.get("ipa_static_analysis") or {},
        keystroke_collection=raw.get("keystroke_collection") or {},
        traffic_interception=raw.get("traffic_interception") or {},
        repackaging=raw.get("repackaging") or {},
        screen_capture=raw.get("screen_capture") or {},
        config_path=config_path,
    )


# Raise ConfigError when the config has any problems.
def validate_config(config: GlobalConfig, dry_run: bool = False) -> None:
    errors = collect_config_errors(config, dry_run=dry_run)
    if errors:
        logger.debug("ios config: validation failed with %s error(s): %s", len(errors), errors)
        raise ConfigError(errors)
    logger.debug("ios config: validation passed")


# Return every config problem instead of raising; app-scoped ones are prefixed apps[<id>].
def collect_config_errors(config: GlobalConfig, dry_run: bool = False) -> list[str]:
    errors: list[str] = []
    _auto_fill_bundle_ids(config, errors)
    if not config.device.udid:
        errors.append("device.udid is required")
    if not config.device.team_id:
        errors.append("device.team_id is required")
    if not config.device.appium_server_url:
        errors.append("device.appium_server_url is required")
    if not config.apps:
        errors.append("apps list must not be empty")
    for app in config.apps:
        label = f"apps[{app.id or '?'}]"
        if not app.id:
            errors.append(f"{label}.id is required")
        if not app.name:
            errors.append(f"{label}.name is required")
        source = app.artifact.get("source")
        if not source:
            errors.append(f"{label}.artifact.source is required")
        elif source not in known_sources():
            errors.append(f"{label}.artifact.source is unknown: {source}")
        # intake_ipa takes its identity from the build, which may not be extracted yet.
        if source != "intake_ipa":
            if not app.bundle_id:
                errors.append(f"{label}.bundle_id is required")
            if not app.test_bundle_id:
                errors.append(f"{label}.test_bundle_id is required")
        if source in LOCAL_IPA_SOURCES:
            # A missing build is a provisioning state, not a config error.
            if not (app.artifact.get("ipa") or app.artifact.get("path")):
                errors.append(f"{label}.artifact.ipa is required for {source}")
        for risk_id, risk_config in app.risks.items():
            if risk_id not in known_risks():
                logger.debug("ios config: %s has unknown risk %s", label, risk_id)
                errors.append(f"{label}.risks.{risk_id} is unknown")
                continue
            if not risk_config or not risk_config.get("enabled", False):
                logger.debug("ios config: %s risk %s disabled, skipping risk validation", label, risk_id)
                continue
            logger.debug("ios config: validating %s risk %s", label, risk_id)
            risk = get_risk(risk_id)
            effective = effective_risk_config(config, risk_id, risk_config)
            if risk_id == "ios-feature-04-risk-01":
                keyboard_app = effective.get("keyboard_app") or {}
                keyboard_ipa = keyboard_app.get("ipa")
                keyboard_bundle_id = keyboard_app.get("bundle_id")
                if not keyboard_ipa and not keyboard_bundle_id:
                    errors.append(f"{label}.risks.{risk_id}.keyboard_app.bundle_id or ipa is required")
                if keyboard_ipa and not dry_run and not Path(keyboard_ipa).expanduser().exists():
                    errors.append(f"{label}.risks.{risk_id}.keyboard_app.ipa does not exist: {keyboard_ipa}")
            if risk_id == "ios-feature-04-risk-01":
                collection = effective.get("collection") or effective.get("control") or {}
                if not str(collection.get("probe_text") or collection.get("expected_collected_text") or "").strip():
                    errors.append(f"{label}.risks.{risk_id}.collection.probe_text is required")
            if risk_id == "ios-feature-03-risk-01":
                recorder_app = effective.get("recorder_app") or {}
                recorder_ipa = recorder_app.get("ipa")
                if not recorder_ipa and not recorder_app.get("bundle_id"):
                    errors.append(f"{label}.risks.{risk_id}.recorder_app.bundle_id or ipa is required")
                if recorder_ipa and not dry_run and not Path(recorder_ipa).expanduser().exists():
                    errors.append(f"{label}.risks.{risk_id}.recorder_app.ipa does not exist: {recorder_ipa}")
                capture = effective.get("capture") or {}
                try:
                    window = float(capture.get("capture_window_seconds", 12))
                except (TypeError, ValueError) as exc:
                    logger.debug("ios config: %s capture_window_seconds not numeric: %s", label, exc, exc_info=True)
                    window = 0
                if window <= 0:
                    errors.append(f"{label}.risks.{risk_id}.capture.capture_window_seconds must be greater than 0")
                if capture.get("ocr_provider", "vision") not in {"vision", "none"}:
                    errors.append(f"{label}.risks.{risk_id}.capture.ocr_provider must be vision or none")
            if risk_id == "ios-feature-02-risk-01":
                burp = effective.get("burp") or {}
                if not str(burp.get("proxy_url") or "").strip():
                    errors.append(f"{label}.risks.{risk_id}.burp.proxy_url is required")
                health_max_age = burp.get("health_max_age_seconds", 300)
                try:
                    health_max_age = float(health_max_age)
                except (TypeError, ValueError) as exc:
                    logger.debug("ios config: %s health_max_age_seconds not numeric: %s", label, exc, exc_info=True)
                    errors.append(f"{label}.risks.{risk_id}.burp.health_max_age_seconds must be a non-negative number")
                else:
                    if health_max_age < 0:
                        errors.append(f"{label}.risks.{risk_id}.burp.health_max_age_seconds must be non-negative")
    logger.debug("ios config: collected %s config error(s) (dry_run=%s)", len(errors), dry_run)
    return errors


# Fill missing app and companion bundle IDs from intake builds or IPA metadata.
def _auto_fill_bundle_ids(config: GlobalConfig, errors: list[str]) -> None:
    for app in config.apps:
        label = f"apps[{app.id or '?'}]"
        source = app.artifact.get("source")
        if source == "intake_ipa":
            from mobile_playbook.platforms.ios.acquisition.intake_ipa import resolve_for_app

            resolution = resolve_for_app(app)
            logger.debug(
                "ios config: %s intake resolution ambiguous=%s match=%s",
                label,
                resolution.ambiguous,
                getattr(resolution.match, "bundle_id", None),
            )
            if resolution.ambiguous:
                found = ", ".join(sorted({build.bundle_id for build in resolution.candidates}))
                errors.append(
                    f"{label}: several different apps in intake are named {app.name!r} ({found}); "
                    "set artifact.expected_bundle_id to say which is meant"
                )
            elif resolution.match:
                if not app.bundle_id:
                    app.bundle_id = resolution.match.bundle_id
                if not app.test_bundle_id:
                    app.test_bundle_id = resolution.match.bundle_id
                if not app.artifact.get("expected_bundle_id"):
                    app.artifact["expected_bundle_id"] = resolution.match.bundle_id
        elif source in LOCAL_IPA_SOURCES:
            ipa = app.artifact.get("ipa") or app.artifact.get("path")
            metadata = _inspect_metadata_if_available(ipa)
            logger.debug("ios config: %s IPA %s metadata available=%s", label, ipa, bool(metadata))
            if metadata:
                bundle_id = metadata.get("bundle_id")
                logger.debug(
                    "ios config: %s IPA bundle_id=%s (configured bundle_id=%s test_bundle_id=%s)",
                    label,
                    bundle_id,
                    app.bundle_id,
                    app.test_bundle_id,
                )
                if bundle_id:
                    if not app.bundle_id:
                        app.bundle_id = bundle_id
                    if not app.test_bundle_id:
                        app.test_bundle_id = bundle_id
                    if not app.artifact.get("expected_bundle_id"):
                        app.artifact["expected_bundle_id"] = bundle_id
                elif not app.bundle_id:
                    errors.append(f"{label}.bundle_id could not be inferred from IPA metadata")
            elif not app.bundle_id and ipa:
                # Path problems are reported by the artifact validation instead.
                pass

        risk_config = app.risks.get("ios-feature-04-risk-01")
        if risk_config and risk_config.get("enabled", False):
            # The keyboard app usually lives in the shared keystroke_collection section; this is an override.
            _auto_fill_companion_bundle_id(risk_config.get("keyboard_app"))
        risk_config = app.risks.get("ios-feature-03-risk-01")
        if risk_config and risk_config.get("enabled", False):
            _auto_fill_companion_bundle_id(risk_config.get("recorder_app"))
    _auto_fill_companion_bundle_id(config.keystroke_collection.get("keyboard_app"))
    _auto_fill_companion_bundle_id(config.screen_capture.get("recorder_app"))


# Fill a companion app's missing bundle ID from its IPA metadata.
def _auto_fill_companion_bundle_id(companion_app: dict[str, Any] | None) -> None:
    if not companion_app or companion_app.get("bundle_id"):
        return
    metadata = _inspect_metadata_if_available(companion_app.get("ipa"))
    if metadata and metadata.get("bundle_id"):
        companion_app["bundle_id"] = metadata["bundle_id"]
        logger.debug(
            "ios config: companion app bundle_id %s inferred from %s", metadata["bundle_id"], companion_app.get("ipa")
        )
    else:
        logger.debug("ios config: companion app bundle_id not inferred from %s", companion_app.get("ipa"))


# Return IPA metadata when the path is an existing, readable IPA, else None.
def _inspect_metadata_if_available(ipa: Any) -> dict[str, Any] | None:
    if not ipa:
        return None
    path = Path(str(ipa)).expanduser()
    if not path.exists() or not path.is_file():
        logger.debug("ios config: IPA %s not present as a file, skipping metadata", path)
        return None
    try:
        return inspect_ipa_metadata(path)
    except Exception as exc:
        logger.debug("ios config: IPA metadata inspection failed for %s: %s", path, exc, exc_info=True)
        return None
