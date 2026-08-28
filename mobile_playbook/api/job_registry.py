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
    status: str = "running"
    run_timestamp: str | None = None
    run_dir: str | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    completed_at: str | None = None


class JobRegistry:
    """Persisted tracker for runs triggered through the API."""

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
        """Claim one physical device platform for the current process."""
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
