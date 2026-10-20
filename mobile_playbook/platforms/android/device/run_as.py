"""
Runs shell commands as a debuggable app's own user through `run-as`, which works over USB debugging without root.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import ClassVar

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)


class RunAsUnavailable(RuntimeError):
    """The device refuses to run commands as the app's user, usually because the app is not debuggable."""


@dataclass(frozen=True)
class RunAsShell:
    """Runs device shell commands as the app's user with `run-as <package>`."""

    adb: AdbClient
    package: str
    access: ClassVar[str] = "with run-as"

    # Run a shell command line as the app's user and return its exit code, stdout and stderr.
    def run(self, command: str, timeout: float | None = AdbClient.DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        return self.adb.run(["shell", "run-as", self.package, "sh", "-c", shlex.quote(command)], timeout=timeout)


# Return a run-as shell for the package, or raise RunAsUnavailable with the device's refusal.
def run_as_shell(adb: AdbClient, package: str) -> RunAsShell:
    code, out, err = adb.run(["shell", "run-as", package, "id", "-u"])
    # run-as reports its own refusal ("run-as: package not debuggable: ...") on stderr or stdout depending on the device.
    refusal = next((line.strip() for line in f"{err or ''}\n{out or ''}".splitlines() if line.strip().startswith("run-as:")), None)
    logger.debug("android run-as: probe for %s exited %s (%r) refusal=%r", package, code, (out or "")[:40], refusal)
    if code != 0 or refusal or not out.strip().isdigit():
        raise RunAsUnavailable(refusal or (err or out or "").strip()[:500] or f"run-as exited {code}")
    return RunAsShell(adb, package)
