"""
Pure helpers for sync keys, run ordering and verdict mapping.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


# Groups dashboard result rows by their app_id.
def rows_by_app(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["app_id"]), []).append(row)
    return grouped


# Builds the idempotency key for one kind of row written for an app's run.
def sync_key(external_id: str, run_timestamp: str, kind: str) -> str:
    return f"{external_id}::{run_timestamp}::{kind}"


# Returns a run timestamp sort key, treating a short numeric suffix as a same-second sequence number.
def run_sort_key(run_timestamp: str) -> tuple[str, int]:
    base, _, suffix = str(run_timestamp).rpartition("-")
    if base and suffix.isdigit() and len(suffix) <= 2 and base.count("-") >= 4:
        return base, int(suffix)
    return str(run_timestamp), 1


# Reports whether candidate is an older run than current, when current is set.
def is_older_run(candidate: str, current: Any) -> bool:
    return bool(current) and run_sort_key(candidate) < run_sort_key(str(current))


# Maps a report verdict to a dashboard finding status.
def finding_status(verdict: str) -> str:
    if verdict == "At Risk":
        return "at_risk"
    if verdict == "Reduced Risk":
        return "reduced_risk"
    return "inconclusive"


# Returns the current local time with its UTC offset as an ISO 8601 string.
def now() -> str:
    return datetime.now().astimezone().isoformat()
