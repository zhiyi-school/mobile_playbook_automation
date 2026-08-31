from __future__ import annotations

import argparse
from copy import deepcopy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

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
    args = parser.parse_args()
    uvicorn.run(
        "mobile_playbook.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=api_log_config(),
    )


if __name__ == "__main__":
    main()
