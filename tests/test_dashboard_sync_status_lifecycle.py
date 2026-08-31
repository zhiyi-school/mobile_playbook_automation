from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mobile_playbook import sync_status
from mobile_playbook.dashboard_sync import SupabaseRestError, sync_reports
from mobile_playbook.reporting.run_manifest import write_manifest
from tests.test_dashboard_sync import FakeStore, _row


def _report(
    tmp_path: Path,
    timestamp: str = "2026-01-01_00-00-00",
    rows: list[dict[str, Any]] | None = None,
    status: str = "completed",
) -> Path:
    run_dir = tmp_path / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps(rows if rows is not None else [_row()]))
    write_manifest(
        run_dir,
        run_timestamp=timestamp,
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status=status,
        error="device disconnected" if status == "failed" else None,
    )
    return run_dir


def test_a_successful_pass_ends_completed_with_the_rows_it_reconciled(tmp_path):
    run_dir = _report(tmp_path)

    sync_reports(tmp_path, FakeStore(), risk_counts={"ios": 3})

    recorded = sync_status.read_status(run_dir)
    assert recorded["status"] == "completed"
    assert recorded["started_at"] is not None
    assert recorded["completed_at"] is not None
    assert recorded["error"] is None
    assert recorded["counts"] == {
        "applications": 1,
        "assessments": 1,
        "findings": 1,
        "history": 1,
        "activity": 1,
    }


def test_a_failing_pass_ends_failed_with_a_retryable_message(tmp_path):
    run_dir = _report(tmp_path)

    class Broken(FakeStore):
        def upsert_application(self, fields):
            raise SupabaseRestError("Supabase POST applications failed with 500: upstream timeout")

    summary = sync_reports(tmp_path, Broken(), risk_counts={"ios": 3})

    recorded = sync_status.read_status(run_dir)
    assert summary.failed_reports == 1
    assert recorded["status"] == "failed"
    assert recorded["retryable"] is True
    assert "500" in recorded["error"]


def test_an_ambiguous_application_is_reported_as_needing_a_person(tmp_path):
    run_dir = _report(tmp_path)
    store = FakeStore()
    for _ in range(2):
        store.applications.append(
            {"id": store._next_id("app"), "external_id": None, "name": "Example Banking App", "platform": "ios"}
        )

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    recorded = sync_status.read_status(run_dir)
    assert recorded["status"] == "failed"
    assert recorded["retryable"] is False


def test_a_failed_automation_run_needs_no_dashboard_sync(tmp_path):
    run_dir = _report(tmp_path, status="failed")

    sync_reports(tmp_path, FakeStore(), risk_counts={"ios": 3})

    recorded = sync_status.read_status(run_dir)
    assert recorded["status"] == "not_required"
    assert recorded["counts"] == sync_status.empty_counts()


def test_a_legacy_report_without_a_manifest_needs_no_dashboard_sync(tmp_path):
    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps([_row()]))

    sync_reports(tmp_path, FakeStore(), risk_counts={"ios": 3})

    assert sync_status.read_status(run_dir)["status"] == "not_required"


def test_a_repeat_pass_stays_completed_without_writing_again(tmp_path):
    run_dir = _report(tmp_path)
    store = FakeStore()
    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    first = sync_status.read_status(run_dir)

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    repeated = sync_status.read_status(run_dir)
    assert summary.unchanged_reports == 1
    assert repeated["status"] == "completed"
    assert repeated["completed_at"] == first["completed_at"]
    assert len(store.findings) == 1
    assert len(store.finding_history) == 1
    assert len(store.activity_log) == 1


def test_a_retry_after_a_failure_converges_without_duplicating_rows(tmp_path):
    run_dir = _report(tmp_path)

    class FailsOnce(FakeStore):
        def __init__(self):
            super().__init__()
            self.activity_attempts = 0

        def log_activity(self, fields):
            self.activity_attempts += 1
            if self.activity_attempts == 1:
                raise SupabaseRestError("Supabase POST activity_log failed with 500")
            super().log_activity(fields)

    store = FailsOnce()
    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    assert sync_status.read_status(run_dir)["status"] == "failed"

    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    recorded = sync_status.read_status(run_dir)
    assert recorded["status"] == "completed"
    assert recorded["attempt"] >= 1
    assert len(store.findings) == 1
    assert len(store.finding_history) == 1
    assert len(store.activity_log) == 1


def test_the_ledger_still_decides_completion_if_the_status_write_is_lost(tmp_path):
    run_dir = _report(tmp_path)
    sync_reports(tmp_path, FakeStore(), risk_counts={"ios": 3})
    sync_status.status_path(run_dir).unlink()

    assert sync_status.describe(tmp_path, run_dir.name)["status"] == "completed"


def test_a_run_still_syncing_is_visible_while_the_automation_run_is_already_done(tmp_path):
    run_dir = _report(tmp_path)
    observed: list[str] = []

    class Slow(FakeStore):
        def upsert_application(self, fields):
            observed.append(sync_status.describe(tmp_path, run_dir.name)["status"])
            return super().upsert_application(fields)

    sync_reports(tmp_path, Slow(), risk_counts={"ios": 3})

    assert observed == ["running"]
    assert sync_status.describe(tmp_path, run_dir.name)["status"] == "completed"
