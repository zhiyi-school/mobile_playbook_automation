from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device.install import (
    PUSH_BASE_TIMEOUT_SECONDS,
    PUSH_MIN_BYTES_PER_SECOND,
    STAGING_DIR,
    install_apks,
)


class FakeAdb:
    def __init__(self, install_result=(0, "Success", ""), push_results=None, remote_size=None):
        self.install_result = install_result
        self.push_results = list(push_results or [])
        self.remote_size = remote_size
        self.sizes: dict[str, int] = {}
        self.commands: list[list[str]] = []
        self.timeouts: dict[str, list[float | None]] = {}

    def run(self, args, timeout=None):
        args = list(args)
        self.commands.append(args)
        if args[0] == "push":
            self.timeouts.setdefault("push", []).append(timeout)
            result = self.push_results.pop(0) if self.push_results else (0, "1 file pushed", "")
            if result[0] == 0:
                self.sizes[args[2]] = len(open(args[1], "rb").read())
            return result
        if args[:2] == ["shell", "stat"]:
            size = self.remote_size if self.remote_size is not None else self.sizes.get(args[-1])
            return (0, f"{size}\n", "") if size is not None else (1, "", "No such file")
        if args[:3] == ["shell", "pm", "install"]:
            self.timeouts.setdefault("install", []).append(timeout)
            return self.install_result
        return 0, "", ""

    def installs(self):
        return [command for command in self.commands if command[:3] == ["shell", "pm", "install"]]

    def pushes(self):
        return [command for command in self.commands if command[0] == "push"]


def apk(tmp_path, name, size=10):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


def test_a_single_apk_is_copied_then_installed_with_permissions_granted(tmp_path):
    adb = FakeAdb()
    app = apk(tmp_path, "app.apk")

    assert install_apks(adb, [app], grant_permissions=True) == (True, "")

    assert adb.commands == [
        ["shell", f"rm -rf {STAGING_DIR} && mkdir -p {STAGING_DIR}"],
        ["push", str(app), f"{STAGING_DIR}/0.apk"],
        ["shell", "stat", "-c", "%s", f"{STAGING_DIR}/0.apk"],
        ["shell", "pm", "install", "-r", "-g", f"{STAGING_DIR}/0.apk"],
        ["shell", "rm", "-rf", STAGING_DIR],
    ]


def test_split_apks_are_installed_together_without_granting_permissions(tmp_path):
    adb = FakeAdb()

    install_apks(adb, [apk(tmp_path, "base.apk"), apk(tmp_path, "split_config.en.apk")], grant_permissions=False)

    assert adb.installs() == [["shell", "pm", "install", "-r", f"{STAGING_DIR}/0.apk", f"{STAGING_DIR}/1.apk"]]


def test_an_installer_is_recorded_when_given(tmp_path):
    adb = FakeAdb()

    install_apks(adb, [apk(tmp_path, "base.apk")], grant_permissions=True, installer="com.android.vending")

    assert adb.installs() == [["shell", "pm", "install", "-r", "-g", "-i", "com.android.vending", f"{STAGING_DIR}/0.apk"]]


def test_the_install_timeout_bounds_only_the_package_manager_and_the_copy_timeout_grows_with_size(tmp_path):
    adb = FakeAdb()
    size = 5 * PUSH_MIN_BYTES_PER_SECOND

    install_apks(adb, [apk(tmp_path, "app.apk", size=size)], grant_permissions=False, timeout=42)

    assert adb.timeouts == {"push": [PUSH_BASE_TIMEOUT_SECONDS + 5], "install": [42]}


def test_a_copy_that_times_out_is_retried(tmp_path):
    adb = FakeAdb(push_results=[(124, "", "adb timed out after 61s: adb push")])

    assert install_apks(adb, [apk(tmp_path, "app.apk")], grant_permissions=False) == (True, "")
    assert len(adb.pushes()) == 2


def test_a_copy_that_keeps_failing_is_reported_and_nothing_is_installed(tmp_path):
    adb = FakeAdb(push_results=[(124, "", "adb timed out after 61s: adb push")] * 2)

    assert install_apks(adb, [apk(tmp_path, "app.apk")], grant_permissions=False) == (
        False,
        "Could not copy app.apk to the device: adb timed out after 61s: adb push",
    )
    assert adb.installs() == []
    assert adb.commands[-1] == ["shell", "rm", "-rf", STAGING_DIR]


def test_a_partial_copy_is_not_installed(tmp_path):
    adb = FakeAdb(remote_size=4)

    assert install_apks(adb, [apk(tmp_path, "app.apk", size=10)], grant_permissions=False) == (
        False,
        "Could not copy app.apk to the device: the device holds 4 of 10 bytes",
    )
    assert adb.installs() == []


@pytest.mark.parametrize(
    "result",
    [(0, "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]", ""), (1, "", "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]")],
)
def test_the_package_managers_failure_line_is_returned(tmp_path, result):
    adb = FakeAdb(install_result=result)

    assert install_apks(adb, [apk(tmp_path, "app.apk")], grant_permissions=False) == (
        False,
        "Failure [INSTALL_FAILED_NO_MATCHING_ABIS]",
    )
    assert adb.commands[-1] == ["shell", "rm", "-rf", STAGING_DIR]


def test_an_install_that_times_out_returns_adbs_reason(tmp_path):
    adb = FakeAdb(install_result=(124, "", "adb timed out after 180s: adb shell pm install"))

    assert install_apks(adb, [apk(tmp_path, "app.apk")], grant_permissions=False) == (
        False,
        "adb timed out after 180s: adb shell pm install",
    )


def test_no_apks_never_reaches_the_device():
    adb = FakeAdb()

    assert install_apks(adb, [], grant_permissions=False) == (False, "No APKs to install")
    assert adb.commands == []
