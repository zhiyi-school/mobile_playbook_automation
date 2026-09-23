from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mobile_playbook import __version__

HEALTH_SCHEMA_VERSION = 1


def health_record_path(capture_path: Path) -> Path:
    return capture_path.expanduser().resolve(strict=False).with_suffix(".health.json")


def normalize_proxy_url(proxy_url: str) -> str:
    value = str(proxy_url or "").strip()
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not parsed.scheme or not hostname:
        return value.rstrip("/")
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def canonical_capture_path(capture_path: Path) -> str:
    return str(capture_path.expanduser().resolve(strict=False))


def device_udid_hash(device_udid: str) -> str:
    return hashlib.sha256(str(device_udid).encode("utf-8")).hexdigest()


def write_health_record(
    *,
    capture_path: Path,
    proxy_url: str,
    canary_host: str,
    valid_entry_count: int,
    device_udid: str,
) -> Path:
    destination = health_record_path(capture_path)
    record = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxy_url": normalize_proxy_url(proxy_url),
        "capture_path": canonical_capture_path(capture_path),
        "canary_host": str(canary_host).strip().lower().rstrip("."),
        "valid_entry_count": int(valid_entry_count),
        "device_udid_sha256": device_udid_hash(device_udid),
        "tool_version": _tool_version(),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def read_health_record(capture_path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(health_record_path(capture_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _tool_version() -> str:
    try:
        return package_version("mobile-playbook-automation")
    except PackageNotFoundError:
        return __version__
