from __future__ import annotations

import zipfile

import pytest

from mobile_playbook.artifact_store import icon_extraction, png, store
from tests.icon_helpers import make_apk, make_cgbi_png, make_ipa, make_png, make_png_rows, primary_icon_info


@pytest.fixture(autouse=True)
def no_sdk_layer_lookup(monkeypatch):
    """Answer as a host without aapt2 would, so results never depend on the SDK tools installed."""
    from mobile_playbook.platforms.android import apk_tools

    monkeypatch.setattr(apk_tools, "adaptive_icon_layers", lambda apk_path, icon_xml: None)


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

    result = icon_extraction.extract_icon("ios", ipa)

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

    result = icon_extraction.extract_icon("ios", ipa)

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

    assert icon_extraction.extract_icon("ios", ipa).width == 152


def test_ios_falls_back_to_conventionally_named_root_icons(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa", files={"AppIcon76x76@2x.png": make_png(152, 152)})

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.available
    assert result.width == 152


def test_ios_without_any_icon_reports_unavailable(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa")

    result = icon_extraction.extract_icon("ios", ipa)

    assert not result.available
    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "no_icon_in_bundle"


def test_ios_asset_catalog_without_tooling_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: False)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"asset catalog bytes"})

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_tool_unavailable"


def test_ios_malformed_asset_catalog_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(icon_extraction.asset_catalog, "read_catalog", lambda path: None)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"not really a catalog"})

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_unreadable"


def test_ios_asset_catalog_identifies_the_primary_icon_but_cannot_export_it(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(
        icon_extraction.asset_catalog,
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

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "asset_catalog_no_extractor"


def test_ios_asset_catalog_without_an_app_icon_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(
        icon_extraction.asset_catalog,
        "read_catalog",
        lambda path: [{"Name": "SomeOtherAsset", "PixelWidth": 40, "PixelHeight": 40, "Idiom": "phone"}],
    )
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"catalog bytes"})

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.reason == "asset_catalog_no_icon"


def test_a_loose_png_still_wins_over_the_asset_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: True)
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120), "Assets.car": b"catalog bytes"},
    )

    assert icon_extraction.extract_icon("ios", ipa).available


def test_ios_unsupported_icon_format_is_not_served(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": b"<svg>not a png</svg>"},
    )

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "unsupported_icon_format"


def test_oversized_icon_member_is_skipped(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(16, 16) + b"\x00" * png.MAX_SOURCE_BYTES},
    )

    assert not icon_extraction.extract_icon("ios", ipa).available


def test_malformed_archive_fails_without_raising(tmp_path):
    broken = tmp_path / "app.ipa"
    broken.write_bytes(b"this is not a zip archive")

    result = icon_extraction.extract_icon("ios", broken)

    assert result.status == icon_extraction.STATUS_FAILED
    assert result.reason == "artifact_unreadable"


def test_missing_artifact_fails_without_raising(tmp_path):
    result = icon_extraction.extract_icon("ios", tmp_path / "absent.ipa")

    assert result.status == icon_extraction.STATUS_FAILED
    assert result.reason == "artifact_unreadable"


def test_traversing_zip_entry_is_never_read(tmp_path):
    ipa = tmp_path / "app.ipa"
    make_ipa(ipa, info_extra=primary_icon_info("AppIcon60x60"))
    with zipfile.ZipFile(ipa, "a") as zf:
        zf.writestr("Payload/Example.app/../../../escape.png", make_png(64, 64))

    result = icon_extraction.extract_icon("ios", ipa)

    assert not result.available
    assert not (tmp_path.parent / "escape.png").exists()


def test_a_cached_icon_still_reports_its_dimensions(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )
    first = icon_extraction.extract_icon("ios", ipa)

    cached = icon_extraction.extract_icon("ios", ipa)

    assert (cached.width, cached.height) == (first.width, first.height) == (120, 120)
    assert cached.storage_ref == first.storage_ref


def test_identical_artifacts_share_one_id_and_one_stored_icon(tmp_path):
    files = {"AppIcon60x60@2x.png": make_png(120, 120)}
    info = primary_icon_info("AppIcon60x60")
    first = icon_extraction.extract_icon("ios", make_ipa(tmp_path / "a.ipa", info_extra=info, files=files))
    second = icon_extraction.extract_icon("ios", make_ipa(tmp_path / "b.ipa", info_extra=info, files=files))

    assert first.artifact_id == second.artifact_id
    assert first.storage_ref == second.storage_ref
    assert len(list((store.store_root() / "icons").glob("*.png"))) == 1


