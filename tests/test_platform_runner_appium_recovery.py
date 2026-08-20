from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.orchestration.appium_process import AppiumStartResult
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.runner import IosPlatformRunner


def _ios_config():
    device = SimpleNamespace(
        udid="UDID",
        team_id="TEAM",
        appium_server_url="http://127.0.0.1:4723",
        appium_auto_start={"enabled": True, "command": ["appium"]},
    )
    return SimpleNamespace(device=device, runner=SimpleNamespace(work_dir="work/ios"))


def _android_config():
    device = SimpleNamespace(
        appium_server_url="http://127.0.0.1:4723",
        adb_path="adb",
        adb_serial=None,
        appium_auto_start={"enabled": True, "command": ["appium"]},
    )
    return SimpleNamespace(device=device, runner=SimpleNamespace(work_dir="work/android"))


# --- iOS -----------------------------------------------------------------


def test_ios_connect_device_starts_appium_when_unreachable(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.ensure_appium_running",
        lambda url, cfg, log_path: (calls.setdefault("ensure_args", (url, cfg, log_path)), AppiumStartResult(status="STARTED"))[1],
    )
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.check_ios_preflight",
        lambda config: SimpleNamespace(ok=True, errors=[]),
    )
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect",
        lambda self: "CONNECTED",
    )

    client = IosPlatformRunner().connect_device(_ios_config(), tmp_path)

    assert client == "CONNECTED"
    assert calls["ensure_args"][2] == tmp_path / "appium.log"


def test_ios_connect_device_raises_with_log_tail_when_appium_fails_to_start(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.ensure_appium_running",
        lambda url, cfg, log_path: AppiumStartResult(status="FAILED", error="boom", log_tail="line1\nline2"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        IosPlatformRunner().connect_device(_ios_config(), tmp_path)


def test_ios_ensure_device_healthy_returns_same_client_when_reachable(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.tcp_reachable", lambda url, timeout=2: True)
    reconnect_called = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device",
        lambda self, config, run_dir=None: reconnect_called.append(1),
    )

    result = IosPlatformRunner().ensure_device_healthy(_ios_config(), "OLD_CLIENT", Path("/tmp"))

    assert result == "OLD_CLIENT"
    assert not reconnect_called


def test_ios_ensure_device_healthy_reconnects_when_unreachable(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.tcp_reachable", lambda url, timeout=2: False)
    closed = []
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.IosPlatformRunner.close_device", lambda self, client: closed.append(client))
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device",
        lambda self, config, run_dir=None: "NEW_CLIENT",
    )

    result = IosPlatformRunner().ensure_device_healthy(_ios_config(), "OLD_CLIENT", Path("/tmp"))

    assert result == "NEW_CLIENT"
    assert closed == ["OLD_CLIENT"]


def test_ios_ensure_device_healthy_tolerates_close_device_raising(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.tcp_reachable", lambda url, timeout=2: False)
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.IosPlatformRunner.close_device",
        lambda self, client: (_ for _ in ()).throw(RuntimeError("already dead")),
    )
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device",
        lambda self, config, run_dir=None: "NEW_CLIENT",
    )

    result = IosPlatformRunner().ensure_device_healthy(_ios_config(), "OLD_CLIENT", Path("/tmp"))

    assert result == "NEW_CLIENT"


# --- Android ---------------------------------------------------------------


def test_android_connect_device_starts_appium_when_unreachable(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.ensure_appium_running",
        lambda url, cfg, log_path: (calls.setdefault("ensure_args", (url, cfg, log_path)), AppiumStartResult(status="STARTED"))[1],
    )

    client = AndroidPlatformRunner().connect_device(_android_config(), tmp_path)

    assert client is not None
    assert calls["ensure_args"][2] == tmp_path / "appium.log"


def test_android_connect_device_raises_with_log_tail_when_appium_fails_to_start(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.ensure_appium_running",
        lambda url, cfg, log_path: AppiumStartResult(status="FAILED", error="boom", log_tail="line1\nline2"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        AndroidPlatformRunner().connect_device(_android_config(), tmp_path)


def test_android_ensure_device_healthy_reconnects_when_unreachable(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.tcp_reachable", lambda url, timeout=2: False)
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.AndroidPlatformRunner.connect_device",
        lambda self, config, run_dir=None: "NEW_CLIENT",
    )

    result = AndroidPlatformRunner().ensure_device_healthy(_android_config(), "OLD_CLIENT", Path("/tmp"))

    assert result == "NEW_CLIENT"


def test_android_ensure_device_healthy_returns_same_client_when_reachable(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.tcp_reachable", lambda url, timeout=2: True)
    reconnect_called = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.AndroidPlatformRunner.connect_device",
        lambda self, config, run_dir=None: reconnect_called.append(1),
    )

    result = AndroidPlatformRunner().ensure_device_healthy(_android_config(), "OLD_CLIENT", Path("/tmp"))

    assert result == "OLD_CLIENT"
    assert not reconnect_called
