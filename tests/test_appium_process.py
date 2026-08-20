from __future__ import annotations

import socket
import sys

import pytest

from mobile_playbook.orchestration.appium_process import ensure_appium_running, tcp_reachable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_tcp_reachable_false_when_nothing_listening():
    assert tcp_reachable(f"http://127.0.0.1:{_free_port()}", timeout=0.5) is False


def test_already_reachable_short_circuits(monkeypatch):
    monkeypatch.setattr("mobile_playbook.orchestration.appium_process.tcp_reachable", lambda url, timeout=2: True)
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
