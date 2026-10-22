from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

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
    assert known_risks() == {"android-feature-01-risk-01", "android-feature-01-risk-02", "android-feature-02-risk-01", "android-feature-02-risk-02", "android-feature-04-risk-01", "android-feature-05-risk-01", "android-feature-06-risk-01", "android-feature-07-risk-01"}


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


def test_an_app_can_say_whether_it_needs_google_play_services():
    apps = [
        {"id": "needs", "package_name": "com.example.needs", "requires_play_services": True},
        {"id": "works_without", "package_name": "com.example.without", "requires_play_services": False},
        {"id": "unset", "package_name": "com.example.unset"},
    ]

    config = parse_config({"apps": apps})

    assert [app.requires_play_services for app in config.apps] == [True, False, None]
    assert not [error for error in collect_config_errors(config) if "requires_play_services" in error]


def test_a_requires_play_services_setting_that_is_not_true_or_false_is_rejected():
    config = parse_config({"apps": [{"id": "one", "package_name": "com.example.one", "requires_play_services": "yes"}]})

    assert "apps[one].requires_play_services must be true or false" in collect_config_errors(config)


def _hybrid_device(physical_roles=("non_rooted",), adb_serial="wifi", **overrides):
    device = _baseline_device(adb_serial=adb_serial, **overrides)
    device["corellium"]["roles"] = {"rooted": {"project": "automation", "instance": "root-id", "adb_address": "10.11.1.1:5001"}}
    device["corellium"]["baselines"] = {
        "root-id": {"rooted": True, "os_version": "16.0.0", "osbuild": "r2 userdebug", "snapshot_id": "snap"}}
    if physical_roles is not None:
        device["physical_roles"] = list(physical_roles) if isinstance(physical_roles, tuple) else physical_roles
    return device


def _errors(device):
    return collect_config_errors(parse_config({"device": device, "apps": ["com.example.parking"]}))


def test_a_phone_can_serve_the_non_rooted_role_beside_a_rooted_vm():
    config = parse_config({"device": _hybrid_device(), "apps": ["com.example.parking"]})

    assert config.device.physical_roles == ("non_rooted",)
    assert config.device.adb_serial == "wifi"
    assert collect_config_errors(config) == []


@pytest.mark.parametrize("serial", ["R5CT1234ABC", "192.168.1.20:5555"])
def test_a_phone_can_be_named_by_serial_or_address(serial):
    assert _errors(_hybrid_device(adb_serial=serial)) == []


def test_configs_without_physical_roles_keep_an_empty_tuple():
    assert parse_config({"device": _baseline_device(), "apps": ["com.example.parking"]}).device.physical_roles == ()
    assert parse_config({}).device.physical_roles == ()
    assert _errors(_hybrid_device(physical_roles=[])) == []


@pytest.mark.parametrize(("physical_roles", "message"), [
    (("rooted",), "device.physical_roles cannot include rooted: rooted risks need a Corellium VM"),
    (("tablet",), "device.physical_roles has unknown role 'tablet'; use non_rooted"),
    (("non_rooted", "non_rooted"), "device.physical_roles lists a role more than once"),
    ("non_rooted", "device.physical_roles must be a list of device roles, such as [non_rooted]"),
    ([1], "device.physical_roles must be a list of device roles, such as [non_rooted]"),
])
def test_malformed_physical_roles_are_rejected(physical_roles, message):
    assert message in _errors(_hybrid_device(physical_roles=physical_roles))


def test_a_role_cannot_be_served_by_both_the_phone_and_a_vm():
    device = _hybrid_device()
    device["corellium"]["roles"]["non_rooted"] = {"project": "automation", "instance": "stock-id"}
    device["corellium"]["baselines"]["stock-id"] = {"rooted": False, "os_version": "16.0.0", "osbuild": "r2 user",
                                                    "snapshot_id": "snap"}

    assert _errors(device) == [
        "device.physical_roles and device.corellium.roles both list non_rooted; each role is served by the phone or a VM, not both"]


@pytest.mark.parametrize("serial", [None, "", "  "])
def test_the_phone_must_be_named_when_it_serves_a_role(serial):
    assert "device.adb_serial must name the phone (a serial, host:port or wifi) when device.physical_roles is set" in _errors(
        _hybrid_device(adb_serial=serial))


def test_the_phone_serial_cannot_be_a_vm_address():
    assert _errors(_hybrid_device(adb_serial="10.11.1.1:5001")) == [
        "device.adb_serial '10.11.1.1:5001' is the adb_address of the rooted VM; it must name the phone"]


@pytest.mark.parametrize("device", [
    {"provider": "physical", "adb_serial": "wifi", "physical_roles": ["non_rooted"]},
    {"provider": "corellium", "adb_serial": "wifi", "physical_roles": ["non_rooted"], "corellium": {"project": "android", "rooted": True}},
], ids=["physical provider", "single-instance corellium"])
def test_physical_roles_need_corellium_baseline_roles(device):
    assert ("device.physical_roles needs device.provider corellium with device.corellium.roles; with provider physical "
            "the phone already runs every risk") in _errors(device)


