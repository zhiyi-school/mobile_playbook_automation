from __future__ import annotations

from pathlib import Path
from typing import Any

from mobile_playbook.orchestration.preflight import load_yaml_config
from mobile_playbook.platforms.android.models import (
    AndroidAppConfig,
    AndroidDeviceConfig,
    AndroidGlobalConfig,
    AndroidRunnerConfig,
)
from mobile_playbook.platforms.android.risks.registry import known_risks


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def load_config(path: Path, dry_run: bool = False) -> AndroidGlobalConfig:
    path = Path(path)
    try:
        raw = load_yaml_config(path)
        raw = _load_apps_file(raw, path.parent)
    except ValueError as exc:
        raise ConfigError([str(exc)]) from exc
    config = parse_config(raw, path)
    validate_config(config, dry_run=dry_run)
    return config


def parse_config(raw: dict[str, Any], config_path: Path | None = None) -> AndroidGlobalConfig:
    device_raw = raw.get("device") or {}
    runner_raw = raw.get("runner") or {}
    permissions_raw = raw.get("permissions") or {}
    paths_raw = raw.get("paths") or {}
    repackaging_raw = dict(raw.get("repackaging") or {})
    if "work_dir" not in repackaging_raw:
        repackaging_raw["work_dir"] = paths_raw.get("repackaging_work_dir", "work/android/repackaging")

    apps = [_parse_app(item) for item in (raw.get("apps") or [])]
    return AndroidGlobalConfig(
        device=AndroidDeviceConfig(
            appium_server_url=device_raw.get("appium_server_url") or device_raw.get("appium_server", "http://127.0.0.1:4723"),
            adb_path=device_raw.get("adb_path", "adb"),
            adb_serial=device_raw.get("adb_serial") or device_raw.get("serial"),
            appium_auto_start=device_raw.get("appium_auto_start") or {},
        ),
        runner=AndroidRunnerConfig(
            work_dir=Path(runner_raw.get("work_dir") or paths_raw.get("work_dir", "work/android")),
            auto_grant_permissions=bool(
                runner_raw.get("auto_grant_permissions", permissions_raw.get("auto_grant", False))
            ),
            launch_wait_seconds=float(runner_raw.get("launch_wait_seconds", 4)),
        ),
        apps=apps,
        tools=raw.get("tools") or {},
        screen_capture=raw.get("screen_capture") or {},
        repackaging=repackaging_raw,
        config_path=config_path,
    )


def validate_config(config: AndroidGlobalConfig, dry_run: bool = False) -> None:
    errors: list[str] = []
    if not config.device.appium_server_url:
        errors.append("device.appium_server_url is required")
    if not config.apps:
        errors.append("apps list must not be empty")
    known = known_risks()
    for app in config.apps:
        label = f"apps[{app.id or '?'}]"
        if not app.id:
            errors.append(f"{label}.id is required")
        if not app.package_name:
            errors.append(f"{label}.package_name is required")
        for risk_id, risk_config in app.risks.items():
            if risk_id not in known:
                errors.append(f"{label}.risks.{risk_id} is unknown")
            elif not isinstance(risk_config, dict):
                errors.append(f"{label}.risks.{risk_id} must be a mapping")
    if errors:
        raise ConfigError(errors)


def _load_apps_file(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    apps_file = raw.get("apps_file")
    if not apps_file:
        return raw
    apps_path = Path(str(apps_file)).expanduser()
    if not apps_path.is_absolute():
        apps_path = (base_dir / apps_path).resolve()
    if not apps_path.exists():
        raise ValueError(f"apps file not found: {apps_path}")
    loaded = load_yaml_config(apps_path)
    raw = dict(raw)
    raw["apps"] = loaded.get("apps", loaded) if isinstance(loaded, dict) else loaded
    return raw


def _parse_app(item: Any) -> AndroidAppConfig:
    if isinstance(item, str):
        package_name = item
        return AndroidAppConfig(
            id=_slugify(package_name),
            name=package_name,
            package_name=package_name,
            risks={risk_id: {"enabled": True} for risk_id in known_risks()},
        )
    if not isinstance(item, dict):
        return AndroidAppConfig(id="", name="", package_name="", risks={})
    package_name = item.get("package_name") or item.get("package") or item.get("bundle_id") or ""
    return AndroidAppConfig(
        id=item.get("id") or _slugify(package_name or item.get("name", "")),
        name=item.get("name") or package_name,
        package_name=package_name,
        artifact=item.get("artifact") or {},
        risks=item.get("risks") or {risk_id: {"enabled": True} for risk_id in known_risks()},
    )


def _slugify(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "android_app"
