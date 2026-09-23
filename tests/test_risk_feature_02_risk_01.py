from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from mobile_playbook.report import ReportWriter
from mobile_playbook.platforms.ios.burp_capture import CaptureObservation, snapshot_capture
from mobile_playbook.platforms.ios.risks import feature_02_risk_01 as risk_module
from mobile_playbook.platforms.ios.risks.feature_02_risk_01 import Feature02Risk01
from tests.conftest import MockDevice


def _free_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _write_capture_lines_soon(capture_path, entries, delay=0.3):
    def write():
        time.sleep(delay)
        with capture_path.open("a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    threading.Thread(target=write, daemon=True).start()


def test_feature_02_risk_01_requires_proxy_url(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {"ios-feature-02-risk-01": {"enabled": True, "burp": {}}}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

    assert result.final_status == "PROXY_NOT_CONFIGURED"
    assert "proxy_url" in result.errors[0]


def test_feature_02_risk_01_reports_unreachable_proxy(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-02-risk-01": {"enabled": True, "burp": {"proxy_url": "http://127.0.0.1:1"}},
    }
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

    assert result.final_status == "PROXY_UNREACHABLE"


def test_feature_02_risk_01_reports_at_risk_when_decrypted_traffic_matches(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        unmatched = {"schema_version": 1, "scheme": "https", "host": "unrelated.example.com", "method": "GET", "path": "/private"}
        matched = {"schema_version": 1, "scheme": "https", "host": "api.example.com", "method": "GET", "path": "/profile"}
        _write_capture_lines_soon(capture_path, [unmatched, matched])
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(capture_path)},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 2,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }
        writer = ReportWriter(tmp_path / "reports", "run1")

        result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

        assert result.final_status == "RISK_EXISTS"
        assert result.verdict == "At Risk"
        assert result.errors == []
        summary = result.launch_result["capture_summary"]
        assert summary == {
            "new_line_count": 2,
            "valid_entry_count": 2,
            "malformed_entry_count": 0,
            "https_entry_count": 2,
            "non_https_entry_count": 0,
            "matched_count": 1,
            "matched_https_count": 1,
            "unmatched_entry_count": 1,
            "created_during_window": True,
            "source_changed": False,
            "source_unavailable": False,
            "trailing_partial_line": False,
            "expected_hosts": ["api.example.com"],
            "hosts": ["api.example.com"],
            "evidence_path": summary["evidence_path"],
        }
        evidence_path = writer.test_report_dir(app.id, Feature02Risk01.risk_id, "traffic_interception")
        assert json.loads((evidence_path / "burp_capture.json").read_text()) == [matched]
        assert "unrelated.example.com" not in (evidence_path / "report.json").read_text()
    finally:
        server.close()


def test_feature_02_risk_01_only_counts_new_capture_entries_for_this_run(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        # Pre-existing line from an earlier run — must not count toward this run's capture.
        capture_path.write_text(json.dumps({"schema_version": 1, "scheme": "https", "host": "api.example.com", "method": "GET", "path": "/old"}) + "\n")
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(capture_path)},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }
        writer = ReportWriter(tmp_path / "reports", "run1")

        result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

        assert result.final_status == "CAPTURE_PIPELINE_SILENT"
        assert result.verdict == "Inconclusive"
        assert result.errors == []
        assert result.launch_result["capture_summary"]["matched_count"] == 0
    finally:
        server.close()


def test_feature_02_risk_01_reports_inconclusive_when_failed_handshake_produces_no_capture(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(tmp_path / "capture.jsonl")},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }
        writer = ReportWriter(tmp_path / "reports", "run1")

        result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

        assert result.final_status == "CAPTURE_PIPELINE_SILENT"
        assert result.verdict == "Inconclusive"
        assert result.errors == []
    finally:
        server.close()


def test_feature_02_risk_01_ignores_capture_entries_for_other_hosts(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        _write_capture_lines_soon(
            capture_path,
            [{"schema_version": 1, "scheme": "https", "host": "unrelated.example.com", "method": "GET", "path": "/x"}],
        )
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(capture_path)},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 2,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }
        writer = ReportWriter(tmp_path / "reports", "run1")

        result = Feature02Risk01().run(app, global_config, MockDevice(), writer)

        assert result.final_status == "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
        assert result.verdict == "Inconclusive"
        assert result.errors == []
        assert result.launch_result["capture_summary"]["unmatched_entry_count"] == 1
    finally:
        server.close()


