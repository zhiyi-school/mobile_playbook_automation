from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mobile_playbook.reporting.serialization import SerializableDataclass


@dataclass
class Evidence(SerializableDataclass):
    kind: str
    path: str
    label: str = ""


@dataclass
class TestResult(SerializableDataclass):
    run_timestamp: str
    platform: str
    app_id: str
    app_name: str
    package_or_bundle_id: str
    test_id: str
    test_name: str
    category: str
    status: str
    severity: str = "info"
    summary: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