def test_android_icon_is_taken_from_the_declared_resource(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk", {"res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192)})
    monkeypatch.setattr(
        icon_extraction, "_android_declared_icons", lambda path: ["res/mipmap-xxxhdpi-v4/ic_launcher.png"]
    )

    result = icon_extraction.extract_icon("android", apk)

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
    monkeypatch.setattr(icon_extraction, "_android_declared_icons", lambda path: [])

    result = icon_extraction.extract_icon("android", apk)

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
        icon_extraction, "_android_declared_icons", lambda path: ["res/mipmap-anydpi-v26/ic_launcher.xml"]
    )

    result = icon_extraction.extract_icon("android", apk)

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
        icon_extraction, "_android_declared_icons", lambda path: ["res/mipmap-anydpi-v26/ic_launcher.xml"]
    )

    result = icon_extraction.extract_icon("android", apk)

    assert result.status == icon_extraction.STATUS_UNAVAILABLE
    assert result.reason == "adaptive_icon_vector_only"


def test_android_resolves_a_mipmap_reference_from_apkanalyzer(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk", {"res/mipmap-xhdpi-v4/custom_icon.png": make_png(96, 96)})
    monkeypatch.setattr(icon_extraction, "_android_declared_icons", lambda path: ["@mipmap/custom_icon"])

    result = icon_extraction.extract_icon("android", apk)

    assert result.available
    assert result.width == 96


def test_android_without_any_icon_reports_unavailable(tmp_path, monkeypatch):
    apk = make_apk(tmp_path / "app.apk")
    monkeypatch.setattr(icon_extraction, "_android_declared_icons", lambda path: [])

    assert icon_extraction.extract_icon("android", apk).reason == "no_icon_in_archive"


def test_describe_artifact_keeps_identity_when_icon_extraction_fails(tmp_path):
    ipa = make_ipa(tmp_path / "app.ipa", bundle_id="com.example.placeholder")

    metadata = icon_extraction.describe_artifact("ios", ipa)

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

    icon = icon_extraction.describe_artifact("ios", ipa)["icon"]

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
    result = icon_extraction.extract_icon("ios", ipa)

    assert store.resolve_icon_ref(result.storage_ref) == store.icon_path(result.artifact_id)
    for hostile in ("icons/../../etc/passwd", "../icons/x.png", "icons/nothex.png", "", None):
        assert store.resolve_icon_ref(hostile) is None


def test_a_recorded_absence_is_not_rescanned(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "app.ipa")
    assert icon_extraction.extract_icon("ios", ipa).reason == "no_icon_in_bundle"

    calls: list[int] = []
    original = icon_extraction._extract_ios_icon
    monkeypatch.setitem(icon_extraction._EXTRACTORS, "ios", lambda path: (calls.append(1), original(path))[1])

    repeat = icon_extraction.extract_icon("ios", ipa)

    assert repeat.reason == "no_icon_in_bundle"
    assert calls == []


def test_an_absence_caused_by_the_host_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: False)
    ipa = make_ipa(tmp_path / "app.ipa", files={"Assets.car": b"catalog bytes"})
    assert icon_extraction.extract_icon("ios", ipa).reason == "asset_catalog_tool_unavailable"

    monkeypatch.setattr(icon_extraction.asset_catalog, "assetutil_available", lambda: True)
    monkeypatch.setattr(icon_extraction.asset_catalog, "read_catalog", lambda path: [])

    assert icon_extraction.extract_icon("ios", ipa).reason == "asset_catalog_no_icon"


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

    one = icon_extraction.extract_icon("ios", first)
    two = icon_extraction.extract_icon("ios", second)

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

    result = icon_extraction.extract_icon("ios", ipa)

    assert result.width == 64
    assert store.icon_path(result.artifact_id).read_bytes() != outside.read_bytes()


ADAPTIVE_XML = "res/mipmap-anydpi-v26/ic_launcher.xml"
BLUE = (0x2B, 0x57, 0x97, 255)
WHITE = (255, 255, 255, 255)


def _declare(monkeypatch, *declared):
    monkeypatch.setattr(icon_extraction, "_android_declared_icons", lambda path: list(declared))


def _stored_pixels(result):
    return png.decode_rgba(store.icon_path(result.artifact_id).read_bytes())


def _foreground() -> bytes:
    """A 432px adaptive foreground: transparent at the edges, opaque white in the middle."""
    clear, white = b"\x00\x00\x00\x00", b"\xff\xff\xff\xff"
    middle = clear * 144 + white * 144 + clear * 144
    return make_png_rows(6, [clear * 432] * 144 + [middle] * 144 + [clear * 432] * 144, 432)


def test_an_adaptive_icon_is_composed_from_its_layers_and_cropped_to_what_launchers_show(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            ADAPTIVE_XML: b"<adaptive-icon/>",
            "res/mipmap-xxxhdpi-v4/ic_launcher_foreground.png": _foreground(),
            "res/mipmap-xxxhdpi-v4/ic_launcher_background.png": make_png(432, 432, BLUE),
            "res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192, (9, 9, 9, 255)),
        },
    )
    _declare(monkeypatch, ADAPTIVE_XML)

    result = icon_extraction.extract_icon("android", apk)

    pixels, width, height = _stored_pixels(result)
    assert result.available
    assert (width, height) == (288, 288)
    assert tuple(pixels[:4]) == BLUE
    centre = (144 * 288 + 144) * 4
    assert tuple(pixels[centre : centre + 4]) == WHITE


