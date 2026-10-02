"""
Routes that list reports and serve their summaries, SARIF, files, evidence and history.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.api.downloads import media_type_for, safe_filename
from mobile_playbook.api.schemas import ReportResultResponse
from mobile_playbook.dashboard_sync import run_status

router = APIRouter()

SARIF_MEDIA_TYPE = "application/sarif+json"
logger = logging.getLogger(__name__)


# Sanitize a run timestamp for use in a download filename.
def secure_filename(value: str) -> str:
    return safe_filename(value, "run", basename=False, strip_leading_dots=False)


# List report timestamps, newest first, optionally filtered by manifest status and by comma-separated sync statuses.
@router.get("/reports")
def list_reports(status: str | None = None, sync_status: str | None = None) -> list[str]:
    logger.debug("api: GET /reports status=%r sync_status=%r.", status, sync_status)
    sync_statuses = None
    if sync_status is not None:
        sync_statuses = {value.strip() for value in sync_status.split(",") if value.strip()}
        unknown = sorted(sync_statuses - set(run_status.STATUSES))
        if unknown or not sync_statuses:
            raise HTTPException(
                status_code=422,
                detail=f"sync_status must be a comma-separated list of {', '.join(run_status.STATUSES)}",
            )
    timestamps = reports_service.list_report_timestamps(status, sync_statuses)
    logger.debug("api: GET /reports -> %d report(s).", len(timestamps))
    return timestamps


# Return a report's dashboard results with enriched evidence.
@router.get("/reports/{run_timestamp}/summary", response_model=list[ReportResultResponse])
def report_summary(run_timestamp: str) -> list[dict]:
    logger.debug("api: GET /reports/%s/summary.", run_timestamp)
    results = reports_service.read_dashboard_results(run_timestamp)
    logger.debug("api: GET /reports/%s/summary -> %d result(s).", run_timestamp, len(results))
    return results


# Serve a report's SARIF document as an attachment.
@router.get("/reports/{run_timestamp}/sarif")
def report_sarif(run_timestamp: str) -> JSONResponse:
    logger.debug("api: GET /reports/%s/sarif.", run_timestamp)
    return JSONResponse(
        content=reports_service.read_sarif(run_timestamp),
        media_type=SARIF_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{secure_filename(run_timestamp)}.sarif"'},
    )


# Serve a file from inside a report directory.
@router.get("/reports/{run_timestamp}/files/{file_path:path}")
def report_file(run_timestamp: str, file_path: str) -> FileResponse:
    logger.debug("api: GET /reports/%s/files/%s.", run_timestamp, file_path)
    return FileResponse(reports_service.report_file_path(run_timestamp, file_path))


# Serve one evidence file identified by its run-scoped ref as an attachment.
@router.get("/reports/{run_timestamp}/evidence-file")
def evidence_file(run_timestamp: str, ref: str) -> FileResponse:
    logger.debug("api: GET /reports/%s/evidence-file ref=%r.", run_timestamp, ref)
    resolved = reports_service.safe_evidence_path(run_timestamp, ref)
    logger.debug(
        "api: serving evidence %s as %s (%s).", resolved, resolved.name, media_type_for(resolved.name)
    )
    return FileResponse(
        str(resolved),
        media_type=media_type_for(resolved.name),
        filename=reports_service.safe_download_name(resolved.name),
        content_disposition_type="attachment",
        stat_result=resolved.stat(),
    )


# Return the most recent results for one app and risk across reports.
@router.get("/apps/{app_id}/risks/{risk_id}/history", response_model=list[ReportResultResponse])
def app_risk_history(app_id: str, risk_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict]:
    logger.debug("api: GET /apps/%s/risks/%s/history limit=%d.", app_id, risk_id, limit)
    history = reports_service.app_risk_history(app_id, risk_id, limit)
    logger.debug("api: GET /apps/%s/risks/%s/history -> %d result(s).", app_id, risk_id, len(history))
    return history