def test_feature_02_risk_01_does_not_treat_http_as_tls_interception(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        _write_capture_lines_soon(
            capture_path,
            [{"schema_version": 1, "scheme": "http", "host": "api.example.com", "method": "GET", "path": "/profile"}],
        )
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(capture_path)},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.5,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        result = Feature02Risk01().run(
            app,
            global_config,
            MockDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        summary = result.launch_result["capture_summary"]
        assert result.final_status == "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
        assert result.verdict == "Inconclusive"
        assert result.errors == []
        assert summary["non_https_entry_count"] == 1
        assert summary["matched_https_count"] == 0
    finally:
        server.close()


def test_feature_02_risk_01_rejects_capture_from_outdated_extension(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        _write_capture_lines_soon(
            capture_path,
            [{"schema_version": 1, "host": "api.example.com", "method": "GET", "path": "/profile"}],
        )
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(capture_path)},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.5,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        result = Feature02Risk01().run(
            app,
            global_config,
            MockDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        summary = result.launch_result["capture_summary"]
        assert result.final_status == "CAPTURE_DATA_INVALID"
        assert result.verdict == "Inconclusive"
        assert result.errors == []
        assert summary["malformed_entry_count"] == 1
        assert summary["matched_https_count"] == 0
    finally:
        server.close()


@pytest.mark.parametrize(
    ("observation", "expected_status"),
    [
        (CaptureObservation(matched_entries=[], source_changed=True), "CAPTURE_SOURCE_CHANGED"),
        (CaptureObservation(matched_entries=[], source_unavailable=True), "CAPTURE_SOURCE_UNAVAILABLE"),
    ],
)
def test_feature_02_risk_01_classifies_capture_source_failures(
    monkeypatch,
    global_config,
    tmp_path,
    observation,
    expected_status,
):
    server = _free_tcp_server()
    monkeypatch.setattr(risk_module, "poll_capture", lambda cursor, expected_hosts: (cursor, observation))
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(tmp_path / "capture.jsonl")},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        result = Feature02Risk01().run(
            app,
            global_config,
            MockDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        assert result.final_status == expected_status
        assert result.verdict == "Inconclusive"
        assert result.errors == []
    finally:
        server.close()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"matched_https_count": 1, "source_unavailable": True},
            "RISK_EXISTS",
        ),
        (
            {"source_unavailable": True, "source_changed": True},
            "CAPTURE_SOURCE_UNAVAILABLE",
        ),
        (
            {
                "source_changed": True,
                "new_line_count": 1,
                "malformed_entry_count": 1,
            },
            "CAPTURE_SOURCE_CHANGED",
        ),
        (
            {"new_line_count": 1, "malformed_entry_count": 1},
            "CAPTURE_DATA_INVALID",
        ),
        (
            {"valid_entry_count": 1},
            "TRAFFIC_INTERCEPTION_NOT_OBSERVED",
        ),
        ({}, "CAPTURE_PIPELINE_SILENT"),
    ],
)
def test_capture_status_precedence(overrides, expected):
    summary = {
        "matched_https_count": 0,
        "source_unavailable": False,
        "source_changed": False,
        "valid_entry_count": 0,
        "new_line_count": 0,
        "malformed_entry_count": 0,
        "trailing_partial_line": False,
    }
    summary.update(overrides)

    assert Feature02Risk01()._capture_final_status(summary) == expected


def test_feature_02_risk_01_exercises_configured_accessibility_ids(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(tmp_path / "capture.jsonl")},
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0, "accessibility_ids": ["tab_profile", "btn_refresh"]},
            }
        }
        writer = ReportWriter(tmp_path / "reports", "run1")
        device = MockDevice()

        Feature02Risk01().run(app, global_config, device, writer)

        assert device.taps == ["tab_profile", "btn_refresh"]
    finally:
        server.close()


