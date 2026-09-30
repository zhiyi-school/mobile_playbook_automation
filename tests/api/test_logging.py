from __future__ import annotations

import logging
import logging.config
import subprocess
import sys

from mobile_playbook.api.__main__ import MOBILE_PLAYBOOK_LOG_FORMAT, api_log_config
from mobile_playbook.common.storage_paths import REPOSITORY_ROOT


def _restore_logger(name: str, state: tuple[int, bool, bool, list[logging.Handler]]) -> None:
    logger = logging.getLogger(name)
    level, disabled, propagate, handlers = state
    logger.setLevel(level)
    logger.disabled = disabled
    logger.propagate = propagate
    logger.handlers[:] = handlers


def test_api_log_config_preserves_uvicorn_and_enables_mobile_playbook_info():
    logger_names = ["", "mobile_playbook", "uvicorn", "uvicorn.error", "uvicorn.access"]
    states = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).disabled,
            logging.getLogger(name).propagate,
            logging.getLogger(name).handlers[:],
        )
        for name in logger_names
    }

    try:
        config = api_log_config()

        assert config["disable_existing_loggers"] is False
        assert config["root"]["level"] == "INFO"
        assert "mobile_playbook" in config["root"]["handlers"]
        assert config["loggers"]["uvicorn.access"]["handlers"] == ["access"]

        logging.config.dictConfig(config)

        mobile_logger = logging.getLogger("mobile_playbook.platforms.ios.runner")
        assert mobile_logger.isEnabledFor(logging.INFO)

        configured_logger = logging.getLogger("mobile_playbook")
        assert configured_logger.handlers
        assert configured_logger.handlers[0].formatter._fmt == MOBILE_PLAYBOOK_LOG_FORMAT

        uvicorn_access = logging.getLogger("uvicorn.access")
        assert uvicorn_access.handlers
        assert uvicorn_access.propagate is False
    finally:
        for name, state in states.items():
            _restore_logger(name, state)


def test_a_log_file_adds_a_rotating_handler_to_every_logger(tmp_path):
    config = api_log_config(tmp_path / "logs" / "api.log")

    handler = config["handlers"]["file"]
    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert handler["filename"] == str(tmp_path / "logs" / "api.log")
    assert handler["maxBytes"] == 10 * 1024 * 1024
    assert handler["backupCount"] == 5
    assert "file" in config["root"]["handlers"]
    for name in ("mobile_playbook", "uvicorn", "uvicorn.access"):
        assert "file" in config["loggers"][name]["handlers"]


def test_without_a_log_file_nothing_is_written_to_disk():
    config = api_log_config()

    assert "file" not in config["handlers"]
    assert all("file" not in logger.get("handlers", []) for logger in config["loggers"].values())


def test_the_log_file_config_writes_and_rotates(tmp_path):
    log_file = tmp_path / "api.log"
    script = (
        "import logging, logging.config, sys\n"
        "from pathlib import Path\n"
        "from mobile_playbook.api.__main__ import api_log_config\n"
        "config = api_log_config(Path(sys.argv[1]))\n"
        "config['handlers']['file']['maxBytes'] = 200\n"
        "logging.config.dictConfig(config)\n"
        "for index in range(20):\n"
        "    logging.getLogger('mobile_playbook.example').warning('line %d of the api log', index)\n"
    )

    subprocess.run([sys.executable, "-c", script, str(log_file)], check=True, capture_output=True, cwd=REPOSITORY_ROOT)

    assert "line 19 of the api log" in log_file.read_text()
    assert (tmp_path / "api.log.1").is_file()
