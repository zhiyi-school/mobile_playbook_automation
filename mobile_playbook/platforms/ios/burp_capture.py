from __future__ import annotations

import json
from pathlib import Path


def capture_line_count(capture_path: Path) -> int:
    if not capture_path.exists():
        return 0
    with capture_path.open() as handle:
        return sum(1 for _ in handle)


def read_new_capture_entries(capture_path: Path, start_line: int, expected_hosts: list[str]) -> list[dict]:
    if not capture_path.exists():
        return []
    lines = capture_path.read_text().splitlines()
    entries: list[dict] = []
    for line in lines[start_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        host = str(entry.get("host") or "").lower()
        if not expected_hosts or any(expected in host for expected in expected_hosts):
            entries.append(entry)
    return entries
