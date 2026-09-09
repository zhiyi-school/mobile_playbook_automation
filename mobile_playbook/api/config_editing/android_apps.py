from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from mobile_playbook.api.config_editing.shared import (
    APPS_FILES,
    merge_into_commented,
    plain,
    rt_yaml,
    write_whole_file_validated,
)
from mobile_playbook.platforms.android.config import _slugify as android_slugify

APP_METADATA_KEYS = {"sector", "agency", "version", "cisos"}


def android_app_id(item: dict) -> str:
    return item.get("id") or android_slugify(item.get("package_name") or item.get("name") or "")


def list_android_apps() -> list[dict]:
    data = rt_yaml.load(APPS_FILES["android"].read_text())
    items = [plain(item) for item in (data.get("apps") or [])]
    for item in items:
        item.setdefault("id", android_app_id(item))
        item.setdefault("sector", "")
        item.setdefault("agency", "")
        item.setdefault("version", "")
        item.setdefault("cisos", [])
    return items


def add_android_app(app: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.setdefault("apps", [])
        app_id = android_app_id(app)
        if any(android_app_id(plain(item)) == app_id for item in apps):
            raise HTTPException(status_code=409, detail={"message": f"App already exists: {app_id}", "app_id": app_id})
        new_app = dict(app)
        new_app.setdefault("id", app_id)
        apps.append(new_app)

    write_whole_file_validated(path, mutate, "android")
    return {"id": android_app_id(app)}


def edit_android_app(app_id: str, updates: dict) -> dict:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        for item in data.get("apps") or []:
            if android_app_id(plain(item)) == app_id:
                merge_into_commented(item, updates)
                for key in APP_METADATA_KEYS & updates.keys():
                    item[key] = updates[key]
                item["id"] = app_id
                return
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")

    data = write_whole_file_validated(path, mutate, "android")
    for item in data.get("apps") or []:
        if android_app_id(plain(item)) == app_id:
            return plain(item)
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")


def delete_android_app(app_id: str) -> None:
    path = APPS_FILES["android"]

    def mutate(data: Any) -> None:
        apps = data.get("apps") or []
        remaining = [item for item in apps if android_app_id(plain(item)) != app_id]
        if len(remaining) == len(apps):
            raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
        data["apps"] = remaining

    write_whole_file_validated(path, mutate, "android")
