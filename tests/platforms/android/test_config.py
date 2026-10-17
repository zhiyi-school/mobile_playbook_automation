from __future__ import annotations

import json
from types import SimpleNamespace

from mobile_playbook.platforms.android.config import collect_config_errors, parse_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_work_dir
from mobile_playbook.platforms.android.risks import get_risk, known_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.reporting.report_writer import ReportWriter


def test_android_config_accepts_legacy_package_list():
    config = parse_config(
        {
            "device": {"appium_server": "http://127.0.0.1:4723"},
            "apps": ["com.example.parking"],
        }
    )

    assert config.device.appium_server_url == "http://127.0.0.1:4723"
    assert config.apps[0].package_name == "com.example.parking"
    assert config.apps[0].risks["android-feature-06-risk-01"]["enabled"] is True
    assert config.apps[0].risks["android-feature-01-risk-02"]["enabled"] is True


def test_android_config_respects_an_explicit_repackaging_work_dir():
    config = parse_config({"repackaging": {"work_dir": "custom/repackaging/dir"}})

    assert config.repackaging["work_dir"] == "custom/repackaging/dir"


def test_android_config_falls_back_to_default_repackaging_work_dir_when_unset():
    config = parse_config({})

    assert config.repackaging["work_dir"] == str(android_work_dir() / "repackaging")


def test_android_config_accepts_structured_apps():
    config = parse_config(
        {
            "apps": [
                {
                    "id": "parking",
                    "name": "Parking",
                    "package_name": "com.example.parking",
                    "risks": {"android-feature-06-risk-01": {"enabled": True}},
                }
            ]
        }
    )

    assert config.apps[0].id == "parking"
    assert config.apps[0].name == "Parking"
    assert config.apps[0].risks == {"android-feature-06-risk-01": {"enabled": True}}


def test_android_registry_exposes_ported_risks():
    assert known_risks() == {"android-feature-01-risk-01", "android-feature-01-risk-02", "android-feature-06-risk-01"}


def test_android_dry_run_filters_selected_apps():
    config = parse_config({"apps": ["com.example.parking", "com.example.services"]})

    lines = AndroidPlatformRunner().dry_run_lines(config, {"android-feature-06-risk-01"}, {"comexampleservices"})

    assert any("com.example.services" in line for line in lines)
    assert not any("com.example.parking" in line for line in lines)


def test_run_test_records_failure_without_raising(monkeypatch, tmp_path):
    # Resolved dynamically via get_risk(), the same way AndroidPlatformRunner.run_test
    # does — not a direct import of a specific risk module.
    risk_class = type(get_risk("android-feature-06-risk-01"))

    def flaky_run(self, app_config, global_config, device_client, report_writer):
        raise RuntimeError("adb exploded")

    monkeypatch.setattr(risk_class, "requires", [])
    monkeypatch.setattr(risk_class, "run", flaky_run)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.wake_and_unlock", lambda adb: None)

    app = SimpleNamespace(id="parking", name="Parking", package_name="com.example.parking")
    config = SimpleNamespace(runner=SimpleNamespace(auto_grant_permissions=False))
    device_client = SimpleNamespace(adb=None)
    writer = ReportWriter(tmp_path, "run1", result_adapter=normalize_android_result, platform="android")

    # must not raise — a single flaky risk cannot be allowed to abort the whole run
    AndroidPlatformRunner().run_test(app, "android-feature-06-risk-01", config, device_client, writer)

    report_dir = tmp_path / "run1" / "android" / "parking" / "android-feature-06-risk-01" / "screen_capture"
    result = json.loads((report_dir / "report.json").read_text())
    assert result["final_status"] == "FAILED"
    assert "adb exploded" in result["errors"][0]


def test_android_relative_runner_work_dir_resolves_against_the_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = parse_config({"runner": {"work_dir": "custom/android-work"}})

    assert config.runner.work_dir == REPOSITORY_ROOT / "custom" / "android-work"


def test_android_unset_runner_work_dir_follows_the_work_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))

    assert parse_config({}).runner.work_dir == android_work_dir() == tmp_path / "work" / "android"


def test_keep_screen_on_defaults_off_and_reads_the_runner_setting():
    assert parse_config({"apps": ["com.example.parking"]}).runner.keep_screen_on is False
    assert parse_config({"apps": ["com.example.parking"], "runner": {"keep_screen_on": True}}).runner.keep_screen_on is True


def test_android_device_provider_defaults_to_physical():
    config = parse_config({"apps": ["com.example.parking"]})

    assert config.device.provider == "physical"
    assert collect_config_errors(config) == []


def test_android_corellium_provider_needs_a_project_rooted_and_valid_numbers():
    config = parse_config(
        {
            "device": {"provider": "Corellium", "corellium": {"adb_port": "5001", "boot_timeout_seconds": 0}},
            "apps": ["com.example.parking"],
        }
    )

    assert config.device.provider == "corellium"
    assert collect_config_errors(config) == [
        "device.corellium.project is required",
        "device.corellium.rooted must be true or false",
        "device.corellium.boot_timeout_seconds must be a positive number",
        "device.corellium.adb_port must be a positive whole number",
    ]


def test_android_corellium_provider_with_a_project_and_rooted_is_valid():
    config = parse_config(
        {
            "device": {"provider": "corellium", "corellium": {"project": "testing", "rooted": False, "auto_stop": True}},
            "apps": ["com.example.parking"],
        }
    )

    assert collect_config_errors(config) == []


def test_android_unknown_device_provider_is_refused():
    config = parse_config({"device": {"provider": "emulator"}, "apps": ["com.example.parking"]})

    assert collect_config_errors(config) == ["device.provider must be one of: physical, corellium"]


def _baseline_device(**overrides):
    corellium = {
        "auto_start": True,
        "auto_stop": True,
        "roles": {"non_rooted": {"project": "automation", "instance": "stock-id", "adb_port": 5001}},
        "baselines": {"stock-id": {"rooted": False, "os_version": "16.0.0", "osbuild": "r2 user", "snapshot_id": "snap"}},
    }
    return {"provider": "corellium", "app_store": "apk", "corellium": corellium, **overrides}


def test_android_corellium_baseline_roles_are_valid_with_apk_installs():
    config = parse_config({"device": _baseline_device(), "apps": ["com.example.parking"]})

    assert collect_config_errors(config) == []


def test_android_corellium_baseline_roles_need_apk_installs_and_valid_adb_overrides():
    device = _baseline_device(app_store="play_store")
    device["corellium"]["roles"]["non_rooted"].update(adb_port="5001", adb_address=5001)
    config = parse_config({"device": device, "apps": ["com.example.parking"]})

    assert collect_config_errors(config) == [
        "device.corellium.roles.non_rooted.adb_port must be a positive whole number",
        "device.corellium.roles.non_rooted.adb_address must be host:port text",
        "device.app_store must be apk when device.corellium.roles is set, because each run installs the intake APK on its restored devices",
    ]


def test_android_baseline_roles_are_ignored_while_the_provider_stays_physical():
    device = _baseline_device(provider="physical", app_store="play_store")
    device["corellium"].pop("auto_stop")

    config = parse_config({"device": device, "apps": ["com.example.parking"]})

    assert config.device.provider == "physical"
    assert collect_config_errors(config) == []
