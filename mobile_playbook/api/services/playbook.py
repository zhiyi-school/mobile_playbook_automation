"""Serve the developer remediation playbook: control catalogue, assets, source archives."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from mobile_playbook.api.models import Platform
from mobile_playbook.playbook import catalogue, controls as control_parser, source

SOURCE_DOWNLOAD_ENV = "PLAYBOOK_SOURCE_DOWNLOAD_ENABLED"
SUMMARY_FIELDS = ("control_id", "risk_id", "title", "status", "required", "step_count", "playbook_revision")
logger = logging.getLogger(__name__)


def source_download_enabled() -> bool:
    raw = os.environ.get(SOURCE_DOWNLOAD_ENV)
    logger.debug("api: %s=%r.", SOURCE_DOWNLOAD_ENV, raw)
    return True if raw is None else raw.strip().lower() not in {"0", "false", "no", "off"}


def _catalogue(platform: Platform) -> dict[str, Any]:
    try:
        return catalogue.get(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook unavailable; responding 503: %s", platform, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def risk_control_summaries(platform: Platform) -> tuple[dict[str, list[dict]], str | None]:
    """Control summaries keyed by risk id, plus the reason they are missing when the playbook is unreadable."""
    try:
        index = catalogue.get(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook unavailable for control summaries: %s", platform, exc)
        return {}, str(exc)
    grouped: dict[str, list[dict]] = {}
    for risk_id, risk in index["risks"].items():
        grouped[risk_id] = [summarize(index["controls"][cid]) for cid in risk["controls"] if cid in index["controls"]]
    logger.debug("api: summarized controls for %d %s risk(s).", len(grouped), platform)
    return grouped, None


def risk_overview(platform: Platform, risk_id: str) -> dict[str, Any] | None:
    """The risk's authored Title, Description and MITRE tactic, or None when the playbook has no such document."""
    try:
        index = catalogue.get(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook unavailable for risk overview %s: %s", platform, risk_id, exc)
        return None
    risk = index["risks"].get(catalogue.canonical_id(control_parser.document_id(risk_id), platform))
    if risk is None:
        logger.debug("api: no playbook document for %s risk %s.", platform, risk_id)
        return None
    return {field: risk.get(field) for field in ("title", "description", "tactic", "tactic_id")}


def risk_demonstration(platform: Platform, risk_id: str) -> list[dict[str, Any]] | None:
    """The risk's manual-testing steps from Markdown, or None when the document declares none."""
    try:
        index = catalogue.get(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook unavailable for demonstration %s: %s", platform, risk_id, exc)
        return None
    risk = index["risks"].get(catalogue.canonical_id(control_parser.document_id(risk_id), platform))
    logger.debug(
        "api: playbook demonstration for %s risk %s present=%s.", platform, risk_id, bool(risk and risk.get("demonstration"))
    )
    return copy.deepcopy(risk["demonstration"]) if risk and risk.get("demonstration") else None


def summarize(control: dict[str, Any]) -> dict[str, Any]:
    summary = {field: control.get(field) for field in SUMMARY_FIELDS}
    summary["has_source_archive"] = bool(control.get("source_download_url"))
    return summary


def list_risk_controls(platform: Platform, risk_id: str) -> list[dict[str, Any]]:
    index = _catalogue(platform)
    key = catalogue.canonical_id(control_parser.document_id(risk_id), platform)
    risk = index["risks"].get(key)
    if risk is None:
        logger.debug("api: playbook risk %s (key %s) not found for %s; responding 404.", risk_id, key, platform)
        raise HTTPException(status_code=404, detail=f"No playbook risk {risk_id} for platform {platform}")
    logger.debug("api: playbook risk %s lists %d control id(s).", key, len(risk["controls"]))
    return [index["controls"][cid] for cid in risk["controls"] if cid in index["controls"]]


def get_control(platform: Platform, control_id: str) -> dict[str, Any]:
    index = _catalogue(platform)
    key = catalogue.canonical_id(control_parser.document_id(control_id), platform)
    control = index["controls"].get(key)
    if control is None:
        logger.debug("api: playbook control %s (key %s) not found for %s; responding 404.", control_id, key, platform)
        raise HTTPException(status_code=404, detail=f"No playbook control {control_id} for platform {platform}")
    logger.debug("api: playbook control %s found (revision %s).", key, control.get("playbook_revision"))
    return control


def control_asset(platform: Platform, control_id: str, asset_path: str) -> Path:
    get_control(platform, control_id)
    root = source.require_root(platform)
    resolved = source.resolve_within(root, asset_path)
    if resolved is None or not resolved.is_file() or resolved.suffix.lower() not in source.IMAGE_SUFFIXES:
        logger.debug(
            "api: playbook asset %r under %s rejected (resolved=%s); responding 404.", asset_path, root, resolved
        )
        raise HTTPException(status_code=404, detail="Playbook asset not found")
    logger.debug("api: playbook asset %r resolved to %s.", asset_path, resolved)
    return resolved


def control_source_metadata(platform: Platform, control_id: str) -> dict[str, Any]:
    control = get_control(platform, control_id)
    archives = [archive for archive in control.get("source_archives") or [] if archive.get("exists")]
    declared = control.get("source_archives") or []
    logger.debug("api: control %s declares %d archive(s), %d present.", control_id, len(declared), len(archives))
    if not archives:
        return {
            "control_id": control["control_id"],
            "exists": False,
            "download_enabled": source_download_enabled(),
            "declared": [{"path": archive["path"], "exists": False} for archive in declared],
        }
    archive = archives[0]
    return {
        "control_id": control["control_id"],
        "exists": True,
        "download_enabled": source_download_enabled(),
        "file_name": archive["file_name"],
        "path": archive["path"],
        "size_bytes": archive["size_bytes"],
        "sha256": archive["sha256"],
        "download_url": f"/platforms/{platform}/controls/{control['control_id']}/source/download",
        "declared": [{"path": item["path"], "exists": item["exists"]} for item in declared],
    }


def control_source_file(platform: Platform, control_id: str) -> tuple[Path, str]:
    metadata = control_source_metadata(platform, control_id)
    if not metadata["exists"]:
        logger.debug("api: control %s has no archive; responding 404.", control_id)
        raise HTTPException(status_code=404, detail="No implemented-control archive for this control")
    if not source_download_enabled():
        logger.debug("api: source archive downloads disabled; responding 403 for %s.", control_id)
        raise HTTPException(
            status_code=403,
            detail=f"Source archive downloads are disabled ({SOURCE_DOWNLOAD_ENV}=false)",
        )
    root = source.require_root(platform)
    resolved = source.resolve_within(root, metadata["path"])
    if resolved is None or not resolved.is_file():
        logger.debug(
            "api: archive %r under %s not found (resolved=%s); responding 404.", metadata["path"], root, resolved
        )
        raise HTTPException(status_code=404, detail="Implemented-control archive not found")
    logger.debug("api: control %s archive resolved to %s.", control_id, resolved)
    return resolved, metadata["file_name"]


def status(platform: Platform) -> dict[str, Any]:
    """Diagnostics for the configured playbook directory — always answers, never raises."""
    configured = source.configured_root(platform)
    payload: dict[str, Any] = {
        "platform": platform,
        "env_key": source.playbook_dir_env_key(platform),
        "configured_path": str(configured) if configured else None,
        "readable": False,
        "risk_count": 0,
        "control_count": 0,
        "warnings": [],
        "revision": None,
        "error": None,
        "source_download_enabled": source_download_enabled(),
    }
    try:
        index = catalogue.get(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook status unreadable: %s", platform, exc)
        payload["error"] = str(exc)
        return payload
    logger.debug(
        "api: %s playbook has %d risk(s), %d control(s), revision %s.",
        platform,
        len(index["risks"]),
        len(index["controls"]),
        index["revision"],
    )
    payload.update(
        readable=True,
        risk_count=len(index["risks"]),
        control_count=len(index["controls"]),
        warnings=index["warnings"],
        revision=index["revision"],
    )
    return payload


def reload(platform: Platform) -> dict[str, Any]:
    logger.debug("api: reloading %s playbook.", platform)
    try:
        catalogue.reload(platform)
    except source.PlaybookUnavailableError as exc:
        logger.debug("api: %s playbook reload failed; responding 503: %s", platform, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return status(platform)
