from __future__ import annotations

import io
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from ruamel.yaml import YAML

from mobile_playbook.orchestration.preflight import load_yaml_config
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import collect_config_errors as collect_android_errors
from mobile_playbook.platforms.android.config import parse_config as parse_android_config
from mobile_playbook.platforms.ios.config import ConfigError as IosConfigError
from mobile_playbook.platforms.ios.config import RISK_GLOBAL_SETTINGS_FIELD
from mobile_playbook.platforms.ios.config import collect_config_errors as collect_ios_errors
from mobile_playbook.platforms.ios.config import parse_config as parse_ios_config

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
TEMPLATE_FILES = {"ios": Path("configs/split/ios/templates.yaml")}

rt_yaml = YAML(typ="rt")
rt_yaml.preserve_quotes = True
rt_yaml.width = 100_000
rt_yaml.indent(mapping=2, sequence=4, offset=2)

_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}
_APP_ERROR_PREFIX = re.compile(r"^apps\[([^\]]+)\]")


def lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


def load_with_errors(platform: str):
    entry_path = ENTRY_FILES[platform]
    try:
        raw = load_yaml_config(entry_path)
        config = parse_android_config(raw, entry_path) if platform == "android" else parse_ios_config(raw, entry_path)
    except (AndroidConfigError, IosConfigError, ValueError, OSError) as exc:
        return None, [str(exc)]
    collect = collect_android_errors if platform == "android" else collect_ios_errors
    return config, list(collect(config, dry_run=False))


def config_errors(platform: str) -> list[str]:
    return load_with_errors(platform)[1]


def app_config_errors(platform: str) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for error in config_errors(platform):
        match = _APP_ERROR_PREFIX.match(error)
        buckets.setdefault(match.group(1) if match else "", []).append(error)
    return buckets


def load_and_validate(platform: str, baseline: list[str] | None = None) -> None:
    introduced = [error for error in config_errors(platform) if error not in (baseline or [])]
    if introduced:
        raise HTTPException(status_code=422, detail=introduced)


def plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, str):
        return str(value)
    return value


def rt_dump(data: Any) -> str:
    buf = io.StringIO()
    rt_yaml.dump(data, buf)
    return buf.getvalue()


def merge_into_commented(node: Any, updates: dict) -> None:
    for key, value in updates.items():
        current = node.get(key) if hasattr(node, "get") else None
        if isinstance(value, dict) and isinstance(current, dict):
            merge_into_commented(current, value)
        elif value in (None, {}, []) and current is not None:
            continue
        else:
            node[key] = value


def write_whole_file_validated(path: Path, mutate: Callable[[Any], None], platform: str) -> Any:
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors(platform)
        data = rt_yaml.load(original_text)
        mutate(data)
        path.write_text(rt_dump(data))
        try:
            load_and_validate(platform, baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
        return data
