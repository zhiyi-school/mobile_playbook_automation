from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.platforms.android.device.install import install_apks


class FakeAdb:
    def __init__(self, result=(0, "Success", "")):
        self.result = result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        return self.result


def test_split_apks_are_installed_together_without_granting_permissions():
    adb = FakeAdb()

    assert install_apks(adb, [Path("base.apk"), Path("split_config.en.apk")], grant_permissions=False) == (True, "")
    assert adb.commands == [["install-multiple", "-r", "base.apk", "split_config.en.apk"]]


def test_a_single_apk_can_be_installed_with_permissions_granted():
    adb = FakeAdb()

    install_apks(adb, [Path("app.apk")], grant_permissions=True)

    assert adb.commands == [["install", "-r", "-g", "app.apk"]]


def test_an_installer_is_recorded_when_given():
    adb = FakeAdb()

    install_apks(adb, [Path("base.apk"), Path("split_config.en.apk")], grant_permissions=True, installer="com.android.vending")

    assert adb.commands == [["install-multiple", "-r", "-g", "-i", "com.android.vending", "base.apk", "split_config.en.apk"]]


@pytest.mark.parametrize(
    "result",
    [(0, "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]", ""), (1, "", "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]")],
)
def test_the_package_managers_failure_line_is_returned(result):
    assert install_apks(FakeAdb(result), [Path("app.apk")], grant_permissions=False) == (
        False,
        "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]",
    )


def test_no_apks_never_reaches_the_device():
    adb = FakeAdb()

    assert install_apks(adb, [], grant_permissions=False) == (False, "No APKs to install")
    assert adb.commands == []
