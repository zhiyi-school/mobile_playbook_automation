from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

EVENTS_FILENAME = "events.jsonl"

_write_lock = threading.Lock()


def append_event(run_dir: Path, event_type: str, **fields: object) -> None:
    """Append one JSON line describing a run-progress event to `run_dir`/events.jsonl.

    This is the same file whether the run was started from the CLI or the
    API — cheap to write, and gives anything polling it (the API's SSE
    endpoint, or a person just reading the file) a blow-by-blow account of
    the run alongside the final report.json/summary.md it already writes.
    """
    record = {"type": event_type, "timestamp": datetime.now().astimezone().isoformat(), **fields}
    line = json.dumps(record, sort_keys=True, default=str)
    run_dir = Path(run_dir)
    with _write_lock:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / EVENTS_FILENAME).open("a") as handle:
            handle.write(line + "\n")


def read_events(run_dir: Path, since: int = 0) -> tuple[list[dict], int]:
    """Return events after index `since`, plus the new count to pass back in as `since` next time.

    Reads the whole file and re-parses it each call rather than tracking a
    byte offset, since these files stay small (one run's worth of progress
    events) and re-parsing avoids ever returning a truncated trailing line
    from a write that's still in progress.
    """
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
            break  # a partially-written trailing line; pick it up on the next read once it's flushed
    return events[since:], len(events)
