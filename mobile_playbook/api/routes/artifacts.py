from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from mobile_playbook.api.models import Platform
from mobile_playbook.api.services import artifacts as artifact_service

router = APIRouter()

DEFAULT_MAX_ARTIFACT_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


def max_artifact_upload_bytes() -> int:
    raw = os.environ.get("MAX_ARTIFACT_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_ARTIFACT_UPLOAD_BYTES
    try:
        limit = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid MAX_ARTIFACT_UPLOAD_BYTES") from exc
    if limit <= 0:
        raise HTTPException(status_code=500, detail="MAX_ARTIFACT_UPLOAD_BYTES must be positive")
    return limit


async def write_upload_file(file: UploadFile, dest_path: Path, max_bytes: int) -> int:
    tmp_path = dest_path.with_name(f".{dest_path.name}.uploading")
    bytes_written = 0
    try:
        with tmp_path.open("wb") as handle:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise HTTPException(status_code=413, detail="Uploaded artifact is too large")
                handle.write(chunk)
        tmp_path.replace(dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return bytes_written


@router.get("/artifacts/{platform}")
def list_artifacts(platform: Platform) -> list[dict]:
    return artifact_service.list_artifacts(platform)


@router.post("/artifacts/{platform}", status_code=201)
async def upload_artifact(platform: Platform, file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "").name
    expected_suffix = artifact_service.ARTIFACT_SUFFIXES[platform]
    if not filename or Path(filename).suffix.lower() != expected_suffix:
        raise HTTPException(
            status_code=400, detail=f"Expected a {expected_suffix} file for platform {platform}, got {file.filename!r}"
        )

    dest_dir = artifact_service.intake_dirs()[platform]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    await write_upload_file(file, dest_path, max_artifact_upload_bytes())

    return {"path": str(dest_path), "metadata": artifact_service.inspect_uploaded_artifact(platform, dest_path)}
