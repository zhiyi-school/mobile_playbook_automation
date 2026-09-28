from __future__ import annotations

import fcntl
import hashlib
import json
import logging
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
logger = logging.getLogger(__name__)


class SyncBusy(RuntimeError):
    pass


def report_digest(run_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in DIGEST_INPUTS:
        path = Path(run_dir) / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"")
    logger.debug("sync state: digest of %s is %s.", run_dir, digest.hexdigest()[:12])
    return digest.hexdigest()


def ledger_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / LEDGER_NAME


def load_ledger(reports_dir: Path) -> dict[str, str]:
    try:
        data = json.loads(ledger_path(reports_dir).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug(
            "sync state: ledger %s unavailable (%s); treating it as empty.",
            ledger_path(reports_dir),
            type(exc).__name__,
        )
        return {}
    if not isinstance(data, dict):
        logger.debug("sync state: ledger %s is not a mapping; treating it as empty.", ledger_path(reports_dir))
        return {}
    logger.debug("sync state: ledger %s has %d entr(ies).", ledger_path(reports_dir), len(data))
    return {str(k): str(v) for k, v in data.items()}


def is_processed(reports_dir: Path, run_timestamp: str, digest: str) -> bool:
    processed = load_ledger(reports_dir).get(run_timestamp) == digest
    logger.debug("sync state: run %s processed with digest %s: %s.", run_timestamp, digest[:12], processed)
    return processed


def mark_processed(reports_dir: Path, run_timestamp: str, digest: str) -> None:
    ledger = load_ledger(reports_dir)
    ledger[run_timestamp] = digest
    write_atomic(ledger_path(reports_dir), json.dumps(ledger, indent=2, sort_keys=True))
    logger.debug("sync state: ledger marked %s processed (digest %s).", run_timestamp, digest[:12])


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
        logger.debug("sync state: atomic write of %s failed; removing %s.", path, tmp_name, exc_info=True)
        Path(tmp_name).unlink(missing_ok=True)
        raise
    logger.debug("sync state: wrote %d characters to %s.", len(text), path)


def lock_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / LOCK_NAME


def worker_running(reports_dir: Path) -> bool:
    """Probe the host lock to see whether a sync pass holds it right now."""
    path = lock_path(reports_dir)
    if not path.exists():
        logger.debug("sync state: no lock file at %s; worker idle.", path)
        return False
    try:
        handle = path.open("r")
    except OSError as exc:
        logger.debug("sync state: lock file %s unreadable (%s); worker treated as idle.", path, type(exc).__name__)
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        logger.debug("sync state: lock %s is held; worker running.", path)
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        logger.debug("sync state: lock %s is free; worker idle.", path)
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
    logger.debug("sync state: acquiring %s (wait up to %.1fs).", path, wait_seconds)
    try:
        attempts = 0
        while True:
            attempts += 1
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    logger.debug("sync state: %s busy after %d attempt(s); giving up.", path, attempts)
                    raise SyncBusy(f"another dashboard sync is already running (lock: {path})") from exc
                if attempts == 1:
                    logger.debug("sync state: %s busy; waiting for release.", path)
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        logger.debug("sync state: acquired %s after %d attempt(s).", path, attempts)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            logger.debug("sync state: released %s.", path)
    finally:
        handle.close()
