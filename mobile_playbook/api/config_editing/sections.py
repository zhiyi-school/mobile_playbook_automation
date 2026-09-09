from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from mobile_playbook.api.config_editing.shared import (
    ENTRY_FILES,
    RISK_SETTINGS,
    merge_into_commented,
    plain,
    rt_yaml,
    write_whole_file_validated,
)


def get_section(platform: str, section: str) -> dict:
    data = rt_yaml.load(ENTRY_FILES[platform].read_text())
    return plain(data.get(section) or {})


def put_section(platform: str, section: str, updates: dict) -> dict:
    path = ENTRY_FILES[platform]

    def mutate(data: Any) -> None:
        if not isinstance(data.get(section), dict):
            data[section] = {}
        merge_into_commented(data[section], updates)

    data = write_whole_file_validated(path, mutate, platform)
    return plain(data.get(section) or {})


def risk_settings_target(platform: str, risk_id: str) -> tuple[str, Path]:
    target = (RISK_SETTINGS.get(platform) or {}).get(risk_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No global risk settings for {risk_id} on platform {platform}")
    return target


def get_risk_settings(platform: str, risk_id: str) -> dict:
    field_name, path = risk_settings_target(platform, risk_id)
    data = rt_yaml.load(path.read_text())
    return plain(data.get(field_name) or {})


def put_risk_settings(platform: str, risk_id: str, updates: dict) -> dict:
    field_name, path = risk_settings_target(platform, risk_id)

    def mutate(data: Any) -> None:
        if not isinstance(data.get(field_name), dict):
            data[field_name] = {}
        merge_into_commented(data[field_name], updates)

    data = write_whole_file_validated(path, mutate, platform)
    return plain(data.get(field_name) or {})
