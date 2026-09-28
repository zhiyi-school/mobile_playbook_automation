"""
Reads and updates feature, risk metadata and demonstration entries in the split YAML.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from mobile_playbook.api.config_editing.shared import (
    FEATURES_FILES,
    RISK_FILES,
    merge_into_commented,
    plain,
    rt_yaml,
    write_whole_file_validated,
)
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks

RISK_METADATA_FIELDS = ("name", "description", "tactic")
logger = logging.getLogger(__name__)


# Return the platform's feature ids in risk order, without duplicates.
def known_feature_ids(platform: str) -> list[str]:
    risks = list_ios_risks() if platform == "ios" else list_android_risks()
    seen: list[str] = []
    for risk in risks:
        if risk["feature_id"] not in seen:
            seen.append(risk["feature_id"])
    return seen


# List every known feature with its stored name and description.
def list_features(platform: str) -> list[dict]:
    stored = plain(rt_yaml.load(FEATURES_FILES[platform].read_text()) or {})
    return [
        {
            "feature_id": feature_id,
            "name": (stored.get(feature_id) or {}).get("name", ""),
            "description": (stored.get(feature_id) or {}).get("description", ""),
        }
        for feature_id in known_feature_ids(platform)
    ]


# Merge updates into a known feature and return its saved name and description.
def put_feature(platform: str, feature_id: str, updates: dict) -> dict:
    if feature_id not in known_feature_ids(platform):
        logger.debug("api: %s feature %s unknown; responding 404.", platform, feature_id)
        raise HTTPException(status_code=404, detail=f"Unknown feature_id: {feature_id}")
    path = FEATURES_FILES[platform]
    logger.debug("api: updating feature %s in %s with keys %s.", feature_id, path, sorted(updates))

    # Create the feature entry if needed and merge the updates into it.
    def mutate(data: Any) -> None:
        if not isinstance(data.get(feature_id), dict):
            data[feature_id] = {}
        merge_into_commented(data[feature_id], updates)

    data = write_whole_file_validated(path, mutate, platform)
    entry = plain(data.get(feature_id) or {})
    return {"feature_id": feature_id, "name": entry.get("name", ""), "description": entry.get("description", "")}


# Raise 404 unless the risk id is known on the platform.
def require_known_risk(platform: str, risk_id: str) -> None:
    known = list_ios_risks() if platform == "ios" else list_android_risks()
    if risk_id not in {risk["risk_id"] for risk in known}:
        logger.debug("api: %s risk %s unknown; responding 404.", platform, risk_id)
        raise HTTPException(status_code=404, detail=f"Unknown risk_id: {risk_id}")


# Return a risk's entry from the risks YAML, or an empty dict.
def risk_entry(platform: str, risk_id: str) -> dict:
    try:
        data = plain(rt_yaml.load(RISK_FILES[platform].read_text()) or {})
    except OSError:
        logger.debug("api: risk file %s unreadable; no entry for %s.", RISK_FILES[platform], risk_id, exc_info=True)
        return {}
    entry = data.get(risk_id)
    logger.debug("api: risk entry %s in %s present=%s.", risk_id, RISK_FILES[platform], isinstance(entry, dict))
    return entry if isinstance(entry, dict) else {}


# Return a known risk's stored name, description and tactic.
def get_risk_metadata(platform: str, risk_id: str) -> dict:
    require_known_risk(platform, risk_id)
    entry = risk_entry(platform, risk_id)
    return {field: entry[field] for field in RISK_METADATA_FIELDS if field in entry}


# Merge allowed metadata fields into a risk entry and return the saved metadata.
def put_risk_metadata(platform: str, risk_id: str, updates: dict) -> dict:
    require_known_risk(platform, risk_id)
    unknown = sorted(set(updates) - set(RISK_METADATA_FIELDS))
    if unknown:
        logger.debug("api: risk %s update has unknown fields %s; responding 422.", risk_id, unknown)
        raise HTTPException(status_code=422, detail=f"Unknown risk fields: {', '.join(unknown)}")
    path = RISK_FILES[platform]
    logger.debug("api: updating risk metadata %s in %s with keys %s.", risk_id, path, sorted(updates))

    # Create the risk entry if needed and merge the updates into it.
    def mutate(data: Any) -> None:
        if not isinstance(data.get(risk_id), dict):
            data[risk_id] = {}
        merge_into_commented(data[risk_id], updates)

    write_whole_file_validated(path, mutate, platform)
    return get_risk_metadata(platform, risk_id)


# Return a known risk's YAML demonstration blocks.
def get_risk_demonstration(platform: str, risk_id: str) -> list:
    require_known_risk(platform, risk_id)
    return risk_entry(platform, risk_id).get("demonstration") or []


# Replace a risk's YAML demonstration and return the saved blocks.
def put_risk_demonstration(platform: str, risk_id: str, demonstration: list) -> list:
    require_known_risk(platform, risk_id)
    path = RISK_FILES[platform]
    logger.debug("api: replacing demonstration for %s in %s with %d block(s).", risk_id, path, len(demonstration))

    # Create the risk entry if needed and set its demonstration.
    def mutate(data: Any) -> None:
        if not isinstance(data.get(risk_id), dict):
            data[risk_id] = {}
        data[risk_id]["demonstration"] = demonstration

    data = write_whole_file_validated(path, mutate, platform)
    return (data.get(risk_id) or {}).get("demonstration") or []
