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
    summary_json = json.loads((tmp_path / "run1" / "summary.json").read_text())
    assert summary_json["run_timestamp"] == "run1"
    assert "run_started_at" in summary_json
    assert "run_completed_at" in summary_json
    assert "duration_seconds" in summary_json
    normalized_json = json.loads((tmp_path / "run1" / "dashboard_results.json").read_text())
    assert normalized_json[0]["platform"] == "ios"
    assert normalized_json[0]["test_id"] == "ios-feature1-risk1"
    assert (tmp_path / "run1" / "ios" / "app" / "ios-feature1-risk1" / "ipa_static_analysis").exists()
    assert (tmp_path / "run1" / "evidence").exists()
    summary_md = (tmp_path / "run1" / "summary.md").read_text()
    assert "# Run Summary" in summary_md
    assert "- Completed:" in summary_md
    assert "| App | Risk | Test Case | Artifact Source | Status | Notes |" in summary_md
