from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class RunRecord:
    run_id: str
    platform: str
    config_path: str
    status: str = "running"  # "running" | "completed" | "failed"
    run_timestamp: str | None = None
    run_dir: str | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    completed_at: str | None = None


class JobRegistry:
    """In-memory tracker for runs triggered through the API.

    Scoped to a single process/run of the API server — restarting the
    server loses this history, though the underlying reports/<timestamp>/
    directories on disk are unaffected and remain browsable via the
    reports endpoints regardless of how they were created.

    `run_id` is always the run's `run_timestamp` (also its
    `reports/<run_timestamp>/` directory name), reserved up front by the
    caller before the record is created — there is no separate ID scheme
    to look up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, RunRecord] = {}
        self._busy_platforms: set[str] = set()

    def try_claim_platform(self, platform: str) -> bool:
        """Claim `platform` for an in-progress run, or return False if one's already running.

        Each platform's config identifies one physical device, and a run
        drives real Appium sessions against it — a second concurrent run for
        the same platform would fight the first over that same device. This
        is per-platform (not global) because iOS and Android runs already
        target separate devices and are meant to run concurrently, the same
        way the CLI's `run-all` already does.
        """
        with self._lock:
            if platform in self._busy_platforms:
                return False
            self._busy_platforms.add(platform)
            return True

    def release_platform(self, platform: str) -> None:
        with self._lock:
            self._busy_platforms.discard(platform)

    def create(self, run_id: str, platform: str, config_path: str) -> RunRecord:
        record = RunRecord(run_id=run_id, platform=platform, config_path=config_path, run_timestamp=run_id)
        with self._lock:
            self._records[record.run_id] = record
        return record

    def mark_completed(self, run_id: str, run_dir: Path) -> None:
        with self._lock:
            record = self._records[run_id]
            record.status = "completed"
            record.run_dir = str(run_dir)
            record.completed_at = datetime.now().astimezone().isoformat()

    def mark_failed(self, run_id: str, error: str) -> None:
        with self._lock:
            record = self._records[run_id]
            record.status = "failed"
            record.error = error
            record.completed_at = datetime.now().astimezone().isoformat()

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(run_id)

    def list(self) -> list[RunRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)


registry = JobRegistry()
