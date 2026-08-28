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


def test_ios_connect_device_unlocks_after_connecting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.ensure_appium_running",
        lambda url, cfg, log_path: AppiumStartResult(status="ALREADY_RUNNING"),
    )
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.check_ios_preflight",
        lambda config: SimpleNamespace(ok=True, errors=[]),
    )
    fake_client = SimpleNamespace(unlock=lambda: {"was_locked": True})
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect",
        lambda self: fake_client,
    )

    client = IosPlatformRunner().connect_device(_ios_config(), tmp_path)

    assert client is fake_client
    events = (tmp_path / "events.jsonl").read_text()
    assert "device_unlocked" in events


def test_ios_connect_device_tolerates_unlock_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.ensure_appium_running",
        lambda url, cfg, log_path: AppiumStartResult(status="ALREADY_RUNNING"),
    )
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.check_ios_preflight",
        lambda config: SimpleNamespace(ok=True, errors=[]),
    )

    def broken_unlock():
        raise RuntimeError("no session")

    fake_client = SimpleNamespace(unlock=broken_unlock)
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect",
        lambda self: fake_client,
    )

    client = IosPlatformRunner().connect_device(_ios_config(), tmp_path)

    assert client is fake_client


def test_ios_ensure_device_healthy_unlocks_when_reachable(monkeypatch, tmp_path):
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.tcp_reachable", lambda url, timeout=2: True)
    unlock_calls = []
    fake_client = SimpleNamespace(unlock=lambda: unlock_calls.append(1) or {"was_locked": False})

    result = IosPlatformRunner().ensure_device_healthy(_ios_config(), fake_client, tmp_path)

    assert result is fake_client
    assert unlock_calls == [1]


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


class FakeReportWriter:
    def __init__(self):
        self.written = []
        self.run_timestamp = "2026-01-01_00-00-00"

    def test_report_dir(self, app_id, test_id, case_id):
        return Path("/tmp") / app_id / test_id / case_id

    def write_result(self, result, report_dir):
        self.written.append((result, report_dir))


def _fake_app():
    return SimpleNamespace(id="app", name="App", bundle_id="com.example.app", test_bundle_id="com.example.app.wda", artifact={})


def _fake_risk(run_side_effects):
    calls = []

    def run(app, config, device_client, report_writer):
        calls.append(1)
        effect = run_side_effects[len(calls) - 1]
        if effect is not None:
            raise effect

    return SimpleNamespace(run=run, test_case_id="the_case", calls=calls)


def test_ios_run_test_retries_once_after_unlocking_a_locked_device(monkeypatch):
    risk = _fake_risk([RuntimeError("no such element"), None])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: risk)
    device_client = SimpleNamespace(unlock=lambda: {"was_locked": True})
    report_writer = FakeReportWriter()

    IosPlatformRunner().run_test(_fake_app(), "risk_id", object(), device_client, report_writer)

    assert len(risk.calls) == 2
    assert report_writer.written == []  # the retry succeeded, so nothing gets recorded as failed


def test_ios_run_test_does_not_retry_when_device_was_not_locked(monkeypatch):
    risk = _fake_risk([RuntimeError("boom"), None])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: risk)
    device_client = SimpleNamespace(unlock=lambda: {"was_locked": False})
    report_writer = FakeReportWriter()

    IosPlatformRunner().run_test(_fake_app(), "risk_id", object(), device_client, report_writer)

    assert len(risk.calls) == 1  # no retry attempted
    assert len(report_writer.written) == 1
    assert report_writer.written[0][0].errors == ["boom"]


def test_ios_run_test_records_the_retrys_own_failure_if_it_also_fails(monkeypatch):
    risk = _fake_risk([RuntimeError("first failure"), RuntimeError("second failure")])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: risk)
    device_client = SimpleNamespace(unlock=lambda: {"was_locked": True})
    report_writer = FakeReportWriter()

    IosPlatformRunner().run_test(_fake_app(), "risk_id", object(), device_client, report_writer)

    assert len(risk.calls) == 2
    assert len(report_writer.written) == 1
    assert report_writer.written[0][0].errors == ["second failure"]


def test_ios_run_test_does_not_retry_when_unlock_itself_raises(monkeypatch):
    risk = _fake_risk([RuntimeError("boom")])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: risk)

    def broken_unlock():
        raise RuntimeError("no session")

    device_client = SimpleNamespace(unlock=broken_unlock)
    report_writer = FakeReportWriter()

    IosPlatformRunner().run_test(_fake_app(), "risk_id", object(), device_client, report_writer)

    assert len(risk.calls) == 1
    assert len(report_writer.written) == 1
    assert report_writer.written[0][0].errors == ["boom"]


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
