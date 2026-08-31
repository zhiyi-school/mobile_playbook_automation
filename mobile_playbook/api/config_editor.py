from __future__ import annotations

import io
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from ruamel.yaml import YAML

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.orchestration.preflight import load_yaml_config
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import _slugify as _android_slugify
from mobile_playbook.platforms.android.config import collect_config_errors as collect_android_errors
from mobile_playbook.platforms.android.config import parse_config as parse_android_config
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.ios.config import ConfigError as IosConfigError
from mobile_playbook.platforms.ios.config import RISK_GLOBAL_SETTINGS_FIELD
from mobile_playbook.platforms.ios.config import _slugify as _ios_slugify
from mobile_playbook.platforms.ios.config import collect_config_errors as collect_ios_errors
from mobile_playbook.platforms.ios.config import parse_config as parse_ios_config
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks

ENTRY_FILES = {"ios": Path("configs/ios.yaml"), "android": Path("configs/android.yaml")}
APPS_FILES = {"ios": Path("configs/split/ios/apps.yaml"), "android": Path("configs/split/android/apps.yaml")}
FEATURES_FILES = {
    "ios": Path("configs/split/ios/features.yaml"),
    "android": Path("configs/split/android/features.yaml"),
}
RISK_FILES = {
    "ios": Path("configs/split/ios/risks.yaml"),
    "android": Path("configs/split/android/risks.yaml"),
}

RISK_METADATA_FIELDS = ("name", "description", "goal", "tactic")

# risk_id -> (settings field, settings file path)
RISK_SETTINGS = {
    "ios": {
        risk_id: (field, Path(f"configs/split/ios/{field}.yaml"))
        for risk_id, field in RISK_GLOBAL_SETTINGS_FIELD.items()
    },
    "android": {
        "android-feature-01-risk-02": ("repackaging", Path("configs/split/android/repackaging.yaml")),
        "android-feature-06-risk-01": ("screen_capture", Path("configs/split/android/screen_capture.yaml")),
    },
}

_rt_yaml = YAML(typ="rt")
_rt_yaml.preserve_quotes = True
_rt_yaml.width = 100_000
_rt_yaml.indent(mapping=2, sequence=4, offset=2)

_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


_APP_ERROR_PREFIX = re.compile(r"^apps\[([^\]]+)\]")


def load_with_errors(platform: str):
    """Return the parsed config and all current validation errors."""
    entry_path = ENTRY_FILES[platform]
    try:
        raw = load_yaml_config(entry_path)
        config = parse_android_config(raw, entry_path) if platform == "android" else parse_ios_config(raw, entry_path)
    except (AndroidConfigError, IosConfigError, ValueError, OSError) as exc:
        return None, [str(exc)]
    collect = collect_android_errors if platform == "android" else collect_ios_errors
    return config, list(collect(config, dry_run=False))


def config_errors(platform: str) -> list[str]:
    """Every current config problem, or a single parse error if it won't even load."""
    return load_with_errors(platform)[1]


def app_config_errors(platform: str) -> dict[str, list[str]]:
    """Config problems bucketed by app id; global ones under the empty-string key."""
    buckets: dict[str, list[str]] = {}
    for error in config_errors(platform):
        match = _APP_ERROR_PREFIX.match(error)
        buckets.setdefault(match.group(1) if match else "", []).append(error)
    return buckets


def _load_and_validate(platform: str, baseline: list[str] | None = None) -> None:
    """Reject a write only for errors introduced by that write."""
    introduced = [error for error in config_errors(platform) if error not in (baseline or [])]
    if introduced:
        raise HTTPException(status_code=422, detail=introduced)


