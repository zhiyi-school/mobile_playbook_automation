from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.models import Platform
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.ios.config import ConfigError, load_config


def load_platform_config(platform: Platform, config_path: str, dry_run: bool = False):
    path = Path(config_path)
    if platform == "android":
        return load_android_config(path, dry_run=dry_run)
    return load_config(path, dry_run=dry_run)


def config_error_detail(exc: ConfigError | AndroidConfigError) -> list[str]:
    return list(exc.errors)


def load_config_or_400(platform: Platform, config_path: str):
    try:
        return load_platform_config(platform, config_path, dry_run=False)
    except (ConfigError, AndroidConfigError) as exc:
        raise HTTPException(status_code=422, detail=config_error_detail(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
