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


def _run_with_status(root, timestamp: str, status: str | None):
    run_dir = root / timestamp
    run_dir.mkdir(parents=True)
    if status is not None:
        (run_dir / "run_manifest.json").write_text(json.dumps({"status": status}))
    return run_dir


def test_listing_reports_without_a_status_returns_every_run(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    _run_with_status(root, "2026-01-01_00-00-00", "completed")
    _run_with_status(root, "2026-01-02_00-00-00", "running")
    _run_with_status(root, "2026-01-03_00-00-00", "failed")

    assert reports_service.list_report_timestamps() == [
        "2026-01-03_00-00-00",
        "2026-01-02_00-00-00",
        "2026-01-01_00-00-00",
    ]


def test_listing_reports_by_status_keeps_only_matching_runs(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    _run_with_status(root, "2026-01-01_00-00-00", "completed")
    _run_with_status(root, "2026-01-02_00-00-00", "running")
    _run_with_status(root, "2026-01-03_00-00-00", "completed")

    assert reports_service.list_report_timestamps("completed") == [
        "2026-01-03_00-00-00",
        "2026-01-01_00-00-00",
    ]
    assert reports_service.list_report_timestamps("running") == ["2026-01-02_00-00-00"]
    assert reports_service.list_report_timestamps("cancelled") == []


def test_a_run_predating_manifests_is_treated_as_completed(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    _run_with_status(root, "2026-01-01_00-00-00", None)

    assert reports_service.list_report_timestamps("completed") == ["2026-01-01_00-00-00"]


def test_a_manifest_with_no_status_is_unknown_rather_than_completed(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    run_dir = root / "2026-01-01_00-00-00"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({"run_timestamp": "2026-01-01_00-00-00"}))

    assert reports_service.list_report_timestamps("completed") == []
    assert reports_service.list_report_timestamps("unknown") == ["2026-01-01_00-00-00"]


def test_an_unreadable_manifest_falls_back_to_completed(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    run_dir = root / "2026-01-01_00-00-00"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text("{ not json")

    assert reports_service.list_report_timestamps("completed") == ["2026-01-01_00-00-00"]


def test_the_route_passes_the_status_through(monkeypatch, tmp_path):
    root = tmp_path / "reports"
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
    _run_with_status(root, "2026-01-01_00-00-00", "completed")
    _run_with_status(root, "2026-01-02_00-00-00", "running")

    assert api_reports.list_reports() == ["2026-01-02_00-00-00", "2026-01-01_00-00-00"]
    assert api_reports.list_reports("completed") == ["2026-01-01_00-00-00"]


def test_listing_reports_says_nothing_when_the_directory_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path / "absent")

    assert reports_service.list_report_timestamps() == []
    assert reports_service.list_report_timestamps("completed") == []


def test_the_manifest_records_the_provenance_of_the_run(tmp_path):
    from mobile_playbook.reporting import run_manifest

    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_manifest.write_manifest(
        run_dir,
        run_timestamp="2026-01-01_00-00-00",
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status="completed",
        config_fingerprint="sha256:examplefingerprint",
        keyboard_ipa_sha256="sha256:examplekeyboard",
    )

    provenance = json.loads((run_dir / "run_manifest.json").read_text())["provenance"]
    assert provenance["config_fingerprint"] == "sha256:examplefingerprint"
    assert provenance["keyboard_ipa_sha256"] == "sha256:examplekeyboard"
    assert provenance["code_revision"] == run_manifest.git_revision()


def test_a_manifest_written_without_hashes_still_carries_the_revision(tmp_path):
    from mobile_playbook.reporting import run_manifest

    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_manifest.write_manifest(
        run_dir,
        run_timestamp="2026-01-01_00-00-00",
        platform="ios",
        attempted=[],
        status="completed",
    )

    provenance = json.loads((run_dir / "run_manifest.json").read_text())["provenance"]
    assert provenance["config_fingerprint"] is None
    assert provenance["keyboard_ipa_sha256"] is None


def test_an_unavailable_git_checkout_reports_no_revision(monkeypatch):
    from mobile_playbook.reporting import run_manifest

    monkeypatch.setattr(run_manifest, "_GIT_REVISION_READ", False)
    monkeypatch.setattr(run_manifest, "_GIT_REVISION", None)
    monkeypatch.setattr(
        run_manifest.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git"))
    )

    assert run_manifest.git_revision() is None


def test_the_health_endpoint_names_the_code_it_is_running():
    from mobile_playbook.api import app as api_app
    from mobile_playbook.reporting import run_manifest

    payload = api_app.health()

    assert payload["status"] == "ok"
    assert payload["code_revision"] == run_manifest.git_revision()
    assert payload["started_at"] == api_app._STARTED_AT