def _plain(value: Any) -> Any:
    """Convert ruamel values into JSON-safe Python values."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, str):
        return str(value)
    return value


def _rt_dump(data: Any) -> str:
    buf = io.StringIO()
    _rt_yaml.dump(data, buf)
    return buf.getvalue()


def _merge_into_commented(node: Any, updates: dict) -> None:
    """Merge updates while preserving comments on untouched keys."""
    for key, value in updates.items():
        current = node.get(key) if hasattr(node, "get") else None
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_into_commented(current, value)
        elif value in (None, {}, []) and current is not None:
            continue  # matches merge_dicts: an empty/None override doesn't clear an existing value
        else:
            node[key] = value


def _write_whole_file_validated(path: Path, mutate: "callable[[Any], None]", platform: str) -> Any:
    """Write a round-tripped YAML file, then validate and revert on failure."""
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        baseline = config_errors(platform)
        data = _rt_yaml.load(original_text)
        mutate(data)
        path.write_text(_rt_dump(data))
        try:
            _load_and_validate(platform, baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
        return data


# Device / runner


def get_section(platform: str, section: str) -> dict:
    path = ENTRY_FILES[platform]
    data = _rt_yaml.load(path.read_text())
    return _plain(data.get(section) or {})


def put_section(platform: str, section: str, updates: dict) -> dict:
    path = ENTRY_FILES[platform]

    def mutate(data: Any) -> None:
        if not isinstance(data.get(section), dict):
            data[section] = {}
        _merge_into_commented(data[section], updates)

    data = _write_whole_file_validated(path, mutate, platform)
    return _plain(data.get(section) or {})


# Risk settings


def _risk_settings_target(platform: str, risk_id: str) -> tuple[str, Path]:
    target = (RISK_SETTINGS.get(platform) or {}).get(risk_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No global risk settings for {risk_id} on platform {platform}")
    return target


def get_risk_settings(platform: str, risk_id: str) -> dict:
    field_name, path = _risk_settings_target(platform, risk_id)
    data = _rt_yaml.load(path.read_text())
    return _plain(data.get(field_name) or {})


def put_risk_settings(platform: str, risk_id: str, updates: dict) -> dict:
    field_name, path = _risk_settings_target(platform, risk_id)

    def mutate(data: Any) -> None:
        if not isinstance(data.get(field_name), dict):
            data[field_name] = {}
        _merge_into_commented(data[field_name], updates)

    data = _write_whole_file_validated(path, mutate, platform)
    return _plain(data.get(field_name) or {})


# Android apps


def list_android_apps() -> list[dict]:
    data = _rt_yaml.load(APPS_FILES["android"].read_text())
    items = [_plain(item) for item in (data.get("apps") or [])]
    for item in items:
        item.setdefault("id", _android_app_id(item))
        item.setdefault("sector", "")
        item.setdefault("agency", "")
        item.setdefault("version", "")
        item.setdefault("cisos", [])
    return items


def _android_app_id(item: dict) -> str:
    return item.get("id") or _android_slugify(item.get("package_name") or item.get("name") or "")


def add_android_app(app: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.setdefault("apps", [])
        app_id = _android_app_id(app)
        if any(_android_app_id(_plain(item)) == app_id for item in apps):
            raise HTTPException(
                status_code=409,
                detail={"message": f"App already exists: {app_id}", "app_id": app_id},
            )
        new_app = dict(app)
        new_app.setdefault("id", app_id)
        apps.append(new_app)

    _write_whole_file_validated(path, mutate, "android")
    return {"id": _android_app_id(app)}


_APP_METADATA_KEYS = {"sector", "agency", "version", "cisos"}


def edit_android_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        for item in apps:
            if _android_app_id(_plain(item)) == app_id:
                _merge_into_commented(item, updates)
                for key in _APP_METADATA_KEYS & updates.keys():
                    item[key] = updates[key]
                item["id"] = app_id
                return
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

    data = _write_whole_file_validated(path, mutate, "android")
    for item in data.get("apps") or []:
        if _android_app_id(_plain(item)) == app_id:
            return _plain(item)
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")  # pragma: no cover


def delete_android_app(app_id: str) -> None:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        remaining = [item for item in apps if _android_app_id(_plain(item)) != app_id]
        if len(remaining) == len(apps):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        data["apps"] = remaining

    _write_whole_file_validated(path, mutate, "android")


# iOS apps. apps.yaml uses anchors from templates.yaml, so app entries are edited as text blocks.

_APP_ITEM_START_RE = re.compile(r"^  - ", re.MULTILINE)


def _field_value_re(field: str) -> re.Pattern:
    return re.compile(rf"^(?:  - |    ){re.escape(field)}:[ \t]*(.*)$", re.MULTILINE)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_ios_app_blocks(apps_text: str) -> tuple[str, list[str]]:
    starts = [m.start() for m in _APP_ITEM_START_RE.finditer(apps_text)]
    if not starts:
        raise HTTPException(status_code=500, detail="Could not locate any app entries in configs/split/ios/apps.yaml")
    preamble = apps_text[: starts[0]]
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(apps_text)
        blocks.append(apps_text[start:end])
    return preamble, blocks


def _ios_block_identity(block: str) -> str:
    id_match = _field_value_re("id").search(block)
    if id_match:
        explicit_id = _unquote(id_match.group(1))
        if explicit_id:
            return explicit_id
    name_match = _field_value_re("name").search(block)
    name = _unquote(name_match.group(1)) if name_match else ""
    return _ios_slugify(name)


def _render_ios_app_block(app: dict) -> str:
    text = yaml.safe_dump(app, default_flow_style=False, sort_keys=False)
    lines = text.rstrip("\n").split("\n")
    rendered = [("  - " if index == 0 else "    ") + line for index, line in enumerate(lines)]
    return "\n".join(rendered) + "\n"


TEMPLATE_FILES = {"ios": Path("configs/split/ios/templates.yaml")}

IOS_APP_FIELD_ORDER = (
    "id",
    "name",
    "version",
    "sector",
    "agency",
    "bundle_id",
    "test_bundle_id",
    "artifact",
    "expected_behavior",
    "risks",
    "cisos",
)


def _ios_templates() -> dict:
    path = TEMPLATE_FILES["ios"]
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def apply_ios_app_defaults(app: dict) -> dict:
    """Fill a minimal iOS app entry with roster defaults."""
    filled = dict(app)

    if not filled.get("expected_behavior"):
        default_check = _ios_templates().get("x-default-launch-check")
        if default_check:
            filled["expected_behavior"] = deepcopy(default_check)

    if not filled.get("test_bundle_id"):
        wda_bundle_id = (get_section("ios", "device") or {}).get("updated_wda_bundle_id")
        if wda_bundle_id:
            filled["test_bundle_id"] = wda_bundle_id

    ordered = {key: filled[key] for key in IOS_APP_FIELD_ORDER if key in filled}
    ordered.update({key: value for key, value in filled.items() if key not in ordered})
    return ordered


def list_ios_apps() -> list[dict]:
    config, _ = load_with_errors("ios")
    if config is None:
        raise HTTPException(status_code=422, detail=config_errors("ios"))
    return [app.to_dict() for app in config.apps]


def _effective_ios_app(app_id: str) -> dict | None:
    return next((app for app in list_ios_apps() if app.get("id") == app_id), None)


def add_ios_app(app: dict) -> dict:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        baseline = config_errors("ios")
        _, blocks = _split_ios_app_blocks(original_text)
        app = apply_ios_app_defaults(app)
        app_id = app.get("id") or _ios_slugify(app.get("name") or "")
        if any(_ios_block_identity(block) == app_id for block in blocks):
            raise HTTPException(
                status_code=409,
                detail={"message": f"App already exists: {app_id}", "app_id": app_id},
            )
        new_text = original_text.rstrip("\n") + "\n" + _render_ios_app_block(app)
        path.write_text(new_text)
        try:
            _load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
    return {"id": app_id}


def edit_ios_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = _split_ios_app_blocks(original_text)
        target_index = next((i for i, block in enumerate(blocks) if _ios_block_identity(block) == app_id), None)
        if target_index is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

        current = _effective_ios_app(app_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")  # pragma: no cover
        merged = merge_dicts(current, updates)
        for key in _APP_METADATA_KEYS & updates.keys():
            merged[key] = updates[key]
        merged["id"] = app_id

        blocks[target_index] = _render_ios_app_block(merged)
        path.write_text(preamble + "".join(blocks))
        try:
            _load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
    return _effective_ios_app(app_id) or merged


def delete_ios_app(app_id: str) -> None:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = _split_ios_app_blocks(original_text)
        remaining = [block for block in blocks if _ios_block_identity(block) != app_id]
        if len(remaining) == len(blocks):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        path.write_text(preamble + "".join(remaining))
        try:
            _load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise


def _known_feature_ids(platform: str) -> list[str]:
    risks = list_ios_risks() if platform == "ios" else list_android_risks()
    seen: list[str] = []
    for risk in risks:
        if risk["feature_id"] not in seen:
            seen.append(risk["feature_id"])
    return seen


def list_features(platform: str) -> list[dict]:
    path = FEATURES_FILES[platform]
    stored = _plain(_rt_yaml.load(path.read_text()) or {})
    return [
        {
            "feature_id": feature_id,
            "name": (stored.get(feature_id) or {}).get("name", ""),
            "description": (stored.get(feature_id) or {}).get("description", ""),
        }
        for feature_id in _known_feature_ids(platform)
    ]


def put_feature(platform: str, feature_id: str, updates: dict) -> dict:
    if feature_id not in _known_feature_ids(platform):
        raise HTTPException(status_code=404, detail=f"Unknown feature_id: {feature_id}")
    path = FEATURES_FILES[platform]

    def mutate(data: Any) -> None:
        if not isinstance(data.get(feature_id), dict):
            data[feature_id] = {}
        _merge_into_commented(data[feature_id], updates)

    data = _write_whole_file_validated(path, mutate, platform)
    entry = _plain(data.get(feature_id) or {})
    return {"feature_id": feature_id, "name": entry.get("name", ""), "description": entry.get("description", "")}


def _require_known_risk(platform: str, risk_id: str) -> None:
    known = list_ios_risks() if platform == "ios" else list_android_risks()
    if risk_id not in {risk["risk_id"] for risk in known}:
        raise HTTPException(status_code=404, detail=f"Unknown risk_id: {risk_id}")


def _risk_entry(platform: str, risk_id: str) -> dict:
    path = RISK_FILES[platform]
    try:
        data = _plain(_rt_yaml.load(path.read_text()) or {})
    except OSError:
        return {}
    entry = data.get(risk_id)
    return entry if isinstance(entry, dict) else {}


def get_risk_metadata(platform: str, risk_id: str) -> dict:
    """The displayed fields this risk overrides, omitting any it doesn't set."""
    _require_known_risk(platform, risk_id)
    entry = _risk_entry(platform, risk_id)
    return {field: entry[field] for field in RISK_METADATA_FIELDS if field in entry}


def put_risk_metadata(platform: str, risk_id: str, updates: dict) -> dict:
    _require_known_risk(platform, risk_id)
    unknown = sorted(set(updates) - set(RISK_METADATA_FIELDS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown risk fields: {', '.join(unknown)}")
    path = RISK_FILES[platform]

    def mutate(data: Any) -> None:
        if not isinstance(data.get(risk_id), dict):
            data[risk_id] = {}
        _merge_into_commented(data[risk_id], updates)

    _write_whole_file_validated(path, mutate, platform)
    return get_risk_metadata(platform, risk_id)


def get_risk_demonstration(platform: str, risk_id: str) -> list:
    _require_known_risk(platform, risk_id)
    return _risk_entry(platform, risk_id).get("demonstration") or []


def put_risk_demonstration(platform: str, risk_id: str, demonstration: list) -> list:
    _require_known_risk(platform, risk_id)
    path = RISK_FILES[platform]

    def mutate(data: Any) -> None:
        if not isinstance(data.get(risk_id), dict):
            data[risk_id] = {}
        data[risk_id]["demonstration"] = demonstration

    data = _write_whole_file_validated(path, mutate, platform)
    return (data.get(risk_id) or {}).get("demonstration") or []
