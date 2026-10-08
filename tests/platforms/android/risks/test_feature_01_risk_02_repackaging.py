from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome, failure
from mobile_playbook.platforms.android.acquisition.store import APP_STORES
from mobile_playbook.platforms.android.models import AndroidDeviceConfig, AndroidRiskRunResult
from mobile_playbook.platforms.android.risks import feature_01_risk_02_repackaging as repackaging
from mobile_playbook.platforms.android.risks.feature_01_risk_02_repackaging import AndroidRepackagingRisk, StageFailure

PACKAGE = "com.example.app"


class FakeAdb:
    def __init__(self, install_result=(0, "Success", "")):
        self.install_result = install_result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(args)
        if args[0] in {"install", "install-multiple"}:
            return self.install_result
        return 0, "Success", ""


class FakeDeviceClient:
    def __init__(self, adb: FakeAdb):
        self.adb = adb

    def make_driver(self):
        raise AssertionError("the store install is stubbed in these tests")


def write_apks(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"apk")


def make_result() -> AndroidRiskRunResult:
    return AndroidRiskRunResult(
        run_timestamp="run",
        timestamp_start="start",
        timestamp_end=None,
        app_id="app",
        app_name="App",
        package_name=PACKAGE,
        risk_id=AndroidRepackagingRisk.risk_id,
        test_case_id=AndroidRepackagingRisk.test_case_id,
        test_case_type=AndroidRepackagingRisk.test_case_type,
    )


def test_an_app_without_splits_skips_the_merge(tmp_path, monkeypatch):
    write_apks(tmp_path / "original", "base.apk")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no merge should run"))

    assert AndroidRepackagingRisk()._merge_split_apks(tmp_path, None) is True
    assert not (tmp_path / "merged.apk").exists()


def test_splits_without_apkeditor_fail_the_stage_with_a_clear_message(tmp_path, monkeypatch):
    write_apks(tmp_path / "original", "base.apk", "split_config.arm64_v8a.apk", "split_config.xxhdpi.apk")
    monkeypatch.setattr(repackaging, "APKEDITOR_DIR", str(tmp_path / "no-vendor"))

    with pytest.raises(StageFailure, match="2 split APKs .* APKEditor was not found"):
        AndroidRepackagingRisk()._merge_split_apks(tmp_path, None)


def test_splits_are_merged_and_the_merged_apk_is_decoded(tmp_path, monkeypatch):
    write_apks(tmp_path / "original", "base.apk", "split_config.xxhdpi.apk")
    jar = tmp_path / "APKEditor.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(repackaging.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "java":
            (tmp_path / "merged.apk").write_bytes(b"merged")
        else:
            (tmp_path / "repackaged" / "base").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    risk = AndroidRepackagingRisk()

    assert risk._merge_split_apks(tmp_path, str(jar)) is True
    assert risk._run_apktool_decode(tmp_path) is True
    assert commands[0] == ["java", "-jar", str(jar), "m", "-i", str(tmp_path / "original"), "-o", str(tmp_path / "merged.apk")]
    assert commands[1][:4] == ["apktool", "d", "-f", str(tmp_path / "merged.apk")]


def test_a_failed_merge_reports_apkeditor_output(tmp_path, monkeypatch):
    write_apks(tmp_path / "original", "base.apk", "split_config.xxhdpi.apk")
    jar = tmp_path / "APKEditor.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(repackaging.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda command, **k: subprocess.CompletedProcess(command, 1, stdout="", stderr="Merging\nError: bad split"))

    with pytest.raises(StageFailure, match="APKEditor merge exited 1: Error: bad split"):
        AndroidRepackagingRisk()._merge_split_apks(tmp_path, str(jar))


@pytest.mark.parametrize(
    "install_result",
    [
        (1, "Performing Streamed Install", "adb: failed to install x.apk: Failure [INSTALL_PARSE_FAILED_NO_CERTIFICATES: no certs]"),
        (0, "Failure [INSTALL_PARSE_FAILED_NO_CERTIFICATES: no certs]", ""),
    ],
)
def test_the_install_stage_keeps_adbs_failure_line(tmp_path, install_result):
    write_apks(tmp_path / "repackaged", "repackaged.apk")
    client = FakeDeviceClient(FakeAdb(install_result))

    with pytest.raises(StageFailure) as raised:
        AndroidRepackagingRisk()._install_repackaged_apk(client, tmp_path, PACKAGE)

    assert str(raised.value) == "Failure [INSTALL_PARSE_FAILED_NO_CERTIFICATES: no certs]"


