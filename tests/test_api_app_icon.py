from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from mobile_playbook.api.routes import config as config_routes
from mobile_playbook.api.services import artifacts as artifact_service
from mobile_playbook.artifact_store import resolver, store
from tests.icon_helpers import make_apk, make_ipa, make_png, primary_icon_info


@dataclass
class FakeRequest:
    headers: dict[str, str]


@pytest.fixture(autouse=True)
def artifact_store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(store.STORE_DIR_ENV, str(tmp_path / "derived"))
    store._digest_cache.clear()


@pytest.fixture
def configured_app(tmp_path, monkeypatch):
    ipa = make_ipa(
        tmp_path / "intake" / "example.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )
    entry = {
        "id": "example_app",
        "name": "Example App",
        "bundle_id": "com.example.placeholder",
        "artifact": {"source": "local_ipa", "ipa": str(ipa)},
    }
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry if app_id == entry["id"] else None)
    return entry


def test_icon_endpoint_serves_a_png_with_cache_headers(configured_app):
    response = config_routes.get_app_icon("ios", "example_app", FakeRequest({}))

    assert isinstance(response, FileResponse)
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == artifact_service.ICON_CACHE_CONTROL
    assert response.headers["etag"].strip('"') == resolver.app_icon("ios", "example_app").artifact_id


def test_icon_endpoint_serves_the_stored_file_not_the_artifact(configured_app):
    response = config_routes.get_app_icon("ios", "example_app", FakeRequest({}))

    assert str(response.path).startswith(str(store.store_root()))
    assert str(response.path).endswith(".png")


def test_icon_endpoint_answers_a_matching_etag_with_304(configured_app):
    first = config_routes.get_app_icon("ios", "example_app", FakeRequest({}))
    etag = first.headers["etag"]

    second = config_routes.get_app_icon("ios", "example_app", FakeRequest({"if-none-match": etag}))

    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_icon_endpoint_404s_for_an_unknown_app(monkeypatch):
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: None)

    with pytest.raises(HTTPException) as exc_info:
        config_routes.get_app_icon("ios", "no_such_app", FakeRequest({}))

    assert exc_info.value.status_code == 404


def test_icon_endpoint_404s_when_the_app_has_no_readable_icon(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "plain.ipa")
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(ipa)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    with pytest.raises(HTTPException) as exc_info:
        config_routes.get_app_icon("ios", "example_app", FakeRequest({}))

    assert exc_info.value.status_code == 404


def test_icon_endpoint_detail_names_no_path_or_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: None)

    with pytest.raises(HTTPException) as exc_info:
        config_routes.get_app_icon("ios", "example_app", FakeRequest({}))

    detail = str(exc_info.value.detail)
    for fragment in ("intake/", "derived/", ".ipa", ".apk", str(tmp_path)):
        assert fragment not in detail


def test_ios_intake_source_resolves_through_the_existing_intake_scan(tmp_path, monkeypatch):
    intake = tmp_path / "intake" / "ios" / "ipas"
    make_ipa(
        intake / "example.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
        bundle_id="com.example.placeholder",
    )
    entry = {
        "id": "example_app",
        "name": "Example App",
        "bundle_id": "com.example.placeholder",
        "artifact": {"source": "intake_ipa", "intake_dir": str(intake)},
    }
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    assert resolver.app_icon("ios", "example_app").available


def test_installed_app_reference_has_no_artifact_to_read(monkeypatch):
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "installed_app_reference"}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    result = resolver.app_icon("ios", "example_app")

    assert result.status == "unavailable"
    assert result.reason == "no_artifact_available"


def test_android_uses_an_apk_the_workflow_already_acquired(tmp_path, monkeypatch):
    acquired = tmp_path / "work" / "com_example_placeholder" / "original" / "base.apk"
    make_apk(acquired, {"res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192)})
    monkeypatch.setattr(resolver, "ANDROID_WORKFLOW_APK_DIR", tmp_path / "work")
    entry = {"id": "example_app", "name": "Example App", "package_name": "com.example.placeholder"}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    assert resolver.app_icon("android", "example_app").width == 192


def test_android_package_name_alone_reads_no_device(monkeypatch, tmp_path):
    monkeypatch.setattr(resolver, "ANDROID_WORKFLOW_APK_DIR", tmp_path / "absent")
    monkeypatch.setattr(resolver, "ANDROID_INTAKE_DIR", tmp_path / "absent")
    entry = {"id": "example_app", "name": "Example App", "package_name": "com.example.placeholder"}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    assert resolver.app_icon("android", "example_app").reason == "no_artifact_available"


def test_icon_reference_for_the_dashboard_is_a_logical_ref(configured_app):
    reference = resolver.app_icon_reference("ios", "example_app")

    assert reference["icon_extraction_status"] == "available"
    assert reference["icon_ref"] == f"icons/{reference['artifact_sha256']}.png"
    assert not reference["icon_ref"].startswith("/")


def test_icon_reference_survives_an_unreadable_config(monkeypatch):
    def explode(platform, app_id):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(resolver, "_app_entry", explode)

    assert resolver.app_icon_reference("ios", "example_app") == {
        "artifact_sha256": None,
        "icon_ref": None,
        "icon_extraction_status": "failed",
    }


def test_uploaded_artifact_inspection_carries_the_icon_reference(tmp_path):
    ipa = make_ipa(
        tmp_path / "example.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )

    metadata = artifact_service.inspect_uploaded_artifact("ios", ipa)

    assert metadata["bundle_id"] == "com.example.placeholder"
    assert metadata["sha256"] == metadata["artifact_id"]
    assert metadata["icon"]["available"] is True
    assert metadata["icon"]["storage_ref"] == f"icons/{metadata['artifact_id']}.png"


def test_uploaded_artifact_inspection_still_reports_a_broken_file(tmp_path):
    broken = tmp_path / "example.ipa"
    broken.write_bytes(b"not a zip")

    assert "error" in artifact_service.inspect_uploaded_artifact("ios", broken)
