from __future__ import annotations

import json

import pytest

from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.report import ReportWriter
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.reporting.run_events import append_event


@pytest.mark.parametrize(
    ("status", "severity", "summary"),
    [
        (
            "CAPTURE_PIPELINE_SILENT",
            "info",
            "No capture activity was observed; this could mean a broken pipeline, no app request, or a blocked TLS handshake.",
        ),
        (
            "CAPTURE_DATA_INVALID",
            "info",
            "New capture data existed but could not be parsed.",
        ),
        (
            "CAPTURE_SOURCE_CHANGED",
            "info",
            "The capture file was replaced or truncated during the test.",
        ),
        (
            "CAPTURE_SOURCE_UNAVAILABLE",
            "info",
            "The capture file could not be read.",
        ),
        (
            "TRAFFIC_INTERCEPTION_NOT_OBSERVED",
            "low",
            "Valid traffic was captured, but none matched the configured hosts.",
        ),
    ],
)
def test_capture_diagnostic_contract(status, severity, summary):
    result = RiskRunResult(
        "run1",
        "start",
        "end",
        "app",
        "App",
        "bid",
        "bid.test",
        "ios-feature-02-risk-01",
        "feature2",
        "traffic_interception",
        "burp_capture",
        "local_ipa",
        final_status=status,
    )

    normalized = normalize_ios_result(result)

    assert normalized.status == status
    assert normalized.verdict == "Inconclusive"
    assert normalized.severity == severity
    assert normalized.summary == summary


def test_intercepted_https_result_contract():
    result = RiskRunResult(
        "run1",
        "start",
        "end",
        "app",
        "App",
        "bid",
        "bid.test",
        "ios-feature-02-risk-01",
        "feature2",
        "traffic_interception",
        "burp_capture",
        "local_ipa",
        final_status="RISK_EXISTS",
        verdict="At Risk",
        launch_result={
            "capture_summary": {
                "matched_count": 1,
                "hosts": ["api.example.com"],
            }
        },
    )

    normalized = normalize_ios_result(result)

    assert normalized.verdict == "At Risk"
    assert normalized.severity == "high"
    assert normalized.summary == (
        "1 decrypted request(s) captured through Burp (api.example.com)"
    )


def test_report_generation(tmp_path):
    writer = ReportWriter(tmp_path, "run1", result_adapter=normalize_ios_result)
    result = RiskRunResult("run1", "start", "end", "app", "App", "bid", "bid.test", "ios-feature-01-risk-01", "feature1", "ipa_static_analysis", "mobsf_or_package_analysis", "local_ipa", final_status="IPA_ANALYSIS_COMPLETE", verdict="At Risk")
    report_dir = writer.test_report_dir("app", "ios-feature-01-risk-01", "ipa_static_analysis")
    writer.write_result(result, report_dir)
    writer.write_summary()
    assert (report_dir / "report.json").exists()
    assert not (tmp_path / "run1" / "summary.json").exists()
    normalized_json = json.loads((tmp_path / "run1" / "dashboard_results.json").read_text())
    assert normalized_json[0]["platform"] == "ios"
    assert normalized_json[0]["test_id"] == "ios-feature-01-risk-01"
    assert normalized_json[0]["report_path"] == "ios/app/ios-feature-01-risk-01/ipa_static_analysis"
    assert normalized_json[0]["verdict"] == "At Risk"
    assert (tmp_path / "run1" / "ios" / "app" / "ios-feature-01-risk-01" / "ipa_static_analysis").exists()
    assert (tmp_path / "run1" / "evidence").exists()
    summary_md = (tmp_path / "run1" / "summary.md").read_text()
    assert "# Run Summary" in summary_md
    assert "- Completed:" in summary_md
    assert "| App | Risk | Test Case | Artifact Source | Status | Notes | Report |" in summary_md
    assert "ios/app/ios-feature-01-risk-01/ipa_static_analysis/" in summary_md
    # the summary table shows the 3-way verdict, not the raw final_status —
    # the raw status is still preserved untouched in report.json
    assert "| At Risk |" in summary_md
    assert "IPA_ANALYSIS_COMPLETE" not in summary_md
    assert json.loads((report_dir / "report.json").read_text())["final_status"] == "IPA_ANALYSIS_COMPLETE"


def test_report_summary_cleans_multiline_errors(tmp_path):
    writer = ReportWriter(tmp_path, "run1", result_adapter=normalize_ios_result)
    raw_error = (
        "Message: The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible\n"
        "Stacktrace:\n"
        "UnknownError: The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible\n"
        "    at getResponseForW3CError (.../errors.js:846:36)"
    )
    result = RiskRunResult(
        "run1", "start", "end", "app", "App", "bid", "bid.test",
        "ios-feature-04-risk-01", "feature-04", "collection_server", "keystroke_collection", "local_ipa",
        final_status="INSTALL_FAILED", errors=[raw_error],
    )
    report_dir = writer.test_report_dir("app", "ios-feature-04-risk-01", "collection_server")
    writer.write_result(result, report_dir)
    writer.write_summary()

    summary_md = (tmp_path / "run1" / "summary.md").read_text()
    assert "Stacktrace" not in summary_md
    assert "The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible" in summary_md
    assert "| Inconclusive |" in summary_md
    assert "INSTALL_FAILED" not in summary_md

    dashboard = json.loads((tmp_path / "run1" / "dashboard_results.json").read_text())
    record = dashboard[0]
    assert record["summary"] == "The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible"
    assert "Stacktrace" not in record["summary"]
    assert "errors" not in record["raw"]
    assert record["report_path"] == "ios/app/ios-feature-04-risk-01/collection_server"

    # the full untouched text still lives in logs.txt and report.json
    assert raw_error in (report_dir / "logs.txt").read_text()
    report_json = json.loads((report_dir / "report.json").read_text())
    assert raw_error in report_json["errors"]


def test_report_summary_lists_each_preflight_warning_once(tmp_path):
    writer = ReportWriter(tmp_path, "run1")
    fields = {
        "code": "BURP_HEALTH_MISSING",
        "risk_id": "ios-feature-02-risk-01",
        "message": "No Burp canary health record exists; run the interception check before the scan.",
        "app_ids": ["app-one"],
    }
    append_event(writer.run_dir, "preflight_warning", **fields)
    append_event(writer.run_dir, "preflight_warning", **fields)

    writer.write_summary()

    summary = (writer.run_dir / "summary.md").read_text()
    assert "## Preflight warnings" in summary
    assert summary.count("BURP_HEALTH_MISSING") == 1
