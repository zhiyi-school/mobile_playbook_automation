from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from mobile_playbook.api.routes import reports as reports_route
from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.reporting.run_manifest import write_manifest
from mobile_playbook.reporting.sarif_writer import sarif_path
from tests.test_sarif_writer import _row, assert_valid_sarif


@pytest.fixture
def reports_root(monkeypatch, tmp_path):
    monkeypatch.setattr(reports_service, "REPORTS_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    return tmp_path


def _report(
    root: Path,
    timestamp: str = "2026-01-01_00-00-00",
    rows: list[dict[str, Any]] | None = None,
    status: str = "completed",
) -> Path:
    run_dir = root / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps(rows if rows is not None else [_row()]))
    write_manifest(
        run_dir,
        run_timestamp=timestamp,
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status=status,
    )
    return run_dir


def test_the_endpoint_returns_a_valid_sarif_document(reports_root):
    _report(reports_root)

    document = reports_service.read_sarif("2026-01-01_00-00-00")

    assert_valid_sarif(document)


def test_the_response_declares_the_sarif_media_type_and_a_download_name(reports_root):
    _report(reports_root)

    response = reports_route.report_sarif("2026-01-01_00-00-00")

    assert response.status_code == 200
    assert response.media_type == "application/sarif+json"
    assert response.headers["content-type"].startswith("application/sarif+json")
    assert response.headers["content-disposition"] == 'attachment; filename="2026-01-01_00-00-00.sarif"'
    assert json.loads(response.body)["version"] == "2.1.0"


def test_a_first_request_generates_the_file_for_a_run_that_predates_sarif(reports_root):
    run_dir = _report(reports_root)
    assert not sarif_path(run_dir).exists()

    reports_service.read_sarif(run_dir.name)

    assert sarif_path(run_dir).is_file()
    assert json.loads(sarif_path(run_dir).read_text())["version"] == "2.1.0"


def test_a_second_request_serves_the_file_already_on_disk(reports_root):
    run_dir = _report(reports_root)
    reports_service.read_sarif(run_dir.name)
    sarif_path(run_dir).write_text(json.dumps({"version": "2.1.0", "$schema": "x", "runs": ["sentinel"]}))

    assert reports_service.read_sarif(run_dir.name)["runs"] == ["sentinel"]


def test_a_corrupt_sarif_file_is_regenerated_rather_than_served(reports_root):
    run_dir = _report(reports_root)
    sarif_path(run_dir).write_text("{ not json")

    document = reports_service.read_sarif(run_dir.name)

    assert_valid_sarif(document)


def test_a_failed_run_has_no_sarif_to_serve(reports_root):
    _report(reports_root, status="failed")

    with pytest.raises(HTTPException) as excinfo:
        reports_service.read_sarif("2026-01-01_00-00-00")

    assert excinfo.value.status_code == 404


def test_a_run_that_does_not_exist_is_a_404(reports_root):
    with pytest.raises(HTTPException) as excinfo:
        reports_service.read_sarif("2026-01-01_00-00-00")

    assert excinfo.value.status_code == 404


def test_a_run_with_no_results_feed_is_a_404(reports_root):
    run_dir = _report(reports_root)
    (run_dir / "dashboard_results.json").unlink()

    with pytest.raises(HTTPException) as excinfo:
        reports_service.read_sarif(run_dir.name)

    assert excinfo.value.status_code == 404


@pytest.mark.parametrize(
    "run_timestamp",
    ["../secrets", "../../etc/passwd", "..", ".", "a/b", "a\\b", "", "/etc"],
)
def test_path_traversal_is_refused_before_anything_is_read(reports_root, run_timestamp):
    (reports_root.parent / "secrets").mkdir(exist_ok=True)

    with pytest.raises(HTTPException) as excinfo:
        reports_service.read_sarif(run_timestamp)

    assert excinfo.value.status_code in {400, 404}


def test_a_download_name_cannot_smuggle_a_header(reports_root):
    assert reports_route.secure_filename('a"; drop\r\nX-Evil: 1') == "a___drop__X-Evil__1"
    assert reports_route.secure_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert reports_route.secure_filename("") == "run"


def test_the_existing_report_file_endpoint_still_serves_results_sarif(reports_root):
    run_dir = _report(reports_root)
    reports_service.read_sarif(run_dir.name)

    path = reports_service.report_file_path(run_dir.name, "results.sarif")

    assert path == sarif_path(run_dir).resolve()


def test_the_existing_summary_endpoint_is_unchanged(reports_root):
    _report(reports_root)

    rows = reports_service.read_dashboard_results("2026-01-01_00-00-00")

    assert rows[0]["test_id"] == "example_risk"
