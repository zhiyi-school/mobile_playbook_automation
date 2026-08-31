from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from mobile_playbook.api.job_registry import registry
from mobile_playbook.api.models import RunRequest
from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.api.services import runs as runs_service
from mobile_playbook.api.services import sync as sync_service
from mobile_playbook.reporting.run_events import read_events

router = APIRouter()


@router.post("/runs", status_code=202)
def create_run(body: RunRequest) -> dict:
    return runs_service.create_run(body)


@router.get("/runs")
def list_runs() -> list[dict]:
    return runs_service.list_runs()


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return runs_service.get_run(run_id)


@router.get("/runs/{run_id}/summary")
def get_run_summary(run_id: str) -> list[dict]:
    return runs_service.run_summary(run_id)


@router.get("/runs/{run_id}/sync-status")
def get_run_sync_status(run_id: str) -> dict:
    return sync_service.run_sync_status(run_id)


@router.post("/runs/{run_id}/sync", status_code=202)
def resync_run(run_id: str) -> dict:
    return sync_service.resync_run(run_id)


EVENT_POLL_SECONDS = 0.5


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    if registry.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    run_dir = reports_service.resolved_run_dir(run_id)

    async def event_stream():
        since = 0
        while True:
            if await request.is_disconnected():
                break
            events, since = read_events(run_dir, since)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
            record = registry.get(run_id)
            if record is not None and record.status != "running" and not events:
                yield f"data: {json.dumps({'type': 'done', 'status': record.status, 'error': record.error})}\n\n"
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(EVENT_POLL_SECONDS)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
