from __future__ import annotations

from fastapi import APIRouter

from mobile_playbook.api.services import sync as sync_service

router = APIRouter()


@router.get("/sync/status")
def get_sync_status() -> dict:
    return sync_service.worker_status()
