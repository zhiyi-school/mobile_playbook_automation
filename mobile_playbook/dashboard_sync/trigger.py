"""
Launches a detached dashboard sync worker after an automation run finishes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, work_root
from typing import Mapping

logger = logging.getLogger(__name__)

AUTO_TRIGGER_ENV = "DASHBOARD_SYNC_AUTO_TRIGGER"
FALSE_VALUES = {"0", "false", "no", "off"}
LOCK_WAIT_SECONDS = 60


# Reports whether the post-run sync trigger is on, from the environment, then the repository .env, default true.
def auto_trigger_enabled(
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> bool:
    environment = environ if environ is not None else os.environ
    value = environment.get(AUTO_TRIGGER_ENV)
    source = "environment"
    if value is None:
        value = _env_file_value(env_path or REPOSITORY_ROOT / ".env", AUTO_TRIGGER_ENV) or "true"
        source = "env file or default"
    logger.debug("dashboard sync: %s=%r from %s.", AUTO_TRIGGER_ENV, value, source)
    return value.strip().lower() not in FALSE_VALUES


# Reads one non-secret setting from an env file without importing the whole .env into the API.
def _env_file_value(path: Path, wanted_key: str) -> str | None:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        logger.debug("dashboard sync: env file %s unreadable for %s: %s", path, wanted_key, type(exc).__name__)
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
        logger.debug("dashboard sync: %s found in %s.", wanted_key, path)
        return value
    logger.debug("dashboard sync: %s not present in %s.", wanted_key, path)
    return None


# Launches a detached, best-effort one-shot dashboard sync that loads its own credentials and returns its pid.
def trigger_dashboard_sync(reports_dir: Path, run_timestamp: str | None = None) -> int | None:
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
    logger.debug("dashboard sync: launching %s (cwd=%s, log=%s).", command, REPOSITORY_ROOT, log_path)

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
        logger.debug("dashboard sync: post-run worker launch failed.", exc_info=True)
        logger.warning("dashboard sync: could not start the post-run worker: %s", exc)
        return None

    logger.info("dashboard sync: queued post-run reconciliation (pid %d).", process.pid)
    return process.pid


# Marks a run's sync status queued if it completed, else not required; failures are only logged.
def _mark_queued(run_dir: Path) -> None:
    try:
        from mobile_playbook.dashboard_sync import run_status
        from mobile_playbook.reporting.run_manifest import is_completed, read_manifest

        if is_completed(read_manifest(run_dir)):
            logger.debug("dashboard sync: run %s completed; marking it queued.", run_dir.name)
            run_status.mark_queued(run_dir)
        else:
            logger.debug("dashboard sync: run %s did not complete; marking sync not required.", run_dir.name)
            run_status.mark_not_required(run_dir, "the automation run did not complete")
    except Exception as exc:
        logger.debug("dashboard sync: queued-status update for %s failed.", run_dir.name, exc_info=True)
        logger.warning("dashboard sync: could not record queued status for %s: %s", run_dir.name, exc)
