import copy

import pytest

from mobile_playbook.platforms.corellium.client import CorelliumError
from mobile_playbook.platforms.corellium.profile import is_baseline_profile, load_profile, profile_errors

PROFILE = {
    "auto_start": True,
    "auto_stop": True,
    "restore_timeout_seconds": 300,
    "roles": {
        "non_rooted": {"project": "automation", "instance": "stock-id", "adb_port": 5001},
        "rooted": {"project": "automation", "os_version": "16"},
    },
    "baselines": {
        "stock-id": {"rooted": False, "os_version": "16.0.0", "osbuild": "r2 user", "snapshot_id": "stock-snap"},
        "rooted-id": {"rooted": True, "os_version": "16.0.0", "osbuild": "r2 userdebug", "snapshot_name": "automation-ready-v1"},
    },
}


def changed(**updates):
    settings = copy.deepcopy(PROFILE)
    for path, value in updates.items():
        target = settings
        *parents, key = path.split("__")
        for parent in parents:
            target = target[parent]
        if value is None:
            target.pop(key)
        else:
            target[key] = value
    return settings


def test_a_valid_profile_resolves_role_selectors_inventory_and_budgets():
    profile = load_profile(PROFILE, "android")

    assert profile.roles["non_rooted"].instance == "stock-id"
    assert profile.roles["rooted"].os_version == "16" and profile.roles["rooted"].instance == ""
    assert profile.baselines["stock-id"].snapshot_id == "stock-snap"
    assert profile.baselines["rooted-id"].snapshot_name == "automation-ready-v1"
    assert profile.baselines["rooted-id"].platform == "android"
    assert profile.role_settings["non_rooted"]["adb_port"] == 5001
    assert (profile.policy.restore_timeout, profile.policy.power_timeout) == (300, 600)


def test_only_settings_with_roles_use_the_baseline_lifecycle():
    assert is_baseline_profile(PROFILE)
    assert not is_baseline_profile({"project": "automation", "rooted": False})
    assert not is_baseline_profile(None)


@pytest.mark.parametrize("key", ["auto_start", "auto_stop"])
def test_auto_start_and_auto_stop_must_be_explicitly_true(key):
    assert f"device.corellium.{key} must be set to true when roles are set" in profile_errors(changed(**{key: None}), "android")
    assert f"device.corellium.{key} must be set to true when roles are set" in profile_errors(changed(**{key: False}), "android")


def test_single_instance_keys_cannot_be_mixed_with_roles():
    errors = profile_errors(changed(project="automation", rooted=False), "android")

    assert "device.corellium.project belongs under device.corellium.roles.<role> when roles are set" in errors
    assert "device.corellium.rooted belongs under device.corellium.roles.<role> when roles are set" in errors


def test_a_role_pinned_to_an_instance_with_the_other_rooted_state_is_rejected():
    errors = profile_errors(changed(roles__rooted__instance="stock-id"), "android")

    assert "device.corellium.roles.rooted.instance 'stock-id' has a non-rooted baseline" in errors
    assert "device.corellium.roles.rooted.instance 'stock-id' is also the non_rooted instance; roles need distinct VMs" in errors


def test_a_role_without_a_baseline_of_its_rooted_state_is_rejected():
    errors = profile_errors(changed(**{"baselines__rooted-id": None}), "android")

    assert errors == ["device.corellium.roles.rooted has no baseline under device.corellium.baselines for a rooted instance"]


def test_a_baseline_needs_exactly_one_snapshot_and_its_exact_os_build():
    errors = profile_errors(changed(**{"baselines__stock-id__snapshot_name": "also-named", "baselines__stock-id__osbuild": None}), "android")

    assert "device.corellium.baselines.stock-id needs exactly one of snapshot_id or snapshot_name" in errors
    assert "device.corellium.baselines.stock-id.osbuild is required and must match the instance exactly" in errors


def test_a_rooted_flag_that_contradicts_the_android_build_variant_is_rejected():
    errors = profile_errors(changed(**{"baselines__rooted-id__osbuild": "r2 user"}), "android")

    assert "device.corellium.baselines.rooted-id.rooted contradicts the 'user' build in osbuild" in errors


def test_unknown_roles_missing_projects_and_bad_budgets_are_rejected():
    errors = profile_errors(changed(roles__admin={"project": "x"}, roles__rooted__project=None, boot_timeout_seconds=0), "android")

    assert "device.corellium.roles.admin is not a device role; use non_rooted or rooted" in errors
    assert "device.corellium.roles.rooted.project is required" in errors
    assert "device.corellium.boot_timeout_seconds must be a positive number" in errors


def test_loading_an_invalid_profile_raises_with_every_problem():
    with pytest.raises(CorelliumError, match="auto_stop must be set to true"):
        load_profile(changed(auto_stop=False), "android")
