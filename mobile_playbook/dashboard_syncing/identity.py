from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


def rows_by_app(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["app_id"]), []).append(row)
    return grouped


def sync_key(external_id: str, run_timestamp: str, kind: str) -> str:
    return f"{external_id}::{run_timestamp}::{kind}"


def run_sort_key(run_timestamp: str) -> tuple[str, int]:
    base, _, suffix = str(run_timestamp).rpartition("-")
    if base and suffix.isdigit() and len(suffix) <= 2 and base.count("-") >= 4:
        return base, int(suffix)
    return str(run_timestamp), 1


def is_older_run(candidate: str, current: Any) -> bool:
    return bool(current) and run_sort_key(candidate) < run_sort_key(str(current))


def finding_status(verdict: str) -> str:
    if verdict == "At Risk":
        return "at_risk"
    if verdict == "Reduced Risk":
        return "reduced_risk"
    return "inconclusive"


def now() -> str:
    return datetime.now().astimezone().isoformat()
