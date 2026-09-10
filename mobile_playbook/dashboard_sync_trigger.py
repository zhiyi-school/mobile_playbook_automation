from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from mobile_playbook.storage import work_root
from typing import Mapping

logger = logging.getLogger(__name__)

AUTO_TRIGGER_ENV = "DASHBOARD_SYNC_AUTO_TRIGGER"
FALSE_VALUES = {"0", "false", "no", "off"}
LOCK_WAIT_SECONDS = 60
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def auto_trigger_enabled(
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> bool:
    environment = environ if environ is not None else os.environ
    value = environment.get(AUTO_TRIGGER_ENV)
    if value is None:
        value = _env_file_value(env_path or REPOSITORY_ROOT / ".env", AUTO_TRIGGER_ENV) or "true"
    return value.strip().lower() not in FALSE_VALUES


def _env_file_value(path: Path, wanted_key: str) -> str | None:
    """Read one non-secret setting without importing the whole .env into the API."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != wanted_key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def trigger_dashboard_sync(reports_dir: Path, run_timestamp: str | None = None) -> int | None:
    """Launch a detached one-shot sync after a run reaches terminal state.

    The child loads dashboard credentials from the repository .env itself, so
    the API process does not need to import the service-role key. Triggering is
    best-effort and can never change the automation run's terminal outcome.
    """
    if not auto_trigger_enabled():
        logger.info("dashboard sync: automatic post-run trigger is disabled.")
        return None

    resolved_reports_dir = Path(reports_dir).resolve()
    if run_timestamp:
        _mark_queued(resolved_reports_dir / run_timestamp)
    log_path = work_root() / "dashboard-sync.log"
    command = [
        sys.executable,
        "-m",
        "mobile_playbook.dashboard_sync",
        "--reports-dir",
        str(resolved_reports_dir),
        "--lock-wait-seconds",
        str(LOCK_WAIT_SECONDS),
    ]
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as output:
            process = subprocess.Popen(
                command,
                cwd=REPOSITORY_ROOT,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as exc:
        logger.warning("dashboard sync: could not start the post-run worker: %s", exc)
        return None

    logger.info("dashboard sync: queued post-run reconciliation (pid %d).", process.pid)
    return process.pid


def _mark_queued(run_dir: Path) -> None:
    """Status is observability only: never let it break the run that produced it."""
    try:
        from mobile_playbook import sync_status
        from mobile_playbook.reporting.run_manifest import is_completed, read_manifest

        if is_completed(read_manifest(run_dir)):
            sync_status.mark_queued(run_dir)
        else:
            sync_status.mark_not_required(run_dir, "the automation run did not complete")
    except Exception as exc:
        logger.warning("dashboard sync: could not record queued status for %s: %s", run_dir.name, exc)