ORIGINAL_SHA = "a" * 64
OTHER_SHA = "b" * 64


class DeviceAdb(FakeAdb):
    """FakeAdb that also answers the fingerprint commands and can refuse installs."""

    def __init__(self, installed_sha=ORIGINAL_SHA, refuse_install=lambda args: False):
        super().__init__()
        self.installed_sha = installed_sha
        self.refuse_install = refuse_install

    def run(self, args, timeout=None):
        self.commands.append(args)
        if args[0] in {"install", "install-multiple"}:
            return (1, "", "Failure [INSTALL_FAILED_REFUSED]") if self.refuse_install(args) else (0, "Success", "")
        if args[:3] == ["shell", "pm", "path"]:
            return 0, f"package:/data/app/{PACKAGE}/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, f"{self.installed_sha}  {args[2]}\n", ""
        return 0, "Success", ""


def make_tested_result(installer=None) -> AndroidRiskRunResult:
    result = make_result()
    result.metadata.update({"installed_base_apk_sha256": ORIGINAL_SHA, "installer": installer})
    return result


def store_that(outcome, calls):
    def install(*args, **kwargs):
        calls.append(args)
        return outcome

    return replace(APP_STORES["play_store"], install=install)


def installs(adb):
    return [command for command in adb.commands if command[0].startswith("install")]


def test_restore_reinstalls_the_backed_up_build_with_its_installer(tmp_path):
    write_apks(tmp_path / "original", "base.apk", "split_config.xxhdpi.apk")
    adb = DeviceAdb()
    risk = AndroidRepackagingRisk()
    store_calls = []
    risk.store = store_that(InstallOutcome(status="installed"), store_calls)
    result = make_tested_result(installer="com.android.vending")

    risk._restore_original(FakeDeviceClient(adb), tmp_path, PACKAGE, result)

    assert result.metadata["restore"] == {
        "method": "backup",
        "installer": "com.android.vending",
        "base_apk_sha256": ORIGINAL_SHA,
        "matches_original": True,
    }
    assert ["-i", "com.android.vending"] == installs(adb)[0][3:5]
    assert store_calls == []
    assert result.errors == []


def test_a_backup_without_an_installer_is_reinstalled_without_one(tmp_path):
    write_apks(tmp_path / "original", "base.apk")
    adb = DeviceAdb()
    result = make_tested_result()

    AndroidRepackagingRisk()._restore_original(FakeDeviceClient(adb), tmp_path, PACKAGE, result)

    assert result.metadata["restore"]["method"] == "backup"
    assert "-i" not in installs(adb)[0]


def test_restore_keeps_the_build_when_the_device_refuses_the_installer(tmp_path):
    write_apks(tmp_path / "original", "base.apk")
    adb = DeviceAdb(refuse_install=lambda args: "-i" in args)
    result = make_tested_result(installer="com.android.vending")

    AndroidRepackagingRisk()._restore_original(FakeDeviceClient(adb), tmp_path, PACKAGE, result)

    restore = result.metadata["restore"]
    assert restore["method"] == "backup"
    assert restore["installer"] is None
    assert "refused to record com.android.vending" in restore["note"]
    assert restore["matches_original"] is True
    assert len(installs(adb)) == 2


def test_restore_falls_back_to_the_store_and_flags_a_different_build(tmp_path):
    write_apks(tmp_path / "original", "base.apk")
    adb = DeviceAdb(installed_sha=OTHER_SHA, refuse_install=lambda args: True)
    risk = AndroidRepackagingRisk()
    store_calls = []
    risk.store = store_that(InstallOutcome(status="installed", installer="com.android.vending"), store_calls)
    result = make_tested_result()

    risk._restore_original(FakeDeviceClient(adb), tmp_path, PACKAGE, result)

    restore = result.metadata["restore"]
    assert restore["method"] == "app_store"
    assert restore["backup_failure"] == "Failure [INSTALL_FAILED_REFUSED]"
    assert restore["base_apk_sha256"] == OTHER_SHA
    assert restore["matches_original"] is False
    assert "may be a different build" in restore["note"]
    assert len(store_calls) == 1
    assert result.errors == []


