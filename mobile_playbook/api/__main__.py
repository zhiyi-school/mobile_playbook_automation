from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from mobile_playbook.orchestration.appium_process import ensure_appium_running, stop_appium
from mobile_playbook.platforms.ios.config import load_config
from mobile_playbook.storage import ios_work_dir

MOBILE_PLAYBOOK_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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
    config["root"] = {"level": "INFO", "handlers": ["mobile_playbook"]}
    config.setdefault("loggers", {})["mobile_playbook"] = {
        "level": "INFO",
        "handlers": ["mobile_playbook"],
        "propagate": False,
    }
    return config


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mobile_playbook.api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)  # 8000 collides with MobSF's default port
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--ios-config", default="configs/ios.yaml")
    args = parser.parse_args()

    config = load_config(Path(args.ios_config), dry_run=False)
    result = ensure_appium_running(
        config.device.appium_server_url,
        config.device.appium_auto_start,
        ios_work_dir() / "appium-api.log",
    )
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
            stop_appium(result.process)


if __name__ == "__main__":
    main()
