from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.run_manifest import MANIFEST_NAME

LEDGER_NAME = ".dashboard_sync_ledger.json"
LOCK_NAME = ".dashboard_sync.lock"
DIGEST_INPUTS = (MANIFEST_NAME, "dashboard_results.json")


class SyncBusy(RuntimeError):
    pass


def report_digest(run_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in DIGEST_INPUTS:
        path = Path(run_dir) / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def ledger_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / LEDGER_NAME


def load_ledger(reports_dir: Path) -> dict[str, str]:
    try:
        data = json.loads(ledger_path(reports_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def is_processed(reports_dir: Path, run_timestamp: str, digest: str) -> bool:
    return load_ledger(reports_dir).get(run_timestamp) == digest


def mark_processed(reports_dir: Path, run_timestamp: str, digest: str) -> None:
    ledger = load_ledger(reports_dir)
    ledger[run_timestamp] = digest
    write_atomic(ledger_path(reports_dir), json.dumps(ledger, indent=2, sort_keys=True))


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def lock_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / LOCK_NAME


def worker_running(reports_dir: Path) -> bool:
    """Probe the host lock to see whether a sync pass holds it right now."""
    path = lock_path(reports_dir)
    if not path.exists():
        return False
    try:
        handle = path.open("r")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


@contextmanager
def single_instance(reports_dir: Path, wait_seconds: float = 0) -> Any:
    """Hold an exclusive host-wide lock for the duration of one sync pass.

    Manual and scheduled passes keep the default non-blocking behavior. A
    post-run trigger may wait briefly so two platforms finishing together are
    serialized instead of allowing the second trigger to disappear as busy.
    """
    if wait_seconds < 0:
        raise ValueError("wait_seconds must be greater than or equal to 0")
    path = Path(reports_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise SyncBusy(f"another dashboard sync is already running (lock: {path})") from exc
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
