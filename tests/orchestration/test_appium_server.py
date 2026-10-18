from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
from functools import partial

import pytest

import mobile_playbook.orchestration.appium_server as appium_process
from mobile_playbook.orchestration.appium_server import AppiumEnvironmentError, ensure_appium_running, stop_appium, tcp_reachable
from mobile_playbook.platforms.android.device.sdk import appium_environment


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_tcp_reachable_false_when_nothing_listening():
    assert tcp_reachable(f"http://127.0.0.1:{_free_port()}", timeout=0.5) is False


def test_already_reachable_short_circuits(monkeypatch):
    monkeypatch.setattr("mobile_playbook.orchestration.appium_server.tcp_reachable", lambda url, timeout=2: True)
    called = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: called.append(1) or pytest.fail("should not spawn"))

    result = ensure_appium_running("http://127.0.0.1:9", {"enabled": True, "command": ["true"]}, __import__("pathlib").Path("/tmp/unused.log"))

    assert result.status == "ALREADY_RUNNING"
    assert not called


def test_disabled_returns_disabled_without_spawning(tmp_path):
    port = _free_port()
    result = ensure_appium_running(f"http://127.0.0.1:{port}", {"enabled": False}, tmp_path / "appium.log")
    assert result.status == "DISABLED"


def test_none_auto_start_config_treated_as_disabled(tmp_path):
    port = _free_port()
    result = ensure_appium_running(f"http://127.0.0.1:{port}", None, tmp_path / "appium.log")
    assert result.status == "DISABLED"


def test_starts_and_polls_until_reachable(tmp_path):
    port = _free_port()
    script = tmp_path / "slow_server.py"
    script.write_text(
        "import socket, time\n"
        "time.sleep(0.3)\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen(50)\n"  # generous backlog: polling connects repeatedly without ever accept()-ing
        "time.sleep(10)\n"
    )
    result = ensure_appium_running(
        f"http://127.0.0.1:{port}",
        {"enabled": True, "command": [sys.executable, str(script)], "wait_seconds": 5, "poll_interval_seconds": 0.1},
        tmp_path / "appium.log",
    )
    try:
        assert result.status == "STARTED"
        assert result.process is not None
        assert tcp_reachable(f"http://127.0.0.1:{port}", timeout=1)
    finally:
        if result.process is not None:
            result.process.terminate()
            result.process.wait(timeout=5)


def test_command_exits_early_reports_failure_and_log_tail(tmp_path):
    port = _free_port()
    result = ensure_appium_running(
        f"http://127.0.0.1:{port}",
        {"enabled": True, "command": [sys.executable, "-c", "import sys; sys.exit(3)"], "wait_seconds": 3, "poll_interval_seconds": 0.1},
        tmp_path / "appium.log",
    )
    assert result.status == "FAILED"
    assert "exited early" in result.error
    assert "code 3" in result.error
    assert (tmp_path / "appium.log").exists()


def test_never_becomes_reachable_times_out(tmp_path):
    port = _free_port()
    result = ensure_appium_running(
        f"http://127.0.0.1:{port}",
        {"enabled": True, "command": [sys.executable, "-c", "import time; time.sleep(2)"], "wait_seconds": 0.3, "poll_interval_seconds": 0.05},
        tmp_path / "appium.log",
    )
    assert result.status == "FAILED"
    assert "did not become reachable" in result.error


def test_log_accumulates_across_multiple_attempts(tmp_path):
    port = _free_port()
    log_path = tmp_path / "appium.log"
    ensure_appium_running(
        f"http://127.0.0.1:{port}",
        {"enabled": True, "command": [sys.executable, "-c", "import sys; sys.exit(1)"], "wait_seconds": 1, "poll_interval_seconds": 0.05},
        log_path,
    )
    ensure_appium_running(
        f"http://127.0.0.1:{port}",
        {"enabled": True, "command": [sys.executable, "-c", "import sys; sys.exit(1)"], "wait_seconds": 1, "poll_interval_seconds": 0.05},
        log_path,
    )
    text = log_path.read_text()
    assert text.count("--- launching") == 2


def test_started_process_uses_a_new_session(monkeypatch, tmp_path):
    reachability = iter([False, True])
    monkeypatch.setattr(appium_process, "tcp_reachable", lambda *args, **kwargs: next(reachability))
    popen_kwargs = {}

    class Process:
        def poll(self):
            return None

    def popen(command, **kwargs):
        popen_kwargs.update(kwargs)
        return Process()

    monkeypatch.setattr(appium_process.subprocess, "Popen", popen)

    result = ensure_appium_running(
        "http://127.0.0.1:4723",
        {"enabled": True, "command": ["appium"], "wait_seconds": 1},
        tmp_path / "appium.log",
    )

    assert result.status == "STARTED"
    assert popen_kwargs["start_new_session"] is True


