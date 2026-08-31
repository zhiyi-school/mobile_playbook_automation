from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from mobile_playbook import sync_state
from mobile_playbook.dashboard_sync import SupabaseRestError, sync_reports
from mobile_playbook.reporting.run_manifest import write_manifest
from tests.test_dashboard_sync import FakeStore, _row


def _report(tmp_path: Path, timestamp: str, rows: list[dict[str, Any]]) -> Path:
    run_dir = tmp_path / timestamp
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps(rows))
    write_manifest(
        run_dir,
        run_timestamp=timestamp,
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:02+00:00",
    )
    return run_dir


class LockingStore(FakeStore):
    """FakeStore whose writes are serialized, standing in for the database."""

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()

    def create_finding(self, fields):
        with self._lock:
            return super().create_finding(fields)

    def _append_once(self, table, prefix, fields):
        with self._lock:
            return super()._append_once(table, prefix, fields)

    def upsert_application(self, fields):
        with self._lock:
            return super().upsert_application(fields)

    def upsert_assessment(self, fields):
        with self._lock:
            return super().upsert_assessment(fields)


def test_concurrent_syncs_of_one_report_produce_single_rows(tmp_path):
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])
    store = LockingStore()
    barrier = threading.Barrier(2)

    def run_sync():
        barrier.wait()
        # force=True so the ledger does not simply short-circuit the second caller;
        # this exercises the database-level guards instead.
        sync_reports(tmp_path, store, risk_counts={"ios": 3}, force=True)

    threads = [threading.Thread(target=run_sync) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.applications) == 1
    assert len(store.assessments) == 1
    assert len(store.findings) == 1
    assert len(store.finding_history) == 1
    assert len(store.activity_log) == 1


def test_failure_between_finding_and_activity_converges_on_retry(tmp_path):
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])

    class FailsOnFirstActivity(FakeStore):
        def __init__(self):
            super().__init__()
            self.activity_attempts = 0

        def log_activity(self, fields):
            self.activity_attempts += 1
            if self.activity_attempts == 1:
                raise SupabaseRestError("Supabase POST activity_log failed with 500")
            super().log_activity(fields)

    store = FailsOnFirstActivity()

    first = sync_reports(tmp_path, store, risk_counts={"ios": 3})
    assert first.failed_reports == 1
    assert store.findings[0]["status"] == "inconclusive"  # not yet applied by this run

    second = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert second.reports == 1
    assert second.failed_reports == 0
    assert len(store.findings) == 1
    assert store.findings[0]["status"] == "at_risk"
    assert len(store.finding_history) == 1
    assert len(store.activity_log) == 1


def test_replaying_an_older_run_does_not_regress_the_finding(tmp_path):
    newer = _row(run_timestamp="2026-01-02_00-00-00", verdict="Reduced Risk")
    older = _row(run_timestamp="2026-01-01_00-00-00", verdict="At Risk")
    _report(tmp_path, "2026-01-02_00-00-00", [newer])
    _report(tmp_path, "2026-01-01_00-00-00", [older])
    store = FakeStore()

    sync_reports(tmp_path, store, run_timestamps=["2026-01-02_00-00-00"], risk_counts={"ios": 3})
    assert store.findings[0]["status"] == "reduced_risk"

    sync_reports(tmp_path, store, run_timestamps=["2026-01-01_00-00-00"], risk_counts={"ios": 3})

    assert store.findings[0]["status"] == "reduced_risk"
    assert store.findings[0]["latest_test_run_id"] == "2026-01-02_00-00-00"
    # The older run still imports its own assessment row.
    assert sorted(row["external_id"] for row in store.assessments) == [
        "2026-01-01_00-00-00::example_app",
        "2026-01-02_00-00-00::example_app",
    ]


def test_collision_suffix_orders_numerically_not_lexically(tmp_path):
    from mobile_playbook.dashboard_sync import _is_older_run

    assert _is_older_run("2026-01-01_12-00-00-2", "2026-01-01_12-00-00-10") is True
    assert _is_older_run("2026-01-01_12-00-00-10", "2026-01-01_12-00-00-2") is False
    assert _is_older_run("2026-01-01_12-00-00", "2026-01-01_12-00-00-2") is True


def test_unchanged_pass_performs_no_writes(tmp_path):
    _report(tmp_path, "2026-01-01_00-00-00", [_row()])
    store = FakeStore()

    sync_reports(tmp_path, store, risk_counts={"ios": 3})
    before = (len(store.applications), len(store.assessments), len(store.findings), len(store.activity_log))

    class NoWritesAllowed(FakeStore):
        def __getattribute__(self, name):
            if name in {"upsert_application", "update_application", "create_finding", "update_finding"}:
                raise AssertionError(f"unchanged pass must not call {name}")
            return object.__getattribute__(self, name)

    summary = sync_reports(tmp_path, NoWritesAllowed(), risk_counts={"ios": 3})

    assert summary.unchanged_reports == 1
    assert summary.reports == 0
    assert before == (1, 1, 1, 1)


def test_changed_report_is_resynced_after_the_ledger_entry(tmp_path):
    run_dir = _report(tmp_path, "2026-01-01_00-00-00", [_row()])
    store = FakeStore()
    sync_reports(tmp_path, store, risk_counts={"ios": 3})

    run_dir.joinpath("dashboard_results.json").write_text(json.dumps([_row(verdict="Reduced Risk")]))
    summary = sync_reports(tmp_path, store, risk_counts={"ios": 3})

    assert summary.reports == 1
    assert store.findings[0]["status"] == "reduced_risk"


def test_single_instance_lock_reports_busy_instead_of_overlapping(tmp_path):
    with sync_state.single_instance(tmp_path):
        with pytest.raises(sync_state.SyncBusy):
            with sync_state.single_instance(tmp_path):
                pass

    with sync_state.single_instance(tmp_path):
        pass


def test_single_instance_can_wait_for_a_post_run_trigger(tmp_path):
    holding = threading.Event()

    def hold_lock():
        with sync_state.single_instance(tmp_path):
            holding.set()
            time.sleep(0.1)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holding.wait(timeout=1)

    with sync_state.single_instance(tmp_path, wait_seconds=1):
        acquired_after_holder = True

    holder.join(timeout=1)
    assert acquired_after_holder is True
    assert holder.is_alive() is False


def test_ledger_survives_a_corrupt_file(tmp_path):
    sync_state.ledger_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    sync_state.ledger_path(tmp_path).write_text("{ not json")

    assert sync_state.load_ledger(tmp_path) == {}

    sync_state.mark_processed(tmp_path, "2026-01-01_00-00-00", "abc")
    assert sync_state.is_processed(tmp_path, "2026-01-01_00-00-00", "abc") is True
    assert sync_state.is_processed(tmp_path, "2026-01-01_00-00-00", "different") is False
