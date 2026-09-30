"""
Routes that start runs, report their status and summaries, and stream run events.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from mobile_playbook.api.run_registry import registry
from mobile_playbook.api.schemas import (
    ReportResultResponse,
    RunCreatedResponse,
    RunRequest,
    RunResponse,
    RunSyncStatusResponse,
)
from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.api.services import runs as runs_service
from mobile_playbook.api.services import sync as sync_service
from mobile_playbook.reporting.run_events import read_events

router = APIRouter()
logger = logging.getLogger(__name__)


# Start an asynchronous run and return its run id.
@router.post("/runs", status_code=202, response_model=RunCreatedResponse)
def create_run(body: RunRequest) -> dict:
    logger.debug("api: POST /runs fields=%s.", sorted(body.model_fields_set))
    return runs_service.create_run(body)


# List runs known to the registry, newest first.
@router.get("/runs", response_model=list[RunResponse])
def list_runs() -> list[dict]:
    logger.debug("api: GET /runs.")
    runs = runs_service.list_runs()
    logger.debug("api: GET /runs -> %d run(s).", len(runs))
    return runs


# Return one run's registry record.
@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str) -> dict:
    logger.debug("api: GET /runs/%s.", run_id)
    return runs_service.get_run(run_id)


# Return a finished run's dashboard results.
@router.get("/runs/{run_id}/summary", response_model=list[ReportResultResponse])
def get_run_summary(run_id: str) -> list[dict]:
    logger.debug("api: GET /runs/%s/summary.", run_id)
    results = runs_service.run_summary(run_id)
    logger.debug("api: GET /runs/%s/summary -> %d result(s).", run_id, len(results))
    return results


# Return a run's dashboard sync status.
@router.get("/runs/{run_id}/sync-status", response_model=RunSyncStatusResponse)
def get_run_sync_status(run_id: str) -> dict:
    logger.debug("api: GET /runs/%s/sync-status.", run_id)
    return sync_service.run_sync_status(run_id)


# Retrigger dashboard sync for a run.
@router.post("/runs/{run_id}/sync", status_code=202, response_model=RunSyncStatusResponse)
def resync_run(run_id: str) -> dict:
    logger.debug("api: POST /runs/%s/sync.", run_id)
    return sync_service.resync_run(run_id)


EVENT_POLL_SECONDS = 0.5


# Stream a run's progress events as server-sent events until it finishes.
@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    logger.debug("api: GET /runs/%s/events.", run_id)
    if registry.get(run_id) is None:
        logger.debug("api: run %s unknown; responding 404.", run_id)
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    run_dir = reports_service.resolved_run_dir(run_id)
    logger.debug("api: streaming events for run %s from %s.", run_id, run_dir)

    # Yield new events, keep-alives and a final done event while the client stays connected.
    async def event_stream():
        since = 0
        while True:
            if await request.is_disconnected():
                logger.debug("api: event stream client for run %s disconnected at offset %s.", run_id, since)
                break
            events, since = read_events(run_dir, since)
            if events:
                logger.debug("api: sending %d event(s) for run %s (next offset %s).", len(events), run_id, since)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
            record = registry.get(run_id)
            if record is not None and record.status != "running" and not events:
                logger.debug("api: event stream for run %s done (status %s).", run_id, record.status)
                yield f"data: {json.dumps({'type': 'done', 'status': record.status, 'error': record.error})}\n\n"
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(EVENT_POLL_SECONDS)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
