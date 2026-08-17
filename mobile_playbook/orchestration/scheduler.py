from __future__ import annotations

from datetime import datetime
from pathlib import Path


def new_run_timestamp(root: Path, now: datetime | None = None, extra_files: tuple[str, ...] = ()) -> str:
    """Return a sortable local timestamp that does not collide under root.

    `extra_files` accepts format strings with `{timestamp}` for sibling files
    that should also reserve the timestamp, for example
    `"{timestamp}-acquire-results.json"`.
    """
    root = Path(root)
    base = (now or datetime.now().astimezone()).strftime("%Y-%m-%d_%H-%M-%S")
    candidate = base
    suffix = 2
    while _timestamp_exists(root, candidate, extra_files):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _timestamp_exists(root: Path, timestamp: str, extra_files: tuple[str, ...]) -> bool:
    if (root / timestamp).exists():
        return True
    return any((root / pattern.format(timestamp=timestamp)).exists() for pattern in extra_files)
