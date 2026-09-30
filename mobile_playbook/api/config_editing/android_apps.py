"""
Lists, adds, edits and deletes Android apps in the split apps YAML.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from mobile_playbook.api.config_editing.shared import (
    APPS_FILES,
    merge_into_commented,
    plain,
    rt_yaml,
    write_whole_file_validated,
)
from mobile_playbook.common.storage_paths import config_path
from mobile_playbook.platforms.android.config import _slugify as android_slugify

APP_METADATA_KEYS = {"sector", "agency", "version", "cisos"}
logger = logging.getLogger(__name__)


# Return an Android app's id, or a slug of its package name or name.
def android_app_id(item: dict) -> str:
    return item.get("id") or android_slugify(item.get("package_name") or item.get("name") or "")


# List Android apps from the apps YAML with id and metadata defaults filled in.
def list_android_apps() -> list[dict]:
    data = rt_yaml.load(config_path(APPS_FILES["android"]).read_text())
    items = [plain(item) for item in (data.get("apps") or [])]
    for item in items:
        item.setdefault("id", android_app_id(item))
        item.setdefault("sector", "")
        item.setdefault("agency", "")
        item.setdefault("version", "")
        item.setdefault("cisos", [])
    logger.debug("api: listed %d Android app(s) from %s.", len(items), config_path(APPS_FILES["android"]))
    return items


# Append a new Android app, raising 409 when its id already exists.
def add_android_app(app: dict) -> dict:
    path = config_path(APPS_FILES["android"])

    # Append the app unless an app with the same id exists.
    def mutate(data: Any) -> None:
        apps = data.setdefault("apps", [])
        app_id = android_app_id(app)
        logger.debug("api: adding Android app %s to %s.", app_id, path)
        if any(android_app_id(plain(item)) == app_id for item in apps):
            logger.debug("api: Android app %s already exists; responding 409.", app_id)
            raise HTTPException(status_code=409, detail={"message": f"App already exists: {app_id}", "app_id": app_id})
        new_app = dict(app)
        new_app.setdefault("id", app_id)
        apps.append(new_app)

    write_whole_file_validated(path, mutate, "android")
    return {"id": android_app_id(app)}


# Merge updates into an Android app and return the saved entry, or raise 404.
def edit_android_app(app_id: str, updates: dict) -> dict:
    path = config_path(APPS_FILES["android"])

    # Merge the updates into the matching app, replacing metadata keys outright.
    def mutate(data: Any) -> None:
        for item in data.get("apps") or []:
            if android_app_id(plain(item)) == app_id:
                logger.debug("api: editing Android app %s in %s with keys %s.", app_id, path, sorted(updates))
                merge_into_commented(item, updates)
                for key in APP_METADATA_KEYS & updates.keys():
                    item[key] = updates[key]
                item["id"] = app_id
                return
        logger.debug("api: Android app %s not found in %s; responding 404.", app_id, path)
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

    data = write_whole_file_validated(path, mutate, "android")
    for item in data.get("apps") or []:
        if android_app_id(plain(item)) == app_id:
            return plain(item)
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")


# Remove an Android app, raising 404 when it does not exist.
def delete_android_app(app_id: str) -> None:
    path = config_path(APPS_FILES["android"])

    # Drop the matching app from the apps list.
    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        remaining = [item for item in apps if android_app_id(plain(item)) != app_id]
        logger.debug("api: deleting Android app %s from %s (%d -> %d).", app_id, path, len(apps), len(remaining))
        if len(remaining) == len(apps):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        data["apps"] = remaining

    write_whole_file_validated(path, mutate, "android")
