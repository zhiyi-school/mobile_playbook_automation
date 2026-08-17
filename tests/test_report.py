from __future__ import annotations

import json

from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.report import ReportWriter
from mobile_playbook.platforms.ios.results import normalize_ios_result


def test_report_generation(tmp_path):
    writer = ReportWriter(tmp_path, "run1", result_adapter=normalize_ios_result)
    result = RiskRunResult("run1", "start", "end", "app", "App", "bid", "bid.test", "ios-feature1-risk1", "feature1", "ipa_static_analysis", "mobsf_or_package_analysis", "local_ipa", final_status="IPA_ANALYSIS_COMPLETE")
    report_dir = writer.test_report_dir("app", "ios-feature1-risk1", "ipa_static_analysis")
    writer.write_result(result, report_dir)
    writer.write_summary()
    assert (report_dir / "report.json").exists()
    assert not (tmp_path / "run1" / "summary.json").exists()
    normalized_json = json.loads((tmp_path / "run1" / "dashboard_results.json").read_text())
    assert normalized_json[0]["platform"] == "ios"
    assert normalized_json[0]["test_id"] == "ios-feature1-risk1"
    assert normalized_json[0]["report_path"] == "ios/app/ios-feature1-risk1/ipa_static_analysis"
    assert (tmp_path / "run1" / "ios" / "app" / "ios-feature1-risk1" / "ipa_static_analysis").exists()
    assert (tmp_path / "run1" / "evidence").exists()
    summary_md = (tmp_path / "run1" / "summary.md").read_text()
    assert "# Run Summary" in summary_md
    assert "- Completed:" in summary_md
    assert "| App | Risk | Test Case | Artifact Source | Status | Notes | Report |" in summary_md
    assert "ios/app/ios-feature1-risk1/ipa_static_analysis/" in summary_md


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
        "ios-feature5-risk1", "feature5", "collection_server", "keystroke_collection", "local_ipa",
        final_status="INSTALL_FAILED", errors=[raw_error],
    )
    report_dir = writer.test_report_dir("app", "ios-feature5-risk1", "collection_server")
    writer.write_result(result, report_dir)
    writer.write_summary()

    summary_md = (tmp_path / "run1" / "summary.md").read_text()
    assert "Stacktrace" not in summary_md
    assert "The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible" in summary_md

    dashboard = json.loads((tmp_path / "run1" / "dashboard_results.json").read_text())
    record = dashboard[0]
    assert record["summary"] == "The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible"
    assert "Stacktrace" not in record["summary"]
    assert "errors" not in record["raw"]
    assert record["report_path"] == "ios/app/ios-feature5-risk1/collection_server"

    # the full untouched text still lives in logs.txt and report.json
    assert raw_error in (report_dir / "logs.txt").read_text()
    report_json = json.loads((report_dir / "report.json").read_text())
    assert raw_error in report_json["errors"]
