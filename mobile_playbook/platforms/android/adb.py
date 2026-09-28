"""
Thin subprocess wrapper around the adb command line.
"""

from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger(__name__)


class AdbClient:
    """Runs adb commands, optionally pinned to one device serial."""

    DEFAULT_TIMEOUT = 30

    # Store the adb executable and the optional device serial.
    def __init__(self, adb_path: str = "adb", serial: str | None = None):
        self.adb_path = adb_path
        self.serial = serial
        logger.debug("android adb: client created (adb_path=%s, serial=%s)", adb_path, serial)

    # Run an adb command and return its exit code, stdout and stderr, using 127 and 124 for missing adb and timeouts.
    def run(self, args: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        command = [self.adb_path]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        logger.debug("android adb: running argv=%s (timeout=%s)", command, timeout)
        started = time.monotonic()
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
            logger.debug("android adb: %s not found on PATH", self.adb_path, exc_info=True)
            return 127, "", f"{self.adb_path} not found on PATH"
        except subprocess.TimeoutExpired:
            logger.debug("android adb: argv=%s timed out after %ss", command, timeout, exc_info=True)
            return 124, "", f"adb timed out after {timeout}s: {' '.join(command)}"
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        logger.debug(
            "android adb: argv=%s exited %s in %.2fs (stdout %s chars, stderr %s chars, stderr head=%r)",
            command,
            result.returncode,
            time.monotonic() - started,
            len(stdout),
            len(stderr),
            stderr[:200],
        )
        return result.returncode, stdout, stderr

    # Report whether `adb version` succeeds.
    def is_available(self) -> bool:
        code, _, _ = self.run(["version"])
        logger.debug("android adb: available=%s (exit %s)", code == 0, code)
        return code == 0

    # Return the serials that `adb devices` lists in the ready state.
    def connected_devices(self) -> list[str]:
        code, out, _ = self.run(["devices"])
        if code != 0:
            logger.debug("android adb: 'devices' exited %s; reporting no devices", code)
            return []
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                devices.append(parts[0])
        logger.debug("android adb: %s connected devices: %s", len(devices), devices)
        return devices

    # Report whether the configured serial, or any device when none is set, is connected.
    def is_device_connected(self) -> bool:
        if self.serial:
            connected = self.serial in self.connected_devices()
            logger.debug("android adb: device %s connected=%s", self.serial, connected)
            return connected
        connected = bool(self.connected_devices())
        logger.debug("android adb: any device connected=%s", connected)
        return connected
