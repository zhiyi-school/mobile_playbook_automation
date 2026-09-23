"""Where the backend keeps runtime files.

Each location resolves in this order, highest first:

1. Its own setting — ``INTAKE_DIR``, ``ARTIFACT_STORE_DIR`` (derived),
   ``REPORTS_DIR``, ``WORK_DIR``.
2. ``ARTIFACTS_DIR``/<name>, defaulting to ``<repository>/artifacts/<name>``.

A relative value resolves against the repository root, never the process
working directory. See docs/storage.md.
"""

from __future__ import annotations

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


def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value
    # Goes through the API's allowlist, so no other .env key can be read here.
    from mobile_playbook.api import settings

    return settings.env_setting(name)


def _absolute(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _contained(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def artifacts_root() -> Path:
    configured = _setting(ARTIFACTS_DIR_ENV)
    return (_absolute(configured) if configured else DEFAULT_ARTIFACTS_DIR).resolve()


def location(name: str) -> Path:
    if name not in LOCATION_ENV:
        raise KeyError(f"unknown storage location: {name}")
    configured = _setting(LOCATION_ENV[name])
    if configured:
        return _absolute(configured).resolve()
    return (artifacts_root() / name).resolve()


def intake_root() -> Path:
    return location("intake")


def derived_root() -> Path:
    return location("derived")


def reports_root() -> Path:
    return location("reports")


def work_root() -> Path:
    return location("work")


def ios_intake_dir() -> Path:
    return intake_root() / "ios" / "ipas"


def android_intake_dir() -> Path:
    return intake_root() / "android" / "apks"


def ios_work_dir() -> Path:
    return work_root() / "ios"


def ios_capture_path() -> Path:
    return ios_work_dir() / "traffic_interception" / "capture.jsonl"


def android_work_dir() -> Path:
    return work_root() / "android"


def resolve_under_repository(value: str | Path) -> Path:
    return _absolute(str(value)).resolve()


def resolve_recorded_path(recorded: str | Path) -> Path:
    """Map a path recorded under the old repository-root layout onto its file.

    Historical reports hold absolute paths such as ``<repo>/work/ios/...``.
    Those directories now live under ``artifacts/``, so such a path is rewritten
    onto the current location; anything outside a known location, or already
    current, is returned unchanged.
    """
    path = Path(recorded).expanduser()
    if not path.is_absolute():
        return path
    for name in LOCATION_NAMES:
        legacy = (REPOSITORY_ROOT / name).resolve()
        if not _contained(path, legacy):
            continue
        current = location(name)
        if current == legacy:
            return path
        moved = current / path.relative_to(legacy)
        return moved if moved.exists() else path
    return path
