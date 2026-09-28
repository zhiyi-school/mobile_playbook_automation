from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.models import Platform
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.ios.config import ConfigError, load_config

logger = logging.getLogger(__name__)


def load_platform_config(platform: Platform, config_path: str, dry_run: bool = False):
    path = Path(config_path)
    logger.debug("api: loading %s config %s (dry_run=%s).", platform, path, dry_run)
    if platform == "android":
        return load_android_config(path, dry_run=dry_run)
    return load_config(path, dry_run=dry_run)


def config_error_detail(exc: ConfigError | AndroidConfigError) -> list[str]:
    return list(exc.errors)


def load_config_or_400(platform: Platform, config_path: str):
    try:
        return load_platform_config(platform, config_path, dry_run=False)
    except (ConfigError, AndroidConfigError) as exc:
        logger.debug("api: %s config %s invalid (%d error(s)); responding 422.", platform, config_path, len(exc.errors))
        raise HTTPException(status_code=422, detail=config_error_detail(exc)) from exc
    except OSError as exc:
        logger.debug("api: %s config %s unreadable; responding 400.", platform, config_path, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
