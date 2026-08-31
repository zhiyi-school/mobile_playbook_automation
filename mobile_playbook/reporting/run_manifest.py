from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MANIFEST_NAME = "run_manifest.json"
COMPLETED = "completed"
FAILED = "failed"


def manifest_path(run_dir: Path) -> Path:
    return Path(run_dir) / MANIFEST_NAME


def write_manifest(
    run_dir: Path,
    *,
    run_timestamp: str,
    platform: str,
    attempted: list[dict[str, str]],
    status: str,
    started_at: str | None = None,
    completed_at: str | None = None,
    error: str | None = None,
) -> Path:
    payload = {
        "run_timestamp": run_timestamp,
        "platform": platform,
        "apps": sorted({item["app_id"] for item in attempted}),
        "risks": sorted({item["risk_id"] for item in attempted}),
        "attempted": attempted,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "error": error,
    }
    path = manifest_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".run_manifest.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def read_manifest(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(manifest_path(run_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_completed(manifest: Mapping[str, Any] | None) -> bool:
    return manifest is not None and manifest.get("status") == COMPLETED
