from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from mobile_playbook.api import config_editor, provisioning
from mobile_playbook.api.dependencies import load_config_or_400
from mobile_playbook.api.services import artifacts as artifact_service
from mobile_playbook.api.models import (
    ConfigAppRequest,
    DeviceUpdateRequest,
    Platform,
    RiskSettingsUpdateRequest,
    RunnerUpdateRequest,
    ValidateRequest,
)

router = APIRouter()


@router.post("/config/validate")
def validate_config(body: ValidateRequest) -> dict:
    load_config_or_400(body.platform, body.config_path)
    return {"valid": True}


_APPS_BY_PLATFORM = {
    "ios": (
        config_editor.list_ios_apps,
        config_editor.add_ios_app,
        config_editor.edit_ios_app,
        config_editor.delete_ios_app,
    ),
    "android": (
        config_editor.list_android_apps,
        config_editor.add_android_app,
        config_editor.edit_android_app,
        config_editor.delete_android_app,
    ),
}


def _apps_ops(platform: Platform):
    return _APPS_BY_PLATFORM[platform]


@router.get("/config/{platform}/apps")
def list_config_apps(platform: Platform) -> list[dict]:
    list_fn, _, _, _ = _apps_ops(platform)
    return list_fn()


@router.get("/config/{platform}/apps/{app_id}")
def get_config_app(platform: Platform, app_id: str) -> dict:
    list_fn, _, _, _ = _apps_ops(platform)
    for app_entry in list_fn():
        if app_entry.get("id") == app_id:
            return app_entry
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")


@router.post("/config/{platform}/apps", status_code=201)
def add_config_app(platform: Platform, body: ConfigAppRequest) -> dict:
    _, add_fn, _, _ = _apps_ops(platform)
    return add_fn(body.updates())


@router.put("/config/{platform}/apps/{app_id}")
def edit_config_app(platform: Platform, app_id: str, body: ConfigAppRequest) -> dict:
    _, _, edit_fn, _ = _apps_ops(platform)
    return edit_fn(app_id, body.updates())


@router.delete("/config/{platform}/apps/{app_id}", status_code=204)
def delete_config_app(platform: Platform, app_id: str) -> None:
    _, _, _, delete_fn = _apps_ops(platform)
    delete_fn(app_id)


@router.get("/config/{platform}/apps/{app_id}/provisioning")
def get_app_provisioning(platform: Platform, app_id: str) -> dict:
    return provisioning.describe(platform, app_id)


@router.get("/config/{platform}/apps/{app_id}/icon")
def get_app_icon(platform: Platform, app_id: str, request: Request) -> Response:
    """The app's icon as PNG. 404 covers unknown apps and apps with no readable icon alike."""
    resolved = artifact_service.app_icon_file_path(platform, app_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="No icon is available for this app")

    path, artifact_id = resolved
    etag = f'"{artifact_id}"'
    headers = {"Cache-Control": artifact_service.ICON_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/png", headers=headers)


@router.get("/config/{platform}/risk-settings/{risk_id}")
def get_config_risk_settings(platform: Platform, risk_id: str) -> dict:
    return config_editor.get_risk_settings(platform, risk_id)


@router.put("/config/{platform}/risk-settings/{risk_id}")
def put_config_risk_settings(platform: Platform, risk_id: str, body: RiskSettingsUpdateRequest) -> dict:
    return config_editor.put_risk_settings(platform, risk_id, body.root)


@router.get("/config/{platform}/device")
def get_config_device(platform: Platform) -> dict:
    return config_editor.get_section(platform, "device")


@router.put("/config/{platform}/device")
def put_config_device(platform: Platform, body: DeviceUpdateRequest) -> dict:
    return config_editor.put_section(platform, "device", body.updates())


@router.get("/config/{platform}/runner")
def get_config_runner(platform: Platform) -> dict:
    return config_editor.get_section(platform, "runner")


@router.put("/config/{platform}/runner")
def put_config_runner(platform: Platform, body: RunnerUpdateRequest) -> dict:
    return config_editor.put_section(platform, "runner", body.updates())
