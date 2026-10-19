from __future__ import annotations

import shlex

import pytest

from mobile_playbook.platforms.android.device.root import ROOT_ADBD, SU_COMMAND, SU_UID, find_root_shell


class ProbeAdb:
    def __init__(self, granting: str | None):
        self.granting = granting
        self.calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        probes = {
            ROOT_ADBD: ["shell", "id", "-u"],
            SU_UID: ["shell", "su", "0", "id", "-u"],
            SU_COMMAND: ["shell", "su", "-c", "id -u"],
        }
        if args in probes.values():
            return (0, "0\n", "") if probes.get(self.granting) == args else (0, "2000\n", "")
        return 0, "ok", ""


@pytest.mark.parametrize(
    ("granting", "expected_args"),
    [
        (ROOT_ADBD, ["shell", "ls -la '/data/data/com.example.app'"]),
        (SU_UID, ["shell", "su", "0", "sh", "-c", shlex.quote("ls -la '/data/data/com.example.app'")]),
        (SU_COMMAND, ["shell", "su", "-c", shlex.quote("ls -la '/data/data/com.example.app'")]),
    ],
)
def test_commands_run_as_root_through_the_method_the_device_grants(granting, expected_args):
    adb = ProbeAdb(granting)

    root = find_root_shell(adb)
    root.run("ls -la '/data/data/com.example.app'")

    assert root.method == granting
    assert adb.calls[-1] == expected_args


def test_a_device_without_root_has_no_root_shell():
    adb = ProbeAdb(None)

    assert find_root_shell(adb) is None
    assert len(adb.calls) == 3
