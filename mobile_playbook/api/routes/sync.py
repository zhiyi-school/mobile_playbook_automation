"""
Route that reports the dashboard sync worker status.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from mobile_playbook.api.services import sync as sync_service
from mobile_playbook.api.schemas import WorkerSyncStatusResponse

router = APIRouter()
logger = logging.getLogger(__name__)


# Return the dashboard sync worker's status.
@router.get("/sync/status", response_model=WorkerSyncStatusResponse)
def get_sync_status() -> dict:
    logger.debug("api: GET /sync/status.")
    return sync_service.worker_status()