def test_a_restore_that_fails_both_ways_is_an_error(tmp_path):
    write_apks(tmp_path / "original", "base.apk")
    adb = DeviceAdb(refuse_install=lambda args: True)
    risk = AndroidRepackagingRisk()
    risk.store = store_that(failure("paid"), [])
    result = make_tested_result()

    risk._restore_original(FakeDeviceClient(adb), tmp_path, PACKAGE, result)

    restore = result.metadata["restore"]
    assert restore["method"] is None
    assert restore["backup_failure"] == "Failure [INSTALL_FAILED_REFUSED]"
    assert restore["app_store"]["blocker_code"] == "app_paid"
    assert result.errors == ["Restore failed: Failure [INSTALL_FAILED_REFUSED]"]


def test_restore_reports_an_unknown_match_when_the_tested_build_was_not_fingerprinted(tmp_path):
    write_apks(tmp_path / "original", "base.apk")
    result = make_result()

    AndroidRepackagingRisk()._restore_original(FakeDeviceClient(DeviceAdb()), tmp_path, PACKAGE, result)

    assert result.metadata["restore"]["matches_original"] is None


class FakeReportWriter:
    run_timestamp = "run"

    def __init__(self, root):
        self.root = root
        self.written = None

    def test_report_dir(self, *args, **kwargs):
        return self.root / "report"

    def write_result(self, result, report_dir):
        self.written = result


def make_configs(tmp_path, **repackaging_cfg):
    app_config = type("App", (), {"id": "app", "name": "App", "package_name": PACKAGE, "risks": {}})()
    global_config = type(
        "Config",
        (),
        {
            "device": AndroidDeviceConfig(app_store="other_store"),
            "repackaging": {"work_dir": str(tmp_path / "work"), "keystore_path": str(tmp_path / "ks"), **repackaging_cfg},
        },
    )()
    return app_config, global_config


def stub_pipeline(risk, monkeypatch):
    for name in ("_backup_apks", "_ensure_keystore", "_merge_split_apks", "_run_apktool_decode", "_add_debuggable_to_manifest", "_change_apk_name", "_rebuild_apk", "_sign_apks", "_install_repackaged_apk"):
        monkeypatch.setattr(risk, name, lambda *a: True)


def test_a_validation_error_still_restores_the_original(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)

    def broken_validation(*args):
        raise RuntimeError("Appium session could not start")

    restored = []
    monkeypatch.setattr(risk, "_validate_with_appium", broken_validation)
    monkeypatch.setattr(risk, "_restore_original", lambda device_client, app_dir, package, result: restored.append(package))

    result = risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), FakeReportWriter(tmp_path))

    assert result.final_status == "FAILED"
    assert result.errors == ["Appium session could not start"]
    assert restored == [PACKAGE]


def test_the_tested_build_and_its_installer_are_recorded_before_anything_changes(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path, restore_original_after_test=False)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)
    monkeypatch.setattr(risk, "_validate_with_appium", lambda *a: ("FAIL: exited", None))

    class InstalledAdb(DeviceAdb):
        def run(self, args, timeout=None):
            if args[:4] == ["shell", "pm", "list", "packages"]:
                self.commands.append(args)
                return 0, f"package:{PACKAGE}  installer=com.android.vending\n", ""
            return super().run(args, timeout)

    result = risk.run(app_config, global_config, FakeDeviceClient(InstalledAdb()), FakeReportWriter(tmp_path))

    assert result.metadata["installed_base_apk_sha256"] == ORIGINAL_SHA
    assert result.metadata["installer"] == "com.android.vending"


def test_a_failed_install_stage_restores_the_uninstalled_original(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)
    monkeypatch.setattr(risk, "_install_repackaged_apk", lambda *a: (_ for _ in ()).throw(StageFailure("Failure [INSTALL_FAILED_INVALID_APK]")))
    restored = []
    monkeypatch.setattr(risk, "_restore_original", lambda device_client, app_dir, package, result: restored.append(package))

    result = risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), FakeReportWriter(tmp_path))

    assert result.errors == ["Failed at stage: install_repackaged_apks - Failure [INSTALL_FAILED_INVALID_APK]"]
    assert restored == [PACKAGE]


