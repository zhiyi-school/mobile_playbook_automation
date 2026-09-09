from __future__ import annotations

import re
from copy import deepcopy

import yaml
from fastapi import HTTPException

from mobile_playbook.api.config_editing.android_apps import APP_METADATA_KEYS
from mobile_playbook.api.config_editing.sections import get_section
from mobile_playbook.api.config_editing.shared import (
    APPS_FILES,
    TEMPLATE_FILES,
    config_errors,
    load_and_validate,
    load_with_errors,
    lock_for,
)
from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.platforms.ios.config import _slugify as ios_slugify

APP_ITEM_START_RE = re.compile(r"^  - ", re.MULTILINE)
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


def field_value_re(field: str) -> re.Pattern:
    return re.compile(rf"^(?:  - |    ){re.escape(field)}:[ \t]*(.*)$", re.MULTILINE)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split_ios_app_blocks(apps_text: str) -> tuple[str, list[str]]:
    starts = [match.start() for match in APP_ITEM_START_RE.finditer(apps_text)]
    if not starts:
        raise HTTPException(status_code=500, detail="Could not locate any app entries in configs/split/ios/apps.yaml")
    preamble = apps_text[: starts[0]]
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(apps_text)
        blocks.append(apps_text[start:end])
    return preamble, blocks


def ios_block_identity(block: str) -> str:
    id_match = field_value_re("id").search(block)
    if id_match:
        explicit_id = unquote(id_match.group(1))
        if explicit_id:
            return explicit_id
    name_match = field_value_re("name").search(block)
    return ios_slugify(unquote(name_match.group(1)) if name_match else "")


def render_ios_app_block(app: dict) -> str:
    text = yaml.safe_dump(app, default_flow_style=False, sort_keys=False)
    return "\n".join(("  - " if index == 0 else "    ") + line for index, line in enumerate(text.rstrip("\n").split("\n"))) + "\n"


def ios_templates() -> dict:
    path = TEMPLATE_FILES["ios"]
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def apply_ios_app_defaults(app: dict) -> dict:
    filled = dict(app)
    if not filled.get("expected_behavior"):
        default_check = ios_templates().get("x-default-launch-check")
        if default_check:
            filled["expected_behavior"] = deepcopy(default_check)
    if not filled.get("test_bundle_id"):
        wda_bundle_id = get_section("ios", "device").get("updated_wda_bundle_id")
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


def effective_ios_app(app_id: str) -> dict | None:
    return next((app for app in list_ios_apps() if app.get("id") == app_id), None)


def add_ios_app(app: dict) -> dict:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        _, blocks = split_ios_app_blocks(original_text)
        app = apply_ios_app_defaults(app)
        app_id = app.get("id") or ios_slugify(app.get("name") or "")
        if any(ios_block_identity(block) == app_id for block in blocks):
            raise HTTPException(status_code=409, detail={"message": f"App already exists: {app_id}", "app_id": app_id})
        path.write_text(original_text.rstrip("\n") + "\n" + render_ios_app_block(app))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
    return {"id": app_id}


def edit_ios_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = split_ios_app_blocks(original_text)
        target_index = next((i for i, block in enumerate(blocks) if ios_block_identity(block) == app_id), None)
        if target_index is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        current = effective_ios_app(app_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        merged = merge_dicts(current, updates)
        for key in APP_METADATA_KEYS & updates.keys():
            merged[key] = updates[key]
        merged["id"] = app_id
        blocks[target_index] = render_ios_app_block(merged)
        path.write_text(preamble + "".join(blocks))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
    return effective_ios_app(app_id) or merged


def delete_ios_app(app_id: str) -> None:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = split_ios_app_blocks(original_text)
        remaining = [block for block in blocks if ios_block_identity(block) != app_id]
        if len(remaining) == len(blocks):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        path.write_text(preamble + "".join(remaining))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            path.write_text(original_text)
            raise
