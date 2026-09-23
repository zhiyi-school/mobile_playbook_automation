from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class DashboardSyncStore(Protocol):
    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    def find_application_by_external_id_and_platform(
        self, external_id: str, platform: str
    ) -> dict[str, Any] | None: ...

    def find_unlinked_applications(self, name: str, platform: str) -> list[dict[str, Any]]: ...

    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    def find_placeholder_assessment(self, application_id: str) -> dict[str, Any] | None: ...

    def claim_placeholder_assessment(
        self, assessment_id: str, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    def test_ids_for_application(self, application_id: str) -> list[str]: ...

    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None: ...

    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]: ...

    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def create_finding_history(self, fields: Mapping[str, Any]) -> None: ...

    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None: ...

    def log_activity(self, fields: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class SyncSummary:
    reports: int = 0
    applications: int = 0
    assessments: int = 0
    findings: int = 0
    history: int = 0
    activity: int = 0
    failed_reports: int = 0
    skipped_reports: int = 0
    unchanged_reports: int = 0

    def plus(self, other: "SyncSummary") -> "SyncSummary":
        return SyncSummary(
            reports=self.reports + other.reports,
            applications=self.applications + other.applications,
            assessments=self.assessments + other.assessments,
            findings=self.findings + other.findings,
            history=self.history + other.history,
            activity=self.activity + other.activity,
            failed_reports=self.failed_reports + other.failed_reports,
            skipped_reports=self.skipped_reports + other.skipped_reports,
            unchanged_reports=self.unchanged_reports + other.unchanged_reports,
        )

    def counts(self) -> dict[str, int]:
        return {
            "applications": self.applications,
            "assessments": self.assessments,
            "findings": self.findings,
            "history": self.history,
            "activity": self.activity,
        }


class SupabaseRestError(RuntimeError):
    pass


class AmbiguousApplicationError(RuntimeError):
    pass
