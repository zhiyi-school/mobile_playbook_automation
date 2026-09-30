from __future__ import annotations

import logging

from mobile_playbook.api.__main__ import api_log_config
from mobile_playbook.common.logging_setup import REDACTED, log_level, redacted, safe_url


def test_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert log_level() == logging.INFO


def test_log_level_reads_environment_and_verbose_wins(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert log_level() == logging.DEBUG
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert log_level() == logging.WARNING
    assert log_level(verbose=True) == logging.DEBUG


def test_log_level_ignores_unknown_values(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "chatty")
    assert log_level() == logging.INFO


def test_api_log_config_applies_log_level_to_package_loggers_only(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    config = api_log_config()
    assert config["loggers"]["mobile_playbook"]["level"] == "DEBUG"
    assert config["root"]["level"] == "INFO"


def test_redacted_masks_credential_keys_at_any_depth():
    assert redacted(
        {
            "Authorization": "Bearer x",
            "apikey": "k",
            "headers": {"Prefer": "return=representation", "SUPABASE_SERVICE_ROLE_KEY": "s"},
            "keystore_pass": "p",
            "bypass_proxy": True,
            "id": 3,
        }
    ) == {
        "Authorization": REDACTED,
        "apikey": REDACTED,
        "headers": {"Prefer": "return=representation", "SUPABASE_SERVICE_ROLE_KEY": REDACTED},
        "keystore_pass": REDACTED,
        "bypass_proxy": True,
        "id": 3,
    }


def test_safe_url_drops_credentials_query_and_fragment():
    assert safe_url("http://user:pass@127.0.0.1:8081/ca.cer?token=x#f") == "http://127.0.0.1:8081/ca.cer"
    assert safe_url("http://burp.example.test/cert") == "http://burp.example.test/cert"
    assert safe_url("http://[::1") == REDACTED
