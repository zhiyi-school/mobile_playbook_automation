"""
Resolves runtime storage locations from their settings or ARTIFACTS_DIR. See docs/storage.md.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

ARTIFACTS_DIR_ENV = "ARTIFACTS_DIR"
DEFAULT_ARTIFACTS_DIR = REPOSITORY_ROOT / "artifacts"

LOCATION_ENV = {
    "intake": "INTAKE_DIR",
    "derived": "ARTIFACT_STORE_DIR",
    "reports": "REPORTS_DIR",
    "work": "WORK_DIR",
}

LOCATION_NAMES = tuple(LOCATION_ENV)
logger = logging.getLogger(__name__)


# Returns a storage setting from the process environment, else from the API's allowlisted .env settings.
def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        logger.debug("storage: %s set in the process environment.", name)
        return value
    # Goes through the API's allowlist, so no other .env key can be read here.
    from mobile_playbook.api import settings

    return settings.env_setting(name)


# Expands a path value and resolves a relative one against the repository root.
def _absolute(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPOSITORY_ROOT / path


# Reports whether child lies under parent.
def _contained(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# Returns the resolved ARTIFACTS_DIR root, defaulting to the repository's artifacts directory.
def artifacts_root() -> Path:
    configured = _setting(ARTIFACTS_DIR_ENV)
    root = (_absolute(configured) if configured else DEFAULT_ARTIFACTS_DIR).resolve()
    logger.debug("storage: artifacts root %s (configured=%s).", root, bool(configured))
    return root


# Returns a named storage location from its own setting, else from under the artifacts root.
def location(name: str) -> Path:
    if name not in LOCATION_ENV:
        logger.debug("storage: unknown location %r requested.", name)
        raise KeyError(f"unknown storage location: {name}")
    configured = _setting(LOCATION_ENV[name])
    if configured:
        resolved = _absolute(configured).resolve()
        logger.debug("storage: %s location %s from %s.", name, resolved, LOCATION_ENV[name])
        return resolved
    resolved = (artifacts_root() / name).resolve()
    logger.debug("storage: %s location %s under the artifacts root.", name, resolved)
    return resolved


# Returns the intake location.
def intake_root() -> Path:
    return location("intake")


# Returns the derived artifact store location.
def derived_root() -> Path:
    return location("derived")


# Returns the reports location.
def reports_root() -> Path:
    return location("reports")


# Returns the work location.
def work_root() -> Path:
    return location("work")


# Returns the directory for intake iOS IPAs.
def ios_intake_dir() -> Path:
    return intake_root() / "ios" / "ipas"


# Returns the directory for intake Android APKs.
def android_intake_dir() -> Path:
    return intake_root() / "android" / "apks"


# Returns the iOS work directory.
def ios_work_dir() -> Path:
    return work_root() / "ios"


# Returns the path of the iOS traffic interception capture file.
def ios_capture_path() -> Path:
    return ios_work_dir() / "traffic_interception" / "capture.jsonl"


# Returns the Android work directory.
def android_work_dir() -> Path:
    return work_root() / "android"


# Resolves a path value, treating a relative one as relative to the repository root.
def resolve_under_repository(value: str | Path) -> Path:
    return _absolute(str(value)).resolve()


# Maps a path recorded under the old repository-root layout onto its current location when that file exists.
def resolve_recorded_path(recorded: str | Path) -> Path:
    path = Path(recorded).expanduser()
    if not path.is_absolute():
        logger.debug("storage: recorded path %s is relative; unchanged.", path)
        return path
    for name in LOCATION_NAMES:
        legacy = (REPOSITORY_ROOT / name).resolve()
        if not _contained(path, legacy):
            continue
        current = location(name)
        if current == legacy:
            logger.debug("storage: recorded path %s already under the current %s location.", path, name)
            return path
        moved = current / path.relative_to(legacy)
        logger.debug("storage: legacy %s path %s maps to %s (exists=%s).", name, path, moved, moved.exists())
        return moved if moved.exists() else path
    logger.debug("storage: recorded path %s is outside every legacy location; unchanged.", path)
    return path
