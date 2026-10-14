from __future__ import annotations

import os

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
