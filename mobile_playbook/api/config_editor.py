from __future__ import annotations

import io
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from ruamel.yaml import YAML

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import _slugify as _android_slugify
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.ios.config import ConfigError as IosConfigError
from mobile_playbook.platforms.ios.config import RISK_GLOBAL_SETTINGS_FIELD
from mobile_playbook.platforms.ios.config import _slugify as _ios_slugify
from mobile_playbook.platforms.ios.config import load_config as load_ios_config

ENTRY_FILES = {"ios": Path("configs/ios.yaml"), "android": Path("configs/android.yaml")}
APPS_FILES = {"ios": Path("configs/split/ios/apps.yaml"), "android": Path("configs/split/android/apps.yaml")}

# risk_id -> (field name under the risk-settings file, that file's path).
# Android has no RISK_GLOBAL_SETTINGS_FIELD equivalent to reuse (tools/repackaging/
# screen_capture are read by fixed field name in android/config.py), so this is
# spelled out directly for both platforms rather than only being derivable for iOS.
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
_rt_yaml.width = 100_000  # avoid re-wrapping long lines back into the file
_rt_yaml.indent(mapping=2, sequence=4, offset=2)  # matches this repo's nested-list style (key:\n  - item)

_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


def _load_and_validate(platform: str) -> None:
    """Re-run the real config loader/validator against what's now on disk.

    This is the safety net every write in this module goes through: if the
    edit just written makes the overall config invalid, the caller reverts
    the file and this raises so nothing bad is left committed.
    """
    entry_path = ENTRY_FILES[platform]
    try:
        if platform == "android":
            load_android_config(entry_path, dry_run=False)
        else:
            load_ios_config(entry_path, dry_run=False)
    except (AndroidConfigError, IosConfigError) as exc:
        raise HTTPException(status_code=422, detail=list(exc.errors)) from exc


