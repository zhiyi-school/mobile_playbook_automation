"""
Appends and reads a run's events.jsonl progress log.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

EVENTS_FILENAME = "events.jsonl"

_write_lock = threading.Lock()
logger = logging.getLogger(__name__)


# Appends one JSON line describing a run-progress event to the run's events.jsonl.
def append_event(run_dir: Path, event_type: str, **fields: object) -> None:
    record = {"type": event_type, "timestamp": datetime.now().astimezone().isoformat(), **fields}
    line = json.dumps(record, sort_keys=True, default=str)
    run_dir = Path(run_dir)
    with _write_lock:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / EVENTS_FILENAME).open("a") as handle:
            handle.write(line + "\n")
    logger.debug(
        "reporting: appended %s event to %s (fields %s).", event_type, run_dir / EVENTS_FILENAME, sorted(fields)
    )


# Returns events after index `since` and the new count, never returning a partially written trailing line.
def read_events(run_dir: Path, since: int = 0) -> tuple[list[dict], int]:
    path = Path(run_dir) / EVENTS_FILENAME
    if not path.exists():
        return [], since
    events: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("reporting: stopping at a partial line in %s after %d event(s).", path, len(events))
            break  # partial trailing line; it is read again once flushed
    if len(events) > since:
        logger.debug("reporting: %d new event(s) in %s since %d.", len(events) - since, path, since)
    return events[since:], len(events)