def test_layers_named_by_aapt2_and_a_colour_background_are_used(tmp_path, monkeypatch):
    from mobile_playbook.platforms.android import apk_tools

    apk = make_apk(
        tmp_path / "app.apk",
        {ADAPTIVE_XML: b"<adaptive-icon/>", "res/a1.png": make_png(108, 108, (0, 0, 0, 0))},
    )
    _declare(monkeypatch, ADAPTIVE_XML)
    monkeypatch.setattr(
        apk_tools,
        "adaptive_icon_layers",
        lambda apk_path, icon_xml: {
            "foreground": {"files": ["res/a1.png"], "color": None},
            "background": {"files": [], "color": "#ff2b5797"},
        },
    )

    result = icon_extraction.extract_icon("android", apk)

    pixels, width, _ = _stored_pixels(result)
    assert width == 72
    assert pixels == bytes(BLUE) * (72 * 72)


def test_without_both_layers_the_complete_icon_beats_a_larger_lone_layer(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            ADAPTIVE_XML: b"<adaptive-icon/>",
            "res/mipmap-xxxhdpi-v4/ic_launcher_foreground.png": make_png(432, 432, WHITE),
            "res/mipmap-xxxhdpi-v4/ic_launcher_round.png": make_png(256, 256, (1, 1, 1, 255)),
            "res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192, BLUE),
        },
    )
    _declare(monkeypatch, ADAPTIVE_XML)

    result = icon_extraction.extract_icon("android", apk)

    pixels, width, _ = _stored_pixels(result)
    assert width == 192
    assert tuple(pixels[:4]) == BLUE


def test_the_round_icon_is_used_when_it_is_the_only_complete_one(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            ADAPTIVE_XML: b"<adaptive-icon/>",
            "res/mipmap-xxxhdpi-v4/ic_launcher_background.png": make_png(432, 432, BLUE),
            "res/mipmap-xxxhdpi-v4/ic_launcher_round.png": make_png(192, 192, WHITE),
        },
    )
    _declare(monkeypatch, ADAPTIVE_XML)

    assert icon_extraction.extract_icon("android", apk).width == 192


def test_an_icon_cached_under_the_old_android_rules_is_extracted_again(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            ADAPTIVE_XML: b"<adaptive-icon/>",
            "res/mipmap-xxxhdpi-v4/ic_launcher_foreground.png": make_png(432, 432, WHITE),
            "res/mipmap-xxxhdpi-v4/ic_launcher.png": make_png(192, 192, BLUE),
        },
    )
    _declare(monkeypatch, ADAPTIVE_XML)
    artifact_id = store.artifact_digest(apk)
    store.write_atomic(store.icon_path(artifact_id), make_png(432, 432, WHITE))
    store.write_metadata(artifact_id, {"artifact_id": artifact_id, "icon": {"status": "available", "width": 432}})

    result = icon_extraction.extract_icon("android", apk)

    assert result.width == 192
    assert tuple(_stored_pixels(result)[0][:4]) == BLUE
    assert store.read_metadata(artifact_id)["icon"]["revision"] == icon_extraction.ANDROID_ICON_REVISION
    assert icon_extraction.extract_icon("android", apk).width == 192


def test_an_ios_icon_cached_without_a_revision_is_left_alone(tmp_path):
    ipa = make_ipa(
        tmp_path / "app.ipa",
        info_extra=primary_icon_info("AppIcon60x60"),
        files={"AppIcon60x60@2x.png": make_png(120, 120)},
    )
    artifact_id = store.artifact_digest(ipa)
    cached = make_png(10, 10, BLUE)
    store.write_atomic(store.icon_path(artifact_id), cached)
    store.write_metadata(artifact_id, {"artifact_id": artifact_id, "icon": {"status": "available", "width": 10}})

    assert icon_extraction.extract_icon("ios", ipa).width == 10
    assert store.icon_path(artifact_id).read_bytes() == cached


def test_a_lone_layer_never_stands_in_for_a_launcher_icon(tmp_path, monkeypatch):
    apk = make_apk(
        tmp_path / "app.apk",
        {
            ADAPTIVE_XML: b"<adaptive-icon/>",
            "res/mipmap-xxxhdpi-v4/ic_launcher_background.png": make_png(432, 432, BLUE),
            "res/mipmap-xxxhdpi-v4/ic_app.png": make_png(192, 192, WHITE),
        },
    )
    _declare(monkeypatch, ADAPTIVE_XML)

    result = icon_extraction.extract_icon("android", apk)

    assert result.width == 192
    assert tuple(_stored_pixels(result)[0][:4]) == WHITE