def _launch_recording_sdk_variables(tmp_path, environment):
    seen = tmp_path / "appium-env.json"
    script = f"import json, os; open({str(seen)!r}, 'w').write(json.dumps({{k: os.environ.get(k) for k in ('ANDROID_HOME', 'ANDROID_SDK_ROOT')}}))"
    result = ensure_appium_running(
        f"http://127.0.0.1:{_free_port()}",
        {"enabled": True, "command": [sys.executable, "-c", script], "wait_seconds": 5, "poll_interval_seconds": 0.05},
        tmp_path / "appium.log",
        environment=environment,
    )
    return result, json.loads(seen.read_text()) if seen.exists() else None


def test_android_sdk_derived_from_adb_reaches_the_appium_process(fake_android_sdk, tmp_path):
    _, seen = _launch_recording_sdk_variables(tmp_path, partial(appium_environment, str(fake_android_sdk / "platform-tools" / "adb")))

    assert seen == {"ANDROID_HOME": str(fake_android_sdk), "ANDROID_SDK_ROOT": str(fake_android_sdk)}


def test_android_sdk_is_derived_from_adb_on_path(fake_android_sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(fake_android_sdk / "platform-tools"))

    _, seen = _launch_recording_sdk_variables(tmp_path, partial(appium_environment, "adb"))

    assert seen == {"ANDROID_HOME": str(fake_android_sdk), "ANDROID_SDK_ROOT": str(fake_android_sdk)}


@pytest.mark.parametrize("name", ["ANDROID_HOME", "ANDROID_SDK_ROOT"])
def test_an_sdk_variable_already_set_reaches_appium_unchanged(fake_android_sdk, monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, "/opt/operator-sdk")

    _, seen = _launch_recording_sdk_variables(tmp_path, partial(appium_environment, str(tmp_path / "no-such-adb")))

    assert seen == {"ANDROID_HOME": None, "ANDROID_SDK_ROOT": None, name: "/opt/operator-sdk"}


@pytest.mark.parametrize("adb_location", ["missing", "outside-platform-tools"])
def test_appium_is_not_launched_when_no_sdk_root_can_be_found(fake_android_sdk, monkeypatch, tmp_path, adb_location):
    adb = tmp_path / "bin" / "adb"
    if adb_location == "outside-platform-tools":
        adb.parent.mkdir()
        adb.write_text("#!/bin/sh\n")
        adb.chmod(0o755)
    monkeypatch.setattr(appium_process.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("should not spawn"))

    result, _ = _launch_recording_sdk_variables(tmp_path, partial(appium_environment, str(adb)))

    assert result.status == "FAILED"
    assert "neither is set" in result.error
    assert str(adb) in result.error
    assert "Export ANDROID_HOME or point device.adb_path at <sdk>/platform-tools/adb" in result.error
    if adb_location == "outside-platform-tools":
        assert "not in an SDK platform-tools directory" in result.error


def test_environment_is_not_prepared_when_appium_is_already_reachable(monkeypatch):
    monkeypatch.setattr(appium_process, "tcp_reachable", lambda url, timeout=2: True)

    result = ensure_appium_running(
        "http://127.0.0.1:4723",
        {"enabled": True},
        __import__("pathlib").Path("/tmp/unused.log"),
        environment=lambda: pytest.fail("should not prepare an environment"),
    )

    assert result.status == "ALREADY_RUNNING"


def test_environment_error_fails_the_start_with_its_message(monkeypatch, tmp_path):
    monkeypatch.setattr(appium_process.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("should not spawn"))

    def unavailable():
        raise AppiumEnvironmentError("no SDK")

    result = ensure_appium_running(f"http://127.0.0.1:{_free_port()}", {"enabled": True}, tmp_path / "appium.log", environment=unavailable)

    assert result.status == "FAILED"
    assert result.error == "Could not start Appium: no SDK"


def test_launch_without_environment_inherits_the_caller_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_HOME", "/opt/inherited-sdk")
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    _, seen = _launch_recording_sdk_variables(tmp_path, None)

    assert seen == {"ANDROID_HOME": "/opt/inherited-sdk", "ANDROID_SDK_ROOT": None}


def test_stop_appium_terminates_backend_owned_process_group(monkeypatch):
    signals = []

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 4

    monkeypatch.setattr(appium_process.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    stop_appium(Process(), timeout=4)

    assert signals == [(123, signal.SIGTERM)]


def test_stop_appium_ignores_already_exited_process(monkeypatch):
    class Process:
        def poll(self):
            return 0

    monkeypatch.setattr(appium_process.os, "killpg", lambda *args: pytest.fail("should not signal"))

    stop_appium(Process())


def test_stop_appium_escalates_to_sigkill_after_timeout(monkeypatch):
    signals = []
    waits = []

    class Process:
        pid = 456

        def poll(self):
            return None

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired(cmd="appium", timeout=timeout)

    monkeypatch.setattr(appium_process.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    stop_appium(Process(), timeout=2)

    assert signals == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert waits == [2, 2]
