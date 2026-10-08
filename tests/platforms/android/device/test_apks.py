from __future__ import annotations

from mobile_playbook.platforms.android.device.apks import installed_apk_paths, pull_installed_apks

PACKAGE = "com.example.app"
PM_PATH = "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.en.apk\n"


class FakeAdb:
    def __init__(self, pm_path=(0, PM_PATH, ""), failing_pull=None):
        self.pm_path = pm_path
        self.failing_pull = failing_pull
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:3] == ["shell", "pm", "path"]:
            return self.pm_path
        if args[0] == "pull":
            if args[1] == self.failing_pull:
                return 1, "", "adb: error: failed to stat remote object"
            with open(args[2], "wb") as handle:
                handle.write(b"apk")
            return 0, "1 file pulled", ""
        return 1, "", "unexpected command"


def test_installed_apk_paths_lists_base_and_splits():
    assert installed_apk_paths(FakeAdb(), PACKAGE) == ["/data/app/x/base.apk", "/data/app/x/split_config.en.apk"]


def test_installed_apk_paths_is_empty_when_pm_fails():
    assert installed_apk_paths(FakeAdb(pm_path=(1, "", "Error")), PACKAGE) == []


def test_pull_installed_apks_replaces_stale_copies(tmp_path):
    destination = tmp_path / "original"
    destination.mkdir()
    (destination / "old_split.apk").write_bytes(b"stale")

    pulled = pull_installed_apks(FakeAdb(), PACKAGE, destination)

    assert pulled == [destination / "base.apk", destination / "split_config.en.apk"]
    assert sorted(path.name for path in destination.glob("*.apk")) == ["base.apk", "split_config.en.apk"]


def test_pull_installed_apks_reports_nothing_when_a_pull_fails(tmp_path):
    adb = FakeAdb(failing_pull="/data/app/x/split_config.en.apk")

    assert pull_installed_apks(adb, PACKAGE, tmp_path / "original") == []


def test_pull_installed_apks_pulls_nothing_when_the_app_is_missing(tmp_path):
    adb = FakeAdb(pm_path=(0, "", ""))

    assert pull_installed_apks(adb, PACKAGE, tmp_path / "original") == []
    assert not any(command[0] == "pull" for command in adb.commands)
