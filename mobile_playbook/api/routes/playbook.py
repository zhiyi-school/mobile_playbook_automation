from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from mobile_playbook.api.models import Platform
from mobile_playbook.api.services import playbook as playbook_service

router = APIRouter()

ARCHIVE_MEDIA_TYPE = "application/zip"


@router.get("/platforms/{platform}/playbook/status")
def playbook_status(platform: Platform) -> dict:
    return playbook_service.status(platform)


@router.post("/platforms/{platform}/playbook/reload")
def reload_playbook(platform: Platform) -> dict:
    return playbook_service.reload(platform)


@router.get("/platforms/{platform}/risks/{risk_id}/controls")
def risk_controls(platform: Platform, risk_id: str) -> list[dict]:
    return playbook_service.list_risk_controls(platform, risk_id)


@router.get("/platforms/{platform}/controls/{control_id}")
def control_detail(platform: Platform, control_id: str) -> dict:
    return playbook_service.get_control(platform, control_id)


@router.get("/platforms/{platform}/controls/{control_id}/assets/{asset_path:path}")
def control_asset(platform: Platform, control_id: str, asset_path: str) -> FileResponse:
    return FileResponse(playbook_service.control_asset(platform, control_id, asset_path))


@router.get("/platforms/{platform}/controls/{control_id}/source")
def control_source(platform: Platform, control_id: str) -> dict:
    return playbook_service.control_source_metadata(platform, control_id)


@router.get("/platforms/{platform}/controls/{control_id}/source/download")
def control_source_download(platform: Platform, control_id: str) -> FileResponse:
    path, file_name = playbook_service.control_source_file(platform, control_id)
    return FileResponse(path, media_type=ARCHIVE_MEDIA_TYPE, filename=file_name)
