from __future__ import annotations

from mobile_playbook.platforms.android.acquisition.apk import install_from_apk, intake_apks

PACKAGE = "com.example.app"


class FakeAdb:
    def __init__(self, installs: str | None = PACKAGE, confirm: str = "null", install_result=(0, "Success", "")):
        self.installs = installs
        self.confirm = confirm
        self.install_result = install_result
        self.installed: set[str] = set()
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:4] == ["shell", "pm", "list", "packages"] and "-i" not in args:
            return (0, f"package:{args[4]}", "") if args[4] in self.installed else (0, "", "")
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, self.confirm, ""
        if args[0] in ("install", "install-multiple"):
            if self.install_result[0] == 0 and self.installs:
                self.installed.add(self.installs)
            return self.install_result
        return 0, "", ""

    def install_commands(self):
        return [command for command in self.commands if command[0].startswith("install")]


def write_apks(intake, package=PACKAGE, names=("split_config.en.apk", "base.apk")):
    folder = intake / package
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"apk")


def install(adb, intake):
    return install_from_apk(adb, lambda: None, PACKAGE, intake / "diagnostics", intake_dir=intake)


def test_intake_apks_list_the_base_apk_first(tmp_path):
    write_apks(tmp_path, names=("split_config.xxhdpi.apk", "base.apk", "notes.txt", "split_config.en.apk"))

    assert [path.name for path in intake_apks(PACKAGE, tmp_path)] == [
        "base.apk",
        "split_config.en.apk",
        "split_config.xxhdpi.apk",
    ]


def test_the_intake_apks_are_installed_together(tmp_path):
    write_apks(tmp_path)
    adb = FakeAdb()

    outcome = install(adb, tmp_path)

    assert outcome.status == "installed"
    assert adb.install_commands() == [
        ["install-multiple", "-r", str(tmp_path / PACKAGE / "base.apk"), str(tmp_path / PACKAGE / "split_config.en.apk")]
    ]


def test_an_installed_app_is_left_alone(tmp_path):
    adb = FakeAdb()
    adb.installed.add(PACKAGE)

    outcome = install(adb, tmp_path)

    assert outcome.status == "already_installed"
    assert adb.install_commands() == []


def test_missing_apks_need_a_person(tmp_path):
    adb = FakeAdb()

    outcome = install(adb, tmp_path)

    assert outcome.blocker_code == "app_unavailable"
    assert outcome.retryable is False
    assert "artifacts/intake/android/apks/<package name>/" in outcome.detail
    assert adb.install_commands() == []


def test_a_device_that_confirms_every_adb_install_is_not_tried(tmp_path):
    write_apks(tmp_path)
    adb = FakeAdb(confirm="1")

    outcome = install(adb, tmp_path)

    assert outcome.blocker_code == "app_install_failed"
    assert outcome.retryable is False
    assert "Monitor apps installed via ADB" in outcome.detail
    assert adb.install_commands() == []


def test_a_refused_build_is_not_retried(tmp_path):
    write_apks(tmp_path)
    adb = FakeAdb(install_result=(1, "", "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]"))

    outcome = install(adb, tmp_path)

    assert outcome.blocker_code == "app_install_failed"
    assert outcome.retryable is False
    assert "INSTALL_FAILED_NO_MATCHING_ABIS" in outcome.detail


def test_an_adb_timeout_is_retried(tmp_path):
    write_apks(tmp_path)
    adb = FakeAdb(install_result=(124, "", "adb timed out after 300s"))

    outcome = install(adb, tmp_path)

    assert outcome.blocker_code == "app_install_failed"
    assert outcome.retryable is True


def test_apks_for_another_app_are_reported(tmp_path):
    write_apks(tmp_path)
    adb = FakeAdb(installs="com.example.other")

    outcome = install(adb, tmp_path)

    assert outcome.status == "failed"
    assert "installed a different app" in outcome.detail
