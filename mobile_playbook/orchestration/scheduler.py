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


def reserve_run_timestamp(root: Path, now: datetime | None = None, extra_files: tuple[str, ...] = ()) -> str:
    """Atomically claim a run_timestamp by creating its directory.

    `new_run_timestamp` only checks-then-returns, which leaves a window
    between two concurrent callers observing the same "does not exist"
    state and both picking the same candidate — fine for the CLI, which
    only ever allocates one run_timestamp per process, but not safe when
    multiple requests can call this in overlapping threads (e.g. the HTTP
    API). This instead creates the directory as part of picking it, so a
    loser of the race retries against the winner's now-existing directory.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = new_run_timestamp(root, now=now, extra_files=extra_files)
        try:
            (root / candidate).mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
