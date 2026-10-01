from __future__ import annotations

import logging
import logging.config
import os
import subprocess
import sys

import pytest

from mobile_playbook.api import settings
from mobile_playbook.api.__main__ import MOBILE_PLAYBOOK_LOG_FORMAT, api_log_config
from mobile_playbook.common.storage_paths import REPOSITORY_ROOT


@pytest.fixture(autouse=True)
def unexported_log_level(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)


@pytest.mark.parametrize("value, expected", [(" debug ", "DEBUG"), ("chatty", "INFO"), ("", "INFO")])
def test_api_log_level_reads_the_env_file_and_validates_the_value(tmp_path, monkeypatch, value, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(f'LOG_LEVEL="{value}"\n')
    monkeypatch.setattr(settings, "ENV_FILE", env_file)

    assert api_log_config()["loggers"]["mobile_playbook"]["level"] == expected


@pytest.mark.parametrize("value, expected", [("WARNING", "WARNING"), ("", "INFO")])
def test_exported_log_level_takes_precedence_over_the_env_file(tmp_path, monkeypatch, value, expected):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=DEBUG\n")
    monkeypatch.setattr(settings, "ENV_FILE", env_file)
    monkeypatch.setenv("LOG_LEVEL", value)

    assert api_log_config()["loggers"]["mobile_playbook"]["level"] == expected


def test_api_log_level_defaults_to_info_without_a_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")

    assert api_log_config()["loggers"]["mobile_playbook"]["level"] == "INFO"


@pytest.mark.parametrize("launch_mode", ["entry_point", "module"])
def test_env_file_debug_logs_include_startup_in_the_console_and_file(tmp_path, launch_mode):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=DEBUG\nSUPABASE_SERVICE_ROLE_KEY=synthetic-secret\n")
    log_file = tmp_path / "logs" / "api.log"
    script = """
import logging
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
import uvicorn
from mobile_playbook.api import settings
from mobile_playbook.platforms.ios import config as ios_config
from mobile_playbook.orchestration import appium_server

settings.ENV_FILE = Path(sys.argv[1])
log_file, launch_mode = sys.argv[2:4]

def load_config(path, dry_run):
    logging.getLogger('mobile_playbook.startup').debug('synthetic config loaded')
    return SimpleNamespace(device=SimpleNamespace(appium_server_url='http://127.0.0.1:4723', appium_auto_start={}))

def ensure_appium(*args):
    logging.getLogger('mobile_playbook.startup').debug('synthetic Appium checked')
    return appium_server.AppiumStartResult(status='ALREADY_RUNNING')

def serve(*args, **kwargs):
    assert kwargs['log_config']['loggers']['mobile_playbook']['level'] == 'DEBUG'
    assert 'SUPABASE_SERVICE_ROLE_KEY' not in os.environ

ios_config.load_config = load_config
appium_server.ensure_appium_running = ensure_appium
uvicorn.run = serve
sys.argv = ['mobile-playbook-api', '--log-file', log_file]
if launch_mode == 'module':
    runpy.run_module('mobile_playbook.api', run_name='__main__')
else:
    from mobile_playbook.api.__main__ import main
    main()
logging.shutdown()
"""
    environment = os.environ.copy()
    environment.pop("LOG_LEVEL", None)
    environment.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(env_file), str(log_file), launch_mode],
        check=True, capture_output=True, text=True, cwd=REPOSITORY_ROOT, env=environment,
    )

    for message in ("api: starting", "synthetic config loaded", "synthetic Appium checked"):
        assert message in result.stdout
        assert message in log_file.read_text()


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
