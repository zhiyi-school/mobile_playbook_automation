from __future__ import annotations

import logging
import logging.config

from mobile_playbook.api.__main__ import MOBILE_PLAYBOOK_LOG_FORMAT, api_log_config


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
