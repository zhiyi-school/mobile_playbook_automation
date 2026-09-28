from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def get_section(platform: str, section: str) -> dict:
    logger.debug("api: reading section %s of %s.", section, ENTRY_FILES[platform])
    data = rt_yaml.load(ENTRY_FILES[platform].read_text())
    return plain(data.get(section) or {})


def put_section(platform: str, section: str, updates: dict) -> dict:
    path = ENTRY_FILES[platform]
    logger.debug("api: updating section %s of %s with keys %s.", section, path, sorted(updates))

    def mutate(data: Any) -> None:
        if not isinstance(data.get(section), dict):
            logger.debug("api: section %s missing or not a mapping in %s; creating it.", section, path)
            data[section] = {}
        merge_into_commented(data[section], updates)

    data = write_whole_file_validated(path, mutate, platform)
    return plain(data.get(section) or {})


def risk_settings_target(platform: str, risk_id: str) -> tuple[str, Path]:
    target = (RISK_SETTINGS.get(platform) or {}).get(risk_id)
    if target is None:
        logger.debug("api: no global settings file for %s risk %s; responding 404.", platform, risk_id)
        raise HTTPException(status_code=404, detail=f"No global risk settings for {risk_id} on platform {platform}")
    logger.debug("api: risk %s settings live in %s field %s.", risk_id, target[1], target[0])
    return target


def get_risk_settings(platform: str, risk_id: str) -> dict:
    field_name, path = risk_settings_target(platform, risk_id)
    data = rt_yaml.load(path.read_text())
    return plain(data.get(field_name) or {})


def put_risk_settings(platform: str, risk_id: str, updates: dict) -> dict:
    field_name, path = risk_settings_target(platform, risk_id)
    logger.debug("api: updating risk settings %s in %s with keys %s.", field_name, path, sorted(updates))

    def mutate(data: Any) -> None:
        if not isinstance(data.get(field_name), dict):
            data[field_name] = {}
        merge_into_commented(data[field_name], updates)

    data = write_whole_file_validated(path, mutate, platform)
    return plain(data.get(field_name) or {})