def test_a_restore_error_is_recorded_and_the_result_still_written(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)
    monkeypatch.setattr(risk, "_validate_with_appium", lambda *a: ("PASS: able to work after repackaging", None))

    def broken_restore(*args):
        raise RuntimeError("device disconnected")

    monkeypatch.setattr(risk, "_restore_original", broken_restore)
    writer = FakeReportWriter(tmp_path)

    result = risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), writer)

    assert result.verdict == "At Risk"
    assert result.errors == ["Restore failed: device disconnected"]
    assert writer.written is result


def test_restore_is_skipped_when_disabled(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path, restore_original_after_test=False)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)
    monkeypatch.setattr(risk, "_validate_with_appium", lambda *a: ("PASS: able to work after repackaging", None))
    monkeypatch.setattr(risk, "_restore_original", lambda *a: pytest.fail("restore is disabled"))

    result = risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), FakeReportWriter(tmp_path))

    assert result.final_status == "REPACKAGING_SURVIVED"


@pytest.fixture(autouse=True)
def other_store(monkeypatch):
    other = replace(APP_STORES["play_store"], key="other_store", name="Other Store")
    monkeypatch.setitem(APP_STORES, "other_store", other)
    return other


def test_a_run_uses_the_store_from_the_device_config(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path, restore_original_after_test=False)
    risk = AndroidRepackagingRisk()
    stub_pipeline(risk, monkeypatch)
    monkeypatch.setattr(risk, "_validate_with_appium", lambda *a: ("PASS: able to work after repackaging", None))

    risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), FakeReportWriter(tmp_path))

    assert risk.store is APP_STORES["other_store"]


def test_a_missing_merge_tool_stops_the_run_as_inconclusive_without_touching_the_app(tmp_path, monkeypatch):
    app_config, global_config = make_configs(tmp_path)
    risk = AndroidRepackagingRisk()

    def backup(device_client, package_name, app_dir):
        write_apks(app_dir / "original", "base.apk", "split_config.xxhdpi.apk")
        return True

    monkeypatch.setattr(risk, "_backup_apks", backup)
    monkeypatch.setattr(risk, "_ensure_keystore", lambda *a: True)
    monkeypatch.setattr(repackaging, "APKEDITOR_DIR", str(tmp_path / "no-vendor"))
    monkeypatch.setattr(risk, "_restore_original", lambda *a: pytest.fail("nothing was replaced, so nothing to restore"))
    writer = FakeReportWriter(tmp_path)

    result = risk.run(app_config, global_config, FakeDeviceClient(FakeAdb()), writer)

    assert result.final_status == "REPACKAGING_FAILED"
    assert result.verdict == "Inconclusive"
    assert result.errors[0].startswith("Failed at stage: merge_split_apks - The app has 1 split APK that")
    assert writer.written is result


class LaunchDriver:
    def __init__(self, foreground: str):
        self.current_package = foreground
        self.terminated: list[str] = []

    def activate_app(self, package: str) -> None:
        pass

    def terminate_app(self, package: str) -> None:
        self.terminated.append(package)


@pytest.mark.parametrize(
    ("store_package", "store_name"), [("com.android.vending", "Play Store"), ("com.aurora.store", "Aurora Store")]
)
@pytest.mark.parametrize("key", ["apk", "play_store", "aurora_store"])
def test_a_redirect_to_any_store_app_blocks_the_repackaged_app(key, store_package, store_name):
    risk = AndroidRepackagingRisk()
    risk.store = APP_STORES[key]
    driver = LaunchDriver(store_package)

    verdict = risk._check_app_after_launch(driver, PACKAGE, {"launch_wait": 0})

    assert verdict == f"FAIL: app fail to work after repackaging (redirected to {store_name} after launch)"
    assert driver.terminated == [store_package]


def test_a_switch_to_another_app_is_an_exit_not_a_redirect():
    driver = LaunchDriver("com.huawei.appmarket")

    verdict = AndroidRepackagingRisk()._check_app_after_launch(driver, PACKAGE, {"launch_wait": 0})

    assert verdict == "FAIL: app fail to work after repackaging (exited immediately after launch)"
