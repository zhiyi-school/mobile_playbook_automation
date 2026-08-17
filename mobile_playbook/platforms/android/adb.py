from __future__ import annotations

import subprocess


class AdbClient:
    DEFAULT_TIMEOUT = 30

    def __init__(self, adb_path: str = "adb", serial: str | None = None):
        self.adb_path = adb_path
        self.serial = serial

    def run(self, args: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        command = [self.adb_path]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            return 127, "", f"{self.adb_path} not found on PATH"
        except subprocess.TimeoutExpired:
            return 124, "", f"adb timed out after {timeout}s: {' '.join(command)}"
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        return result.returncode, stdout, stderr

    def is_available(self) -> bool:
        code, _, _ = self.run(["version"])
        return code == 0

    def connected_devices(self) -> list[str]:
        code, out, _ = self.run(["devices"])
        if code != 0:
            return []
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def is_device_connected(self) -> bool:
        if self.serial:
            return self.serial in self.connected_devices()
        return bool(self.connected_devices())
