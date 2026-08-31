from __future__ import annotations

from pathlib import Path

from mobile_playbook.api.settings import env_setting

DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
CORS_ENV_KEY = "CORS_ALLOWED_ORIGINS"


def cors_allowed_origins(env_path: Path | None = None) -> list[str]:
    """Resolved on call, so it is correct under any entrypoint that imports the app."""
    configured = env_setting(CORS_ENV_KEY, env_path) or ""
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError(f"{CORS_ENV_KEY} must list explicit origins; '*' is not allowed")
    return origins or DEFAULT_CORS_ORIGINS
