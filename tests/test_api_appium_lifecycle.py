from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mobile_playbook.api.__main__ as api_main
from mobile_playbook.orchestration.appium_process import AppiumStartResult


def _configure_launcher(monkeypatch, tmp_path, result, events, uvicorn_run):
    config = SimpleNamespace(
        device=SimpleNamespace(
            appium_server_url="http://127.0.0.1:4723",
            appium_auto_start={"enabled": True},
        )
    )
    process = result.process
    monkeypatch.setattr(sys, "argv", ["python -m mobile_playbook.api"])
    monkeypatch.setattr(api_main, "load_config", lambda path, dry_run: config)
    monkeypatch.setattr(api_main, "ios_work_dir", lambda: tmp_path)
    monkeypatch.setattr(
        api_main,
        "ensure_appium_running",
        lambda *args: events.append("start") or result,
    )
    monkeypatch.setattr(api_main.uvicorn, "run", uvicorn_run)
    monkeypatch.setattr(
        api_main,
        "stop_appium",
        lambda stopped: events.append("stop") if stopped is process else pytest.fail("wrong process"),
    )


def test_api_starts_appium_before_uvicorn_and_stops_after_shutdown(monkeypatch, tmp_path):
    events = []
    result = AppiumStartResult(status="STARTED", process=object())
    _configure_launcher(
        monkeypatch,
        tmp_path,
        result,
        events,
        lambda *args, **kwargs: events.append("uvicorn"),
    )

    api_main.main()

    assert events == ["start", "uvicorn", "stop"]


def test_api_stops_owned_appium_when_uvicorn_raises(monkeypatch, tmp_path):
    events = []
    result = AppiumStartResult(status="STARTED", process=object())

    def raise_from_uvicorn(*args, **kwargs):
        events.append("uvicorn")
        raise RuntimeError("uvicorn failed")

    _configure_launcher(monkeypatch, tmp_path, result, events, raise_from_uvicorn)

    with pytest.raises(RuntimeError, match="uvicorn failed"):
        api_main.main()

    assert events == ["start", "uvicorn", "stop"]


def test_api_does_not_stop_independently_running_appium(monkeypatch, tmp_path):
    events = []
    result = AppiumStartResult(status="ALREADY_RUNNING")
    _configure_launcher(
        monkeypatch,
        tmp_path,
        result,
        events,
        lambda *args, **kwargs: events.append("uvicorn"),
    )

    api_main.main()

    assert events == ["start", "uvicorn"]


def test_appium_startup_failure_prevents_api_start(monkeypatch, tmp_path):
    events = []
    result = AppiumStartResult(status="FAILED", error="Appium failed")
    _configure_launcher(
        monkeypatch,
        tmp_path,
        result,
        events,
        lambda *args, **kwargs: pytest.fail("uvicorn should not start"),
    )

    with pytest.raises(RuntimeError, match="Appium failed"):
        api_main.main()

    assert events == ["start"]


def test_api_accepts_ios_config_argument(monkeypatch, tmp_path):
    loaded = []
    config = SimpleNamespace(
        device=SimpleNamespace(
            appium_server_url="http://127.0.0.1:4723",
            appium_auto_start={"enabled": True},
        )
    )
    monkeypatch.setattr(sys, "argv", ["python -m mobile_playbook.api", "--ios-config", "custom-ios.yaml"])
    monkeypatch.setattr(
        api_main,
        "load_config",
        lambda path, dry_run: loaded.append((path, dry_run)) or config,
    )
    monkeypatch.setattr(api_main, "ios_work_dir", lambda: tmp_path)
    monkeypatch.setattr(
        api_main,
        "ensure_appium_running",
        lambda *args: AppiumStartResult(status="ALREADY_RUNNING"),
    )
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *args, **kwargs: None)

    api_main.main()

    assert loaded == [(Path("custom-ios.yaml"), False)]
