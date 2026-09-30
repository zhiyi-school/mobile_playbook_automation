"""
Routes for validating configs and editing apps, risk settings, device and runner sections.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from mobile_playbook.api import config_editing
from mobile_playbook.api.services import provisioning
from mobile_playbook.api.dependencies import load_config_or_400
from mobile_playbook.api.services import artifacts as artifact_service
from mobile_playbook.api.schemas import (
    ConfigAppRequest,
    DeviceUpdateRequest,
    Platform,
    RiskSettingsUpdateRequest,
    RunnerUpdateRequest,
    ValidateRequest,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# Validate a platform config file.
@router.post("/config/validate")
def validate_config(body: ValidateRequest) -> dict:
    logger.debug("api: POST /config/validate platform=%s config_path=%s.", body.platform, body.config_path)
    load_config_or_400(body.platform, body.config_path)
    logger.debug("api: config %s is valid.", body.config_path)
    return {"valid": True}


_APPS_BY_PLATFORM = {
    "ios": (
        config_editing.list_ios_apps,
        config_editing.add_ios_app,
        config_editing.edit_ios_app,
        config_editing.delete_ios_app,
    ),
    "android": (
        config_editing.list_android_apps,
        config_editing.add_android_app,
        config_editing.edit_android_app,
        config_editing.delete_android_app,
    ),
}


# Return the platform's list, add, edit and delete app functions.
def _apps_ops(platform: Platform):
    return _APPS_BY_PLATFORM[platform]


# List the platform's configured apps.
@router.get("/config/{platform}/apps")
def list_config_apps(platform: Platform) -> list[dict]:
    logger.debug("api: GET /config/%s/apps.", platform)
    list_fn, _, _, _ = _apps_ops(platform)
    apps = list_fn()
    logger.debug("api: GET /config/%s/apps -> %d app(s).", platform, len(apps))
    return apps


# Return one configured app, or 404.
@router.get("/config/{platform}/apps/{app_id}")
def get_config_app(platform: Platform, app_id: str) -> dict:
    logger.debug("api: GET /config/%s/apps/%s.", platform, app_id)
    list_fn, _, _, _ = _apps_ops(platform)
    for app_entry in list_fn():
        if app_entry.get("id") == app_id:
            return app_entry
    logger.debug("api: %s app %s not found; responding 404.", platform, app_id)
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")


# Add an app to the platform config.
@router.post("/config/{platform}/apps", status_code=201)
def add_config_app(platform: Platform, body: ConfigAppRequest) -> dict:
    logger.debug("api: POST /config/%s/apps fields=%s.", platform, sorted(body.model_fields_set))
    _, add_fn, _, _ = _apps_ops(platform)
    return add_fn(body.updates())


# Update a configured app.
@router.put("/config/{platform}/apps/{app_id}")
def edit_config_app(platform: Platform, app_id: str, body: ConfigAppRequest) -> dict:
    logger.debug("api: PUT /config/%s/apps/%s fields=%s.", platform, app_id, sorted(body.model_fields_set))
    _, _, edit_fn, _ = _apps_ops(platform)
    return edit_fn(app_id, body.updates())


# Delete a configured app.
@router.delete("/config/{platform}/apps/{app_id}", status_code=204)
def delete_config_app(platform: Platform, app_id: str) -> None:
    logger.debug("api: DELETE /config/%s/apps/%s.", platform, app_id)
    _, _, _, delete_fn = _apps_ops(platform)
    delete_fn(app_id)


# Return an app's setup stages and execution readiness.
@router.get("/config/{platform}/apps/{app_id}/provisioning")
def get_app_provisioning(platform: Platform, app_id: str) -> dict:
    logger.debug("api: GET /config/%s/apps/%s/provisioning.", platform, app_id)
    return provisioning.describe(platform, app_id)


# The app's icon as PNG. 404 covers unknown apps and apps with no readable icon alike.
@router.get(
    "/config/{platform}/apps/{app_id}/icon",
    description="The app's icon as PNG. 404 covers unknown apps and apps with no readable icon alike.",
)
def get_app_icon(platform: Platform, app_id: str, request: Request) -> Response:
    logger.debug("api: GET /config/%s/apps/%s/icon.", platform, app_id)
    resolved = artifact_service.app_icon_file_path(platform, app_id)
    if resolved is None:
        logger.debug("api: no icon for %s app %s; responding 404.", platform, app_id)
        raise HTTPException(status_code=404, detail="No icon is available for this app")

    path, artifact_id = resolved
    etag = f'"{artifact_id}"'
    headers = {"Cache-Control": artifact_service.ICON_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        logger.debug("api: icon for %s app %s unchanged (ETag %s); responding 304.", platform, app_id, etag)
        return Response(status_code=304, headers=headers)
    logger.debug("api: serving icon %s for %s app %s.", path, platform, app_id)
    return FileResponse(path, media_type="image/png", headers=headers)


# Return a risk's global settings.
@router.get("/config/{platform}/risk-settings/{risk_id}")
def get_config_risk_settings(platform: Platform, risk_id: str) -> dict:
    logger.debug("api: GET /config/%s/risk-settings/%s.", platform, risk_id)
    return config_editing.get_risk_settings(platform, risk_id)


# Update a risk's global settings.
@router.put("/config/{platform}/risk-settings/{risk_id}")
def put_config_risk_settings(platform: Platform, risk_id: str, body: RiskSettingsUpdateRequest) -> dict:
    logger.debug("api: PUT /config/%s/risk-settings/%s keys=%s.", platform, risk_id, sorted(body.root))
    return config_editing.put_risk_settings(platform, risk_id, body.root)


# Return the platform config's device section.
@router.get("/config/{platform}/device")
def get_config_device(platform: Platform) -> dict:
    logger.debug("api: GET /config/%s/device.", platform)
    return config_editing.get_section(platform, "device")


# Update the platform config's device section.
@router.put("/config/{platform}/device")
def put_config_device(platform: Platform, body: DeviceUpdateRequest) -> dict:
    logger.debug("api: PUT /config/%s/device fields=%s.", platform, sorted(body.model_fields_set))
    return config_editing.put_section(platform, "device", body.updates())


# Return the platform config's runner section.
@router.get("/config/{platform}/runner")
def get_config_runner(platform: Platform) -> dict:
    logger.debug("api: GET /config/%s/runner.", platform)
    return config_editing.get_section(platform, "runner")


# Update the platform config's runner section.
@router.put("/config/{platform}/runner")
def put_config_runner(platform: Platform, body: RunnerUpdateRequest) -> dict:
    logger.debug("api: PUT /config/%s/runner fields=%s.", platform, sorted(body.model_fields_set))
    return config_editing.put_section(platform, "runner", body.updates())
