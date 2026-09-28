"""
Routes that list and upload app build artifacts.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from mobile_playbook.api.models import Platform
from mobile_playbook.api.services import artifacts as artifact_service

router = APIRouter()

DEFAULT_MAX_ARTIFACT_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


# Return the upload size limit from MAX_ARTIFACT_UPLOAD_BYTES, or the default.
def max_artifact_upload_bytes() -> int:
    raw = os.environ.get("MAX_ARTIFACT_UPLOAD_BYTES")
    if not raw:
        logger.debug("api: upload limit defaults to %d bytes.", DEFAULT_MAX_ARTIFACT_UPLOAD_BYTES)
        return DEFAULT_MAX_ARTIFACT_UPLOAD_BYTES
    try:
        limit = int(raw)
    except ValueError as exc:
        logger.debug("api: MAX_ARTIFACT_UPLOAD_BYTES=%r is not an integer.", raw, exc_info=True)
        raise HTTPException(status_code=500, detail="Invalid MAX_ARTIFACT_UPLOAD_BYTES") from exc
    if limit <= 0:
        logger.debug("api: MAX_ARTIFACT_UPLOAD_BYTES=%d is not positive.", limit)
        raise HTTPException(status_code=500, detail="MAX_ARTIFACT_UPLOAD_BYTES must be positive")
    logger.debug("api: upload limit is %d bytes.", limit)
    return limit


# Stream an upload to a temporary file within the size limit, then move it into place.
async def write_upload_file(file: UploadFile, dest_path: Path, max_bytes: int) -> int:
    tmp_path = dest_path.with_name(f".{dest_path.name}.uploading")
    bytes_written = 0
    logger.debug("api: streaming upload to %s via %s (limit %d bytes).", dest_path, tmp_path, max_bytes)
    try:
        with tmp_path.open("wb") as handle:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    logger.debug("api: upload to %s exceeded %d bytes; aborting.", dest_path, max_bytes)
                    raise HTTPException(status_code=413, detail="Uploaded artifact is too large")
                handle.write(chunk)
        tmp_path.replace(dest_path)
    except Exception:
        logger.debug(
            "api: upload to %s failed after %d bytes; removing %s.", dest_path, bytes_written, tmp_path, exc_info=True
        )
        tmp_path.unlink(missing_ok=True)
        raise
    logger.debug("api: upload wrote %d bytes to %s.", bytes_written, dest_path)
    return bytes_written


# List the platform's uploaded builds.
@router.get("/artifacts/{platform}")
def list_artifacts(platform: Platform) -> list[dict]:
    logger.debug("api: GET /artifacts/%s.", platform)
    artifacts = artifact_service.list_artifacts(platform)
    logger.debug("api: GET /artifacts/%s -> %d artifact(s).", platform, len(artifacts))
    return artifacts


# Store an uploaded .ipa or .apk in the intake directory and return its metadata.
@router.post("/artifacts/{platform}", status_code=201)
async def upload_artifact(platform: Platform, file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "").name
    expected_suffix = artifact_service.ARTIFACT_SUFFIXES[platform]
    logger.debug("api: POST /artifacts/%s filename=%r.", platform, file.filename)
    if not filename or Path(filename).suffix.lower() != expected_suffix:
        logger.debug("api: upload %r rejected; expected suffix %s.", file.filename, expected_suffix)
        raise HTTPException(
            status_code=400, detail=f"Expected a {expected_suffix} file for platform {platform}, got {file.filename!r}"
        )

    dest_dir = artifact_service.intake_dirs()[platform]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    size = await write_upload_file(file, dest_path, max_artifact_upload_bytes())
    logger.debug("api: POST /artifacts/%s stored %s (%d bytes); inspecting.", platform, dest_path, size)

    return {"path": str(dest_path), "metadata": artifact_service.inspect_uploaded_artifact(platform, dest_path)}
