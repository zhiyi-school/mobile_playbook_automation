from __future__ import annotations

import json
import socket
import threading
import time

from mobile_playbook.report import ReportWriter
from mobile_playbook.platforms.ios.risks.feature_02_risk_01 import Feature02Risk01
from tests.conftest import MockDevice


def _free_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _write_capture_line_soon(capture_path, entry, delay=0.3):
    def write():
        time.sleep(delay)
        with capture_path.open("a") as handle:
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
        _write_capture_line_soon(capture_path, {"host": "api.example.com", "method": "GET", "path": "/profile"})
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
        assert result.launch_result["capture_summary"]["matched_count"] == 1
    finally:
        server.close()


def test_feature_02_risk_01_only_counts_new_capture_entries_for_this_run(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        # Pre-existing line from an earlier run — must not count toward this run's capture.
        capture_path.write_text(json.dumps({"host": "api.example.com", "method": "GET", "path": "/old"}) + "\n")
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

        assert result.final_status == "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
        assert result.verdict == "Inconclusive"
        assert result.launch_result["capture_summary"]["matched_count"] == 0
    finally:
        server.close()


def test_feature_02_risk_01_reports_inconclusive_when_nothing_captured(global_config, tmp_path):
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

        assert result.final_status == "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
        assert result.verdict == "Inconclusive"
    finally:
        server.close()


def test_feature_02_risk_01_ignores_capture_entries_for_other_hosts(global_config, tmp_path):
    server = _free_tcp_server()
    try:
        proxy_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        capture_path = tmp_path / "capture.jsonl"
        _write_capture_line_soon(capture_path, {"host": "unrelated.example.com", "method": "GET", "path": "/x"})
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
    finally:
        server.close()


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
