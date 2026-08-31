from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from mobile_playbook import sync_status
from mobile_playbook.reporting.run_manifest import write_manifest
from mobile_playbook.sync_state import mark_processed, report_digest


def _run_dir(tmp_path: Path, timestamp: str = "2026-01-01_00-00-00", status: str = "completed") -> Path:
    run_dir = tmp_path / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("dashboard_results.json").write_text("[]")
    write_manifest(
        run_dir,
        run_timestamp=timestamp,
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status=status,
    )
    return run_dir


def test_queued_status_is_created_with_an_attempt_and_empty_counts(tmp_path):
    run_dir = _run_dir(tmp_path)

    recorded = sync_status.mark_queued(run_dir)

    assert recorded["status"] == "queued"
    assert recorded["attempt"] == 1
    assert recorded["queued_at"] is not None
    assert recorded["started_at"] is None
    assert recorded["counts"] == sync_status.empty_counts()


def test_running_status_records_a_start_without_losing_the_queue_time(tmp_path):
    run_dir = _run_dir(tmp_path)
    queued = sync_status.mark_queued(run_dir)

    running = sync_status.mark_running(run_dir)

    assert running["status"] == "running"
    assert running["queued_at"] == queued["queued_at"]
    assert running["started_at"] is not None
    assert running["completed_at"] is None


def test_completed_status_carries_the_counts_that_were_written(tmp_path):
    run_dir = _run_dir(tmp_path)
    sync_status.mark_running(run_dir)

    completed = sync_status.mark_completed(
        run_dir, {"applications": 1, "assessments": 1, "findings": 4, "history": 2, "activity": 2}
    )

    assert completed["status"] == "completed"
    assert completed["error"] is None
    assert completed["counts"] == {
        "applications": 1,
        "assessments": 1,
        "findings": 4,
        "history": 2,
        "activity": 2,
    }


def test_failed_status_keeps_a_short_message_and_marks_it_retryable(tmp_path):
    run_dir = _run_dir(tmp_path)

    failed = sync_status.mark_failed(run_dir, "Supabase POST findings failed with 500\nStacktrace:\nboom")

    assert failed["status"] == "failed"
    assert failed["error"] == "Supabase POST findings failed with 500"
    assert failed["retryable"] is True


def test_failed_status_redacts_anything_shaped_like_a_bearer_token(tmp_path):
    run_dir = _run_dir(tmp_path)
    token = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c2lnbmF0dXJl"

    failed = sync_status.mark_failed(run_dir, f"rejected apikey={token} by the server")

    assert token not in failed["error"]
    assert "[redacted]" in failed["error"]


def test_not_required_status_for_a_run_that_never_completed(tmp_path):
    run_dir = _run_dir(tmp_path, status="failed")

    recorded = sync_status.mark_not_required(run_dir, "run manifest status is 'failed', not completed")

    assert recorded["status"] == "not_required"
    assert recorded["retryable"] is False


def test_missing_status_for_a_completed_run_reads_as_queued(tmp_path):
    _run_dir(tmp_path)

    described = sync_status.describe(tmp_path, "2026-01-01_00-00-00")

    assert described["status"] == "queued"
    assert described["attempt"] == 0
    assert described["counts"] == sync_status.empty_counts()


def test_missing_status_falls_back_to_the_ledger_as_the_source_of_truth(tmp_path):
    run_dir = _run_dir(tmp_path)
    mark_processed(tmp_path, run_dir.name, report_digest(run_dir))

    assert sync_status.describe(tmp_path, run_dir.name)["status"] == "completed"


def test_missing_status_for_a_failed_run_reads_as_not_required(tmp_path):
    _run_dir(tmp_path, status="failed")

    assert sync_status.describe(tmp_path, "2026-01-01_00-00-00")["status"] == "not_required"


def test_missing_status_for_a_legacy_run_reads_as_not_required(tmp_path):
    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text("[]")

    assert sync_status.describe(tmp_path, run_dir.name)["status"] == "not_required"


def test_status_for_a_run_that_does_not_exist_at_all(tmp_path):
    described = sync_status.describe(tmp_path, "2026-01-01_00-00-00")

    assert described["status"] == "not_required"
    assert described["run_id"] == "2026-01-01_00-00-00"


