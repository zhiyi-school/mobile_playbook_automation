from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanJob:
    platform: str
    app_id: str
    risk_id: str


class JobQueue:
    def __init__(self, jobs: list[ScanJob] | None = None):
        self._jobs: Deque[ScanJob] = deque(jobs or [])
        logger.debug("job queue: created with %s jobs", len(self._jobs))

    def add(self, job: ScanJob) -> None:
        self._jobs.append(job)
        logger.debug("job queue: added %s (%s queued)", job, len(self._jobs))

    def pop(self) -> ScanJob | None:
        job = self._jobs.popleft() if self._jobs else None
        logger.debug("job queue: popped %s (%s remaining)", job, len(self._jobs))
        return job

    def __iter__(self) -> Iterator[ScanJob]:
        while self._jobs:
            job = self._jobs.popleft()
            logger.debug("job queue: yielding %s (%s remaining)", job, len(self._jobs))
            yield job
