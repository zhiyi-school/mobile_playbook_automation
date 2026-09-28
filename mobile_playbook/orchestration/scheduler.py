"""
Allocates collision-free run timestamps under a reports root.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# Return a sortable local timestamp not yet used under root or by the `{timestamp}` extra_files patterns.
def new_run_timestamp(root: Path, now: datetime | None = None, extra_files: tuple[str, ...] = ()) -> str:
    root = Path(root)
    base = (now or datetime.now().astimezone()).strftime("%Y-%m-%d_%H-%M-%S")
    candidate = base
    suffix = 2
    while _timestamp_exists(root, candidate, extra_files):
        logger.debug("scheduler: run timestamp %s already taken under %s", candidate, root)
        candidate = f"{base}-{suffix}"
        suffix += 1
    logger.debug("scheduler: candidate run timestamp %s under %s", candidate, root)
    return candidate


# Report whether the timestamp's directory or any of its extra files already exists under root.
def _timestamp_exists(root: Path, timestamp: str, extra_files: tuple[str, ...]) -> bool:
    if (root / timestamp).exists():
        return True
    return any((root / pattern.format(timestamp=timestamp)).exists() for pattern in extra_files)


# Atomically claim a run timestamp by creating its directory, retrying when a concurrent caller wins.
def reserve_run_timestamp(root: Path, now: datetime | None = None, extra_files: tuple[str, ...] = ()) -> str:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = new_run_timestamp(root, now=now, extra_files=extra_files)
        try:
            (root / candidate).mkdir(parents=True, exist_ok=False)
            logger.debug("scheduler: reserved run directory %s", root / candidate)
            return candidate
        except FileExistsError as exc:
            logger.debug("scheduler: lost race for %s, retrying: %s", root / candidate, exc, exc_info=True)
            continue
