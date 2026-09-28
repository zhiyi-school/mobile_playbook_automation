from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY = re.compile(r"token|passw(?:or)?d|pwd|(?:^|[-_])pass(?:$|[-_])|secret|credential|private[-_]?key|authorization|cookie|api[-_]?key|service[-_]?role", re.I)
REDACTED = "[redacted]"


def log_level(verbose: bool = False) -> int:
    """DEBUG when verbose, otherwise the LOG_LEVEL environment variable (default INFO)."""
    if verbose:
        return logging.DEBUG
    level = logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO").strip().upper())
    return level if isinstance(level, int) else logging.INFO


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=log_level(verbose),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def redacted(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of `mapping` safe for logs, with credential-like keys masked at any depth."""
    return {
        key: REDACTED if _SENSITIVE_KEY.search(str(key)) else redacted(value) if isinstance(value, Mapping) else value
        for key, value in mapping.items()
    }


def safe_url(url: object) -> str:
    """`url` without user info, query or fragment, for logging configured URLs."""
    try:
        parts = urlsplit(str(url))
        host = f"{parts.hostname or ''}:{parts.port}" if parts.port else parts.hostname or ""
    except ValueError:
        return REDACTED
    return urlunsplit((parts.scheme, host, parts.path, "", ""))
