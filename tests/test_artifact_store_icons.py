from __future__ import annotations

import zipfile

import pytest

from mobile_playbook.artifact_store import extraction, png, store
from tests.icon_helpers import make_apk, make_cgbi_png, make_ipa, make_png, primary_icon_info


@pytest.fixture(autouse=True)
def artifact_store_dir(tmp_path, monkeypatch):
    root = tmp_path / "derived"
    monkeypatch.setenv(store.STORE_DIR_ENV, str(root))
    store._digest_cache.clear()
    return root


def test_ios_icon_named_by_cfbundleiconfiles_is_extracted(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )

    result = extraction.extract_icon("ios", ipa)

    assert result.available
    assert (result.width, result.height) == (120, 120)
    assert result.storage_ref == f"icons/{result.artifact_id}.png"
    assert store.icon_path(result.artifact_id).is_file()


def test_ios_crushed_icon_is_normalized_before_it_is_stored(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_cgbi_png(60, 60)},
    )

    result = extraction.extract_icon("ios", ipa)

    stored = store.icon_path(result.artifact_id).read_bytes()
    assert result.available
    assert png.looks_like_png(stored)
    assert b"CgBI" not in stored


def test_ios_picks_the_largest_readable_icon(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60", "AppIcon76x76"),
        files={
            "AppIcon60x60@2x.png": make_png(120, 120),
            "AppIcon76x76@2x~ipad.png": make_png(152, 152),
        },
    )

    assert extraction.extract_icon("ios", ipa).width == 152


def test_ios_falls_back_to_conventionally_named_root_icons(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa", files={"AppIcon76x76@2x.png": make_png(152, 152)})

    result = extraction.extract_icon("ios", ipa)

    assert result.available
    assert result.width == 152


def test_ios_without_any_icon_reports_unavailable(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa")

    result = extraction.extract_icon("ios", ipa)

    assert not result.available
    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "no_icon_in_bundle"


def test_ios_asset_catalog_without_tooling_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: False)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"asset catalog bytes"})

    result = extraction.extract_icon("ios", ipa)

    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_tool_unavailable"


def test_ios_malformed_asset_catalog_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(extraction.asset_catalog, "read_catalog", lambda path: None)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"not really a catalog"})

    result = extraction.extract_icon("ios", ipa)

    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_unreadable"


def test_ios_asset_catalog_identifies_the_primary_icon_but_cannot_export_it(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(
        extraction.asset_catalog,
        "read_catalog",
        lambda path: [
            {"Name": "AppIcon", "RenditionName": "a.png", "Idiom": "phone", "Scale": 3,
             "PixelWidth": 180, "PixelHeight": 180},
            {"Name": "AppIcon", "RenditionName": "b.png", "Idiom": "marketing", "Scale": 1,
             "PixelWidth": 1024, "PixelHeight": 1024},
        ],
    )
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra={"CFBundleIconName": "AppIcon"},
        files={"Assets.car": b"catalog bytes"},
    )

    result = extraction.extract_icon("ios", ipa)

    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_no_extractor"


def test_ios_asset_catalog_without_an_app_icon_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(
        extraction.asset_catalog,
        "read_catalog",
        lambda path: [{"Name": "SomeOtherAsset", "PixelWidth": 40, "PixelHeight": 40, "Idiom": "phone"}],
    )
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"catalog bytes"})

    result = extraction.extract_icon("ios", ipa)

    assert result.reason == "asset_catalog_no_icon"


def test_a_loose_png_still_wins_over_the_asset_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: True)
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120), "Assets.car": b"catalog bytes"},
    )

    assert extraction.extract_icon("ios", ipa).available


def test_ios_unsupported_icon_format_is_not_served(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": b"<svg>not a png</svg>"},
    )

    result = extraction.extract_icon("ios", ipa)

    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "unsupported_icon_format"


def test_oversized_icon_member_is_skipped(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(16, 16) + b"\x00" * png.MAX_SOURCE_BYTES},
    )

    assert not extraction.extract_icon("ios", ipa).available


def test_malformed_archive_fails_without_raising(tmp_path):
    broken = tmp_path / "app.ipa"
    broken.write_bytes(b"this is not a zip archive")

    result = extraction.extract_icon("ios", broken)

    assert result.status == extraction.STATUS_FAILED
    assert result.reason == "artifact_unreadable"


def test_missing_artifact_fails_without_raising(tmp_path):
    result = extraction.extract_icon("ios", tmp_path / "absent.ipa")

    assert result.status == extraction.STATUS_FAILED
    assert result.reason == "artifact_unreadable"


def test_traversing_zip_entry_is_never_read(tmp_path):
    ipa = tmp_path / "app.ipa"
    make_ipa(ipa, info_extra=primary_icon_info("AppIcon60x60"))
    with zipfile.ZipFile(ipa, "a") as zf:
        zf.writestr("Payload/Example.app/../../../escape.png", make_png(64, 64))

    result = extraction.extract_icon("ios", ipa)

    assert not result.available
    assert not (tmp_path.parent / "escape.png").exists()


def test_a_cached_icon_still_reports_its_dimensions(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )
    first = extraction.extract_icon("ios", ipa)

    cached = extraction.extract_icon("ios", ipa)

    assert (cached.width, cached.height) == (first.width, first.height) == (120, 120)
    assert cached.storage_ref == first.storage_ref


def test_identical_artifacts_share_one_id_and_one_stored_icon(tmp_path):
    files = {"AppIcon60x60@2x.png": make_png(120, 120)}
    info = primary_icon_info("AppIcon60x60")
    first = extraction.extract_icon("ios", make_ipa(tmp_path / "a.ipa", info_extra=info, files=files))
    second = extraction.extract_icon("ios", make_ipa(tmp_path / "b.ipa", info_extra=info, files=files))

    assert first.artifact_id == second.artifact_id
    assert first.storage_ref == second.storage_ref
    assert len(list((store.store_root() / "icons").glob("*.png"))) == 1


