from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mobile_playbook.dashboard_sync import AmbiguousApplicationError, sync_dashboard_results, sync_reports
from mobile_playbook.reporting.run_manifest import write_manifest
from tests.test_dashboard_sync import FakeStore, _row

RUN = "2026-01-01_00-00-00"


def _report(tmp_path: Path, timestamp: str, rows: list[dict[str, Any]], status: str = "completed") -> Path:
    run_dir = tmp_path / timestamp
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps(rows))
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


def _store_with_retest(status: str = "running") -> FakeStore:
    store = FakeStore()
    store.tickets.append({"id": "ticket_1", "status": "retest_in_progress"})
    store.findings.append(
        {
            "id": "finding_1",
            "external_id": "example_app::example_risk",
            "status": "at_risk",
            "latest_test_run_id": "2025-12-31_00-00-00",
        }
    )
    store.retest_runs.append(
        {
            "id": "retest_1",
            "ticket_id": "ticket_1",
            "finding_id": "finding_1",
            "external_test_run_id": RUN,
            "status": status,
            "result": None,
            "completed_at": None,
        }
    )
    return store


def test_completed_run_completes_the_retest_and_advances_the_ticket(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = _store_with_retest()

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.retest_runs[0]["status"] == "completed"
    assert store.retest_runs[0]["completed_at"] is not None
    assert store.tickets[0]["status"] == "under_review"


def test_retest_completion_is_idempotent(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = _store_with_retest()

    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    store.tickets[0]["status"] = "closed"
    sync_reports(tmp_path, store, risk_counts={"ios": 3}, force=True)

    # The retest is already terminal, so a second pass must not move the ticket again.
    assert store.retest_runs[0]["status"] == "completed"
    assert store.tickets[0]["status"] == "closed"


def test_failed_manifest_fails_the_retest_without_importing_findings(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)], status="failed")
    store = _store_with_retest()

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.skipped_reports == 1
    assert store.assessments == []
    assert store.retest_runs[0]["status"] == "failed"
    assert "device fell off the bus" in store.retest_runs[0]["result"]
    assert store.tickets[0]["status"] == "retest_in_progress"


def test_completed_run_with_no_result_rows_still_closes_the_retest(tmp_path):
    _report(tmp_path, RUN, [])
    store = _store_with_retest()

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert store.retest_runs[0]["status"] == "completed"
    assert store.tickets[0]["status"] == "under_review"


def test_a_run_with_no_retest_is_unaffected(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = FakeStore()

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.reports == 1
    assert store.retest_runs == []


def test_unlinked_application_is_adopted_case_insensitively(tmp_path):
    store = FakeStore()
    store.applications.append(
        {"id": "app_1", "external_id": None, "name": "EXAMPLE Banking App", "platform": "ios"}
    )

    sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert len(store.applications) == 1
    assert store.applications[0]["id"] == "app_1"
    assert store.applications[0]["external_id"] == "example_app"


def test_linked_application_wins_over_a_same_name_unlinked_row(tmp_path):
    store = FakeStore()
    store.applications.append(
        {"id": "linked", "external_id": "example_app", "name": "Example Banking App", "platform": "ios"}
    )
    store.applications.append(
        {"id": "unlinked", "external_id": None, "name": "example banking app", "platform": "ios"}
    )

    sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert store.applications[0]["id"] == "linked"
    assert store.applications[1]["external_id"] is None  # untouched, no duplicate external_id
    assert len(store.applications) == 2


def test_ambiguous_unlinked_applications_fail_the_report(tmp_path):
    store = FakeStore()
    for index in (1, 2):
        store.applications.append(
            {"id": f"app_{index}", "external_id": None, "name": "Example Banking App", "platform": "ios"}
        )

    with pytest.raises(AmbiguousApplicationError, match="Link or rename them"):
        sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert all(row["external_id"] is None for row in store.applications)


def test_ambiguous_application_marks_the_report_failed_not_skipped(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = FakeStore()
    for index in (1, 2):
        store.applications.append(
            {"id": f"app_{index}", "external_id": None, "name": "Example Banking App", "platform": "ios"}
        )

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.failed_reports == 1
    assert summary.reports == 0


def test_provisioning_fields_are_set_on_every_adoption_path(tmp_path):
    store = FakeStore()
    store.applications.append(
        {
            "id": "app_1",
            "external_id": None,
            "name": "Example Banking App",
            "platform": "ios",
            "provisioning_status": "pending",
            "provisioning_error": "waiting for build",
        }
    )

    sync_dashboard_results([_row()], store, risk_counts={"ios": 3})

    assert store.applications[0]["provisioning_status"] == "ready"
    assert store.applications[0]["provisioning_error"] is None


def _find_row(rows, row_id):
    return next(row for row in rows if row["id"] == row_id)


def test_a_second_outstanding_request_keeps_the_ticket_waiting(tmp_path):
    """Completing one request must not put the remediation under review while others wait."""
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = _store_with_retest()
    store.retest_runs.append(
        {
            "id": "retest_2",
            "ticket_id": "ticket_1",
            "finding_id": "finding_1",
            "external_test_run_id": None,
            "status": "queued",
            "result": None,
            "completed_at": None,
        }
    )

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert _find_row(store.retest_runs, "retest_1")["status"] == "completed"
    assert _find_row(store.retest_runs, "retest_2")["status"] == "queued"
    assert store.tickets[0]["status"] == "retest_requested"


def test_a_run_linked_to_two_requests_is_refused_rather_than_guessed(tmp_path):
    store = _store_with_retest()
    store.tickets[0]["status"] = "retest_in_progress"
    store.retest_runs.append(
        {
            "id": "retest_2",
            "ticket_id": "ticket_1",
            "finding_id": "finding_1",
            "external_test_run_id": RUN,
            "status": "running",
            "result": None,
            "completed_at": None,
        }
    )

    from mobile_playbook.dashboard_syncing.supabase import SupabaseRestStore

    rows = [row for row in store.retest_runs if row["external_test_run_id"] == RUN]
    assert len(rows) == 2

    # The real store refuses an ambiguous link rather than picking a row.
    ambiguous = SupabaseRestStore.__new__(SupabaseRestStore)
    object.__setattr__(ambiguous, "_get", lambda table, params: rows)
    with pytest.raises(ValueError, match="more than one reassessment"):
        SupabaseRestStore.find_retest_by_external_run_id(ambiguous, RUN)


def test_repeated_synchronisation_of_one_request_stays_idempotent(tmp_path):
    _report(tmp_path, RUN, [_row(run_timestamp=RUN)])
    store = _store_with_retest()

    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    events = len(store.risk_conversation_entries)
    sync_reports(tmp_path, store, risk_counts={"ios": 3}, force=True)

    assert len(store.risk_conversation_entries) == events
    assert _find_row(store.retest_runs, "retest_1")["status"] == "completed"
