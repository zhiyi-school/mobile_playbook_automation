from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from mobile_playbook.dashboard_sync.contracts import SyncSummary, placeholder_reserved
from mobile_playbook.dashboard_sync.mapping import sync_dashboard_results, sync_report_dir
from mobile_playbook.dashboard_sync.orchestrator import sync_reports
from mobile_playbook.reporting.run_manifest import write_manifest


class FakeStore:
    def __init__(self):
        self.applications: list[dict[str, Any]] = []
        self.assessments: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.finding_history: list[dict[str, Any]] = []
        self.activity_log: list[dict[str, Any]] = []
        self.retest_runs: list[dict[str, Any]] = []
        self.risk_conversation_entries: list[dict[str, Any]] = []
        self.tickets: list[dict[str, Any]] = []
        self.run_requests: list[dict[str, Any]] = []
        self.upserted_assessments = 0
        self._ids: dict[str, int] = {}

    def _next_id(self, prefix: str) -> str:
        self._ids[prefix] = self._ids.get(prefix, 0) + 1
        return f"{prefix}_{self._ids[prefix]}"

    def _insert(self, table: list[dict[str, Any]], prefix: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        row = {"id": self._next_id(prefix), **dict(fields)}
        table.append(row)
        return row

    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return _find(self.applications, external_id=external_id)

    def find_application_by_external_id_and_platform(
        self, external_id: str, platform: str
    ) -> dict[str, Any] | None:
        return _find(self.applications, external_id=external_id, platform=platform)

    def find_unlinked_applications(self, name: str, platform: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.applications
            if row.get("external_id") is None
            and row.get("platform") == platform
            and str(row.get("name", "")).casefold() == name.casefold()
        ]

    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None:
        return _find(self.retest_runs, external_test_run_id=run_timestamp)

    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _update(self.retest_runs, retest_id, fields)

    def outstanding_retests_for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        return [
            {"id": row["id"], "status": row["status"]}
            for row in self.retest_runs
            if row.get("ticket_id") == ticket_id and row.get("status") in {"queued", "running"}
        ]

    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        return _update(self.tickets, ticket_id, {"status": status})

    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _update(self.applications, application_id, fields)

    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Mirror the unique constraint on (external_id, platform)."""
        existing = self.find_application_by_external_id_and_platform(
            str(fields["external_id"]), str(fields["platform"])
        )
        return _update(self.applications, existing["id"], fields) if existing else self._insert(self.applications, "app", fields)

    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return _find(self.assessments, external_id=external_id)

    def assessments_for_application(self, application_id: str) -> list[dict[str, Any]]:
        return [row for row in self.assessments if row["application_id"] == application_id]

    def find_requested_assessment(self, run_timestamp: str) -> dict[str, Any] | None:
        request = _find(self.run_requests, run_id=run_timestamp)
        return _find(self.assessments, id=request["assessment_id"]) if request else None

    def find_placeholder_assessment(self, application_id: str) -> dict[str, Any] | None:
        placeholders = [
            row
            for row in self.assessments
            if row["application_id"] == application_id
            and str(row["external_id"]).startswith("manual::")
            and not any(
                placeholder_reserved(request)
                for request in self.run_requests
                if request["assessment_id"] == row["id"]
            )
        ]
        return sorted(placeholders, key=lambda row: row.get("created_at", ""))[0] if placeholders else None

    def claim_placeholder_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        row = _find(self.assessments, id=assessment_id)
        if row is None or not str(row["external_id"]).startswith("manual::"):
            return None
        return _update(self.assessments, assessment_id, fields)

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _update(self.assessments, assessment_id, fields)

    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        self.upserted_assessments += 1
        existing = self.find_assessment_by_external_id(str(fields["external_id"]))
        return _update(self.assessments, existing["id"], fields) if existing else self._insert(self.assessments, "assessment", fields)

    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return _find(self.findings, external_id=external_id)

    def test_ids_for_application(self, application_id: str) -> list[str]:
        return [
            str(row["test_id"])
            for row in self.findings
            if row.get("application_id") == application_id and row.get("test_id")
        ]

    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        if self.find_finding_by_external_id(str(fields["external_id"])) is not None:
            return {"_conflicted": True}
        # findings.status is NOT NULL with a database default.
        return self._insert(self.findings, "finding", {"status": "inconclusive", **dict(fields)})

    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _update(self.findings, finding_id, fields)

    def create_finding_history(self, fields: Mapping[str, Any]) -> None:
        self._append_once(self.finding_history, "history", fields)

    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None:
        self._append_once(self.risk_conversation_entries, "entry", fields)

    def log_activity(self, fields: Mapping[str, Any]) -> None:
        self._append_once(self.activity_log, "activity", fields)

    def _append_once(self, table: list[dict[str, Any]], prefix: str, fields: Mapping[str, Any]) -> None:
        """Mirror the partial unique index on sync_key."""
        key = fields.get("sync_key")
        if key is not None and any(row.get("sync_key") == key for row in table):
            return
        self._insert(table, prefix, fields)


class RacingStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.raced = False

    def claim_placeholder_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.raced:
            self.raced = True
            row = _find(self.assessments, id=assessment_id)
            assert row is not None
            row["external_id"] = fields["external_id"]
            row.update({key: value for key, value in fields.items() if key != "external_id"})
            return None
        return super().claim_placeholder_assessment(assessment_id, fields)


def _find(rows: list[dict[str, Any]], **criteria: Any) -> dict[str, Any] | None:
    for row in rows:
        if all(row.get(key) == value for key, value in criteria.items()):
            return row
    return None


def _update(rows: list[dict[str, Any]], row_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    row = _find(rows, id=row_id)
    if row is None:
        raise AssertionError(f"Missing row {row_id}")
    row.update(dict(fields))
    return row


def _row(run_timestamp: str = "2026-01-01_00-00-00", verdict: str = "At Risk") -> dict[str, Any]:
    return {
        "app_id": "example_app",
        "app_name": "Example Banking App",
        "category": "ios",
        "completed_at": "2026-01-01T00:00:02+00:00",
        "duration_seconds": 2,
        "evidence": [],
        "package_or_bundle_id": "com.example.app",
        "platform": "ios",
        "raw": {"test_case_id": "example_case"},
        "report_path": "ios/example_app/example_risk/example_case",
        "run_timestamp": run_timestamp,
        "severity": "high",
        "started_at": "2026-01-01T00:00:00+00:00",
        "status": "RISK_EXISTS",
        "summary": "Example summary",
        "test_id": "example_risk",
        "test_name": "Example Risk",
        "verdict": verdict,
    }


def test_sync_dashboard_results_is_idempotent_for_same_run():
    store = FakeStore()
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )

    first = sync_dashboard_results([_row()], store, triggered_by="user_1", risk_counts={"ios": 3})
    second = sync_dashboard_results([_row()], store, triggered_by="user_1", risk_counts={"ios": 3})

    assert first.findings == 1
    assert second.findings == 1
    assert len(store.applications) == 1
    assert len(store.assessments) == 1
    assert store.assessments[0]["external_id"] == "2026-01-01_00-00-00::example_app"
    assert store.assessments[0]["completed_tests"] == 1
    assert store.assessments[0]["total_tests"] == 3
    assert len(store.findings) == 1
    assert len(store.finding_history) == 1
    assert len(store.activity_log) == 1


def test_a_single_risk_rerun_keeps_the_application_s_whole_coverage():
    store = FakeStore()
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    full = [
        {**_row(), "test_id": f"example_risk_{index:02d}", "test_name": f"Example Risk {index:02d}"}
        for index in range(1, 13)
    ]

    sync_dashboard_results(full, store, triggered_by="user_1", risk_counts={"ios": 12})
    rerun = [
        {**_row("2026-01-02_00-00-00"), "test_id": "example_risk_01", "test_name": "Example Risk 01"}
    ]
    sync_dashboard_results(rerun, store, triggered_by="user_1", risk_counts={"ios": 12})

    # The rerun updates the assessment it belongs to rather than adding another.
    assert len(store.assessments) == 1
    assessment = store.assessments[0]
    assert assessment["external_id"] == "2026-01-01_00-00-00::example_app"
    assert assessment["latest_run_id"] == "2026-01-02_00-00-00"
    # The re-run touched one risk, but the application's coverage is still complete.
    assert assessment["completed_tests"] == 12
    assert assessment["total_tests"] == 12


def test_a_first_run_counts_only_what_it_actually_tested():
    store = FakeStore()
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    rows = [
        {**_row(), "test_id": f"example_risk_{index:02d}", "test_name": f"Example Risk {index:02d}"}
        for index in range(1, 4)
    ]

    sync_dashboard_results(rows, store, triggered_by="user_1", risk_counts={"ios": 12})

    assert store.assessments[0]["completed_tests"] == 3
    assert store.assessments[0]["total_tests"] == 12


def test_coverage_is_counted_per_application_not_across_them():
    store = FakeStore()
    store.applications.extend(
        [
            {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"},
            {"id": "app_2", "external_id": "other_app", "name": "Other App", "platform": "ios"},
        ]
    )
    sync_dashboard_results(
        [
            {**_row(), "test_id": f"example_risk_{index:02d}", "test_name": f"Example Risk {index:02d}"}
            for index in range(1, 6)
        ],
        store,
        triggered_by="user_1",
        risk_counts={"ios": 12},
    )
    sync_dashboard_results(
        [{**_row("2026-01-02_00-00-00", verdict="At Risk"), "app_id": "other_app", "app_name": "Other App", "test_id": "example_risk_01"}],
        store,
        triggered_by="user_1",
        risk_counts={"ios": 12},
    )

    by_key = {row["external_id"]: row for row in store.assessments}
    assert by_key["2026-01-02_00-00-00::other_app"]["completed_tests"] == 1


def test_sync_dashboard_results_conditionally_adopts_placeholder_after_race():
    store = RacingStore()
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    store.assessments.append(
        {
            "id": "assessment_1",
            "external_id": "manual::placeholder",
            "application_id": "app_1",
            "status": "running",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert len(store.assessments) == 1
    assert store.assessments[0]["external_id"] == "2026-01-01_00-00-00::example_app"
    assert store.assessments[0]["status"] == "completed"
    assert store.upserted_assessments == 0


def _report(tmp_path: Path, timestamp: str, feed: Any, status: str | None = "completed") -> Path:
    run_dir = tmp_path / timestamp
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text(feed if isinstance(feed, str) else json.dumps(feed))
    if status is not None:
        write_manifest(
            run_dir,
            run_timestamp=timestamp,
            platform="ios",
            attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
            status=status,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:02+00:00",
            error=None if status == "completed" else "device fell off the bus",
        )
    return run_dir


def test_sync_reports_continues_after_failed_report(tmp_path: Path):
    store = FakeStore()
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])
    _report(tmp_path, "2026-01-01_00-00-01", "{")

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.reports == 1
    assert summary.failed_reports == 1
    assert len(store.assessments) == 1


def test_failed_run_is_not_synced_as_a_completed_assessment(tmp_path: Path):
    store = FakeStore()
    # A fatal orchestration failure still leaves a partial feed behind, because
    # write_summary() runs from `finally`.
    _report(tmp_path, "2026-01-01_00-00-00", [_row()], status="failed")

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.skipped_reports == 1
    assert summary.reports == 0
    assert store.assessments == []
    assert store.findings == []


def test_completed_run_with_a_recorded_risk_failure_still_syncs(tmp_path: Path):
    store = FakeStore()
    row = _row(verdict="Inconclusive")
    row["status"] = "FAILED"
    _report(tmp_path, "2026-01-01_00-00-00", [row])

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.reports == 1
    assert summary.skipped_reports == 0
    assert len(store.assessments) == 1
    assert store.assessments[0]["status"] == "completed"
    assert len(store.findings) == 1


def test_report_without_a_manifest_is_skipped_unless_legacy_is_allowed(tmp_path: Path):
    store = FakeStore()
    _report(tmp_path, "2026-01-01_00-00-00", [_row()], status=None)

    skipped = sync_reports(tmp_path, store, risk_counts={"ios": 3})
    assert skipped.skipped_reports == 1
    assert store.assessments == []

    imported = sync_reports(tmp_path, store, risk_counts={"ios": 3}, allow_legacy_report=True)
    assert imported.reports == 1
    assert len(store.assessments) == 1


def test_missing_reports_directory_is_an_empty_queue(tmp_path: Path):
    summary = sync_reports(tmp_path / "does_not_exist", FakeStore(), risk_counts={"ios": 3})

    assert summary == SyncSummary()


def _retest_fixture(store: FakeStore, conversation_id: str | None = "conversation_1") -> None:
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    store.tickets.append({"id": "ticket_1", "status": "retest_in_progress"})
    store.retest_runs.append(
        {
            "id": "retest_1",
            "conversation_id": conversation_id,
            "ticket_id": "ticket_1",
            "finding_id": "finding_1",
            "external_test_run_id": "2026-01-01_00-00-00",
            "status": "running",
        }
    )


def test_retest_completion_posts_one_conversation_event(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.retest_runs[0]["status"] == "completed"
    assert store.tickets[0]["status"] == "under_review"
    assert len(store.risk_conversation_entries) == 1
    entry = store.risk_conversation_entries[0]
    assert entry["conversation_id"] == "conversation_1"
    assert entry["kind"] == "retest_completed"
    assert entry["source_ticket_id"] == "ticket_1"
    assert entry["metadata"] == {"run_timestamp": "2026-01-01_00-00-00"}
    assert entry["sync_key"] == "retest_1::2026-01-01_00-00-00::retest"
    assert "author_id" not in entry


def test_the_event_follows_the_conversation_the_retest_names(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    # A merge repoints the retest without touching the ticket that raised it.
    store.retest_runs[0]["conversation_id"] = "conversation_merged"
    store.tickets[0]["risk_conversation_id"] = "conversation_1"
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert [entry["conversation_id"] for entry in store.risk_conversation_entries] == [
        "conversation_merged"
    ]


def test_retest_failure_posts_a_failure_event_and_leaves_the_ticket(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    _report(tmp_path, "2026-01-01_00-00-00", [_row()], status="failed")

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.skipped_reports == 1
    assert store.retest_runs[0]["status"] == "failed"
    assert store.tickets[0]["status"] == "retest_in_progress"
    assert [entry["kind"] for entry in store.risk_conversation_entries] == ["retest_failed"]


def test_a_resynced_report_does_not_post_the_event_twice(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    sync_reports(tmp_path, store, risk_counts={"ios": 3}, force=True)

    assert len(store.risk_conversation_entries) == 1


def test_a_retest_with_no_conversation_still_completes(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store, conversation_id=None)
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.retest_runs[0]["status"] == "completed"
    assert store.tickets[0]["status"] == "under_review"
    assert store.risk_conversation_entries == []


def test_a_withdrawn_retest_is_left_resolved(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    store.retest_runs[0]["status"] = "cancelled"
    store.tickets[0]["status"] = "in_progress"
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.retest_runs[0]["status"] == "cancelled"
    assert store.tickets[0]["status"] == "in_progress"
    assert store.risk_conversation_entries == []


def test_a_run_with_no_retest_behind_it_posts_nothing(tmp_path: Path):
    store = FakeStore()
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.risk_conversation_entries == []
    assert len(store.findings) == 1


def test_a_retry_that_finds_the_retest_unresolved_still_posts_one_event(tmp_path: Path):
    """The early return covers a resynced report; the sync_key covers a pass that
    died between updating the retest and appending its event."""
    store = FakeStore()
    _retest_fixture(store)
    run_dir = _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_report_dir(run_dir, store)
    store.retest_runs[0]["status"] = "running"
    sync_report_dir(run_dir, store)

    assert len(store.risk_conversation_entries) == 1


def test_resyncing_a_resolved_retest_does_not_reopen_what_security_closed(tmp_path: Path):
    store = FakeStore()
    _retest_fixture(store)
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    store.tickets[0]["status"] = "closed"
    sync_reports(tmp_path, store, risk_counts={"ios": 3}, force=True)

    assert store.tickets[0]["status"] == "closed"
    assert len(store.risk_conversation_entries) == 1


def _app_with_placeholders(store: FakeStore, *placeholders: tuple[str, str]) -> None:
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    for assessment_id, created_at in placeholders:
        store.assessments.append(
            {
                "id": assessment_id,
                "external_id": f"manual::{assessment_id}",
                "application_id": "app_1",
                "status": "running",
                "created_at": created_at,
            }
        )


def test_a_requested_run_completes_the_assessment_its_request_named():
    store = FakeStore()
    _app_with_placeholders(store, ("first", "2026-01-01T00:00:00+00:00"), ("second", "2026-01-02T00:00:00+00:00"))
    store.run_requests.append({"assessment_id": "second", "status": "completed", "run_id": "2026-01-03_00-00-00"})

    sync_dashboard_results(
        [_row("2026-01-03_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "3.9.9"}
    )

    second = _find(store.assessments, id="second")
    assert second["external_id"] == "2026-01-03_00-00-00::example_app"
    assert second["status"] == "completed"
    assert second["app_version"] == "3.9.9"
    assert _find(store.assessments, id="first")["external_id"] == "manual::first"


def test_an_unrequested_run_leaves_a_queued_assessment_for_its_own_run():
    store = FakeStore()
    _app_with_placeholders(store, ("queued", "2026-01-01T00:00:00+00:00"))
    store.run_requests.append({"assessment_id": "queued", "status": "queued", "run_id": None})

    sync_dashboard_results([_row("2026-01-03_00-00-00")], store, risk_counts={"ios": 3})

    assert _find(store.assessments, id="queued")["external_id"] == "manual::queued"
    assert _find(store.assessments, external_id="2026-01-03_00-00-00::example_app") is not None
    assert store.upserted_assessments == 1


def test_a_placeholder_whose_request_started_another_run_is_not_taken():
    store = FakeStore()
    _app_with_placeholders(store, ("other", "2026-01-01T00:00:00+00:00"))
    store.run_requests.append({"assessment_id": "other", "status": "completed", "run_id": "2026-01-02_00-00-00"})

    sync_dashboard_results([_row("2026-01-03_00-00-00")], store, risk_counts={"ios": 3})

    assert _find(store.assessments, id="other")["external_id"] == "manual::other"


def test_a_placeholder_from_before_run_ids_is_still_claimed():
    store = FakeStore()
    _app_with_placeholders(store, ("legacy", "2026-01-01T00:00:00+00:00"))
    store.run_requests.append({"assessment_id": "legacy", "status": "completed", "run_id": None})

    sync_dashboard_results([_row("2026-01-03_00-00-00")], store, risk_counts={"ios": 3})

    assert _find(store.assessments, id="legacy")["external_id"] == "2026-01-03_00-00-00::example_app"


def test_a_run_without_a_recorded_version_leaves_the_version_alone():
    store = FakeStore()
    _app_with_placeholders(store, ("first", "2026-01-01T00:00:00+00:00"))

    sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert "app_version" not in _find(store.assessments, id="first")


def test_the_manifest_carries_each_apps_tested_version_into_the_sync(tmp_path):
    from mobile_playbook.reporting.run_manifest import artifact_versions, read_manifest

    run_dir = tmp_path / "2026-01-01_00-00-00"
    write_manifest(
        run_dir,
        run_timestamp="2026-01-01_00-00-00",
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status="completed",
        artifact_versions={"example_app": "4.3.4"},
    )

    assert artifact_versions(read_manifest(run_dir)) == {"example_app": "4.3.4"}
    assert artifact_versions({}) == {}
    assert artifact_versions({"artifact_versions": {"example_app": ""}}) == {}


def test_a_report_sync_records_the_version_its_manifest_names(tmp_path):
    store = FakeStore()
    _app_with_placeholders(store, ("first", "2026-01-01T00:00:00+00:00"))
    run_dir = _report(tmp_path, "2026-01-01_00-00-00", [_row()])
    write_manifest(
        run_dir,
        run_timestamp="2026-01-01_00-00-00",
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status="completed",
        artifact_versions={"example_app": "4.3.4"},
    )

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert _find(store.assessments, id="first")["app_version"] == "4.3.4"


def _assessed_app(store: FakeStore, *runs: tuple[str, str]) -> None:
    store.applications.append(
        {"id": "app_1", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    for assessment_id, run in runs:
        store.assessments.append(
            {
                "id": assessment_id,
                "external_id": f"{run}::example_app",
                "application_id": "app_1",
                "status": "completed",
                "app_version": None,
                "latest_run_id": run,
                "created_at": run,
            }
        )


def test_a_rerun_updates_the_assessment_that_was_current_when_it_ran():
    store = FakeStore()
    _assessed_app(store, ("v1", "2026-01-01_00-00-00"), ("v2", "2026-02-01_00-00-00"))

    sync_dashboard_results([_row("2026-01-15_00-00-00")], store, risk_counts={"ios": 3})

    assert len(store.assessments) == 2
    assert _find(store.assessments, id="v1")["latest_run_id"] == "2026-01-15_00-00-00"
    assert _find(store.assessments, id="v2")["latest_run_id"] == "2026-02-01_00-00-00"


def test_an_older_report_replayed_later_changes_nothing_newer():
    store = FakeStore()
    _assessed_app(store, ("v1", "2026-01-01_00-00-00"))
    sync_dashboard_results(
        [_row("2026-01-20_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "4.3.4"}
    )

    sync_dashboard_results(
        [_row("2026-01-10_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "4.0.0"}
    )

    assessment = _find(store.assessments, id="v1")
    assert assessment["latest_run_id"] == "2026-01-20_00-00-00"
    assert len(store.assessments) == 1


def test_resyncing_the_same_rerun_gives_the_same_result():
    store = FakeStore()
    _assessed_app(store, ("v1", "2026-01-01_00-00-00"))
    rerun = [_row("2026-01-15_00-00-00")]

    sync_dashboard_results(rerun, store, risk_counts={"ios": 3})
    first = dict(_find(store.assessments, id="v1"))
    sync_dashboard_results(rerun, store, risk_counts={"ios": 3})
    second = _find(store.assessments, id="v1")

    assert len(store.assessments) == 1
    assert {k: v for k, v in second.items() if k != "updated_at"} == {k: v for k, v in first.items() if k != "updated_at"}


def test_a_requested_run_still_gets_its_own_assessment():
    store = FakeStore()
    _assessed_app(store, ("v1", "2026-01-01_00-00-00"))
    store.assessments.append(
        {"id": "v2", "external_id": "manual::v2", "application_id": "app_1", "status": "running", "created_at": "z"}
    )
    store.run_requests.append({"assessment_id": "v2", "status": "completed", "run_id": "2026-02-01_00-00-00"})

    sync_dashboard_results([_row("2026-02-01_00-00-00")], store, risk_counts={"ios": 3})

    assert _find(store.assessments, id="v2")["external_id"] == "2026-02-01_00-00-00::example_app"
    assert _find(store.assessments, id="v1")["latest_run_id"] == "2026-01-01_00-00-00"


def test_a_run_before_any_assessment_of_the_app_still_creates_one():
    store = FakeStore()
    _assessed_app(store, ("later", "2026-03-01_00-00-00"))

    sync_dashboard_results([_row("2026-01-01_00-00-00")], store, risk_counts={"ios": 3})

    assert _find(store.assessments, external_id="2026-01-01_00-00-00::example_app") is not None
    assert _find(store.assessments, id="later")["latest_run_id"] == "2026-03-01_00-00-00"


def test_the_assessments_own_run_cannot_undo_a_newer_rerun():
    store = FakeStore()
    _assessed_app(store, ("v1", "2026-01-01_00-00-00"))
    sync_dashboard_results(
        [_row("2026-01-20_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "4.3.4"}
    )

    sync_dashboard_results(
        [_row("2026-01-01_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "4.0.0"}
    )

    assessment = _find(store.assessments, id="v1")
    assert assessment["latest_run_id"] == "2026-01-20_00-00-00"
    assert assessment["app_version"] is None


def test_a_rerun_of_another_build_keeps_the_assessments_version():
    store = FakeStore()
    _assessed_app(store, ("v2", "2026-02-01_00-00-00"))
    _find(store.assessments, id="v2")["app_version"] = "4.3.4"

    sync_dashboard_results(
        [_row("2026-02-10_00-00-00")], store, risk_counts={"ios": 3}, artifact_versions={"example_app": "3.9.9"}
    )

    assessment = _find(store.assessments, id="v2")
    assert assessment["latest_run_id"] == "2026-02-10_00-00-00"
    assert assessment["app_version"] == "4.3.4"


def test_an_android_run_sharing_an_ios_app_id_gets_its_own_application():
    store = FakeStore()
    store.applications.append(
        {
            "id": "ios_app",
            "external_id": "example_app",
            "name": "Example Banking App",
            "platform": "ios",
            "identifier": "com.example.app",
        }
    )
    store.assessments.append(
        {
            "id": "ios_assessment",
            "external_id": "2026-01-01_00-00-00::example_app",
            "application_id": "ios_app",
            "status": "completed",
            "latest_run_id": "2026-01-01_00-00-00",
            "created_at": "2026-01-01_00-00-00",
        }
    )
    android_row = {
        **_row("2026-02-01_00-00-00"),
        "platform": "android",
        "category": "android",
        "package_or_bundle_id": "com.example.android",
        "test_id": "android-example-risk",
    }

    sync_dashboard_results([android_row], store, risk_counts={"android": 2})

    ios_app = _find(store.applications, id="ios_app")
    android_app = _find(store.applications, external_id="example_app", platform="android")
    assert ios_app["platform"] == "ios"
    assert ios_app["identifier"] == "com.example.app"
    assert android_app is not None and android_app["id"] != "ios_app"
    assert _find(store.assessments, id="ios_assessment")["latest_run_id"] == "2026-01-01_00-00-00"
    android_assessment = _find(store.assessments, external_id="2026-02-01_00-00-00::example_app")
    assert android_assessment is not None and android_assessment["application_id"] == android_app["id"]
    assert _find(store.findings, external_id="example_app::android-example-risk")["application_id"] == android_app["id"]
