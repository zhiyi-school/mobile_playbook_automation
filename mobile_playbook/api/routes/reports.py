from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.api.downloads import safe_filename
from mobile_playbook.api.models import ReportResultResponse

router = APIRouter()

SARIF_MEDIA_TYPE = "application/sarif+json"


def secure_filename(value: str) -> str:
    return safe_filename(value, "run", basename=False, strip_leading_dots=False)
@router.get("/reports")
def list_reports() -> list[str]:
    return reports_service.list_report_timestamps()


@router.get("/reports/{run_timestamp}/summary", response_model=list[ReportResultResponse])
def report_summary(run_timestamp: str) -> list[dict]:
    return reports_service.read_dashboard_results(run_timestamp)


@router.get("/reports/{run_timestamp}/sarif")
def report_sarif(run_timestamp: str) -> JSONResponse:
    return JSONResponse(
        content=reports_service.read_sarif(run_timestamp),
        media_type=SARIF_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{secure_filename(run_timestamp)}.sarif"'},
    )


@router.get("/reports/{run_timestamp}/files/{file_path:path}")
def report_file(run_timestamp: str, file_path: str) -> FileResponse:
    return FileResponse(reports_service.report_file_path(run_timestamp, file_path))


@router.get("/reports/{run_timestamp}/evidence-file")
def evidence_file(run_timestamp: str, ref: str) -> FileResponse:
    resolved = reports_service.safe_evidence_path(run_timestamp, ref)
    return FileResponse(
        str(resolved),
        media_type=reports_service.media_type_for(resolved.name),
        filename=reports_service.safe_download_name(resolved.name),
        content_disposition_type="attachment",
        stat_result=resolved.stat(),
    )


@router.get("/apps/{app_id}/risks/{risk_id}/history", response_model=list[ReportResultResponse])
def app_risk_history(app_id: str, risk_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict]:
    return reports_service.app_risk_history(app_id, risk_id, limit)
