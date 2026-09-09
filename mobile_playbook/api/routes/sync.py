from __future__ import annotations

from fastapi import APIRouter

from mobile_playbook.api.services import sync as sync_service
from mobile_playbook.api.models import WorkerSyncStatusResponse

router = APIRouter()


@router.get("/sync/status", response_model=WorkerSyncStatusResponse)
def get_sync_status() -> dict:
    return sync_service.worker_status()
