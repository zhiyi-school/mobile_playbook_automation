from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.mutations.hashing import sha256_file

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR_ENV = "ARTIFACT_STORE_DIR"
DEFAULT_STORE_DIR = REPOSITORY_ROOT / "derived"

ICONS_SUBDIR = "icons"
METADATA_SUBDIR = "artifacts"
ICON_REF_PREFIX = f"{ICONS_SUBDIR}/"

ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ICON_REF_PATTERN = re.compile(r"^icons/([0-9a-f]{64})\.png$")

_digest_cache: dict[tuple[str, int, float], str] = {}


def store_root() -> Path:
    from mobile_playbook.api import settings

    configured = os.environ.get(STORE_DIR_ENV) or settings.read_env_file_value(settings.ENV_FILE, STORE_DIR_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_STORE_DIR


def is_artifact_id(value: str | None) -> bool:
    return bool(value) and bool(ARTIFACT_ID_PATTERN.match(str(value)))


def icon_ref(artifact_id: str) -> str:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return f"{ICON_REF_PREFIX}{artifact_id}.png"


def icon_path(artifact_id: str) -> Path:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return store_root() / ICONS_SUBDIR / f"{artifact_id}.png"


def metadata_path(artifact_id: str) -> Path:
    if not is_artifact_id(artifact_id):
        raise ValueError("artifact_id must be a sha256 hex digest")
    return store_root() / METADATA_SUBDIR / f"{artifact_id}.json"


def resolve_icon_ref(ref: str | None) -> Path | None:
    """Map a stored logical reference back to a file, or `None` if it is not one we issued."""
    match = ICON_REF_PATTERN.match(str(ref or ""))
    if match is None:
        return None
    path = icon_path(match.group(1))
    return path if path.is_file() else None


def artifact_digest(path: Path) -> str:
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime)
    cached = _digest_cache.get(key)
    if cached is None:
        cached = sha256_file(path)
        _digest_cache[key] = cached
    return cached


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
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def read_metadata(artifact_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(metadata_path(artifact_id).read_text())
    except (OSError, ValueError):
        return None


def write_metadata(artifact_id: str, metadata: dict[str, Any]) -> Path:
    return write_atomic(metadata_path(artifact_id), json.dumps(metadata, indent=2, sort_keys=True).encode())
