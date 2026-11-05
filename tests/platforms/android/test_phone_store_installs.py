import pathlib

import pytest

from mobile_playbook.platforms.android.acquisition import play_store
from mobile_playbook.platforms.android.baseline_devices import BuildInstallError, BuildUnavailable
from mobile_playbook.platforms.android.device.phone_lock import phone_lock_holder
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.adb_fakes import base_apk_sha256
from tests.platforms.android.baseline_fakes import (
    PACKAGE,
    PHONE_SERIAL,
    PLAIN,
    ROOT,
    ROOTED_SERIAL,
    BaselineBench,
    events,
    last_run_dir,
    phone_commands,
    report,
    run,
    use_fake_play_store,
)

PLAY = play_store.PLAY_STORE_PACKAGE
PLAY_VERSION = ("3.3.0", "330")


@pytest.fixture
def bench(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT), physical_roles=("non_rooted",),
                          phone_app_store="play_store")
    bench.phone.packages["com.example.personal"] = ("1.0", "1")
    return bench


@pytest.fixture
def store(bench, monkeypatch):
    return use_fake_play_store(bench, monkeypatch)


def installed_event(run_dir):
    [event] = [event for event in events(run_dir, "app_installed") if event["device_type"] == "physical_phone"]
    return event


def phone_cleanup(run_dir):
    return next(event for event in events(run_dir, "device_cleanup") if event["role"] == "non_rooted")


def test_a_missing_app_is_installed_from_the_store_without_an_intake_apk_and_kept(bench, store, tmp_path):
    bench.apk.unlink()

    outcome = run(bench, tmp_path, PLAIN)

    assert store.installs == [PACKAGE]
    assert bench.risks[PLAIN].used == [PHONE_SERIAL]
    sha = base_apk_sha256(PACKAGE, PLAY_VERSION)
    event = installed_event(outcome.run_dir)
    assert (event["source"], event["action"], event["installer"]) == ("play_store", "installed", PLAY)
    assert (event["version"], event["build_number"], event["artifact_sha256"]) == ("3.3.0", "330", sha)
    assert "apk" not in event
    device = report(outcome.run_dir, PLAIN)["metadata"]["device"]
    assert (device["version"], device["build_number"], device["artifact_sha256"]) == ("3.3.0", "330", sha)
    cleanup = phone_cleanup(outcome.run_dir)
    assert (cleanup["status"], cleanup["removed"], cleanup["kept"]) == ("cleaned", [], [PACKAGE])
    assert bench.phone.packages[PACKAGE] == PLAY_VERSION and bench.phone.installers[PACKAGE] == PLAY
    assert phone_commands(bench, "shell", "pm", "install") == []
    assert bench.api.data["calls"] == [] and phone_lock_holder() is None


def test_an_app_the_store_installed_is_kept_as_it_is(bench, store, tmp_path):
    bench.phone.packages[PACKAGE] = ("3.1.0", "310")
    bench.phone.installers[PACKAGE] = PLAY

    outcome = run(bench, tmp_path, PLAIN)

    assert store.installs == [] and phone_commands(bench, "uninstall") == []
    event = installed_event(outcome.run_dir)
    assert (event["action"], event["version"]) == ("kept", "3.1.0")
    assert phone_cleanup(outcome.run_dir)["kept"] == [PACKAGE]
    assert bench.phone.packages[PACKAGE] == ("3.1.0", "310")


def test_a_copy_another_installer_put_on_the_phone_is_replaced_from_the_store(bench, store, tmp_path):
    bench.phone.packages[PACKAGE] = ("3.0.0", "300")

    outcome = run(bench, tmp_path, PLAIN)

    assert phone_commands(bench, "uninstall") == [["uninstall", PACKAGE]]
    assert store.installs == [PACKAGE]
    event = installed_event(outcome.run_dir)
    assert (event["action"], event["version"], event["installer"]) == ("replaced", "3.3.0", PLAY)
    assert phone_cleanup(outcome.run_dir)["kept"] == [PACKAGE]
    assert bench.phone.packages[PACKAGE] == PLAY_VERSION


def test_a_failed_store_install_fails_the_run_with_the_stores_blocker_code(bench, store, tmp_path):
    store.fail_with = "sign_in_required"

    with pytest.raises(BuildInstallError, match="needs a signed-in Google account") as raised:
        run(bench, tmp_path, PLAIN)

    assert raised.value.outcome.blocker_code == "store_account_action_required"
    assert raised.value.outcome.retryable is False
    assert bench.risks[PLAIN].used == []
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"
    assert set(bench.phone.packages) == {"com.example.personal"} and phone_lock_holder() is None


def test_an_app_whose_installer_changed_during_the_run_is_removed(bench, store, tmp_path):
    bench.risks[PLAIN].on_run = lambda: bench.phone.installers.pop(PACKAGE)

    outcome = run(bench, tmp_path, PLAIN)

    cleanup = phone_cleanup(outcome.run_dir)
    assert (cleanup["status"], cleanup["removed"], cleanup["kept"]) == ("cleaned", [PACKAGE], [])
    assert PACKAGE not in bench.phone.packages


