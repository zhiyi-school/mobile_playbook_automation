from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterator


@dataclass(frozen=True)
class ScanJob:
    platform: str
    app_id: str
    risk_id: str


class JobQueue:
    def __init__(self, jobs: list[ScanJob] | None = None):
        self._jobs: Deque[ScanJob] = deque(jobs or [])

    def add(self, job: ScanJob) -> None:
        self._jobs.append(job)

    def pop(self) -> ScanJob | None:
        return self._jobs.popleft() if self._jobs else None

    def __iter__(self) -> Iterator[ScanJob]:
        while self._jobs:
            yield self._jobs.popleft()
