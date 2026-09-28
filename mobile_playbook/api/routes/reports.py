from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.api.downloads import safe_filename
from mobile_playbook.api.models import ReportResultResponse

router = APIRouter()

SARIF_MEDIA_TYPE = "application/sarif+json"
logger = logging.getLogger(__name__)


def secure_filename(value: str) -> str:
    return safe_filename(value, "run", basename=False, strip_leading_dots=False)
@router.get("/reports")
def list_reports(status: str | None = None) -> list[str]:
    logger.debug("api: GET /reports status=%r.", status)
    timestamps = reports_service.list_report_timestamps(status)
    logger.debug("api: GET /reports -> %d report(s).", len(timestamps))
    return timestamps


@router.get("/reports/{run_timestamp}/summary", response_model=list[ReportResultResponse])
def report_summary(run_timestamp: str) -> list[dict]:
    logger.debug("api: GET /reports/%s/summary.", run_timestamp)
    results = reports_service.read_dashboard_results(run_timestamp)
    logger.debug("api: GET /reports/%s/summary -> %d result(s).", run_timestamp, len(results))
    return results


@router.get("/reports/{run_timestamp}/sarif")
def report_sarif(run_timestamp: str) -> JSONResponse:
    logger.debug("api: GET /reports/%s/sarif.", run_timestamp)
    return JSONResponse(
        content=reports_service.read_sarif(run_timestamp),
        media_type=SARIF_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{secure_filename(run_timestamp)}.sarif"'},
    )


@router.get("/reports/{run_timestamp}/files/{file_path:path}")
def report_file(run_timestamp: str, file_path: str) -> FileResponse:
    logger.debug("api: GET /reports/%s/files/%s.", run_timestamp, file_path)
    return FileResponse(reports_service.report_file_path(run_timestamp, file_path))


@router.get("/reports/{run_timestamp}/evidence-file")
def evidence_file(run_timestamp: str, ref: str) -> FileResponse:
    logger.debug("api: GET /reports/%s/evidence-file ref=%r.", run_timestamp, ref)
    resolved = reports_service.safe_evidence_path(run_timestamp, ref)
    logger.debug(
        "api: serving evidence %s as %s (%s).", resolved, resolved.name, reports_service.media_type_for(resolved.name)
    )
    return FileResponse(
        str(resolved),
        media_type=reports_service.media_type_for(resolved.name),
        filename=reports_service.safe_download_name(resolved.name),
        content_disposition_type="attachment",
        stat_result=resolved.stat(),
    )


@router.get("/apps/{app_id}/risks/{risk_id}/history", response_model=list[ReportResultResponse])
def app_risk_history(app_id: str, risk_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict]:
    logger.debug("api: GET /apps/%s/risks/%s/history limit=%d.", app_id, risk_id, limit)
    history = reports_service.app_risk_history(app_id, risk_id, limit)
    logger.debug("api: GET /apps/%s/risks/%s/history -> %d result(s).", app_id, risk_id, len(history))
    return history
