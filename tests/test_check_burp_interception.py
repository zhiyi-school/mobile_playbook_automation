from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.burp_capture import CaptureObservation
from mobile_playbook.platforms.ios.burp_health import health_record_path, write_health_record
from tools import check_burp_interception


class FakeClient:
    def connect(self):
        return self

    def open_url(self, url):
        return None

    def quit(self):
        return None


def _configure_tool(monkeypatch, capture_path, observation):
    config = SimpleNamespace(
        device=SimpleNamespace(udid="device-1"),
        traffic_interception={"burp": {"proxy_url": "HTTP://LOCALHOST:8080/"}},
    )
    monkeypatch.setattr(check_burp_interception, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(check_burp_interception, "tcp_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr(check_burp_interception, "AppiumDeviceClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(
        check_burp_interception,
        "poll_capture",
        lambda cursor, expected_hosts: (cursor, observation),
    )
    return ["--config", "unused.yaml", "--capture-path", str(capture_path), "--timeout", "0.1"]


def test_pass_writes_normalized_health_record(tmp_path, monkeypatch, capsys):
    capture_path = tmp_path / "capture.jsonl"
    observation = CaptureObservation(
        matched_entries=[{"scheme": "https", "host": "example.com"}],
        valid_entry_count=1,
        https_entry_count=1,
    )
    argv = _configure_tool(monkeypatch, capture_path, observation)

    assert check_burp_interception.main(argv) == 0

    record_path = health_record_path(capture_path)
    record = json.loads(record_path.read_text())
    assert record["proxy_url"] == "http://localhost:8080"
    assert record["capture_path"] == str(capture_path.resolve())
    assert record["valid_entry_count"] == 1
    assert "device-1" not in record_path.read_text()
    assert str(record_path) in capsys.readouterr().out


def test_failure_does_not_write_or_overwrite_health_record(tmp_path, monkeypatch):
    capture_path = tmp_path / "capture.jsonl"
    record_path = health_record_path(capture_path)
    record_path.write_text("existing health")
    observation = CaptureObservation(matched_entries=[], source_unavailable=True)
    argv = _configure_tool(monkeypatch, capture_path, observation)

    assert check_burp_interception.main(argv) == 1
    assert record_path.read_text() == "existing health"


def test_health_record_write_replaces_atomically(tmp_path, monkeypatch):
    capture_path = tmp_path / "capture.jsonl"
    destination = health_record_path(capture_path)
    destination.write_text("old")
    replacements = []
    real_replace = __import__("os").replace

    def observe_replace(source, target):
        assert source.exists()
        assert target == destination
        replacements.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr("mobile_playbook.platforms.ios.burp_health.os.replace", observe_replace)

    write_health_record(
        capture_path=capture_path,
        proxy_url="http://localhost:8080",
        canary_host="example.com",
        valid_entry_count=1,
        device_udid="device-1",
    )

    assert len(replacements) == 1
    assert json.loads(destination.read_text())["schema_version"] == 1
    assert not list(tmp_path.glob(".capture.health.json.*.tmp"))


def test_help_documents_canary_options(capsys):
    with pytest.raises(SystemExit) as exc:
        check_burp_interception.main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--test-url" in output
    assert "--proxy-url" in output
    assert "--capture-path" in output
    assert "--timeout" in output


def test_canary_prepares_before_snapshot_and_restores_before_quit(tmp_path, monkeypatch):
    events = []
    capture_path = tmp_path / "capture.jsonl"
    config = SimpleNamespace(
        device=SimpleNamespace(udid="device-1"),
        traffic_interception={
            "burp": {"proxy_url": "http://localhost:8080"},
            "device_setup": {"mode": "appium_ui"},
        },
    )

    class OrderedClient(FakeClient):
        def open_url(self, url):
            events.append("open")

        def quit(self):
            events.append("quit")

    monkeypatch.setattr(check_burp_interception, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(check_burp_interception, "tcp_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr(check_burp_interception, "AppiumDeviceClient", lambda *args, **kwargs: OrderedClient())
    monkeypatch.setattr(
        check_burp_interception,
        "prepare_traffic_interception",
        lambda *args: events.append("prepare") or {"status": "READY", "restore_required": True},
    )
    monkeypatch.setattr(
        check_burp_interception,
        "snapshot_capture",
        lambda path: events.append("snapshot") or SimpleNamespace(initial_size=0, existed=False),
    )
    monkeypatch.setattr(
        check_burp_interception,
        "poll_capture",
        lambda cursor, hosts: (
            cursor,
            CaptureObservation(
                matched_entries=[{"schema_version": 1, "scheme": "https", "host": "example.com"}],
                valid_entry_count=1,
            ),
        ),
    )
    monkeypatch.setattr(
        check_burp_interception,
        "restore_traffic_interception",
        lambda *args: events.append("restore") or {"status": "RESTORED"},
    )

    result = check_burp_interception.main(
        ["--config", "unused.yaml", "--capture-path", str(capture_path), "--timeout", "0.1"]
    )

    assert result == 0
    assert events == ["prepare", "snapshot", "open", "restore", "quit"]


def test_canary_restores_after_capture_failure(tmp_path, monkeypatch):
    events = []
    capture_path = tmp_path / "capture.jsonl"
    config = SimpleNamespace(
        device=SimpleNamespace(udid="device-1"),
        traffic_interception={
            "burp": {"proxy_url": "http://localhost:8080"},
            "device_setup": {"mode": "appium_ui"},
        },
    )
    monkeypatch.setattr(check_burp_interception, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(check_burp_interception, "tcp_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr(check_burp_interception, "AppiumDeviceClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(
        check_burp_interception,
        "prepare_traffic_interception",
        lambda *args: {"status": "READY", "restore_required": True},
    )
    monkeypatch.setattr(
        check_burp_interception,
        "snapshot_capture",
        lambda path: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )
    monkeypatch.setattr(
        check_burp_interception,
        "restore_traffic_interception",
        lambda *args: events.append("restore") or {"status": "RESTORED"},
    )

    result = check_burp_interception.main(
        ["--config", "unused.yaml", "--capture-path", str(capture_path), "--timeout", "0.1"]
    )

    assert result == 1
    assert events == ["restore"]
