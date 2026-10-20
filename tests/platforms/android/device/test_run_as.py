from __future__ import annotations

import shlex

import pytest

from mobile_playbook.platforms.android.device.run_as import RunAsShell, RunAsUnavailable, run_as_shell

PACKAGE = "com.example.app"


class ProbeAdb:
    def __init__(self, result):
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        return self.result


def test_a_debuggable_app_gives_a_shell_that_runs_commands_as_its_user():
    adb = ProbeAdb((0, "10234\n", ""))

    shell = run_as_shell(adb, PACKAGE)
    shell.run("ls -laR '/data/data/com.example.app'")

    assert shell == RunAsShell(adb, PACKAGE)
    assert shell.access == "with run-as"
    assert adb.calls == [
        ["shell", "run-as", PACKAGE, "id", "-u"],
        ["shell", "run-as", PACKAGE, "sh", "-c", shlex.quote("ls -laR '/data/data/com.example.app'")],
    ]


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        ((1, "", f"run-as: package not debuggable: {PACKAGE}"), f"run-as: package not debuggable: {PACKAGE}"),
        ((0, f"run-as: package not debuggable: {PACKAGE}", ""), f"run-as: package not debuggable: {PACKAGE}"),
        ((1, "", f"run-as: unknown package: {PACKAGE}"), f"run-as: unknown package: {PACKAGE}"),
        ((255, "", "error: no devices/emulators found"), "error: no devices/emulators found"),
        ((1, "", ""), "run-as exited 1"),
    ],
    ids=["refused-on-stderr", "refused-on-stdout", "unknown-package", "no-device", "no-output"],
)
def test_a_refusal_raises_with_the_devices_reason(result, detail):
    with pytest.raises(RunAsUnavailable) as raised:
        run_as_shell(ProbeAdb(result), PACKAGE)

    assert str(raised.value) == detail
