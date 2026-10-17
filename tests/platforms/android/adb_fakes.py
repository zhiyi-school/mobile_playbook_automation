"""
A synthetic adb server for tests: per-serial states, scripted state sequences, boot progress and build types.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class FakeDevice:
    state: str = "absent"
    connects_to: str = "device"
    build_type: str = "user"
    boots_after: int = 0
    script: list[str] = field(default_factory=list)
    root_uid: bool = False
    packages: dict[str, tuple[str, str]] = field(default_factory=dict)


class FakeAdbHost:
    def __init__(self):
        self.devices: dict[str, FakeDevice] = {}
        self.commands: list[tuple[str | None, list[str]]] = []

    def add(self, serial: str, **settings) -> FakeDevice:
        self.devices[serial] = FakeDevice(**settings)
        return self.devices[serial]

    def sent(self, serial: str | None, *args: str) -> int:
        return self.commands.count((serial, list(args)))

    def run(self, argv, **kwargs):
        args = list(argv[1:])
        serial = None
        if args[:1] == ["-s"]:
            serial, args = args[1], args[2:]
        self.commands.append((serial, args))
        return subprocess.CompletedProcess(argv, *self._answer(serial, args))

    def _answer(self, serial, args):
        if args == ["devices"]:
            rows = []
            for name, device in self.devices.items():
                if device.script:
                    device.state = device.script.pop(0)
                if device.state != "absent":
                    rows.append(f"{name}\t{device.state}")
            return 0, "List of devices attached\n" + "\n".join(rows) + "\n", ""
        if args[:1] == ["connect"]:
            device = self.devices.get(args[1])
            if device is None or device.connects_to == "absent":
                return 1, f"failed to connect to {args[1]}", ""
            device.state = device.connects_to
            return 0, f"connected to {args[1]}", ""
        if args[:1] == ["disconnect"]:
            if args[1] in self.devices:
                self.devices[args[1]].state = "absent"
            return 0, f"disconnected {args[1]}", ""
        device = self.devices.get(serial)
        if device is None or device.state != "device":
            return 1, "", f"device '{serial}' not found"
        if args == ["shell", "getprop", "sys.boot_completed"]:
            booted = device.boots_after <= 0
            device.boots_after -= 1
            return 0, "1\n" if booted else "\n", ""
        if args == ["shell", "getprop", "ro.build.type"]:
            return 0, device.build_type + "\n", ""
        if args == ["shell", "id", "-u"]:
            return 0, "0\n" if device.root_uid else "2000\n", ""
        if args[:3] == ["shell", "dumpsys", "package"]:
            version = device.packages.get(args[3])
            if version is None:
                return 0, "", ""
            return 0, f"    versionCode={version[1]} minSdk=24\n    versionName={version[0]}\n", ""
        return 0, "", ""
