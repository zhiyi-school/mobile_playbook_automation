from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.run_manifest import is_completed, read_manifest
from mobile_playbook.sync_state import is_processed, report_digest, write_atomic

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
NOT_REQUIRED = "not_required"
STATUSES = (QUEUED, RUNNING, COMPLETED, FAILED, NOT_REQUIRED)
PENDING_STATUSES = (QUEUED, RUNNING)

STATUS_NAME = "sync_status.json"
STATUS_LOCK_NAME = ".sync_status.lock"
WORKER_STATE_NAME = ".dashboard_sync_worker.json"

COUNT_FIELDS = ("applications", "assessments", "findings", "history", "activity")

_JWT = re.compile(r"ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")


def empty_counts() -> dict[str, int]:
    return {field: 0 for field in COUNT_FIELDS}


def safe_error(text: str | Any) -> str:
    """Reduce an exception to one short line with any bearer token redacted."""
    return _JWT.sub("[redacted]", clean_message(str(text)))


def status_path(run_dir: Path) -> Path:
    return Path(run_dir) / STATUS_NAME


def read_status(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(status_path(run_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") not in STATUSES:
        return None
    return _normalized(data)


def mark_queued(run_dir: Path) -> dict[str, Any]:
    def mutate(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is not None and current["status"] in PENDING_STATUSES:
            return None
        now = _now()
        attempt = int(current["attempt"]) + 1 if current else 1
        return {
            **_base(run_dir),
            "status": QUEUED,
            "attempt": attempt,
            "queued_at": now,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "retryable": False,
            "counts": empty_counts(),
        }

    return _update(run_dir, mutate)


def mark_running(run_dir: Path) -> dict[str, Any]:
    def mutate(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current or {**_base(run_dir), "attempt": 0, "queued_at": None, "counts": empty_counts()}
        return {
            **base,
            "status": RUNNING,
            "attempt": max(int(base.get("attempt") or 0), 1),
            "started_at": _now(),
            "completed_at": None,
            "error": None,
            "retryable": False,
        }

    return _update(run_dir, mutate)


def mark_completed(run_dir: Path, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
    def mutate(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if counts is None and current is not None and current["status"] == COMPLETED:
            return None
        base = current or {**_base(run_dir), "attempt": 1, "queued_at": None, "started_at": None}
        merged = {**empty_counts(), **{k: int(v) for k, v in (counts or {}).items() if k in COUNT_FIELDS}}
        return {
            **base,
            "status": COMPLETED,
            "completed_at": _now(),
            "error": None,
            "retryable": False,
            "counts": merged if counts is not None else base.get("counts") or empty_counts(),
        }

    return _update(run_dir, mutate)


def mark_failed(run_dir: Path, error: str, retryable: bool = True) -> dict[str, Any]:
    def mutate(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current or {**_base(run_dir), "attempt": 1, "queued_at": None, "started_at": None}
        return {
            **base,
            "status": FAILED,
            "completed_at": _now(),
            "error": safe_error(error),
            "retryable": bool(retryable),
            "counts": base.get("counts") or empty_counts(),
        }

    return _update(run_dir, mutate)


def mark_not_required(run_dir: Path, reason: str) -> dict[str, Any]:
    def mutate(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current or {**_base(run_dir), "attempt": 0, "queued_at": None, "started_at": None}
        return {
            **base,
            "status": NOT_REQUIRED,
            "completed_at": _now(),
            "error": safe_error(reason),
            "retryable": False,
            "counts": empty_counts(),
        }

    return _update(run_dir, mutate)


def describe(reports_dir: Path, run_timestamp: str) -> dict[str, Any]:
    """The API view: the recorded status, or one derived from the manifest and ledger."""
    run_dir = Path(reports_dir) / run_timestamp
    recorded = read_status(run_dir)
    if recorded is not None:
        return {**recorded, "run_timestamp": run_timestamp}
    return {
        **_base(run_dir),
        "run_timestamp": run_timestamp,
        "status": _derived_status(reports_dir, run_dir),
        "attempt": 0,
        "queued_at": None,
        "started_at": None,
        "completed_at": None,
        "last_updated_at": None,
        "error": None,
        "retryable": False,
        "counts": empty_counts(),
    }


def pending_run_timestamps(reports_dir: Path) -> list[str]:
    root = Path(reports_dir)
    if not root.is_dir():
        return []
    pending = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        recorded = read_status(child)
        if recorded is not None and recorded["status"] in PENDING_STATUSES:
            pending.append(child.name)
    return pending


def worker_state_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / WORKER_STATE_NAME


def read_worker_state(reports_dir: Path) -> dict[str, Any]:
    empty = {"last_success_at": None, "last_failure_at": None, "last_error": None}
    try:
        data = json.loads(worker_state_path(reports_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {key: data.get(key) if isinstance(data.get(key), str) else None for key in empty}


def record_worker_pass(reports_dir: Path, succeeded: bool, error: str | None = None) -> dict[str, Any]:
    state = read_worker_state(reports_dir)
    now = _now()
    if succeeded:
        state["last_success_at"] = now
    else:
        state["last_failure_at"] = now
        state["last_error"] = safe_error(error) if error else None
    write_atomic(worker_state_path(reports_dir), json.dumps(state, indent=2, sort_keys=True))
    return state


def _derived_status(reports_dir: Path, run_dir: Path) -> str:
    manifest = read_manifest(run_dir)
    if manifest is None:
        # Legacy folders are skipped unless explicitly imported, so none is expected.
        return COMPLETED if _in_ledger(reports_dir, run_dir) else NOT_REQUIRED
    if not is_completed(manifest):
        return NOT_REQUIRED
    return COMPLETED if _in_ledger(reports_dir, run_dir) else QUEUED


def _in_ledger(reports_dir: Path, run_dir: Path) -> bool:
    if not run_dir.is_dir():
        return False
    return is_processed(reports_dir, run_dir.name, report_digest(run_dir))


def _base(run_dir: Path) -> dict[str, Any]:
    return {"run_id": Path(run_dir).name, "run_timestamp": Path(run_dir).name}


def _normalized(data: Mapping[str, Any]) -> dict[str, Any]:
    counts = data.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    return {
        "run_id": str(data.get("run_id") or ""),
        "run_timestamp": str(data.get("run_timestamp") or data.get("run_id") or ""),
        "status": str(data["status"]),
        "attempt": int(data.get("attempt") or 0),
        "queued_at": _text_or_none(data.get("queued_at")),
        "started_at": _text_or_none(data.get("started_at")),
        "completed_at": _text_or_none(data.get("completed_at")),
        "last_updated_at": _text_or_none(data.get("last_updated_at")),
        "error": _text_or_none(data.get("error")),
        "retryable": bool(data.get("retryable")),
        "counts": {field: int(counts.get(field) or 0) for field in COUNT_FIELDS},
    }


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _update(run_dir: Path, mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None]) -> dict[str, Any]:
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    with _status_lock(path):
        current = read_status(path)
        updated = mutate(current)
        if updated is None:
            return current if current is not None else describe(path.parent, path.name)
        updated["last_updated_at"] = _now()
        payload = _normalized(updated)
        write_atomic(status_path(path), json.dumps(payload, indent=2, sort_keys=True))
        return payload


@contextmanager
def _status_lock(run_dir: Path) -> Iterator[None]:
    """A separate lock file: the status file itself is replaced, not rewritten."""
    handle = (Path(run_dir) / STATUS_LOCK_NAME).open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _now() -> str:
    return datetime.now().astimezone().isoformat()
