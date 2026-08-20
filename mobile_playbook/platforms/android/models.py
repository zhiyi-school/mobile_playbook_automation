from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.serialization import SerializableDataclass


@dataclass
class AndroidDeviceConfig(SerializableDataclass):
    appium_server_url: str = "http://127.0.0.1:4723"
    adb_path: str = "adb"
    adb_serial: str | None = None
    # If set and appium_server_url isn't reachable, connect_device() launches
    # appium_auto_start.command and waits for it to come up instead of
    # failing preflight outright. Shape mirrors ipa_static_analysis.yaml's
    # analyzer.auto_start: {enabled, command, wait_seconds, poll_interval_seconds}.
    appium_auto_start: dict[str, Any] = field(default_factory=dict)


@dataclass
class AndroidRunnerConfig(SerializableDataclass):
    work_dir: Path = Path("work/android")
    auto_grant_permissions: bool = False
    launch_wait_seconds: float = 4


@dataclass
class AndroidAppConfig(SerializableDataclass):
    id: str
    name: str
    package_name: str
    artifact: dict[str, Any] = field(default_factory=dict)
    risks: dict[str, Any] = field(default_factory=dict)


@dataclass
class AndroidGlobalConfig(SerializableDataclass):
    device: AndroidDeviceConfig
    runner: AndroidRunnerConfig
    apps: list[AndroidAppConfig]
    tools: dict[str, Any] = field(default_factory=dict)
    screen_capture: dict[str, Any] = field(default_factory=dict)
    repackaging: dict[str, Any] = field(default_factory=dict)
    config_path: Path | None = None


@dataclass
class AndroidRiskRunResult(SerializableDataclass):
    run_timestamp: str
    timestamp_start: str
    timestamp_end: str | None
    app_id: str
    app_name: str
    package_name: str
    risk_id: str
    test_case_id: str
    test_case_type: str
    artifact_source: str = "installed_app"
    final_status: str = "NOT_RUN"
    # 3-way security verdict for the run summary: "At Risk", "Reduced Risk",
    # or the default "Inconclusive". Each risk sets this directly alongside
    # final_status, at the point it decides the outcome.
    verdict: str = "Inconclusive"
    errors: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_result(self):
        return None
