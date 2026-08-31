from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mobile_playbook.api.routes import reports as api_reports
from mobile_playbook.api.services import reports as reports_service


def _write_dashboard(root, run_id, rows):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "dashboard_results.json").write_text(json.dumps(rows))
    return run_dir


def test_app_risk_history_reads_matching_rows(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    _write_dashboard(
        reports_service.REPORTS_ROOT,
        "2026-01-02_00-00-00",
        [{"run_timestamp": "2026-01-02_00-00-00", "app_id": "app_one", "test_id": "risk_one", "verdict": "At Risk"}],
    )
    _write_dashboard(
        reports_service.REPORTS_ROOT,
        "2026-01-01_00-00-00",
        [{"run_timestamp": "2026-01-01_00-00-00", "app_id": "app_one", "test_id": "risk_two", "verdict": "Reduced Risk"}],
    )

    history = api_reports.app_risk_history("app_one", "risk_one")

    assert [row["run_timestamp"] for row in history] == ["2026-01-02_00-00-00"]
    assert history[0]["verdict"] == "At Risk"


def test_app_risk_history_falls_back_to_report_json_verdict(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    row = {
        "run_timestamp": "2026-01-02_00-00-00",
        "app_id": "app_one",
        "test_id": "risk_one",
        "report_path": "ios/app_one/risk_one/case_one",
    }
    run_dir = _write_dashboard(reports_service.REPORTS_ROOT, row["run_timestamp"], [row])
    report_dir = run_dir / row["report_path"]
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(json.dumps({"verdict": "Reduced Risk"}))

    history = api_reports.app_risk_history("app_one", "risk_one")

    assert history[0]["verdict"] == "Reduced Risk"


def test_evidence_file_allows_work_and_report_roots(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    _write_dashboard(reports_service.REPORTS_ROOT, "run1", [])
    evidence = tmp_path / "work" / "ios" / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("ok")

    response = api_reports.evidence_file("run1", "work/ios/evidence.txt")

    assert response.path == str(evidence.resolve())


def test_evidence_file_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    _write_dashboard(reports_service.REPORTS_ROOT, "run1", [])
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")

    with pytest.raises(HTTPException) as exc_info:
        api_reports.evidence_file("run1", str(secret))

    assert exc_info.value.status_code == 404
