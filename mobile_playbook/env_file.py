from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


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