def test_android_icon_is_taken_from_the_declared_resource(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk", {"res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192)})
    monkeypatch.setattr(
        extraction, "_android_declared_icons", lambda path: ["res/mipmap-xxxhdpi-v4/ic_launcher.png"]
    )

    result = extraction.extract_icon("android", apk)

    assert result.available
    assert result.width == 192


def test_android_falls_back_to_conventional_resources_without_sdk_tools(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            "res/mipmap-mdpi-v4/ic_launcher.png": make_png(48, 48),
            "res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192),
        },
    )
    monkeypatch.setattr(extraction, "_android_declared_icons", lambda path: [])

    result = extraction.extract_icon("android", apk)

    assert result.available
    assert result.width == 192


def test_android_adaptive_icon_uses_its_raster_foreground(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            "res/mipmap-anydpi-v26/ic_launcher.xml": b"<adaptive-icon/>",
            "res/drawable-xxxhdpi-v4/ic_launcher_foreground.png": make_png(432, 432),
        },
    )
    monkeypatch.setattr(
        extraction, "_android_declared_icons", lambda path: ["res/mipmap-anydpi-v26/ic_launcher.xml"]
    )

    result = extraction.extract_icon("android", apk)

    assert result.available
    assert result.width == 432


def test_android_vector_only_adaptive_icon_reports_unavailable(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            "res/mipmap-anydpi-v26/ic_launcher.xml": b"<adaptive-icon/>",
            "res/drawable/ic_launcher_foreground.xml": b"<vector/>",
        },
    )
    monkeypatch.setattr(
        extraction, "_android_declared_icons", lambda path: ["res/mipmap-anydpi-v26/ic_launcher.xml"]
    )

    result = extraction.extract_icon("android", apk)

    assert result.status == extraction.STATUS_UNAVAILABLE
    assert result.reason == "adaptive_icon_vector_only"


def test_android_resolves_a_mipmap_reference_from_apkanalyzer(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk", {"res/mipmap-xhdpi-v4/custom_icon.png": make_png(96, 96)})
    monkeypatch.setattr(extraction, "_android_declared_icons", lambda path: ["@mipmap/custom_icon"])

    result = extraction.extract_icon("android", apk)

    assert result.available
    assert result.width == 96


def test_android_without_any_icon_reports_unavailable(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk")
    monkeypatch.setattr(extraction, "_android_declared_icons", lambda path: [])

    assert extraction.extract_icon("android", apk).reason == "no_icon_in_archive"


def test_describe_artifact_keeps_identity_when_icon_extraction_fails(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa", bundle_id="com.example.placeholder")

    metadata = extraction.describe_artifact("ios", ipa)

    assert metadata["bundle_id"] == "com.example.placeholder"
    assert metadata["display_name"] == "Example App"
    assert metadata["version"] == "1.0.0"
    assert metadata["sha256"] == metadata["artifact_id"]
    assert metadata["icon"]["available"] is False
    assert metadata["icon"]["mime_type"] is None
    assert store.read_metadata(metadata["artifact_id"]) == metadata


def test_describe_artifact_reports_the_icon_it_stored(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )

    icon = extraction.describe_artifact("ios", ipa)["icon"]

    assert icon == {
        "available": True,
        "status": "available",
        "reason": None,
        "storage_ref": icon["storage_ref"],
        "mime_type": "image/png",
        "width": 120,
        "height": 120,
    }


def test_icon_ref_resolution_rejects_references_we_did_not_issue(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(64, 64)},
    )
    result = extraction.extract_icon("ios", ipa)

    assert store.resolve_icon_ref(result.storage_ref) == store.icon_path(result.artifact_id)
    for hostile in ("icons/../../etc/passwd", "../icons/x.png", "icons/nothex.png", "", None):
        assert store.resolve_icon_ref(hostile) is None


def test_a_recorded_absence_is_not_rescanned(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "app.ipa")
    assert extraction.extract_icon("ios", ipa).reason == "no_icon_in_bundle"

    calls: list[int] = []
    original = extraction._extract_ios_icon
    monkeypatch.setitem(extraction._EXTRACTORS, "ios", lambda path: (calls.append(1), original(path))[1])

    repeat = extraction.extract_icon("ios", ipa)

    assert repeat.reason == "no_icon_in_bundle"
    assert calls == []


def test_an_absence_caused_by_the_host_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: False)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"catalog bytes"})
    assert extraction.extract_icon("ios", ipa).reason == "asset_catalog_tool_unavailable"

    monkeypatch.setattr(extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(extraction.asset_catalog, "read_catalog", lambda path: [])

    assert extraction.extract_icon("ios", ipa).reason == "asset_catalog_no_icon"


def test_a_changed_artifact_gets_its_own_identity_and_icon(tmp_path):
    first = make_ipa(
        tmp_path / "v1.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120, (255, 0, 0, 255))},
    )
    second = make_ipa(
        tmp_path / "v2.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120, (0, 0, 255, 255))},
    )

    one = extraction.extract_icon("ios", first)
    two = extraction.extract_icon("ios", second)

    assert one.artifact_id != two.artifact_id
    assert one.storage_ref != two.storage_ref
    assert store.icon_path(one.artifact_id).read_bytes() != store.icon_path(two.artifact_id).read_bytes()


def test_only_the_named_artifact_is_read_from_the_archive(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(64, 64)},
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(make_png(256, 256))

    result = extraction.extract_icon("ios", ipa)

    assert result.width == 64
    assert store.icon_path(result.artifact_id).read_bytes() != outside.read_bytes()
