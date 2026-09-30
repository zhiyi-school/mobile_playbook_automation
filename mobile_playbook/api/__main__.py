"""
Command-line entry point that checks Appium and starts the API server with uvicorn.
"""

from __future__ import annotations

import argparse
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from mobile_playbook.common.logging_setup import log_level
from mobile_playbook.orchestration.appium_process import ensure_appium_running, stop_appium
from mobile_playbook.platforms.ios.config import load_config
from mobile_playbook.common.storage_paths import ios_work_dir

MOBILE_PLAYBOOK_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logger = logging.getLogger(__name__)


# Build a uvicorn logging config that also routes mobile_playbook loggers at the configured level.
def api_log_config() -> dict[str, Any]:
    config = deepcopy(LOGGING_CONFIG)
    config["disable_existing_loggers"] = False
    config.setdefault("formatters", {})["mobile_playbook"] = {
        "format": MOBILE_PLAYBOOK_LOG_FORMAT,
    }
    config.setdefault("handlers", {})["mobile_playbook"] = {
        "class": "logging.StreamHandler",
        "formatter": "mobile_playbook",
        "stream": "ext://sys.stdout",
    }
    level = logging.getLevelName(log_level())
    config["root"] = {"level": "INFO", "handlers": ["mobile_playbook"]}
    config.setdefault("loggers", {})["mobile_playbook"] = {
        "level": level,
        "handlers": ["mobile_playbook"],
        "propagate": False,
    }
    return config


# Parse CLI options, ensure Appium is running, serve the API, and stop any Appium it started.
def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mobile_playbook.api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)  # 8000 collides with MobSF's default port
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--ios-config", default="configs/ios.yaml")
    args = parser.parse_args()
    logger.debug(
        "api: starting on %s:%s (reload=%s, ios_config=%s).", args.host, args.port, args.reload, args.ios_config
    )

    config = load_config(Path(args.ios_config), dry_run=False)
    result = ensure_appium_running(
        config.device.appium_server_url,
        config.device.appium_auto_start,
        ios_work_dir() / "appium-api.log",
    )
    logger.debug("api: Appium check at %s returned %s.", config.device.appium_server_url, result.status)
    if result.status == "FAILED":
        raise RuntimeError(result.error)

    try:
        uvicorn.run(
            "mobile_playbook.api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_config=api_log_config(),
        )
    finally:
        if result.status == "STARTED" and result.process is not None:
            logger.debug("api: stopping the Appium process started for this server.")
            stop_appium(result.process)


if __name__ == "__main__":
    main()
