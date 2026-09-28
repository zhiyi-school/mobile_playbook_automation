"""
Resolves the CORS origins the API allows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.api.settings import env_setting

DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
CORS_ENV_KEY = "CORS_ALLOWED_ORIGINS"
logger = logging.getLogger(__name__)


# Return the configured CORS origins, or the local defaults, rejecting a wildcard. Resolved on each call.
def cors_allowed_origins(env_path: Path | None = None) -> list[str]:
    configured = env_setting(CORS_ENV_KEY, env_path) or ""
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in origins:
        logger.debug("api: %s contains a wildcard origin; rejecting it.", CORS_ENV_KEY)
        raise RuntimeError(f"{CORS_ENV_KEY} must list explicit origins; '*' is not allowed")
    logger.debug("api: CORS origins %s (default used: %s).", origins or DEFAULT_CORS_ORIGINS, not origins)
    return origins or DEFAULT_CORS_ORIGINS
