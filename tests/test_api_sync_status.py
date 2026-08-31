from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from mobile_playbook import sync_status
from mobile_playbook.api.services import sync as sync_service
from mobile_playbook.reporting.run_manifest import write_manifest
from mobile_playbook.sync_state import mark_processed, report_digest, single_instance


@pytest.fixture
def reports(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_service, "REPORTS_ROOT", tmp_path)
    monkeypatch.setattr(sync_service, "resolved_run_dir", lambda run_id: tmp_path / run_id)
    return tmp_path


def _report(reports: Path, timestamp: str = "2026-01-01_00-00-00", status: str = "completed") -> Path:
    run_dir = reports / timestamp
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


def test_sync_status_shape_is_stable_for_a_run_awaiting_the_worker(reports):
    _report(reports)

    payload = sync_service.run_sync_status("2026-01-01_00-00-00")

    assert set(payload) == {
        "run_id",
        "run_timestamp",
        "status",
        "attempt",
        "queued_at",
        "started_at",
        "completed_at",
        "last_updated_at",
        "error",
        "retryable",
        "counts",
    }
    assert payload["status"] == "queued"
    assert set(payload["counts"]) == set(sync_status.COUNT_FIELDS)
    assert json.loads(json.dumps(payload)) == payload


def test_sync_status_reports_completion_once_the_worker_has_landed(reports):
    run_dir = _report(reports)
    sync_status.mark_completed(run_dir, {"findings": 2, "history": 1, "activity": 1})

    payload = sync_service.run_sync_status(run_dir.name)

    assert payload["status"] == "completed"
    assert payload["counts"]["findings"] == 2


def test_sync_status_for_an_unknown_run_is_a_404(reports):
    with pytest.raises(HTTPException) as excinfo:
        sync_service.run_sync_status("2026-01-01_00-00-00")

    assert excinfo.value.status_code == 404


def test_sync_status_never_leaks_a_service_role_key_in_its_error(reports):
    run_dir = _report(reports)
    token = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c2ln"
    sync_status.mark_failed(run_dir, f"Supabase rejected apikey={token}")

    payload = sync_service.run_sync_status(run_dir.name)

    assert token not in json.dumps(payload)


def test_worker_status_reports_an_idle_worker_and_an_empty_queue(monkeypatch, reports):
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)

    payload = sync_service.worker_status()

    assert payload["enabled"] is True
    assert payload["worker_state"] == "idle"
    assert payload["queue_depth"] == 0
    assert payload["last_success_at"] is None
    assert set(payload) == {
        "enabled",
        "worker_state",
        "queue_depth",
        "last_success_at",
        "last_failure_at",
        "last_error",
        "recovery_sweep_enabled",
    }


def test_worker_status_sees_a_pass_that_holds_the_host_lock(monkeypatch, reports):
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)
    _report(reports, "2026-01-01_00-00-00")
    sync_status.mark_queued(reports / "2026-01-01_00-00-00")

    with single_instance(reports):
        payload = sync_service.worker_status()

    assert payload["worker_state"] == "running"
    assert payload["queue_depth"] == 1
    assert sync_service.worker_status()["worker_state"] == "idle"


def test_worker_status_surfaces_the_last_failure_for_an_operator(monkeypatch, reports):
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: False)
    sync_status.record_worker_pass(reports, succeeded=False, error="1 report(s) failed to sync")

    payload = sync_service.worker_status()

    assert payload["enabled"] is False
    assert payload["last_error"] == "1 report(s) failed to sync"
    assert payload["last_failure_at"] is not None


def test_resync_queues_a_worker_for_a_failed_run(monkeypatch, reports):
    run_dir = _report(reports)
    sync_status.mark_failed(run_dir, "Supabase POST findings failed with 500")
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)
    triggered: list[tuple] = []
    monkeypatch.setattr(
        sync_service,
        "trigger_dashboard_sync",
        lambda root, run_id: triggered.append((root, run_id)) or 4242,
    )

    payload = sync_service.resync_run(run_dir.name)

    assert triggered == [(reports, run_dir.name)]
    assert payload["status"] in {"queued", "failed"}


def test_resync_is_a_no_op_while_a_pass_is_already_pending(monkeypatch, reports):
    run_dir = _report(reports)
    sync_status.mark_running(run_dir)
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)
    monkeypatch.setattr(
        sync_service, "trigger_dashboard_sync", lambda *args: pytest.fail("must not start a second worker")
    )

    assert sync_service.resync_run(run_dir.name)["status"] == "running"


def test_resync_refuses_a_run_that_needs_no_sync(monkeypatch, reports):
    _report(reports, status="failed")
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)

    with pytest.raises(HTTPException) as excinfo:
        sync_service.resync_run("2026-01-01_00-00-00")

    assert excinfo.value.status_code == 409


def test_resync_refuses_a_run_already_recorded_in_the_ledger(monkeypatch, reports):
    run_dir = _report(reports)
    mark_processed(reports, run_dir.name, report_digest(run_dir))
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)
    started: list[str] = []
    monkeypatch.setattr(
        sync_service, "trigger_dashboard_sync", lambda root, run_id: started.append(run_id) or 1
    )

    payload = sync_service.resync_run(run_dir.name)

    # Retrying a completed run is allowed, but the ledger makes the pass a no-op.
    assert payload["status"] == "completed"
    assert started == [run_dir.name]


def test_resync_reports_a_worker_that_will_not_start(monkeypatch, reports):
    run_dir = _report(reports)
    sync_status.mark_failed(run_dir, "boom")
    monkeypatch.setattr(sync_service, "auto_trigger_enabled", lambda: True)
    monkeypatch.setattr(sync_service, "trigger_dashboard_sync", lambda *args: None)

    with pytest.raises(HTTPException) as excinfo:
        sync_service.resync_run(run_dir.name)

    assert excinfo.value.status_code == 503