def test_helper_apps_are_removed_while_the_store_app_is_kept(bench, store, tmp_path):
    bench.risks[PLAIN].on_run = lambda: bench.phone.packages.update({"io.appium.uiautomator2.server": ("1", "1")})

    outcome = run(bench, tmp_path, PLAIN)

    cleanup = phone_cleanup(outcome.run_dir)
    assert (cleanup["removed"], cleanup["kept"]) == (["io.appium.uiautomator2.server"], [PACKAGE])
    assert set(bench.phone.packages) == {"com.example.personal", PACKAGE}


def test_the_vm_still_needs_an_intake_apk(bench, store, tmp_path):
    bench.apk.unlink()

    with pytest.raises(BuildUnavailable, match="No APK of com.example.one"):
        run(bench, tmp_path, PLAIN, ROOT)

    assert store.installs == [] and bench.api.data["calls"] == []


def test_differing_phone_and_vm_versions_are_both_tested_and_flagged(bench, store, tmp_path):
    outcome = run(bench, tmp_path, PLAIN, ROOT)

    assert bench.risks[PLAIN].used == [PHONE_SERIAL] and bench.risks[ROOT].used == [ROOTED_SERIAL]
    [warning] = events(outcome.run_dir, "app_versions_differ")
    assert {device["role"]: (device["version"], device["source"]) for device in warning["devices"]} == {
        "non_rooted": ("3.3.0", "play_store"), "rooted": ("3.2.1", "apk")}
    assert "non_rooted has 3.3.0 (build 330) from Play Store" in warning["message"]
    [tested] = events(outcome.run_dir, "app_version")
    assert (tested["version"], tested["build_number"]) == ("3.2.1", "321")
    assert report(outcome.run_dir, PLAIN)["metadata"]["device"]["version"] == "3.3.0"
    assert report(outcome.run_dir, ROOT)["metadata"]["device"]["version"] == "3.2.1"
    assert events(outcome.run_dir, "app_version_changed") == []


def test_matching_phone_and_vm_versions_raise_no_warning(bench, store, tmp_path):
    store.version = ("3.2.1", "321")

    outcome = run(bench, tmp_path, PLAIN, ROOT)

    assert events(outcome.run_dir, "app_versions_differ") == []


def test_an_app_tested_only_on_the_phone_records_the_store_build_as_its_artifact(bench, store, tmp_path):
    from mobile_playbook.common.storage_paths import android_work_dir

    outcome = run(bench, tmp_path, PLAIN)

    sha = base_apk_sha256(PACKAGE, PLAY_VERSION)
    manifest = read_manifest(outcome.run_dir)
    assert manifest["artifacts"]["one"] == sha
    assert manifest["artifact_versions"]["one"] == "3.3.0"
    assert (android_work_dir() / "phone_builds/com_example_one/base.apk").is_file()


def test_an_app_also_tested_on_a_vm_keeps_its_intake_apk_as_the_artifact(bench, store, tmp_path):
    from mobile_playbook.artifact_store.store import artifact_digest

    outcome = run(bench, tmp_path, PLAIN, ROOT)

    manifest = read_manifest(outcome.run_dir)
    assert manifest["artifacts"]["one"] == artifact_digest(bench.apk)
    assert manifest["artifact_versions"]["one"] == "3.2.1"
    assert phone_commands(bench, "pull") == []


def refuse_pulls(bench, count):
    original, refused = bench.adb._answer, []

    def answer(serial, args):
        if serial == PHONE_SERIAL and args[:1] == ["pull"] and len(refused) < count:
            refused.append(args)
            return 1, "", f"[  0%] {args[1]}"
        return original(serial, args)

    bench.adb._answer = answer
    return refused


def test_a_copy_that_fails_once_is_retried(bench, store, tmp_path):
    refused = refuse_pulls(bench, 1)

    outcome = run(bench, tmp_path, PLAIN)

    assert len(refused) == 1 and len(phone_commands(bench, "pull")) == 2
    assert read_manifest(outcome.run_dir)["artifacts"]["one"] == base_apk_sha256(PACKAGE, PLAY_VERSION)


def test_a_copy_that_is_not_the_installed_build_is_discarded(bench, store, tmp_path, caplog):
    from mobile_playbook.common.storage_paths import android_work_dir

    original = bench.adb._answer

    def truncated(serial, args):
        answer = original(serial, args)
        if serial == PHONE_SERIAL and args[:1] == ["pull"]:
            pathlib.Path(args[2]).write_bytes(b"truncated")
        return answer

    bench.adb._answer = truncated

    with caplog.at_level("WARNING"):
        run(bench, tmp_path, PLAIN)

    assert "is not the build installed there" in caplog.text
    assert not (android_work_dir() / "phone_builds/com_example_one/base.apk").exists()


def test_a_store_build_that_cannot_be_copied_still_lets_the_run_finish(bench, store, tmp_path, caplog):
    refuse_pulls(bench, 2)

    with caplog.at_level("WARNING"):
        outcome = run(bench, tmp_path, PLAIN)

    assert "Could not copy the store build of one from the phone" in caplog.text
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert bench.risks[PLAIN].used == [PHONE_SERIAL]
