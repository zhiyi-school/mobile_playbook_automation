from __future__ import annotations

import json
import os

import pytest

from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk, list_intake_apks

PACKAGE = "com.example.app"


def add_apk(intake, apk_metadata, name, package=PACKAGE, version_code="1", version_name="1.0", mtime=None):
    intake.mkdir(parents=True, exist_ok=True)
    path = intake / name
    path.write_bytes(b"apk")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    apk_metadata[name] = {"package_name": package, "version_code": version_code, "version_name": version_name, "display_name": "Example"}
    return path


def test_the_apk_is_found_by_the_package_inside_it_not_its_file_name(tmp_path, apk_metadata):
    add_apk(tmp_path, apk_metadata, "Other_App_1.0.apk", package="com.example.other")
    wanted = add_apk(tmp_path, apk_metadata, "Example_1.0.apk")

    assert find_intake_apk(PACKAGE, tmp_path).path == wanted


def test_the_highest_version_code_wins_over_a_newer_file(tmp_path, apk_metadata):
    newer = add_apk(tmp_path, apk_metadata, "Example_9.0.apk", version_code="9", version_name="9.0", mtime=1_000)
    add_apk(tmp_path, apk_metadata, "Example_10.0.apk", version_code="10", version_name="10.0", mtime=500)

    assert find_intake_apk(PACKAGE, tmp_path).version_name == "10.0"
    assert newer.exists()


def test_unreadable_files_and_split_folders_are_ignored(tmp_path, apk_metadata):
    (tmp_path / "unreadable.apk").write_bytes(b"not an apk")
    split_folder = tmp_path / PACKAGE
    split_folder.mkdir()
    (split_folder / "base.apk").write_bytes(b"apk")
    apk_metadata["base.apk"] = {"package_name": PACKAGE}

    assert list_intake_apks(tmp_path) == []
    assert find_intake_apk(PACKAGE, tmp_path) is None


def test_the_listing_reports_each_apks_identity(tmp_path, apk_metadata):
    add_apk(tmp_path, apk_metadata, "Example_1.2.3.apk", version_code="7", version_name="1.2.3")

    [listed] = [apk.as_dict() for apk in list_intake_apks(tmp_path)]

    assert {key: listed[key] for key in ("file", "bundle_id", "display_name", "version")} == {
        "file": "Example_1.2.3.apk",
        "bundle_id": PACKAGE,
        "display_name": "Example",
        "version": "1.2.3",
    }


def test_a_replaced_apk_is_read_again(tmp_path, apk_metadata):
    path = add_apk(tmp_path, apk_metadata, "Example.apk", version_name="1.0", mtime=1_000)
    assert find_intake_apk(PACKAGE, tmp_path).version_name == "1.0"

    apk_metadata["Example.apk"] = {**apk_metadata["Example.apk"], "version_name": "2.0"}
    os.utime(path, (2_000, 2_000))

    assert find_intake_apk(PACKAGE, tmp_path).version_name == "2.0"


def test_complete_split_set_is_one_artifact_with_base_first(tmp_path, write_apk_set):
    paths = write_apk_set(tmp_path / PACKAGE)
    [apk] = list_intake_apks(tmp_path)
    assert apk.package_name == PACKAGE
    assert apk.paths == paths
    assert apk.as_dict()["path"] == str(paths[0])


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "wrong_package", "wrong_version", "traversal", "symlink"])
def test_invalid_sets_are_rejected_even_after_a_cached_scan(tmp_path, write_apk_set, apk_metadata, damage):
    folder = tmp_path / PACKAGE
    paths = write_apk_set(folder)
    assert find_intake_apk(PACKAGE, tmp_path) is not None
    split = paths[1]
    if damage == "missing":
        split.unlink()
    elif damage == "changed":
        split.write_bytes(b"changed bytes")
    elif damage == "extra":
        (folder / "unexpected.apk").write_bytes(b"extra")
    elif damage in {"wrong_package", "wrong_version"}:
        apk_metadata[split.name]["package_name" if damage == "wrong_package" else "version_code"] = "other"
        os.utime(split, (1000, 1000))
    elif damage == "traversal":
        manifest = folder / "apk-set.json"
        record = json.loads(manifest.read_text())
        record["files"][1]["name"] = "../outside.apk"
        manifest.write_text(json.dumps(record))
    else:
        outside = tmp_path / "outside.bin"
        outside.write_bytes(split.read_bytes())
        split.unlink()
        split.symlink_to(outside)
    assert find_intake_apk(PACKAGE, tmp_path) is None


def test_split_set_competes_with_standalone_by_version(tmp_path, write_apk_set, apk_metadata):
    add_apk(tmp_path, apk_metadata, "old.apk", version_code="1")
    paths = write_apk_set(tmp_path / PACKAGE)
    assert find_intake_apk(PACKAGE, tmp_path).paths == paths
