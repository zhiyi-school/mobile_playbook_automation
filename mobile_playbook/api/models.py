from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

Platform = Literal["ios", "android"]


class FeatureUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None

    def updates(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.model_fields_set}


class RiskMetadataUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    goal: str | None = None
    tactic: str | None = None

    def updates(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.model_fields_set}


class DemonstrationStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    text: str | None = None
    images: list[dict[str, Any]] | None = None
    commands: list[Any] | None = None


class RiskDemonstrationBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: str
    label: str | None = None
    rows: list[dict[str, Any]] | None = None
    items: list[DemonstrationStep] | None = None


class RiskDemonstrationUpdateRequest(RootModel[list[RiskDemonstrationBlock]]):
    def blocks(self) -> list[dict[str, Any]]:
        return self.model_dump(mode="json", exclude_unset=True)


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    config_path: str


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    config_path: str
    apps: str | None = None
    risks: str | None = None
    out_dir: str = "reports"


class CisoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""


class ConfigAppRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str | None = None
    package_name: str | None = None
    package: str | None = None
    bundle_id: str | None = None
    test_bundle_id: str | None = None
    artifact: dict[str, Any] | None = None
    expected_behavior: dict[str, Any] | None = None
    risks: dict[str, Any] | None = None
    sector: str | None = None
    agency: str | None = None
    version: str | None = None
    cisos: list[CisoRequest] | None = None

    def updates(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=self.model_fields_set, exclude_unset=True)


class RiskSettingsUpdateRequest(RootModel[dict[str, Any]]):
    """Open-ended because each risk owns its nested YAML settings schema."""


class AppiumAutoStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    command: list[str] | None = None
    wait_seconds: float | None = None
    poll_interval_seconds: float | None = None


class DeviceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    udid: str | None = None
    team_id: str | None = None
    appium_server_url: str | None = None
    platform_version: str | None = None
    xcode_signing_id: str | None = None
    keep_wda: bool | None = None
    show_xcode_log: bool | None = None
    updated_wda_bundle_id: str | None = None
    allow_provisioning_device_registration: bool | None = None
    adb_path: str | None = None
    adb_serial: str | None = None
    appium_auto_start: AppiumAutoStartRequest | None = None

    def updates(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=self.model_fields_set, exclude_unset=True)


class PermissionAlertsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    action: str | None = None
    wait_seconds: float | None = None
    max_alerts: int | None = None


class RunnerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequential: bool | None = None
    uninstall_after_each_test: bool | None = None
    stop_on_first_failure: bool | None = None
    app_install_timeout_ms: int | None = None
    launch_wait_seconds: float | None = None
    work_dir: str | None = None
    permission_alerts: PermissionAlertsRequest | None = None
    auto_grant_permissions: bool | None = None

    def updates(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=self.model_fields_set, exclude_unset=True)