def _plain(value: Any) -> Any:
    """Convert ruamel's CommentedMap/CommentedSeq into plain, JSON-safe dict/list."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _rt_dump(data: Any) -> str:
    buf = io.StringIO()
    _rt_yaml.dump(data, buf)
    return buf.getvalue()


def _merge_into_commented(node: Any, updates: dict) -> None:
    """Apply `updates` onto a live ruamel node in place, key by key.

    `merge_dicts` builds a brand new plain structure, which would replace an
    existing CommentedMap wholesale and drop every comment attached to its
    untouched keys. Mutating the same node object instead means keys nobody
    asked to change keep their original formatting and comments; only the
    keys actually present in `updates` are touched.
    """
    for key, value in updates.items():
        current = node.get(key) if hasattr(node, "get") else None
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_into_commented(current, value)
        elif value in (None, {}, []) and current is not None:
            continue  # matches merge_dicts: an empty/None override doesn't clear an existing value
        else:
            node[key] = value


def _write_whole_file_validated(path: Path, mutate: "callable[[Any], None]", platform: str) -> Any:
    """Round-trip load `path`, apply `mutate` to the parsed document, write it back,
    validate the platform's config, and revert on failure.

    Only safe for files with no cross-file YAML anchors (everything except
    configs/split/ios/apps.yaml — see `_edit_ios_apps_file` for why that one
    is handled differently).
    """
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        data = _rt_yaml.load(original_text)
        mutate(data)
        path.write_text(_rt_dump(data))
        try:
            _load_and_validate(platform)
        except HTTPException:
            path.write_text(original_text)
            raise
        return data


# ---------------------------------------------------------------------------
# Device / runner — inlined directly in configs/{platform}.yaml, no anchors.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Risk settings — one risk's global defaults, one file each, no anchors.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Android apps — one file, no anchors: full round-trip works cleanly.
# ---------------------------------------------------------------------------


def list_android_apps() -> list[dict]:
    data = _rt_yaml.load(APPS_FILES["android"].read_text())
    items = [_plain(item) for item in (data.get("apps") or [])]
    for item in items:
        item.setdefault("id", _android_app_id(item))
    return items


def _android_app_id(item: dict) -> str:
    return item.get("id") or _android_slugify(item.get("package_name") or item.get("name") or "")


def add_android_app(app: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.setdefault("apps", [])
        app_id = _android_app_id(app)
        if any(_android_app_id(_plain(item)) == app_id for item in apps):
            raise HTTPException(status_code=409, detail=f"App already exists: {app_id}")
        new_app = dict(app)
        new_app.setdefault("id", app_id)
        apps.append(new_app)

    _write_whole_file_validated(path, mutate, "android")
    return {"id": _android_app_id(app)}


def edit_android_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        for item in apps:
            if _android_app_id(_plain(item)) == app_id:
                _merge_into_commented(item, updates)
                item["id"] = app_id
                return
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

    data = _write_whole_file_validated(path, mutate, "android")
    for item in data.get("apps") or []:
        if _android_app_id(_plain(item)) == app_id:
            return _plain(item)
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")  # pragma: no cover — validated above


def delete_android_app(app_id: str) -> None:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        remaining = [item for item in apps if _android_app_id(_plain(item)) != app_id]
        if len(remaining) == len(apps):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        data["apps"] = remaining

    _write_whole_file_validated(path, mutate, "android")


# ---------------------------------------------------------------------------
# iOS apps — configs/split/ios/apps.yaml cannot be parsed on its own: its
# `<<: *anchor` entries reference anchors defined in the sibling
# templates.yaml, only resolvable when the two files are concatenated and
# parsed together (see orchestration/preflight.py's include-list handling).
# Re-serializing that combined, multi-file document back into two physical
# files while preserving the original hand-authored anchors reliably isn't
# solvable in general, so writes here operate on apps.yaml's own raw text
# directly: each top-level app entry is a text block starting at a `  - `
# line at 2-space indent (the YAML block-sequence marker; nothing else in
# this file's structure starts a line at that exact indent), located by
# regex rather than by parsing. Blocks that are untouched are left as
# byte-identical text — comments and anchor usage on every OTHER app are
# never at risk. A block being added or edited is rendered with fully
# explicit values (no anchor usage) via a plain YAML dump — consistent with
# accepting that API-written entries come out expanded rather than
# templated.
# ---------------------------------------------------------------------------

_APP_ITEM_START_RE = re.compile(r"^  - ", re.MULTILINE)


def _field_value_re(field: str) -> re.Pattern:
    # A block's first field sits inline after the "  - " list marker (e.g.
    # `  - name: "SP"`); every field after that starts its own line at
    # 4-space indent. Match either position.
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


def list_ios_apps() -> list[dict]:
    config = load_ios_config(ENTRY_FILES["ios"], dry_run=False)
    return [app.to_dict() for app in config.apps]


def _effective_ios_app(app_id: str) -> dict | None:
    config = load_ios_config(ENTRY_FILES["ios"], dry_run=False)
    for app in config.apps:
        if app.id == app_id:
            return app.to_dict()
    return None


def add_ios_app(app: dict) -> dict:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        _, blocks = _split_ios_app_blocks(original_text)
        app = dict(app)
        app_id = app.get("id") or _ios_slugify(app.get("name") or "")
        if any(_ios_block_identity(block) == app_id for block in blocks):
            raise HTTPException(status_code=409, detail=f"App already exists: {app_id}")
        new_text = original_text.rstrip("\n") + "\n" + _render_ios_app_block(app)
        path.write_text(new_text)
        try:
            _load_and_validate("ios")
        except HTTPException:
            path.write_text(original_text)
            raise
    return {"id": app_id}


def edit_ios_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        preamble, blocks = _split_ios_app_blocks(original_text)
        target_index = next((i for i, block in enumerate(blocks) if _ios_block_identity(block) == app_id), None)
        if target_index is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

        current = _effective_ios_app(app_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")  # pragma: no cover
        merged = merge_dicts(current, updates)
        merged["id"] = app_id

        blocks[target_index] = _render_ios_app_block(merged)
        path.write_text(preamble + "".join(blocks))
        try:
            _load_and_validate("ios")
        except HTTPException:
            path.write_text(original_text)
            raise
    return _effective_ios_app(app_id) or merged


def delete_ios_app(app_id: str) -> None:
    path = APPS_FILES["ios"]
    lock = _lock_for(path)
    with lock:
        original_text = path.read_text()
        preamble, blocks = _split_ios_app_blocks(original_text)
        remaining = [block for block in blocks if _ios_block_identity(block) != app_id]
        if len(remaining) == len(blocks):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        path.write_text(preamble + "".join(remaining))
        try:
            _load_and_validate("ios")
        except HTTPException:
            path.write_text(original_text)
            raise
