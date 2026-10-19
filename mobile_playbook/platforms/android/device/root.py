"""
Finds how an Android device grants root and runs shell commands as root that way.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

ROOT_ADBD = "adbd"
SU_UID = "su_0"
SU_COMMAND = "su_c"
ROOT_PROBES = (
    (ROOT_ADBD, ["shell", "id", "-u"]),
    (SU_UID, ["shell", "su", "0", "id", "-u"]),
    (SU_COMMAND, ["shell", "su", "-c", "id -u"]),
)


@dataclass(frozen=True)
class RootShell:
    """Runs device shell commands as root through the method the device grants it with."""

    adb: AdbClient
    method: str

    # Run a shell command line as root and return its exit code, stdout and stderr.
    def run(self, command: str, timeout: float | None = AdbClient.DEFAULT_TIMEOUT) -> tuple[int, str, str]:
        if self.method == ROOT_ADBD:
            args = ["shell", command]
        elif self.method == SU_UID:
            args = ["shell", "su", "0", "sh", "-c", shlex.quote(command)]
        else:
            args = ["shell", "su", "-c", shlex.quote(command)]
        return self.adb.run(args, timeout=timeout)


# Return a root shell for the device, trying a root adbd, the su of userdebug images and Magisk-style su in turn.
def find_root_shell(adb: AdbClient) -> RootShell | None:
    for method, command in ROOT_PROBES:
        code, out, _ = adb.run(command)
        logger.debug("android root: probe %s exited %s (%r)", command, code, (out or "")[:40])
        if code == 0 and out.strip() == "0":
            return RootShell(adb, method)
    return None
