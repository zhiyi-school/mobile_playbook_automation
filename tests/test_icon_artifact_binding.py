from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from mobile_playbook import dashboard_sync
from mobile_playbook.artifact_store import extraction, resolver, store
from mobile_playbook.reporting.run_manifest import artifact_checksums, read_manifest, write_manifest
from tests.icon_helpers import make_ipa, make_png, primary_icon_info


class RecordingStore:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.updates: list[dict] = []
        self.updated_ids: list[str] = []
        self.upserted = False

    def find_application_by_external_id_and_platform(self, external_id, platform):
        return self.rows.get((external_id, platform))

    def find_unlinked_applications(self, name, platform):
        return []

    def update_application(self, application_id, fields):
        self.updates.append(dict(fields))
        self.updated_ids.append(application_id)
        return {"id": application_id, **fields}

    def upsert_application(self, fields):
        self.updates.append(dict(fields))
        self.upserted = True
        return {"id": "new-row", **fields}


@pytest.fixture(autouse=True)
def artifact_store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(store.STORE_DIR_ENV, str(tmp_path / "derived"))
    store._digest_cache.clear()


def _ipa(path, colour):
    return make_ipa(
        path,
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120, colour)},
    )


def _row(app_id="example_app", platform="ios"):
    return {
        "app_id": app_id,
        "platform": platform,
        "app_name": "Example App",
        "package_or_bundle_id": "com.example.placeholder",
    }


def test_the_manifest_records_the_build_each_app_was_tested_from(tmp_path):
    artifact = _ipa(tmp_path / "build.ipa", (255, 0, 0, 255))
    digest = store.artifact_digest(artifact)

    write_manifest(
        tmp_path / "run",
        run_timestamp="<RUN_ID>",
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "<RISK_ID>"}],
        artifacts={"example_app": digest},
        status="completed",
    )

    assert artifact_checksums(read_manifest(tmp_path / "run")) == {"example_app": digest}


def test_a_manifest_without_artifacts_reports_none(tmp_path):
    write_manifest(
        tmp_path / "run",
        run_timestamp="<RUN_ID>",
        platform="ios",
        attempted=[],
        status="completed",
    )
    manifest = read_manifest(tmp_path / "run")
    del manifest["artifacts"]

    assert artifact_checksums(manifest) == {}


def test_sync_links_the_build_the_run_used_not_a_newer_upload(tmp_path, monkeypatch):
    artifact_a = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    artifact_b = _ipa(tmp_path / "b.ipa", (0, 0, 255, 255))
    digest_a = store.artifact_digest(artifact_a)
    digest_b = store.artifact_digest(artifact_b)
    assert digest_a != digest_b

    extraction.extract_icon("ios", artifact_a)

    # The config now points at the newer build, as it would after a fresh upload.
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact_b)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, digest_a)

    written = recording.updates[0]
    assert written["artifact_sha256"] == digest_a
    assert written["icon_ref"] == f"icons/{digest_a}.png"
    assert written["icon_ref"] != f"icons/{digest_b}.png"


def test_sync_keeps_the_existing_icon_when_the_recorded_build_has_no_derived_icon(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    digest = store.artifact_digest(artifact)
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, digest)

    written = recording.updates[0]
    assert "icon_ref" not in written
    assert "artifact_sha256" not in written


def test_a_legacy_run_without_artifacts_falls_back_to_the_resolver(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    digest = store.artifact_digest(artifact)
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, None)

    assert recording.updates[0]["icon_ref"] == f"icons/{digest}.png"


def test_sync_matches_the_application_row_on_platform_as_well_as_id(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "ios-row"}
    dashboard_sync._sync_application(_row(platform="android"), recording, None)

    assert "ios-row" not in recording.updated_ids
    assert recording.upserted
    assert recording.updates[0]["platform"] == "android"


def test_the_run_pins_the_build_it_is_executing(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "build.ipa", (0, 255, 0, 255))
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)
    app_config = SimpleNamespace(id="example_app", name="Example App", artifact=entry["artifact"], bundle_id=None)

    digest = resolver.prepare_icon_for_app_config("ios", app_config)

    assert digest == store.artifact_digest(artifact)
    assert store.icon_path(digest).is_file()


def test_pinning_a_build_never_raises_when_there_is_no_artifact():
    app_config = SimpleNamespace(id="example_app", name="Example App", artifact={}, bundle_id=None)

    assert resolver.prepare_icon_for_app_config("ios", app_config) is None


ARTIFACT_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ICON_REF_PATTERN = re.compile(r"^icons/[0-9a-f]{64}\.png$")
ALLOWED_STATUSES = {"available", "unavailable", "failed"}


def _assert_migration_compatible(fields: dict) -> None:
    """Mirrors the CHECK constraints in 0016_application_icon_refs.sql."""
    if fields.get("artifact_sha256") is not None:
        assert ARTIFACT_SHA_PATTERN.match(fields["artifact_sha256"])
    if fields.get("icon_ref") is not None:
        assert ICON_REF_PATTERN.match(fields["icon_ref"])
    if fields.get("icon_extraction_status") is not None:
        assert fields["icon_extraction_status"] in ALLOWED_STATUSES


def test_written_icon_fields_satisfy_the_migration_constraints(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    digest = store.artifact_digest(artifact)
    extraction.extract_icon("ios", artifact)
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, digest)

    _assert_migration_compatible(recording.updates[0])


def test_an_unavailable_icon_writes_constraint_safe_values(tmp_path, monkeypatch):
    artifact = make_ipa(tmp_path / "plain.ipa")
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, None)

    written = recording.updates[0]
    _assert_migration_compatible(written)
    assert written["icon_ref"] is None
    assert written["icon_extraction_status"] == "unavailable"


def test_no_image_bytes_are_ever_written_to_the_dashboard(tmp_path, monkeypatch):
    artifact = _ipa(tmp_path / "a.ipa", (255, 0, 0, 255))
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(artifact)}}
    monkeypatch.setattr(resolver, "_app_entry", lambda platform, app_id: entry)

    recording = RecordingStore()
    recording.rows[("example_app", "ios")] = {"id": "row-1"}
    dashboard_sync._sync_application(_row(), recording, None)

    for value in recording.updates[0].values():
        assert not isinstance(value, (bytes, bytearray))
        assert not (isinstance(value, str) and value.startswith("data:"))
