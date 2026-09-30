"""
Store protocol, summary and error types shared by the dashboard sync modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class DashboardSyncStore(Protocol):
    """Dashboard database operations the report sync needs."""

    # Returns the application with this external id, or None.
    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    # Returns the application with this external id on this platform, or None.
    def find_application_by_external_id_and_platform(
        self, external_id: str, platform: str
    ) -> dict[str, Any] | None: ...

    # Returns applications without an external id whose name matches on this platform.
    def find_unlinked_applications(self, name: str, platform: str) -> list[dict[str, Any]]: ...

    # Updates one application by id and returns the stored row.
    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Inserts or merges an application keyed on external_id.
    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Returns the assessment with this external id, or None.
    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    # Returns the application's oldest manual placeholder assessment, or None.
    def find_placeholder_assessment(self, application_id: str) -> dict[str, Any] | None: ...

    # Updates the assessment only while it is still a placeholder; returns None if already claimed.
    def claim_placeholder_assessment(
        self, assessment_id: str, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    # Updates one assessment by id and returns the stored row.
    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Inserts or merges an assessment keyed on external_id.
    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Returns the finding with this external id, or None.
    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    # Returns the test ids of the application's existing findings.
    def test_ids_for_application(self, application_id: str) -> list[str]: ...

    # Returns the reassessment request linked to a run, or None.
    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None: ...

    # Updates one reassessment request by id and returns the stored row.
    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Sets a ticket's status and returns the stored row.
    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]: ...

    # Inserts a finding and returns it, or a conflict marker if it already exists.
    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Updates one finding by id and returns the stored row.
    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Appends a finding history row once per sync key.
    def create_finding_history(self, fields: Mapping[str, Any]) -> None: ...

    # Appends a risk conversation entry once per sync key.
    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None: ...

    # Appends an activity log row once per sync key.
    def log_activity(self, fields: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class SyncSummary:
    """Counts of reports and dashboard rows touched by a sync pass."""
    reports: int = 0
    applications: int = 0
    assessments: int = 0
    findings: int = 0
    history: int = 0
    activity: int = 0
    failed_reports: int = 0
    skipped_reports: int = 0
    unchanged_reports: int = 0

    # Returns the field-by-field sum of two summaries.
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

    # Returns the per-entity row counts recorded in sync status.
    def counts(self) -> dict[str, int]:
        return {
            "applications": self.applications,
            "assessments": self.assessments,
            "findings": self.findings,
            "history": self.history,
            "activity": self.activity,
        }


class SupabaseRestError(RuntimeError):
    """Raised when a Supabase request fails or returns an unexpected response."""
    pass


class AmbiguousApplicationError(RuntimeError):
    """Raised when a report cannot be matched to exactly one dashboard application."""
    pass
