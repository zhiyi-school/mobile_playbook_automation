"""
Allowlisted API settings read from the environment or the repository .env file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPOSITORY_ROOT / ".env"
logger = logging.getLogger(__name__)

#: Only these keys may be read from .env; the rest, notably the service-role key, stay out of the API.
ALLOWED_ENV_KEYS = frozenset(
    {
        "CORS_ALLOWED_ORIGINS",
        "ARTIFACTS_DIR",
        "ARTIFACT_STORE_DIR",
        "INTAKE_DIR",
        "IOS_PLAYBOOK_DIR",
        "ANDROID_PLAYBOOK_DIR",
        "REPORTS_DIR",
        "WORK_DIR",
    }
)


class DisallowedSettingError(KeyError):
    pass


# Resolve one allowlisted setting from the process environment, then the .env file.
def env_setting(name: str, env_path: Path | None = None) -> str | None:
    if name not in ALLOWED_ENV_KEYS:
        logger.debug("api: setting %s is not allowlisted; refusing to read it.", name)
        raise DisallowedSettingError(f"{name} is not readable from .env by the API process")
    value = os.environ.get(name)
    if value is not None:
        logger.debug("api: setting %s taken from the process environment.", name)
        return value
    logger.debug("api: setting %s not in the process environment; checking the env file.", name)
    return read_env_file_value(env_path if env_path is not None else ENV_FILE, name)


# Read one key from a .env file without loading any other key into the process.
def read_env_file_value(path: Path, wanted_key: str) -> str | None:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        logger.debug("api: env file %s unreadable while looking up %s: %s", path, wanted_key, type(exc).__name__)
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != wanted_key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        logger.debug("api: setting %s found in env file %s.", wanted_key, path)
        return value
    logger.debug("api: setting %s not present in env file %s.", wanted_key, path)
    return None


# Resolve a path setting, relative to the repository root when not absolute.
def repository_path_setting(name: str, default: str, env_path: Path | None = None) -> Path:
    configured = env_setting(name, env_path) or default
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    logger.debug("api: path setting %s resolved to %s.", name, path.resolve())
    return path.resolve()


# Import the shared storage paths module lazily.
def _storage() -> Any:
    from mobile_playbook.storage import paths

    return paths


# Kept for existing importers; resolved by mobile_playbook.storage.paths.
REPORTS_ROOT = _storage().reports_root()
WORK_ROOT = _storage().work_root()
