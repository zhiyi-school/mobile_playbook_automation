"""
Lists, adds, edits and deletes iOS apps by rewriting text blocks in the split apps YAML.
"""

from __future__ import annotations

import logging
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
from mobile_playbook.common.config_loader import merge_dicts
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
logger = logging.getLogger(__name__)


# Build a regex that captures a top-level field's value within an app block.
def field_value_re(field: str) -> re.Pattern:
    return re.compile(rf"^(?:  - |    ){re.escape(field)}:[ \t]*(.*)$", re.MULTILINE)


# Strip whitespace and one pair of matching quotes from a YAML scalar.
def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# Split the iOS apps YAML into its preamble and per-app text blocks, or raise 500.
def split_ios_app_blocks(apps_text: str) -> tuple[str, list[str]]:
    starts = [match.start() for match in APP_ITEM_START_RE.finditer(apps_text)]
    if not starts:
        logger.debug("api: no app entries found in %d characters of iOS apps YAML.", len(apps_text))
        raise HTTPException(status_code=500, detail="Could not locate any app entries in configs/split/ios/apps.yaml")
    preamble = apps_text[: starts[0]]
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(apps_text)
        blocks.append(apps_text[start:end])
    logger.debug("api: split iOS apps YAML into %d app block(s).", len(blocks))
    return preamble, blocks


# Return an app block's explicit id, or a slug of its name.
def ios_block_identity(block: str) -> str:
    id_match = field_value_re("id").search(block)
    if id_match:
        explicit_id = unquote(id_match.group(1))
        if explicit_id:
            return explicit_id
    name_match = field_value_re("name").search(block)
    return ios_slugify(unquote(name_match.group(1)) if name_match else "")


# Render an app dict as an indented YAML list item block.
def render_ios_app_block(app: dict) -> str:
    text = yaml.safe_dump(app, default_flow_style=False, sort_keys=False)
    return "\n".join(("  - " if index == 0 else "    ") + line for index, line in enumerate(text.rstrip("\n").split("\n"))) + "\n"


# Load the iOS templates YAML, or an empty dict when it is missing.
def ios_templates() -> dict:
    path = TEMPLATE_FILES["ios"]
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


# Fill the default launch check and WDA test bundle id, then order fields canonically.
def apply_ios_app_defaults(app: dict) -> dict:
    filled = dict(app)
    if not filled.get("expected_behavior"):
        default_check = ios_templates().get("x-default-launch-check")
        if default_check:
            logger.debug("api: applying the default launch check to iOS app %r.", filled.get("id"))
            filled["expected_behavior"] = deepcopy(default_check)
    if not filled.get("test_bundle_id"):
        wda_bundle_id = get_section("ios", "device").get("updated_wda_bundle_id")
        if wda_bundle_id:
            logger.debug("api: defaulting test_bundle_id of iOS app %r to %s.", filled.get("id"), wda_bundle_id)
            filled["test_bundle_id"] = wda_bundle_id
    ordered = {key: filled[key] for key in IOS_APP_FIELD_ORDER if key in filled}
    ordered.update({key: value for key, value in filled.items() if key not in ordered})
    return ordered


# List the effective iOS apps, raising 422 when the config does not parse.
def list_ios_apps() -> list[dict]:
    config, _ = load_with_errors("ios")
    if config is None:
        logger.debug("api: iOS config unparseable; responding 422 to app listing.")
        raise HTTPException(status_code=422, detail=config_errors("ios"))
    logger.debug("api: listed %d iOS app(s).", len(config.apps))
    return [app.to_dict() for app in config.apps]


# Return one effective iOS app by id, or None.
def effective_ios_app(app_id: str) -> dict | None:
    return next((app for app in list_ios_apps() if app.get("id") == app_id), None)


# Append a new iOS app block, raising 409 on a duplicate and rolling back on new validation errors.
def add_ios_app(app: dict) -> dict:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        _, blocks = split_ios_app_blocks(original_text)
        app = apply_ios_app_defaults(app)
        app_id = app.get("id") or ios_slugify(app.get("name") or "")
        logger.debug("api: adding iOS app %s to %s (%d existing block(s)).", app_id, path, len(blocks))
        if any(ios_block_identity(block) == app_id for block in blocks):
            logger.debug("api: iOS app %s already exists; responding 409.", app_id)
            raise HTTPException(status_code=409, detail={"message": f"App already exists: {app_id}", "app_id": app_id})
        path.write_text(original_text.rstrip("\n") + "\n" + render_ios_app_block(app))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            logger.debug("api: adding iOS app %s failed validation; restoring %s.", app_id, path)
            path.write_text(original_text)
            raise
    logger.debug("api: added iOS app %s.", app_id)
    return {"id": app_id}


# Rewrite an iOS app block with merged updates, rolling back on new validation errors.
def edit_ios_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = split_ios_app_blocks(original_text)
        target_index = next((i for i, block in enumerate(blocks) if ios_block_identity(block) == app_id), None)
        if target_index is None:
            logger.debug("api: iOS app %s has no block in %s; responding 404.", app_id, path)
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        current = effective_ios_app(app_id)
        if current is None:
            logger.debug("api: iOS app %s missing from the effective config; responding 404.", app_id)
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        logger.debug("api: editing iOS app %s (block %d) with keys %s.", app_id, target_index, sorted(updates))
        merged = merge_dicts(current, updates)
        for key in APP_METADATA_KEYS & updates.keys():
            merged[key] = updates[key]
        merged["id"] = app_id
        blocks[target_index] = render_ios_app_block(merged)
        path.write_text(preamble + "".join(blocks))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            logger.debug("api: editing iOS app %s failed validation; restoring %s.", app_id, path)
            path.write_text(original_text)
            raise
    return effective_ios_app(app_id) or merged


# Remove an iOS app block, raising 404 when missing and rolling back on new validation errors.
def delete_ios_app(app_id: str) -> None:
    path = APPS_FILES["ios"]
    with lock_for(path):
        original_text = path.read_text()
        baseline = config_errors("ios")
        preamble, blocks = split_ios_app_blocks(original_text)
        remaining = [block for block in blocks if ios_block_identity(block) != app_id]
        logger.debug("api: deleting iOS app %s from %s (%d -> %d).", app_id, path, len(blocks), len(remaining))
        if len(remaining) == len(blocks):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        path.write_text(preamble + "".join(remaining))
        try:
            load_and_validate("ios", baseline)
        except HTTPException:
            logger.debug("api: deleting iOS app %s failed validation; restoring %s.", app_id, path)
            path.write_text(original_text)
            raise
