from __future__ import annotations

import os

import pytest

from mobile_playbook.orchestration.appium_server import AppiumEnvironmentError
from mobile_playbook.platforms.android.device.sdk import appium_environment, sdk_root_from_adb


def test_a_symlinked_adb_resolves_to_the_sdk_that_holds_the_real_binary(fake_android_sdk, tmp_path):
    link = tmp_path / "bin" / "adb"
    link.parent.mkdir()
    link.symlink_to(fake_android_sdk / "platform-tools" / "adb")

    assert sdk_root_from_adb(str(link)) == fake_android_sdk


def test_derived_environment_keeps_other_variables_and_leaves_the_process_environment_alone(fake_android_sdk):
    env = appium_environment(str(fake_android_sdk / "platform-tools" / "adb"), {"PATH": "/usr/bin", "HOME": "/home/tester"})

    assert env == {"PATH": "/usr/bin", "HOME": "/home/tester", "ANDROID_HOME": str(fake_android_sdk), "ANDROID_SDK_ROOT": str(fake_android_sdk)}
    assert "ANDROID_HOME" not in os.environ


def test_an_empty_sdk_variable_counts_as_unset(fake_android_sdk):
    env = appium_environment(str(fake_android_sdk / "platform-tools" / "adb"), {"ANDROID_HOME": ""})

    assert env["ANDROID_HOME"] == env["ANDROID_SDK_ROOT"] == str(fake_android_sdk)


def test_adb_missing_from_path_names_the_configured_value(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(AppiumEnvironmentError, match="device.adb_path 'adb' was not found"):
        appium_environment("adb", {})
