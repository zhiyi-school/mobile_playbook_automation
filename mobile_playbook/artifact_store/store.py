"""
Content-addressed store for derived artifact icons and metadata.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.storage import LOCATION_ENV

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR_ENV = LOCATION_ENV["derived"]

ICONS_SUBDIR = "icons"
METADATA_SUBDIR = "artifacts"
ICON_REF_PREFIX = f"{ICONS_SUBDIR}/"

ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ICON_REF_PATTERN = re.compile(r"^icons/([0-9a-f]{64})\.png$")

_digest_cache: dict[tuple[str, int, float], str] = {}
logger = logging.getLogger(__name__)


# Returns the derived artifact store root.
def store_root() -> Path:
    from mobile_playbook.storage import derived_root

    return derived_root()


# Reports whether value is a lowercase SHA-256 hex digest.
def is_artifact_id(value: str | None) -> bool:
    return bool(value) and bool(ARTIFACT_ID_PATTERN.match(str(value)))


# Returns the logical icon reference for an artifact id.
def icon_ref(artifact_id: str) -> str:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return f"{ICON_REF_PREFIX}{artifact_id}.png"


# Returns the stored icon file path for an artifact id.
def icon_path(artifact_id: str) -> Path:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return store_root() / ICONS_SUBDIR / f"{artifact_id}.png"


# Returns the stored metadata file path for an artifact id.
def metadata_path(artifact_id: str) -> Path:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return store_root() / METADATA_SUBDIR / f"{artifact_id}.json"


# Maps an issued icon reference back to its existing file, or None if it is not one we issued.
def resolve_icon_ref(ref: str | None) -> Path | None:
    match = ICON_REF_PATTERN.match(str(ref or ""))
    if match is None:
        logger.debug("artifact store: %r is not an issued icon reference.", ref)
        return None
    path = icon_path(match.group(1))
    logger.debug("artifact store: icon reference %s -> %s (exists=%s).", ref, path, path.is_file())
    return path if path.is_file() else None


# Returns the file's SHA-256, cached by resolved path, size and modification time.
def artifact_digest(path: Path) -> str:
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime)
    cached = _digest_cache.get(key)
    if cached is None:
        logger.debug("artifact store: digest cache miss for %s (%d bytes); hashing.", path, stat.st_size)
        cached = sha256_file(path)
        _digest_cache[key] = cached
    else:
        logger.debug("artifact store: digest cache hit for %s.", path)
    return cached


# Replaces path with data in one step and returns the path.
def write_atomic(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        logger.debug("artifact store: atomic write of %s failed; removing %s.", path, temp_name, exc_info=True)
        Path(temp_name).unlink(missing_ok=True)
        raise
    logger.debug("artifact store: wrote %d bytes to %s.", len(data), path)
    return path


# Reads an artifact's stored metadata, or returns None if missing or unreadable.
def read_metadata(artifact_id: str) -> dict[str, Any] | None:
    try:
        metadata = json.loads(metadata_path(artifact_id).read_text())
    except (OSError, ValueError) as exc:
        logger.debug("artifact store: no readable metadata for %s (%s).", artifact_id, type(exc).__name__)
        return None
    logger.debug("artifact store: read metadata for %s.", artifact_id)
    return metadata


# Atomically writes an artifact's metadata as JSON and returns its path.
def write_metadata(artifact_id: str, metadata: dict[str, Any]) -> Path:
    return write_atomic(metadata_path(artifact_id), json.dumps(metadata, indent=2, sort_keys=True).encode())
