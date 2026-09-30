"""
Writes and reads the health record that marks a Burp capture file as verified for a proxy and device.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mobile_playbook import __version__

logger = logging.getLogger(__name__)

HEALTH_SCHEMA_VERSION = 1


# Return the .health.json path that sits beside a capture file.
def health_record_path(capture_path: Path) -> Path:
    return capture_path.expanduser().resolve(strict=False).with_suffix(".health.json")


# Normalize a proxy URL's scheme, host, port and path so equivalent URLs compare equal.
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


# Return the capture path expanded and resolved as a string.
def canonical_capture_path(capture_path: Path) -> str:
    return str(capture_path.expanduser().resolve(strict=False))


# Return the SHA-256 hex digest of a device UDID so the record never stores it raw.
def device_udid_hash(device_udid: str) -> str:
    return hashlib.sha256(str(device_udid).encode("utf-8")).hexdigest()


# Atomically write the health record for a verified capture and return its path.
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
    logger.debug(
        "ios burp health: writing %s (proxy=%s canary_host=%s valid_entries=%s tool_version=%s)",
        destination,
        record["proxy_url"],
        record["canary_host"],
        record["valid_entry_count"],
        record["tool_version"],
    )
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
        logger.debug("ios burp health: wrote %s", destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            logger.debug("ios burp health: removing leftover temporary file %s", temporary_path)
            temporary_path.unlink()
    return destination


# Return the capture's health record, or None when it is missing, unreadable or not an object.
def read_health_record(capture_path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(health_record_path(capture_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("ios burp health: no readable health record for %s: %s", capture_path, exc, exc_info=True)
        return None
    if not isinstance(value, dict):
        logger.debug("ios burp health: health record for %s is not an object (%s)", capture_path, type(value).__name__)
        return None
    logger.debug(
        "ios burp health: read record for %s (schema=%s verified_at=%s proxy=%s canary_host=%s valid_entries=%s)",
        capture_path,
        value.get("schema_version"),
        value.get("verified_at"),
        value.get("proxy_url"),
        value.get("canary_host"),
        value.get("valid_entry_count"),
    )
    return value


# Return the installed package version, falling back to the source __version__.
def _tool_version() -> str:
    try:
        return package_version("mobile-playbook-automation")
    except PackageNotFoundError as exc:
        logger.debug("ios burp health: package metadata unavailable, using __version__: %s", exc, exc_info=True)
        return __version__
