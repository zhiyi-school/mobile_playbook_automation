from __future__ import annotations

import os
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPOSITORY_ROOT / ".env"

#: Only these may be read from .env. Everything else in that file — the Supabase
#: service-role key above all — stays out of the API process entirely.
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


def env_setting(name: str, env_path: Path | None = None) -> str | None:
    """Resolve one allowlisted setting: shell environment, then .env, then `None`."""
    if name not in ALLOWED_ENV_KEYS:
        raise DisallowedSettingError(f"{name} is not readable from .env by the API process")
    value = os.environ.get(name)
    if value is not None:
        return value
    return read_env_file_value(env_path if env_path is not None else ENV_FILE, name)


def read_env_file_value(path: Path, wanted_key: str) -> str | None:
    """Parse one key out of a .env file without importing any other key into the process."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
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
        return value
    return None


def repository_path_setting(name: str, default: str, env_path: Path | None = None) -> Path:
    configured = env_setting(name, env_path) or default
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    return path.resolve()


def _storage() -> Any:
    from mobile_playbook.storage import paths

    return paths


# Kept as module attributes for existing importers; both follow the shared
# resolution in mobile_playbook.storage.paths.
REPORTS_ROOT = _storage().reports_root()
WORK_ROOT = _storage().work_root()
