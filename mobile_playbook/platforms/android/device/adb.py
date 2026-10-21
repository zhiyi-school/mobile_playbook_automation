"""
Thin subprocess wrapper around the adb command line.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Commands the adb host answers itself; they never target a device, so they skip resolving `adb_serial: wifi`.
HOST_COMMANDS = frozenset({"version", "devices", "connect", "disconnect", "start-server", "kill-server", "mdns"})


class AdbClient:
    """Runs adb commands, optionally pinned to one device serial."""

    DEFAULT_TIMEOUT = 30

    # Store the adb executable and the optional device serial.
    def __init__(self, adb_path: str = "adb", serial: str | None = None):
        self.adb_path = adb_path
        self.serial = serial
        self._wifi_serial: str | None = None
        self._wifi_resolved = False
        logger.debug("android adb: client created (adb_path=%s, serial=%s)", adb_path, serial)

    # Run an adb command and return its exit code, stdout and stderr, using 127 and 124 for missing adb and timeouts.
    def run(self, args: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        command = self._command(args)
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

    # Run an adb command with its stdout written unchanged to a file and return its exit code and stderr, using 127 and
    # 124 for missing adb and timeouts as run() does.
    def run_to_file(self, args: list[str], destination: Path, timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str]:
        command = self._command(args)
        logger.debug("android adb: streaming argv=%s to %s (timeout=%s)", command, destination, timeout)
        started = time.monotonic()
        try:
            with destination.open("wb") as handle:
                result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, timeout=timeout)
        except FileNotFoundError:
            logger.debug("android adb: %s not found on PATH", self.adb_path, exc_info=True)
            return 127, f"{self.adb_path} not found on PATH"
        except subprocess.TimeoutExpired:
            logger.debug("android adb: argv=%s timed out after %ss", command, timeout, exc_info=True)
            return 124, f"adb timed out after {timeout}s: {' '.join(command)}"
        stderr = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else ""
        logger.debug("android adb: argv=%s exited %s in %.2fs (%s bytes written, stderr head=%r)", command, result.returncode, time.monotonic() - started, destination.stat().st_size, stderr[:200])
        return result.returncode, stderr

    # Start an adb command in the background with its stdout and stderr appended to a file, and return the process; a
    # missing adb raises FileNotFoundError.
    def start_to_file(self, args: list[str], destination: Path) -> subprocess.Popen:
        command = self._command(args)
        logger.debug("android adb: starting argv=%s in the background, output to %s", command, destination)
        # The child keeps its own copy of the descriptor, so the file can be closed here.
        with destination.open("ab") as handle:
            return subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    # Return the adb argv for a command, adding the target serial unless the adb host answers the command itself.
    def _command(self, args: list[str]) -> list[str]:
        command = [self.adb_path]
        serial = self.serial if args[:1] and args[0] in HOST_COMMANDS else self.target_serial()
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        return command

    # Return the serial device commands use, resolving `wifi` to the phone's Wi-Fi connection once per client.
    def target_serial(self) -> str | None:
        from mobile_playbook.platforms.android.device.wifi import WIFI_SERIAL, connect_over_wifi

        if self.serial != WIFI_SERIAL:
            return self.serial
        if not self._wifi_resolved:
            self._wifi_serial = connect_over_wifi(self.adb_path)
            self._wifi_resolved = True
            logger.debug("android adb: resolved adb_serial wifi to %s", self._wifi_serial)
        # Unresolved, commands keep targeting "wifi" so they fail as no device instead of reaching a USB connection.
        return self._wifi_serial or self.serial

    # Report whether `adb version` succeeds.
    def is_available(self) -> bool:
        code, _, _ = self.run(["version"])
        logger.debug("android adb: available=%s (exit %s)", code == 0, code)
        return code == 0

    # Return each serial `adb devices` lists with its state, such as device, unauthorized or offline.
    def device_states(self) -> dict[str, str]:
        code, out, _ = self.run(["devices"])
        if code != 0:
            logger.debug("android adb: 'devices' exited %s; reporting no devices", code)
            return {}
        states = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = " ".join(parts[1:])
        logger.debug("android adb: device states %s", states)
        return states

    # Return the serials that `adb devices` lists in the ready state.
    def connected_devices(self) -> list[str]:
        devices = [serial for serial, state in self.device_states().items() if state == "device"]
        logger.debug("android adb: %s connected devices: %s", len(devices), devices)
        return devices

    # Report whether the configured serial, or any device when none is set, is connected.
    def is_device_connected(self) -> bool:
        serial = self.target_serial()
        if serial:
            connected = serial in self.connected_devices()
            logger.debug("android adb: device %s connected=%s", serial, connected)
            return connected
        connected = bool(self.connected_devices())
        logger.debug("android adb: any device connected=%s", connected)
        return connected
