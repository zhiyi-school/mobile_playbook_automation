"""
Routes that serve the remediation playbook status, controls, assets and source archives.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import FileResponse

from mobile_playbook.api.schemas import Platform
from mobile_playbook.api.services import playbook as playbook_service

router = APIRouter()

ARCHIVE_MEDIA_TYPE = "application/zip"
logger = logging.getLogger(__name__)


# Return diagnostics for the platform's configured playbook.
@router.get("/platforms/{platform}/playbook/status")
def playbook_status(platform: Platform) -> dict:
    logger.debug("api: GET /platforms/%s/playbook/status.", platform)
    return playbook_service.status(platform)


# Reload the platform's playbook and return its status.
@router.post("/platforms/{platform}/playbook/reload")
def reload_playbook(platform: Platform) -> dict:
    logger.debug("api: POST /platforms/%s/playbook/reload.", platform)
    return playbook_service.reload(platform)


# Return the playbook controls listed for a risk.
@router.get("/platforms/{platform}/risks/{risk_id}/controls")
def risk_controls(platform: Platform, risk_id: str) -> list[dict]:
    logger.debug("api: GET /platforms/%s/risks/%s/controls.", platform, risk_id)
    controls = playbook_service.list_risk_controls(platform, risk_id)
    logger.debug("api: GET /platforms/%s/risks/%s/controls -> %d control(s).", platform, risk_id, len(controls))
    return controls


# Return one playbook control.
@router.get("/platforms/{platform}/controls/{control_id}")
def control_detail(platform: Platform, control_id: str) -> dict:
    logger.debug("api: GET /platforms/%s/controls/%s.", platform, control_id)
    return playbook_service.get_control(platform, control_id)


# Serve an image asset belonging to a playbook control.
@router.get("/platforms/{platform}/controls/{control_id}/assets/{asset_path:path}")
def control_asset(platform: Platform, control_id: str, asset_path: str) -> FileResponse:
    logger.debug("api: GET /platforms/%s/controls/%s/assets/%s.", platform, control_id, asset_path)
    return FileResponse(playbook_service.control_asset(platform, control_id, asset_path))


# Return metadata about a control's implemented-source archive.
@router.get("/platforms/{platform}/controls/{control_id}/source")
def control_source(platform: Platform, control_id: str) -> dict:
    logger.debug("api: GET /platforms/%s/controls/%s/source.", platform, control_id)
    return playbook_service.control_source_metadata(platform, control_id)


# Serve a control's implemented-source archive as a zip download.
@router.get("/platforms/{platform}/controls/{control_id}/source/download")
def control_source_download(platform: Platform, control_id: str) -> FileResponse:
    logger.debug("api: GET /platforms/%s/controls/%s/source/download.", platform, control_id)
    path, file_name = playbook_service.control_source_file(platform, control_id)
    logger.debug("api: serving control source archive %s as %s.", path, file_name)
    return FileResponse(path, media_type=ARCHIVE_MEDIA_TYPE, filename=file_name)
