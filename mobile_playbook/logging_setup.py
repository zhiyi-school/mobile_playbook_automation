"""
Logging configuration plus helpers that redact credentials and URLs before they are logged.
"""

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


# Returns DEBUG when verbose, otherwise the LOG_LEVEL environment variable (default INFO).
def log_level(verbose: bool = False) -> int:
    if verbose:
        return logging.DEBUG
    level = logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO").strip().upper())
    return level if isinstance(level, int) else logging.INFO


# Configures root logging to stdout with timestamps at the level chosen by log_level.
def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=log_level(verbose),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


# Returns a copy of `mapping` safe for logs, with credential-like keys masked at any depth.
def redacted(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: REDACTED if _SENSITIVE_KEY.search(str(key)) else redacted(value) if isinstance(value, Mapping) else value
        for key, value in mapping.items()
    }


# Returns `url` without user info, query or fragment, or a redaction marker if it cannot be parsed.
def safe_url(url: object) -> str:
    try:
        parts = urlsplit(str(url))
        host = f"{parts.hostname or ''}:{parts.port}" if parts.port else parts.hostname or ""
    except ValueError:
        return REDACTED
    return urlunsplit((parts.scheme, host, parts.path, "", ""))
