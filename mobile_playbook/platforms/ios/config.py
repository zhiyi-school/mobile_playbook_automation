from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from mobile_playbook.orchestration.preflight import load_yaml_config
from mobile_playbook.platforms.ios.artifacts.registry import known_sources
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import (
    AppConfig,
    DeviceConfig,
    ExpectedBehaviorConfig,
    GlobalConfig,
    RunnerConfig,
)
from mobile_playbook.platforms.ios.risks.registry import get_risk, known_risks

LOCAL_IPA_SOURCES = {"local_ipa", "ci_artifact", "vendor_ipa", "xcode_archive_export"}


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "app"


def _require(mapping: dict[str, Any], key: str, label: str, errors: list[str]) -> Any:
    value = mapping.get(key)
    if value in (None, ""):
        errors.append(f"{label}.{key} is required")
    return value


def load_config(path: Path, dry_run: bool = False) -> GlobalConfig:
    path = Path(path)
    try:
        raw = load_yaml_config(path)
    except ValueError as exc:
        raise ConfigError([str(exc)]) from exc
    config = parse_config(raw, path)
    validate_config(config, dry_run=dry_run)
    return config


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
        apps.append(
            AppConfig(
                id=app_raw.get("id") or _slugify(app_name),
                name=app_name,
                bundle_id=bundle_id,
                test_bundle_id=app_raw.get("test_bundle_id") or bundle_id,
                artifact=app_raw.get("artifact") or {},
                expected_behavior=behavior,
                risks=app_raw.get("risks") or {},
            )
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
        ),
        runner=RunnerConfig(
            sequential=bool(runner_raw.get("sequential", True)),
            uninstall_after_each_test=bool(runner_raw.get("uninstall_after_each_test", True)),
            stop_on_first_failure=bool(runner_raw.get("stop_on_first_failure", False)),
            app_install_timeout_ms=int(runner_raw.get("app_install_timeout_ms", 480000)),
            launch_wait_seconds=int(runner_raw.get("launch_wait_seconds", 5)),
            work_dir=Path(runner_raw.get("work_dir", "work/ios")),
            permission_alerts=runner_raw.get("permission_alerts") or {},
        ),
        apps=apps,
        config_path=config_path,
    )


def validate_config(config: GlobalConfig, dry_run: bool = False) -> None:
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
        if not app.bundle_id:
            errors.append(f"{label}.bundle_id is required")
        if not app.test_bundle_id:
            errors.append(f"{label}.test_bundle_id is required")
        if source in LOCAL_IPA_SOURCES:
            ipa = app.artifact.get("ipa") or app.artifact.get("path")
            if not ipa:
                errors.append(f"{label}.artifact.ipa is required for {source}")
            elif not dry_run and not Path(ipa).expanduser().exists():
                errors.append(f"{label}.artifact.ipa does not exist: {ipa}")
        for risk_id, risk_config in app.risks.items():
            if risk_id not in known_risks():
                errors.append(f"{label}.risks.{risk_id} is unknown")
                continue
            if not risk_config or not risk_config.get("enabled", False):
                continue
            risk = get_risk(risk_id)
            if risk_id == "ios-feature5-risk1":
                keyboard_app = risk_config.get("keyboard_app") or {}
                keyboard_ipa = keyboard_app.get("ipa")
                keyboard_bundle_id = keyboard_app.get("bundle_id")
                if not keyboard_ipa and not keyboard_bundle_id:
                    errors.append(f"{label}.risks.{risk_id}.keyboard_app.bundle_id or ipa is required")
                if keyboard_ipa and not dry_run and not Path(keyboard_ipa).expanduser().exists():
                    errors.append(f"{label}.risks.{risk_id}.keyboard_app.ipa does not exist: {keyboard_ipa}")
            if risk_id == "ios-feature5-risk1":
                collection = risk_config.get("collection") or risk_config.get("control") or {}
                if not str(collection.get("probe_text") or collection.get("expected_collected_text") or "").strip():
                    errors.append(f"{label}.risks.{risk_id}.collection.probe_text is required")
    if errors:
        raise ConfigError(errors)


def _auto_fill_bundle_ids(config: GlobalConfig, errors: list[str]) -> None:
    for app in config.apps:
        label = f"apps[{app.id or '?'}]"
        source = app.artifact.get("source")
        if source in LOCAL_IPA_SOURCES:
            ipa = app.artifact.get("ipa") or app.artifact.get("path")
            metadata = _inspect_metadata_if_available(ipa)
            if metadata:
                bundle_id = metadata.get("bundle_id")
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
                # Path existence/readability is reported by the normal artifact
                # validation below; this keeps the bundle-id error actionable.
                pass

        for risk_id, risk_config in app.risks.items():
            if risk_id != "ios-feature5-risk1" or not risk_config or not risk_config.get("enabled", False):
                continue
            keyboard_app = risk_config.get("keyboard_app") or {}
            if keyboard_app.get("bundle_id"):
                continue
            metadata = _inspect_metadata_if_available(keyboard_app.get("ipa"))
            if metadata and metadata.get("bundle_id"):
                keyboard_app["bundle_id"] = metadata["bundle_id"]


def _inspect_metadata_if_available(ipa: Any) -> dict[str, Any] | None:
    if not ipa:
        return None
    path = Path(str(ipa)).expanduser()
    if not path.exists() or not path.is_file():
        return None
    try:
        return inspect_ipa_metadata(path)
    except Exception:
        return None
