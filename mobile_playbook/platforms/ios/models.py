from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ARTIFACT_STATUSES = {
    "ACQUIRED",
    "ARTIFACT_REQUIRED",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_INVALID",
    "ARTIFACT_BUNDLE_ID_MISMATCH",
    "APPLE_CONFIGURATOR_CACHE_TIMEOUT",
    "ORIGINAL_APP_NOT_INSTALLED",
    "INSTALLED_APP_VERIFIED",
    "INSTALLED_APP_NOT_FOUND",
    "UNSUPPORTED_ARTIFACT_SOURCE",
    "FAILED",
}

FINAL_STATUSES = {
    "NOT_RUN",
    "CONFIG_INVALID",
    "ARTIFACT_REQUIRED",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_INVALID",
    "ARTIFACT_BUNDLE_ID_MISMATCH",
    "ARTIFACT_ACQUISITION_FAILED",
    "APPLE_CONFIGURATOR_CACHE_TIMEOUT",
    "ORIGINAL_APP_NOT_INSTALLED",
    "UNSUPPORTED_ARTIFACT_SOURCE",
    "PRE_UNINSTALL_FAILED",
    "UNPACK_FAILED",
    "PROTECTED_OR_ENCRYPTED_BINARY",
    "NOT_MUTABLE_AS_PROVIDED",
    "INSTALL_FAILED",
    "LAUNCH_FAILED",
    "BEHAVIOR_FAILED",
    "IPA_ANALYSIS_COMPLETE",
    "PAIRING_TIMEOUT",
    "KEYSTROKE_COLLECTION_NOT_OBSERVED",
    "RISK_EXISTS",
    "CUSTOM_KEYBOARD_NOT_AVAILABLE",
    "CONTROL_SERVER_FAILED",
    "CLEANUP_FAILED",
    "FAILED",
}

BINARY_INSPECTION_STATUSES = {
    "MUTABLE_AS_PROVIDED",
    "PROTECTED_OR_ENCRYPTED_BINARY",
    "EXECUTABLE_NOT_FOUND",
    "INSPECTION_FAILED",
}


def serialize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, tuple):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    return value


@dataclass
class SerializableDataclass:
    def to_dict(self) -> dict[str, Any]:
        return serialize(asdict(self))


@dataclass
class DeviceConfig(SerializableDataclass):
    udid: str
    team_id: str
    appium_server_url: str
    platform_version: str | None = None
    xcode_signing_id: str = "Apple Development"
    keep_wda: bool = True
    show_xcode_log: bool = False
    updated_wda_bundle_id: str | None = None
    allow_provisioning_device_registration: bool = False


@dataclass
class RunnerConfig(SerializableDataclass):
    sequential: bool = True
    uninstall_after_each_test: bool = True
    stop_on_first_failure: bool = False
    app_install_timeout_ms: int = 480000
    launch_wait_seconds: int = 5
    work_dir: Path = Path("work/ios")
    permission_alerts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpectedBehaviorConfig(SerializableDataclass):
    app_state_must_be_foreground: bool = True
    source_contains: list[str] = field(default_factory=list)
    source_not_contains: list[str] = field(default_factory=list)
    app_specific_check: str | None = None


@dataclass
class AppConfig(SerializableDataclass):
    id: str
    name: str
    bundle_id: str
    test_bundle_id: str
    artifact: dict[str, Any]
    expected_behavior: ExpectedBehaviorConfig
    risks: dict[str, Any]


@dataclass
class GlobalConfig(SerializableDataclass):
    device: DeviceConfig
    runner: RunnerConfig
    apps: list[AppConfig]
    config_path: Path | None = None


@dataclass
class ArtifactAcquisitionResult(SerializableDataclass):
    app_id: str
    source: str
    status: str
    ipa_path: Path | None = None
    source_path: Path | None = None
    bundle_id: str | None = None
    display_name: str | None = None
    executable_name: str | None = None
    input_sha256: str | None = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BinaryInspectionResult(SerializableDataclass):
    status: str
    executable_path: Path | None = None
    encrypted: bool | None = None
    cryptid: int | None = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallResult(SerializableDataclass):
    status: str
    ipa_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BehaviorResult(SerializableDataclass):
    status: str
    foreground_state: int | None = None
    screenshot_path: Path | None = None
    page_source_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CleanupResult(SerializableDataclass):
    status: str
    removed: bool = False
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskRunResult(SerializableDataclass):
    run_timestamp: str
    timestamp_start: str
    timestamp_end: str | None
    app_id: str
    app_name: str
    original_bundle_id: str
    test_bundle_id: str
    risk_id: str
    feature_id: str
    test_case_id: str
    test_case_type: str
    artifact_source: str
    artifact_result: ArtifactAcquisitionResult | None = None
    acquired_ipa: Path | None = None
    acquired_ipa_sha256: str | None = None
    input_ipa: Path | None = None
    input_ipa_sha256: str | None = None
    binary_inspection_result: BinaryInspectionResult | None = None
    install_result: InstallResult | None = None
    launch_result: dict[str, Any] | None = None
    behavior_result: BehaviorResult | None = None
    final_status: str = "NOT_RUN"
    errors: list[str] = field(default_factory=list)
    cleanup_result: CleanupResult | None = None