@pytest.mark.parametrize(
    "content",
    ['{"status": "run', "", "null", "[]", '{"status": "bogus"}', '{"no_status": true}'],
)
def test_malformed_status_falls_back_instead_of_crashing(tmp_path, content):
    run_dir = _run_dir(tmp_path)
    sync_status.status_path(run_dir).write_text(content)

    assert sync_status.read_status(run_dir) is None
    assert sync_status.describe(tmp_path, run_dir.name)["status"] == "queued"


def test_partially_written_counts_are_filled_in_rather_than_trusted(tmp_path):
    run_dir = _run_dir(tmp_path)
    sync_status.status_path(run_dir).write_text(json.dumps({"status": "completed", "counts": {"findings": 3}}))

    described = sync_status.describe(tmp_path, run_dir.name)

    assert described["counts"] == {
        "applications": 0,
        "assessments": 0,
        "findings": 3,
        "history": 0,
        "activity": 0,
    }


def test_a_second_trigger_does_not_reset_a_pass_already_running(tmp_path):
    run_dir = _run_dir(tmp_path)
    sync_status.mark_queued(run_dir)
    running = sync_status.mark_running(run_dir)

    requeued = sync_status.mark_queued(run_dir)

    assert requeued["status"] == "running"
    assert requeued["started_at"] == running["started_at"]


def test_a_retry_after_a_failure_starts_a_new_attempt(tmp_path):
    run_dir = _run_dir(tmp_path)
    sync_status.mark_queued(run_dir)
    sync_status.mark_running(run_dir)
    sync_status.mark_failed(run_dir, "boom")

    requeued = sync_status.mark_queued(run_dir)

    assert requeued["status"] == "queued"
    assert requeued["attempt"] == 2
    assert requeued["error"] is None


def test_repeating_completion_keeps_the_counts_from_the_pass_that_did_the_work(tmp_path):
    run_dir = _run_dir(tmp_path)
    sync_status.mark_completed(run_dir, {"applications": 1, "findings": 2})
    first = sync_status.read_status(run_dir)

    sync_status.mark_completed(run_dir)

    repeated = sync_status.read_status(run_dir)
    assert repeated["counts"]["findings"] == 2
    assert repeated["completed_at"] == first["completed_at"]


def test_concurrent_writers_never_leave_an_unreadable_status(tmp_path):
    run_dir = _run_dir(tmp_path)
    errors: list[BaseException] = []

    def churn(index: int):
        try:
            for _ in range(25):
                sync_status.mark_running(run_dir)
                sync_status.mark_completed(run_dir, {"findings": index})
                assert sync_status.read_status(run_dir) is not None
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sync_status.read_status(run_dir)["status"] == "completed"


def test_pending_runs_are_the_ones_a_queue_depth_should_count(tmp_path):
    queued = _run_dir(tmp_path, "2026-01-01_00-00-00")
    running = _run_dir(tmp_path, "2026-01-02_00-00-00")
    done = _run_dir(tmp_path, "2026-01-03_00-00-00")
    sync_status.mark_queued(queued)
    sync_status.mark_running(running)
    sync_status.mark_completed(done, {})

    assert sync_status.pending_run_timestamps(tmp_path) == [
        "2026-01-01_00-00-00",
        "2026-01-02_00-00-00",
    ]


def test_worker_pass_records_success_and_failure_independently(tmp_path):
    sync_status.record_worker_pass(tmp_path, succeeded=True)
    first = sync_status.read_worker_state(tmp_path)

    sync_status.record_worker_pass(tmp_path, succeeded=False, error="1 report(s) failed to sync")

    state = sync_status.read_worker_state(tmp_path)
    assert state["last_success_at"] == first["last_success_at"]
    assert state["last_failure_at"] is not None
    assert state["last_error"] == "1 report(s) failed to sync"


def test_worker_state_survives_a_corrupt_file(tmp_path):
    sync_status.worker_state_path(tmp_path).write_text("{ not json")

    assert sync_status.read_worker_state(tmp_path) == {
        "last_success_at": None,
        "last_failure_at": None,
        "last_error": None,
    }
