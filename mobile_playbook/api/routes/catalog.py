from __future__ import annotations

import logging

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
logger = logging.getLogger(__name__)


@router.get("/platforms/{platform}/risks")
def platform_risks(platform: Platform) -> list[dict]:
    logger.debug("api: GET /platforms/%s/risks.", platform)
    risks = catalog_service.list_platform_risks(platform)
    logger.debug("api: GET /platforms/%s/risks -> %d risk(s).", platform, len(risks))
    return risks


@router.put("/platforms/{platform}/risks/{risk_id}")
def put_platform_risk(platform: Platform, risk_id: str, body: RiskMetadataUpdateRequest) -> dict:
    logger.debug("api: PUT /platforms/%s/risks/%s fields=%s.", platform, risk_id, sorted(body.model_fields_set))
    return config_editor.put_risk_metadata(platform, risk_id, body.updates())


@router.put("/platforms/{platform}/risks/{risk_id}/demonstration")
def put_platform_risk_demonstration(
    platform: Platform, risk_id: str, body: RiskDemonstrationUpdateRequest
) -> list[dict]:
    logger.debug("api: PUT /platforms/%s/risks/%s/demonstration.", platform, risk_id)
    stored = config_editor.put_risk_demonstration(platform, risk_id, playbook_assets.strip_derived(body.blocks()))
    logger.debug("api: stored %d demonstration block(s) for %s/%s.", len(stored), platform, risk_id)
    return playbook_assets.decorate_demonstration(platform, stored)


@router.get("/platforms/{platform}/playbook/images/{image_path:path}")
def playbook_image(platform: Platform, image_path: str) -> FileResponse:
    logger.debug("api: GET /platforms/%s/playbook/images/%s.", platform, image_path)
    resolved = playbook_assets.resolve_image(platform, image_path)
    if resolved is None:
        logger.debug("api: playbook image %r not found for %s; responding 404.", image_path, platform)
        raise HTTPException(status_code=404, detail="Image not found")
    logger.debug("api: serving playbook image %s.", resolved)
    return FileResponse(resolved)


@router.get("/platforms/{platform}/traffic-interception/proxy.pac")
def traffic_interception_pac(platform: Platform, proxy_host: str | None = None) -> PlainTextResponse:
    logger.debug("api: GET /platforms/%s/traffic-interception/proxy.pac proxy_host=%r.", platform, proxy_host)
    return PlainTextResponse(
        catalog_service.traffic_interception_pac(platform, proxy_host),
        media_type="application/x-ns-proxy-autoconfig",
    )


@router.get("/platforms/{platform}/features")
def platform_features(platform: Platform) -> list[dict]:
    logger.debug("api: GET /platforms/%s/features.", platform)
    features = config_editor.list_features(platform)
    logger.debug("api: GET /platforms/%s/features -> %d feature(s).", platform, len(features))
    return features


@router.put("/platforms/{platform}/features/{feature_id}")
def put_platform_feature(platform: Platform, feature_id: str, body: FeatureUpdateRequest) -> dict:
    logger.debug("api: PUT /platforms/%s/features/%s fields=%s.", platform, feature_id, sorted(body.model_fields_set))
    return config_editor.put_feature(platform, feature_id, body.updates())