def _runtime_log_errors(runtime_logs=None, app_settings=None):
    config = parse_config({"runtime_logs": runtime_logs or {}, "apps": [{"id": "example", "package_name": "com.example.app", "risks": {"android-feature-02-risk-02": app_settings or {"enabled": True}}}]})
    return [error for error in collect_config_errors(config) if "runtime_logs" in error or "android-feature-02-risk-02" in error]


def test_runtime_log_steps_may_type_shared_app_and_default_canaries():
    steps = [{"type": {"id": "a", "value": "{canary.email}"}}, {"type": {"id": "b", "value": "{canary.otp}"}}, {"type": {"id": "c", "value": "{canary.member}"}}]

    assert _runtime_log_errors({"canaries": {"otp": "000000"}}, {"enabled": True, "canaries": {"member": "M-0001"}, "steps": steps}) == []


def test_runtime_log_step_problems_are_reported_for_the_app():
    errors = _runtime_log_errors(app_settings={"enabled": True, "steps": [{"tap": {"xpath": "//x"}}, {"type": {"id": "a", "value": "{canary.otp}"}}]})

    assert errors == [
        "apps[example].risks.android-feature-02-risk-02.steps[0].tap must name the element with exactly one of id, accessibility_id, text",
        "apps[example].risks.android-feature-02-risk-02.steps[0].tap.xpath is not a known setting",
        "apps[example].risks.android-feature-02-risk-02.steps[1].type.value names unknown canary 'otp'",
    ]


def test_shared_runtime_log_steps_and_bad_canaries_are_rejected():
    errors = _runtime_log_errors({"steps": [], "canaries": {"email": ""}}, {"enabled": True, "canaries": "x"})

    assert errors == [
        "runtime_logs.canaries must map names of letters, digits and underscores to non-empty text",
        "runtime_logs.steps is not supported; scripted steps belong to one app, under its risks.android-feature-02-risk-02 entry",
        "apps[example].risks.android-feature-02-risk-02.canaries must map names of letters, digits and underscores to non-empty text",
    ]


def _overlay_errors(overlay=None, app_settings=None):
    config = parse_config({"overlay": overlay or {}, "apps": [{"id": "example", "package_name": "com.example.app", "risks": {"android-feature-04-risk-01": app_settings or {"enabled": True}}}]})
    return [error for error in collect_config_errors(config) if "overlay" in error or "android-feature-04-risk-01" in error]


def test_overlay_settings_are_parsed_and_default_to_empty():
    assert parse_config({"overlay": {"launch_wait": 2}, "apps": ["com.example.app"]}).overlay == {"launch_wait": 2}
    assert parse_config({"apps": ["com.example.app"]}).overlay == {}


def test_overlay_steps_may_type_shared_app_and_default_canaries():
    steps = [{"type": {"id": "a", "value": "{canary.email}"}}, {"type": {"id": "b", "value": "{canary.otp}"}}, {"type": {"id": "c", "value": "{canary.member}"}}, {"tap": {"text": "Sign in"}}]

    assert _overlay_errors({"canaries": {"otp": "000000"}}, {"enabled": True, "canaries": {"member": "M-0001"}, "steps": steps}) == []


def test_overlay_step_problems_are_reported_for_the_app():
    errors = _overlay_errors(app_settings={"enabled": True, "steps": [{"tap": {"xpath": "//x"}}, {"type": {"id": "a", "value": "{canary.otp}"}}]})

    assert errors == [
        "apps[example].risks.android-feature-04-risk-01.steps[0].tap must name the element with exactly one of id, accessibility_id, text",
        "apps[example].risks.android-feature-04-risk-01.steps[0].tap.xpath is not a known setting",
        "apps[example].risks.android-feature-04-risk-01.steps[1].type.value names unknown canary 'otp'",
    ]


def test_shared_overlay_steps_and_bad_canaries_are_rejected():
    errors = _overlay_errors({"steps": [], "canaries": {"email": ""}}, {"enabled": True, "canaries": "x"})

    assert errors == [
        "overlay.canaries must map names of letters, digits and underscores to non-empty text",
        "overlay.steps is not supported; scripted steps belong to one app, under its risks.android-feature-04-risk-01 entry",
        "apps[example].risks.android-feature-04-risk-01.canaries must map names of letters, digits and underscores to non-empty text",
    ]


def test_overlay_canaries_do_not_satisfy_runtime_log_steps():
    config = parse_config({
        "overlay": {"canaries": {"otp": "000000"}},
        "apps": [{"id": "example", "package_name": "com.example.app", "risks": {"android-feature-02-risk-02": {"enabled": True, "steps": [{"type": {"id": "a", "value": "{canary.otp}"}}]}}}],
    })

    assert collect_config_errors(config) == ["apps[example].risks.android-feature-02-risk-02.steps[0].type.value names unknown canary 'otp'"]
