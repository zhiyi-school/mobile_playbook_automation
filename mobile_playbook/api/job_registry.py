from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PERSIST_PATH = Path("reports/.job_registry.json")

INTERRUPTED_ERROR = "Interrupted by API server restart"


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
    """Tracker for runs triggered through the API, persisted to disk.

    `run_id` is always the run's `run_timestamp` (also its
    `reports/<run_timestamp>/` directory name), reserved up front by the
    caller before the record is created — there is no separate ID scheme
    to look up.

    Records are written to `persist_path` on every change and reloaded from
    there on startup, so `GET /runs`/`GET /runs/{run_id}` history survives an
    API server restart. Platform-claim state (`_busy_platforms`) is *not*
    persisted: it only ever reflects a run actually in progress in this
    process's threads, and a restart kills those threads regardless, so any
    record still `"running"` at load time is rewritten to `"failed"` — it can
    never actually finish and polling it would hang forever otherwise.
    """

    def __init__(self, persist_path: Path | None = DEFAULT_PERSIST_PATH) -> None:
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._records: dict[str, RunRecord] = {}
        self._busy_platforms: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"api: could not read {self._persist_path} ({exc}) — starting with an empty run registry.")
            return
        interrupted = 0
        for run_id, data in raw.items():
            try:
                record = RunRecord(**data)
            except TypeError as exc:
                print(f"api: skipping malformed run record {run_id!r} in {self._persist_path} ({exc}).")
                continue
            if record.status == "running":
                record.status = "failed"
                record.error = INTERRUPTED_ERROR
                record.completed_at = datetime.now().astimezone().isoformat()
                interrupted += 1
            self._records[record.run_id] = record
        print(f"api: restored {len(self._records)} run record(s) from {self._persist_path}.")
        if interrupted:
            print(f"api: marked {interrupted} still-'running' run(s) as failed ({INTERRUPTED_ERROR}).")
            self._save()

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {run_id: asdict(record) for run_id, record in self._records.items()}
        fd, tmp_path = tempfile.mkstemp(dir=self._persist_path.parent, prefix=".job_registry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self._persist_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

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
            self._save()
        return record

    def mark_completed(self, run_id: str, run_dir: Path) -> None:
        with self._lock:
            record = self._records[run_id]
            record.status = "completed"
            record.run_dir = str(run_dir)
            record.completed_at = datetime.now().astimezone().isoformat()
            self._save()

    def mark_failed(self, run_id: str, error: str) -> None:
        with self._lock:
            record = self._records[run_id]
            record.status = "failed"
            record.error = error
            record.completed_at = datetime.now().astimezone().isoformat()
            self._save()

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(run_id)

    def list(self) -> list[RunRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)


registry = JobRegistry()
