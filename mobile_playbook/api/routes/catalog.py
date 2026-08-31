from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from mobile_playbook.api import config_editor, playbook_assets
from mobile_playbook.api.models import (
    FeatureUpdateRequest,
    Platform,
    RiskDemonstrationUpdateRequest,
    RiskMetadataUpdateRequest,
)
from mobile_playbook.api.services import catalog as catalog_service

router = APIRouter()


@router.get("/platforms/{platform}/risks")
def platform_risks(platform: Platform) -> list[dict]:
    return catalog_service.list_platform_risks(platform)


@router.put("/platforms/{platform}/risks/{risk_id}")
def put_platform_risk(platform: Platform, risk_id: str, body: RiskMetadataUpdateRequest) -> dict:
    return config_editor.put_risk_metadata(platform, risk_id, body.updates())


@router.put("/platforms/{platform}/risks/{risk_id}/demonstration")
def put_platform_risk_demonstration(
    platform: Platform, risk_id: str, body: RiskDemonstrationUpdateRequest
) -> list[dict]:
    stored = config_editor.put_risk_demonstration(platform, risk_id, playbook_assets.strip_derived(body.blocks()))
    return playbook_assets.decorate_demonstration(platform, stored)


@router.get("/platforms/{platform}/playbook/images/{image_path:path}")
def playbook_image(platform: Platform, image_path: str) -> FileResponse:
    resolved = playbook_assets.resolve_image(platform, image_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(resolved)


@router.get("/platforms/{platform}/traffic-interception/proxy.pac")
def traffic_interception_pac(platform: Platform, proxy_host: str | None = None) -> PlainTextResponse:
    return PlainTextResponse(
        catalog_service.traffic_interception_pac(platform, proxy_host),
        media_type="application/x-ns-proxy-autoconfig",
    )


@router.get("/platforms/{platform}/features")
def platform_features(platform: Platform) -> list[dict]:
    return config_editor.list_features(platform)


@router.put("/platforms/{platform}/features/{feature_id}")
def put_platform_feature(platform: Platform, feature_id: str, body: FeatureUpdateRequest) -> dict:
    return config_editor.put_feature(platform, feature_id, body.updates())
