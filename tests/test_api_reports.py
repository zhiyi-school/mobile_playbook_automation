from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, HTTPException

from mobile_playbook.api.routes import reports as api_reports
from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.reporting.evidence import encode_ref


def _write_dashboard(root, run_id, rows):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "dashboard_results.json").write_text(json.dumps(rows))
    return run_dir


async def _get(app: FastAPI, path: str, params: dict[str, str] | None = None):
    sent: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(params or {}).encode(),
            "headers": [],
            "client": ("test", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    return start["status"], headers, body


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
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
    _write_dashboard(reports_service.REPORTS_ROOT, "run1", [])
    evidence = tmp_path / "work" / "ios" / "run1" / "evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("ok")

    response = api_reports.evidence_file("run1", encode_ref("work", "ios/run1/evidence.txt"))

    assert response.path == str(evidence.resolve())


def test_evidence_file_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
    _write_dashboard(reports_service.REPORTS_ROOT, "run1", [])
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")

    with pytest.raises(HTTPException) as exc_info:
        api_reports.evidence_file("run1", encode_ref("work", "../secret.txt"))

    assert exc_info.value.status_code == 400


def test_evidence_query_validation_and_headers_over_http(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", reports)
    monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
    run_dir = _write_dashboard(reports, "run1", [])
    artifact = run_dir / "critical findings.md"
    artifact.write_text("example")
    app = FastAPI()
    app.include_router(api_reports.router)
    missing_query = asyncio.run(_get(app, "/reports/run1/evidence-file"))
    malformed = asyncio.run(_get(app, "/reports/run1/evidence-file", {"ref": "not-a-ref"}))
    absent = asyncio.run(_get(
        app,
        "/reports/run1/evidence-file",
        {"ref": encode_ref("reports", "run1/missing.txt")},
    ))
    response = asyncio.run(_get(
        app,
        "/reports/run1/evidence-file",
        {"ref": encode_ref("reports", "run1/critical findings.md")},
    ))

    assert missing_query[0] == 422
    assert malformed[0] == 400
    assert absent[0] == 404
    assert response[0] == 200
    assert response[2] == b"example"
    assert response[1]["content-type"] == "text/markdown; charset=utf-8"
    assert response[1]["content-length"] == str(len(b"example"))
    assert 'filename="critical_findings.md"' in response[1]["content-disposition"]


def test_summary_response_model_preserves_extension_fields(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", reports)
    _write_dashboard(
        reports,
        "run1",
        [{"run_timestamp": "run1", "app_id": "app", "test_id": "risk", "future_field": {"value": 1}}],
    )
    app = FastAPI()
    app.include_router(api_reports.router)

    status, _, body = asyncio.run(_get(app, "/reports/run1/summary"))

    assert status == 200
    assert json.loads(body)[0]["future_field"] == {"value": 1}
