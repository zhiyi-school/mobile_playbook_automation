from __future__ import annotations

import pytest
from pydantic import ValidationError

from mobile_playbook.api import config_editor
from mobile_playbook.api.cors import cors_allowed_origins
from mobile_playbook.api.models import (
    ConfigAppRequest,
    DeviceUpdateRequest,
    RiskDemonstrationUpdateRequest,
    RiskMetadataUpdateRequest,
    RiskSettingsUpdateRequest,
    RunnerUpdateRequest,
)
from mobile_playbook.api.routes import catalog, config


def test_cors_origins_default_to_local_vite(monkeypatch, tmp_path):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    assert cors_allowed_origins(tmp_path / "absent.env") == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_cors_origins_reject_wildcard(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://dashboard.example.com,*")

    with pytest.raises(RuntimeError):
        cors_allowed_origins()


def test_risk_metadata_body_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RiskMetadataUpdateRequest.model_validate({"automation_available": False})


def test_risk_metadata_body_allows_null_tactic(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        config_editor,
        "put_risk_metadata",
        lambda platform, risk_id, updates: seen.setdefault("updates", updates),
    )

    response = catalog.put_platform_risk(
        "ios",
        "ios-feature-01-risk-01",
        RiskMetadataUpdateRequest.model_validate({"tactic": None}),
    )

    assert response == {"tactic": None}
    assert seen["updates"] == {"tactic": None}


def test_risk_demonstration_body_must_be_a_list():
    with pytest.raises(ValidationError):
        RiskDemonstrationUpdateRequest.model_validate({"id": "steps", "type": "steps"})


def test_config_app_body_rejects_unknown_top_level_fields():
    with pytest.raises(ValidationError):
        ConfigAppRequest.model_validate({"name": "Example App", "unexpected": True})


def test_config_app_body_keeps_flexible_nested_config(monkeypatch):
    seen: dict = {}
    monkeypatch.setitem(
        config._APPS_BY_PLATFORM,
        "ios",
        (
            lambda: [],
            lambda body: seen.setdefault("body", body),
            lambda app_id, body: body,
            lambda app_id: None,
        ),
    )

    response = config.add_config_app(
        "ios",
        ConfigAppRequest.model_validate(
            {
                "name": "Example App",
                "artifact": {"source": "intake_ipa", "custom_future_field": {"enabled": True}},
                "risks": {"ios-feature-01-risk-01": {"enabled": True, "custom_setting": "ok"}},
            }
        ),
    )

    assert response == seen["body"]
    assert seen["body"]["artifact"]["custom_future_field"] == {"enabled": True}
    assert seen["body"]["risks"]["ios-feature-01-risk-01"]["custom_setting"] == "ok"


def test_config_app_flexible_fields_must_be_objects():
    with pytest.raises(ValidationError):
        ConfigAppRequest.model_validate({"name": "Example App", "artifact": ["not", "an", "object"]})


def test_risk_settings_body_must_be_an_object():
    with pytest.raises(ValidationError):
        RiskSettingsUpdateRequest.model_validate(["not", "an", "object"])


def test_risk_settings_body_allows_open_ended_sections(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        config_editor,
        "put_risk_settings",
        lambda platform, risk_id, body: seen.setdefault("body", body) or body,
    )

    response = config.put_config_risk_settings(
        "ios",
        "ios-feature-01-risk-01",
        RiskSettingsUpdateRequest.model_validate(
            {"analyzer": {"provider": "mobsf"}, "future_section": {"knob": 1}}
        ),
    )

    assert response == seen["body"]
    assert seen["body"]["future_section"] == {"knob": 1}


def test_device_body_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        DeviceUpdateRequest.model_validate({"unknown_device_field": True})


def test_device_appium_auto_start_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        DeviceUpdateRequest.model_validate({"appium_auto_start": {"enabled": True, "surprise": "nope"}})


def test_device_update_keeps_nested_partial_updates_partial():
    body = DeviceUpdateRequest.model_validate({"appium_auto_start": {"enabled": True}})

    assert body.updates() == {"appium_auto_start": {"enabled": True}}


def test_runner_body_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RunnerUpdateRequest.model_validate({"unknown_runner_field": True})


def test_runner_permission_alerts_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RunnerUpdateRequest.model_validate({"permission_alerts": {"enabled": True, "surprise": "nope"}})


def test_runner_update_keeps_nested_partial_updates_partial():
    body = RunnerUpdateRequest.model_validate({"permission_alerts": {"enabled": False}})

    assert body.updates() == {"permission_alerts": {"enabled": False}}
