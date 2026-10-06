"""
Command-line entry point that checks Appium, starts the API server with uvicorn, and runs the assessment worker alongside it.
"""

from __future__ import annotations

import argparse
import logging
import threading
from copy import deepcopy
from logging.config import dictConfig
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from mobile_playbook.api.settings import env_setting
from mobile_playbook.orchestration.appium_server import ensure_appium_running, stop_appium
from mobile_playbook.platforms.ios.config import load_config
from mobile_playbook.common.storage_paths import config_path, ios_work_dir, resolve_under_repository
from mobile_playbook.workers.launch_agent import (
    LAUNCHCTL_TIMEOUT_SECONDS,
    start_worker_agent_when_listening,
    stop_worker_agent,
)

MOBILE_PLAYBOOK_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5
logger = logging.getLogger(__name__)


# Build a uvicorn logging config that routes mobile_playbook loggers at the configured level, optionally also to a rotating file.
def api_log_config(log_file: Path | None = None) -> dict[str, Any]:
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
    level = (env_setting("LOG_LEVEL") or "INFO").strip().upper()
    if not isinstance(logging.getLevelName(level), int):
        level = "INFO"
    config["root"] = {"level": "INFO", "handlers": ["mobile_playbook"]}
    config.setdefault("loggers", {})["mobile_playbook"] = {
        "level": level,
        "handlers": ["mobile_playbook"],
        "propagate": False,
    }
    if log_file is not None:
        config["handlers"]["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "mobile_playbook",
            "filename": str(log_file),
            "maxBytes": LOG_FILE_MAX_BYTES,
            "backupCount": LOG_FILE_BACKUPS,
            "encoding": "utf-8",
        }
        config["root"]["handlers"] = [*config["root"]["handlers"], "file"]
        for name in ("mobile_playbook", "uvicorn", "uvicorn.access"):
            config["loggers"][name]["handlers"] = [*config["loggers"][name].get("handlers", []), "file"]
    # Python names the launcher __main__ when it is executed with -m.
    if __name__ == "__main__":
        config["loggers"]["__main__"] = deepcopy(config["loggers"]["mobile_playbook"])
    return config


# Parse CLI options, ensure Appium is running, serve the API with the assessment worker, then stop what it started.
def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mobile_playbook.api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)  # 8000 collides with MobSF's default port
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--ios-config", default=str(config_path("ios.yaml")))
    parser.add_argument("--log-file", default=None, help="Also write logs to this file, rotated at 10 MB with 5 backups.")
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Do not start the assessment worker's launch agent with the server or stop it when the server stops.",
    )
    args = parser.parse_args()
    log_file = resolve_under_repository(args.log_file) if args.log_file else None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    logging_config = api_log_config(log_file)
    dictConfig(logging_config)
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

    cancel_worker_start = threading.Event()
    worker_start = None
    if not args.no_worker:
        probe_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        worker_start = start_worker_agent_when_listening(f"http://{probe_host}:{args.port}", cancel_worker_start)
    try:
        uvicorn.run(
            "mobile_playbook.api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_config=logging_config,
        )
    finally:
        if worker_start is not None:
            # Let a start already in progress finish, so the stop below cannot be overtaken by it.
            cancel_worker_start.set()
            worker_start.join(timeout=3 * LAUNCHCTL_TIMEOUT_SECONDS)
            stop_worker_agent()
        if result.status == "STARTED" and result.process is not None:
            logger.debug("api: stopping the Appium process started for this server.")
            stop_appium(result.process)


if __name__ == "__main__":
    main()
