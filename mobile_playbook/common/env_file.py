"""
Reads individual KEY=VALUE settings without changing the process environment, and loads files for CLI and worker startup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


# Resolve a requested setting from the environment or repository .env without exporting any file values.
def env_value(name: str, env_path: Path | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        return value
    return file_value(name, env_path)


# Read one setting from the repository .env alone, ignoring the process environment.
def file_value(name: str, env_path: Path | None = None) -> str | None:
    path = env_path if env_path is not None else ENV_FILE
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        logger.debug("env file: %s unreadable while looking up %s (%s).", path, name, type(exc).__name__)
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


# Sets unset environment variables from a KEY=VALUE file, stripping matching quotes; missing files are ignored.
def load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        logger.debug("env file: %s not found; nothing loaded.", path)
        return
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        logger.debug("env file: %s unreadable (%s); nothing loaded.", path, type(exc).__name__)
        return
    loaded: list[str] = []
    skipped: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            if key:
                skipped.append(key)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
        loaded.append(key)
    logger.debug("env file: %s loaded keys %s; kept existing environment for %s.", path, loaded, skipped)
