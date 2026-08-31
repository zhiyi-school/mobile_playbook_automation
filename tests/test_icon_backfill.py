from __future__ import annotations

import pytest

from mobile_playbook import icon_backfill
from mobile_playbook.artifact_store import resolver, store
from tests.icon_helpers import make_ipa, make_png, primary_icon_info


class FakeStore:
    """Keyed by (external_id, platform), the way the applications table actually resolves."""

    def __init__(self, applications: dict[str, dict], platform: str = "ios", unlinked: list[dict] | None = None):
        self.applications = {(external_id, platform): row for external_id, row in applications.items()}
        self.unlinked = unlinked or []
        self.updates: list[tuple[str, dict]] = []
        self.unlinked_queries: list[tuple[str, str]] = []

    def find_application_by_external_id_and_platform(self, external_id: str, platform: str) -> dict | None:
        return self.applications.get((external_id, platform))

    def find_unlinked_applications(self, name: str, platform: str) -> list[dict]:
        self.unlinked_queries.append((name, platform))
        return list(self.unlinked)

    def update_application(self, application_id: str, fields: dict) -> dict:
        self.updates.append((application_id, fields))
        return {"id": application_id, **fields}


@pytest.fixture(autouse=True)
def artifact_store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(store.STORE_DIR_ENV, str(tmp_path / "derived"))
    store._digest_cache.clear()


def _configure(monkeypatch, entry: dict, platform: str = "ios") -> dict:
    monkeypatch.setattr(resolver, "_app_entry", lambda p, app_id: entry if app_id == entry["id"] else None)
    monkeypatch.setattr(icon_backfill, "configured_app_ids", lambda p: [entry["id"]] if p == platform else [])
    return entry


@pytest.fixture
def app_with_icon(tmp_path, monkeypatch):
    ipa = make_ipa(
        tmp_path / "example.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )
    entry = {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(ipa)}}
    return _configure(monkeypatch, entry)


def test_backfill_links_icons_for_applications_that_already_exist(app_with_icon):
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.as_dict() == {
        "scanned": 1,
        "linked": 1,
        "unavailable": 0,
        "skipped": 0,
        "ambiguous": 0,
        "failed": 0,
    }
    application_id, fields = store_double.updates[0]
    assert application_id == "row-1"
    assert fields["icon_extraction_status"] == "available"
    assert fields["icon_ref"] == f"icons/{fields['artifact_sha256']}.png"


def test_backfill_adopts_a_single_unlinked_row_by_name(app_with_icon):
    store_double = FakeStore({}, unlinked=[{"id": "unlinked-row"}])

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.linked == 1
    assert store_double.unlinked_queries == [("Example App", "ios")]
    assert store_double.updates[0][0] == "unlinked-row"


def test_backfill_refuses_to_guess_between_ambiguous_unlinked_rows(app_with_icon):
    store_double = FakeStore({}, unlinked=[{"id": "row-a"}, {"id": "row-b"}])

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.ambiguous == 1
    assert counts.linked == 0
    assert store_double.updates == []


def test_backfill_leaves_apps_the_dashboard_has_never_seen_alone(app_with_icon):
    store_double = FakeStore({})

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.skipped == 1
    assert store_double.updates == []


def test_backfill_records_an_app_whose_build_has_no_icon(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "plain.ipa")
    _configure(
        monkeypatch,
        {"id": "example_app", "name": "Example App", "artifact": {"source": "local_ipa", "ipa": str(ipa)}},
    )
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.unavailable == 1
    assert store_double.updates[0][1]["icon_ref"] is None
    assert store_double.updates[0][1]["icon_extraction_status"] == "unavailable"


def test_backfill_keeps_the_existing_reference_when_the_config_cannot_be_read(monkeypatch):
    monkeypatch.setattr(icon_backfill, "configured_app_ids", lambda platform: ["example_app"])
    monkeypatch.setattr(resolver, "_app_entry", lambda p, a: (_ for _ in ()).throw(RuntimeError("boom")))
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double)

    assert counts.failed == 1
    assert store_double.updates == []


def test_backfill_can_be_limited_to_named_apps(app_with_icon):
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double, app_ids=["other_app"])

    assert counts.scanned == 1
    assert counts.linked == 0


def test_dry_run_reports_without_writing(app_with_icon):
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double, dry_run=True)

    assert counts.linked == 1
    assert store_double.updates == []


def test_force_replaces_a_stale_stored_icon(app_with_icon):
    first = resolver.app_icon("ios", "example_app")
    store.icon_path(first.artifact_id).write_bytes(b"stale bytes that are not a png")

    refreshed = resolver.app_icon("ios", "example_app", force=True)

    assert refreshed.available
    assert store.icon_path(refreshed.artifact_id).read_bytes() != b"stale bytes that are not a png"


def test_backfill_force_rewrites_the_stored_icon(app_with_icon):
    first = resolver.app_icon("ios", "example_app")
    store.icon_path(first.artifact_id).write_bytes(b"stale bytes that are not a png")
    store_double = FakeStore({"example_app": {"id": "row-1"}})

    counts = icon_backfill.backfill_platform("ios", store_double, force=True)

    assert counts.linked == 1
    assert store.icon_path(first.artifact_id).read_bytes() != b"stale bytes that are not a png"


def test_backfill_does_not_touch_a_same_named_app_on_the_other_platform(app_with_icon):
    store_double = FakeStore({"example_app": {"id": "ios-row"}}, platform="ios")

    counts = icon_backfill.backfill_platform("android", store_double, app_ids=["example_app"])

    assert counts.linked == 0
    assert store_double.updates == []


def test_no_icon_refresh_endpoint_is_exposed():
    from mobile_playbook.api.app import app

    icon_paths = [path for path in app.openapi()["paths"] if "icon" in path]
    assert icon_paths == ["/config/{platform}/apps/{app_id}/icon"]
