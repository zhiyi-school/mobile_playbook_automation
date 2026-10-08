from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device.app_version import InstalledVersion, installed_version

DUMPSYS = """\
Packages:
  Package [com.example.one] (1a2b3c4):
    userId=10234
    versionCode=321 minSdk=24 targetSdk=34
    versionName=3.2.1
    installerPackageName=com.android.vending

Hidden system packages:
  Package [com.example.one] (5d6e7f8):
    versionCode=100 minSdk=24 targetSdk=34
    versionName=1.0.0
"""


class FakeAdb:
    def __init__(self, result):
        self.result = result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        return self.result


def test_the_active_packages_version_is_read_rather_than_the_factory_one():
    adb = FakeAdb((0, DUMPSYS, ""))

    assert installed_version(adb, "com.example.one") == InstalledVersion(version_name="3.2.1", version_code="321")
    assert adb.commands == [["shell", "dumpsys", "package", "com.example.one"]]


def test_a_version_name_with_spaces_is_kept_whole():
    text = "    versionCode=7 minSdk=24 targetSdk=34\n    versionName=2.0 beta 1  \n"

    assert installed_version(FakeAdb((0, text, "")), "com.example.one") == InstalledVersion("2.0 beta 1", "7")


def test_a_missing_version_code_leaves_only_the_name():
    assert installed_version(FakeAdb((0, "    versionName=4.0\n", "")), "com.example.one") == InstalledVersion("4.0", None)


@pytest.mark.parametrize(
    "result",
    [
        (1, "", "device offline"),
        (0, "", ""),
        (0, "Unable to find package: com.example.one\n", ""),
        (0, "    versionCode=1 minSdk=24\n    versionName=null\n", ""),
    ],
)
def test_no_version_is_reported_when_the_device_gives_none(result):
    assert installed_version(FakeAdb(result), "com.example.one") is None
