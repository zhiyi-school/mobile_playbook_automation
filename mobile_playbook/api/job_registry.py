"""
Persisted registry of API-triggered runs and the per-platform device claims.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from mobile_playbook.api.settings import REPORTS_ROOT

DEFAULT_PERSIST_PATH = REPORTS_ROOT / ".job_registry.json"

INTERRUPTED_ERROR = "Interrupted by API server restart"
logger = logging.getLogger(__name__)


@dataclass
class RunRecord:
    run_id: str
    platform: str
    config_path: str
    status: str = "running"
    run_timestamp: str | None = None
    run_dir: str | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    completed_at: str | None = None
    apps: str | None = None
    risks: str | None = None


class JobRegistry:
    """Persisted tracker for runs triggered through the API."""

    # Create the registry and load any persisted records.
    def __init__(self, persist_path: Path | None = DEFAULT_PERSIST_PATH) -> None:
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._records: dict[str, RunRecord] = {}
        self._busy_platforms: set[str] = set()
        self._load()

    # Load persisted records, marking runs left running by a restart as failed.
    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            logger.debug("api: no persisted run registry at %s; starting empty.", self._persist_path)
            return
        logger.debug("api: loading run registry from %s.", self._persist_path)
        try:
            raw = json.loads(self._persist_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("api: run registry read failed for %s.", self._persist_path, exc_info=True)
            logger.warning("api: could not read %s (%s) — starting with an empty run registry.", self._persist_path, exc)
            return
        interrupted = 0
        for run_id, data in raw.items():
            try:
                record = RunRecord(**data)
            except TypeError as exc:
                logger.debug("api: run record %r could not be parsed.", run_id, exc_info=True)
                logger.warning(
                    "api: skipping malformed run record %r in %s (%s).",
                    run_id,
                    self._persist_path,
                    exc,
                )
                continue
            if record.status == "running":
                logger.debug("api: recovering interrupted run %s (running -> failed).", record.run_id)
                record.status = "failed"
                record.error = INTERRUPTED_ERROR
                record.completed_at = datetime.now().astimezone().isoformat()
                interrupted += 1
            self._records[record.run_id] = record
        logger.info("api: restored %d run record(s) from %s.", len(self._records), self._persist_path)
        if interrupted:
            logger.info("api: marked %d still-'running' run(s) as failed (%s).", interrupted, INTERRUPTED_ERROR)
            self._save()

    # Atomically write every record to the persist path, when one is set.
    def _save(self) -> None:
        if self._persist_path is None:
            logger.debug("api: run registry has no persist path; skipping save.")
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {run_id: asdict(record) for run_id, record in self._records.items()}
        fd, tmp_path = tempfile.mkstemp(dir=self._persist_path.parent, prefix=".job_registry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self._persist_path)
        except BaseException:
            logger.debug(
                "api: run registry save to %s failed; removing %s.", self._persist_path, tmp_path, exc_info=True
            )
            Path(tmp_path).unlink(missing_ok=True)
            raise
        logger.debug("api: saved %d run record(s) to %s.", len(payload), self._persist_path)

    # Claim one physical device platform for the current process.
    def try_claim_platform(self, platform: str) -> bool:
        with self._lock:
            if platform in self._busy_platforms:
                logger.debug("api: platform %s is busy; claim refused.", platform)
                return False
            self._busy_platforms.add(platform)
            logger.debug("api: claimed platform %s.", platform)
            return True

    # Release a platform claim.
    def release_platform(self, platform: str) -> None:
        with self._lock:
            self._busy_platforms.discard(platform)
            logger.debug("api: released platform %s.", platform)

    # Return whether a platform is currently claimed.
    def is_platform_busy(self, platform: str) -> bool:
        with self._lock:
            busy = platform in self._busy_platforms
        logger.debug("api: platform %s busy=%s.", platform, busy)
        return busy

    # Record and persist a new running run.
    def create(
        self,
        run_id: str,
        platform: str,
        config_path: str,
        apps: str | None = None,
        risks: str | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=run_id,
            platform=platform,
            config_path=config_path,
            run_timestamp=run_id,
            apps=apps,
            risks=risks,
        )
        with self._lock:
            self._records[record.run_id] = record
            self._save()
        logger.debug(
            "api: run %s created (platform=%s, apps=%s, risks=%s, config=%s).", run_id, platform, apps, risks, config_path
        )
        return record

    # Mark a run completed with its report directory and persist it.
    def mark_completed(self, run_id: str, run_dir: Path) -> None:
        with self._lock:
            record = self._records[run_id]
            logger.debug("api: run %s %s -> completed (run_dir=%s).", run_id, record.status, run_dir)
            record.status = "completed"
            record.run_dir = str(run_dir)
            record.completed_at = datetime.now().astimezone().isoformat()
            self._save()

    # Mark a run failed with its error and persist it.
    def mark_failed(self, run_id: str, error: str) -> None:
        with self._lock:
            record = self._records[run_id]
            logger.debug("api: run %s %s -> failed (%s).", run_id, record.status, error)
            record.status = "failed"
            record.error = error
            record.completed_at = datetime.now().astimezone().isoformat()
            self._save()

    # Return a run's record, or None.
    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(run_id)

    # Return every record, most recently started first.
    def list(self) -> list[RunRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)


registry = JobRegistry()
