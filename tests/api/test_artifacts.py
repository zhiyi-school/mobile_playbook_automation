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
    monkeypatch.setattr(artifact_service, "intake_dirs", lambda: {"ios": intake})
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
    monkeypatch.setattr(artifact_service, "intake_dirs", lambda: {"ios": intake})
    monkeypatch.setattr(api_artifacts, "max_artifact_upload_bytes", lambda: 5)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api_artifacts.upload_artifact("ios", ChunkedUpload("example_app.ipa", [b"abc", b"def"])))

    assert exc_info.value.status_code == 413
    assert existing.read_bytes() == b"existing"
    assert not (intake / ".example_app.ipa.uploading").exists()


def test_android_artifacts_list_each_apks_package_and_version(monkeypatch, tmp_path, apk_metadata):
    intake = tmp_path / "intake"
    intake.mkdir()
    for name, code, version in (("Example_App_1.0.apk", "1", "1.0"), ("Example_App_2.0.apk", "2", "2.0")):
        (intake / name).write_bytes(b"apk")
        apk_metadata[name] = {"package_name": "com.example.app", "version_code": code, "version_name": version, "display_name": "Example App"}
    (intake / "unreadable.apk").write_bytes(b"apk")
    monkeypatch.setattr(artifact_service, "intake_dirs", lambda: {"android": intake})

    listed = artifact_service.list_artifacts("android")

    assert [(item["file"], item["bundle_id"], item["display_name"], item["version"]) for item in listed] == [
        ("Example_App_2.0.apk", "com.example.app", "Example App", "2.0"),
        ("Example_App_1.0.apk", "com.example.app", "Example App", "1.0"),
    ]
