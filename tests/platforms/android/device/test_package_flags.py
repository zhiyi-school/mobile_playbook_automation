from __future__ import annotations

from mobile_playbook.platforms.android.device.package_flags import PackageFlags, package_flags, parse_package_flags

DUMPSYS = """\
Permissions:
  Permission [com.example.one.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION] (8a1b2c3):
    sourcePackage=com.example.one
    flags=0x0

Packages:
  Package [com.example.one] (1a2b3c4):
    userId=10234
    versionCode=321 minSdk=24 targetSdk=30
    flags=[ DEBUGGABLE HAS_CODE ALLOW_CLEAR_USER_DATA ALLOW_BACKUP ]
    privateFlags=[ PRIVATE_FLAG_ACTIVITIES_RESIZE_MODE_RESIZEABLE ALLOW_AUDIO_PLAYBACK_CAPTURE ]

Hidden system packages:
  Package [com.example.one] (5d6e7f8):
    versionCode=100 minSdk=24 targetSdk=28
    flags=[ SYSTEM HAS_CODE ]
"""


class FakeAdb:
    def __init__(self, result):
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        return self.result


def test_the_active_packages_flags_and_target_sdk_are_read():
    adb = FakeAdb((0, DUMPSYS, ""))

    flags = package_flags(adb, "com.example.one")

    assert flags == PackageFlags(True, True, 30, ("DEBUGGABLE", "HAS_CODE", "ALLOW_CLEAR_USER_DATA", "ALLOW_BACKUP"))
    assert adb.calls == [["shell", "dumpsys", "package", "com.example.one"]]
    assert flags.to_dict() == {"debuggable": True, "allow_backup": True, "target_sdk": 30, "flags": ["DEBUGGABLE", "HAS_CODE", "ALLOW_CLEAR_USER_DATA", "ALLOW_BACKUP"]}


def test_older_releases_list_pkg_flags():
    flags = parse_package_flags("  Package [com.example.two] (1):\n    versionCode=2 targetSdk=22\n    pkgFlags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ]\n")

    assert flags == PackageFlags(False, False, 22, ("HAS_CODE", "ALLOW_CLEAR_USER_DATA"))


def test_a_package_the_device_does_not_report_has_no_flags():
    assert package_flags(FakeAdb((0, "", "")), "com.example.missing") is None
    assert package_flags(FakeAdb((1, "error", "")), "com.example.missing") is None
    assert parse_package_flags("Packages:\n  Package [com.example.one] (1):\n    versionCode=1\n") is None
