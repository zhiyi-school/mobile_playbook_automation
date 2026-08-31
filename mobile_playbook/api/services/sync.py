from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from mobile_playbook import sync_status
from mobile_playbook.api.job_registry import registry
from mobile_playbook.api.services.reports import REPORTS_ROOT, resolved_run_dir
from mobile_playbook.dashboard_sync_trigger import auto_trigger_enabled, trigger_dashboard_sync
from mobile_playbook.sync_state import worker_running

LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.mobile-playbook.dashboard-sync.plist"


def run_sync_status(run_id: str) -> dict:
    run_dir = resolved_run_dir(run_id)
    if not run_dir.is_dir() and registry.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    return sync_status.describe(REPORTS_ROOT, run_id)


def worker_status() -> dict:
    state = sync_status.read_worker_state(REPORTS_ROOT)
    return {
        "enabled": auto_trigger_enabled(),
        "worker_state": "running" if worker_running(REPORTS_ROOT) else "idle",
        "queue_depth": len(sync_status.pending_run_timestamps(REPORTS_ROOT)),
        "last_success_at": state["last_success_at"],
        "last_failure_at": state["last_failure_at"],
        "last_error": state["last_error"],
        "recovery_sweep_enabled": LAUNCH_AGENT.is_file(),
    }


def resync_run(run_id: str) -> dict:
    run_dir = resolved_run_dir(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No report directory for run_id: {run_id}")
    if not auto_trigger_enabled():
        raise HTTPException(status_code=409, detail="Automatic dashboard sync is disabled on this host")
    current = sync_status.describe(REPORTS_ROOT, run_id)
    if current["status"] == sync_status.NOT_REQUIRED:
        raise HTTPException(status_code=409, detail="This run does not need a dashboard sync")
    if current["status"] in sync_status.PENDING_STATUSES:
        return current
    # No --force: the ledger still short-circuits a run that did land, so a retry
    # of a partial failure cannot write its findings, history or activity twice.
    if trigger_dashboard_sync(REPORTS_ROOT, run_id) is None:
        sync_status.mark_failed(run_dir, "Could not start the dashboard sync worker", retryable=True)
        raise HTTPException(status_code=503, detail="Could not start the dashboard sync worker")
    return sync_status.describe(REPORTS_ROOT, run_id)