def test_feature_02_risk_01_snapshots_after_install_and_before_launch(monkeypatch, global_config, tmp_path):
    server = _free_tcp_server()
    events = []

    class OrderedDevice(MockDevice):
        def install_app(self, ipa_path, timeout_ms):
            installed = super().install_app(ipa_path, timeout_ms)
            events.append("install")
            return installed

        def launch_app(self, bundle_id):
            events.append("launch")
            return super().launch_app(bundle_id)

    def observed_snapshot(path):
        events.append("snapshot")
        return snapshot_capture(path)

    monkeypatch.setattr(risk_module, "snapshot_capture", observed_snapshot)
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {"proxy_url": proxy_url, "capture_path": str(tmp_path / "capture.jsonl")},
                "expected_hosts": ["api.example.com"],
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        Feature02Risk01().run(
            app,
            global_config,
            OrderedDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        assert events == ["install", "snapshot", "launch"]
    finally:
        server.close()


@pytest.mark.parametrize(
    ("failure_stage", "expected_status"),
    [
        ("setup", "DEVICE_PROXY_SETUP_FAILED"),
        ("launch", "LAUNCH_FAILED"),
        ("capture", "FAILED"),
    ],
)
def test_feature_02_risk_01_restores_proxy_after_failures(
    monkeypatch,
    global_config,
    tmp_path,
    failure_stage,
    expected_status,
):
    server = _free_tcp_server()
    events = []
    setup_state = {
        "status": "READY",
        "restore_required": True,
        "wifi_ssid": "Test WiFi",
        "original_proxy": {"mode": "Off", "server": None, "port": None, "url": None},
    }

    class FailingDevice(MockDevice):
        def launch_app(self, bundle_id):
            events.append("launch")
            if failure_stage == "launch":
                raise RuntimeError("launch failed")
            return super().launch_app(bundle_id)

    def prepare(*args):
        events.append("setup")
        if failure_stage == "setup":
            raise risk_module.TrafficInterceptionSetupError(
                "DEVICE_PROXY_SETUP_FAILED",
                "setup failed",
                setup_state,
            )
        return setup_state

    def restore(*args):
        events.append("restore")
        return {"status": "RESTORED"}

    monkeypatch.setattr(risk_module, "prepare_traffic_interception", prepare)
    monkeypatch.setattr(risk_module, "restore_traffic_interception", restore)
    if failure_stage == "capture":
        monkeypatch.setattr(
            Feature02Risk01,
            "_collect_capture",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("capture failed")),
        )
    try:
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {
                    "proxy_url": f"http://127.0.0.1:{server.getsockname()[1]}",
                    "capture_path": str(tmp_path / "capture.jsonl"),
                },
                "device_setup": {"mode": "appium_ui"},
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        result = Feature02Risk01().run(
            app,
            global_config,
            FailingDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        assert result.final_status == expected_status
        assert result.verdict == "Inconclusive"
        assert events[-1] == "restore"
        if failure_stage == "setup":
            assert "launch" not in events
    finally:
        server.close()


def test_feature_02_risk_01_reports_restore_failure_as_inconclusive(monkeypatch, global_config, tmp_path):
    server = _free_tcp_server()
    setup_state = {"status": "READY", "restore_required": True}
    monkeypatch.setattr(risk_module, "prepare_traffic_interception", lambda *args: setup_state)
    monkeypatch.setattr(
        risk_module,
        "restore_traffic_interception",
        lambda *args: (_ for _ in ()).throw(
            risk_module.TrafficInterceptionSetupError(
                "DEVICE_PROXY_RESTORE_FAILED",
                "restore failed",
                {"status": "DEVICE_PROXY_RESTORE_FAILED", "error": "restore failed"},
            )
        ),
    )
    try:
        app = global_config.apps[0]
        app.risks = {
            "ios-feature-02-risk-01": {
                "enabled": True,
                "burp": {
                    "proxy_url": f"http://127.0.0.1:{server.getsockname()[1]}",
                    "capture_path": str(tmp_path / "capture.jsonl"),
                },
                "device_setup": {"mode": "appium_ui"},
                "capture_timeout_seconds": 0.01,
                "exercise": {"exercise_wait_seconds": 0, "settle_seconds": 0},
            }
        }

        result = Feature02Risk01().run(
            app,
            global_config,
            MockDevice(),
            ReportWriter(tmp_path / "reports", "run1"),
        )

        assert result.final_status == "DEVICE_PROXY_RESTORE_FAILED"
        assert result.verdict == "Inconclusive"
        assert result.launch_result["device_setup"]["restore"]["error"] == "restore failed"
    finally:
        server.close()
