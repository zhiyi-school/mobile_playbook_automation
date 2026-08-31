from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mobile_playbook.api.routes import artifacts as api_artifacts
from mobile_playbook.api.services import artifacts as artifact_service


class ChunkedUpload:
    def __init__(self, filename: str, chunks: list[bytes]):
        self.filename = filename
        self._chunks = chunks

    async def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size >= 0 and len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


def test_upload_artifact_streams_to_intake(monkeypatch, tmp_path):
    intake = tmp_path / "intake"
    monkeypatch.setitem(artifact_service.INTAKE_DIRS, "ios", intake)
    monkeypatch.setattr(api_artifacts, "max_artifact_upload_bytes", lambda: 10)
    monkeypatch.setattr(artifact_service, "inspect_uploaded_artifact", lambda platform, path: {"ok": path.name})

    response = asyncio.run(api_artifacts.upload_artifact("ios", ChunkedUpload("example_app.ipa", [b"abc", b"def"])))

    assert response == {"path": str(intake / "example_app.ipa"), "metadata": {"ok": "example_app.ipa"}}
    assert (intake / "example_app.ipa").read_bytes() == b"abcdef"


def test_upload_artifact_rejects_oversized_file_without_replacing_existing(monkeypatch, tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    existing = intake / "example_app.ipa"
    existing.write_bytes(b"existing")
    monkeypatch.setitem(artifact_service.INTAKE_DIRS, "ios", intake)
    monkeypatch.setattr(api_artifacts, "max_artifact_upload_bytes", lambda: 5)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api_artifacts.upload_artifact("ios", ChunkedUpload("example_app.ipa", [b"abc", b"def"])))

    assert exc_info.value.status_code == 413
    assert existing.read_bytes() == b"existing"
    assert not (intake / ".example_app.ipa.uploading").exists()
