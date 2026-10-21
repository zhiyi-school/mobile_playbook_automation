"""
Captures the device log with `adb logcat` while an app runs, and keeps only the lines that app wrote, as Android
Studio's Logcat window does when filtered by package.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

THREADTIME = ["logcat", "-v", "threadtime"]
UID_ADVERB = ["-v", "uid"]
# Seconds a new capture is given to fail before it counts as started; logcat rejects an unknown -v at once.
START_CHECK_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 5.0
# threadtime prints "MM-DD hh:mm:ss.mmm  PID  TID L Tag: message"; with the uid adverb the UID, or a short user name,
# comes before the PID, and a blank field takes its place when logd does not know it.
LINE_RE = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(?:(?P<uid>[^\s:]+):?\s+)?(?P<pid>\d+)\s+\d+\s+[VDIWEFS]\s")
APP_UID_RE_TEMPLATE = r"Package \[{package}\][^\n]*\n(?:(?![ \t]*Package \[)[ \t]+[^\n]*\n)*?[ \t]+userId=(\d+)"


class LogcatError(RuntimeError):
    """The device log could not be captured."""


@dataclass
class LogcatCapture:
    """A running `adb logcat` writing every buffer line to a file in the work directory."""

    process: subprocess.Popen
    path: Path
    with_uid: bool

    # Report whether logcat is still streaming; it stops early when the device disconnects.
    def running(self) -> bool:
        return self.process.poll() is None

    # Stop logcat, killing it when it does not exit in time, and report whether it was still streaming until now.
    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> bool:
        was_running = self.running()
        if was_running:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.debug("android logcat: %s did not exit in %ss; killing it", self.path, timeout)
                self.process.kill()
                self.process.wait(timeout=timeout)
        logger.debug("android logcat: capture %s stopped (was_running=%s, exit %s)", self.path, was_running, self.process.returncode)
        return was_running


# Clear the device's log buffers so a capture holds only what follows, and return None or why they were not cleared.
def clear_logcat(adb: AdbClient) -> str | None:
    code, out, err = adb.run(["logcat", "-c"])
    if code == 0:
        return None
    detail = (err or out or f"logcat -c exited {code}").strip()[:500]
    logger.debug("android logcat: clearing the buffers failed: %s", detail)
    return detail


# Start streaming the device log to path, with each line's UID where logcat supports the uid adverb (Android 7 and
# later) and without it otherwise.
def start_logcat(adb: AdbClient, path: Path, start_check_seconds: float = START_CHECK_SECONDS) -> LogcatCapture:
    failures = []
    for with_uid in (True, False):
        path.write_bytes(b"")
        try:
            process = adb.start_to_file(THREADTIME + (UID_ADVERB if with_uid else []), path)
        except FileNotFoundError:
            raise LogcatError(f"{adb.adb_path} not found on PATH") from None
        time.sleep(start_check_seconds)
        if process.poll() is None:
            logger.debug("android logcat: capturing to %s (with_uid=%s)", path, with_uid)
            return LogcatCapture(process, path, with_uid)
        output = path.read_text(encoding="utf-8", errors="replace").strip()[:500]
        failures.append(f"logcat{' -v uid' if with_uid else ''} exited {process.returncode}: {output or 'no output'}")
        logger.debug("android logcat: %s", failures[-1])
    raise LogcatError("; ".join(failures))


# Return the app's Linux UID from `dumpsys package`, or None when the device does not report it.
def app_uid(adb: AdbClient, package: str) -> int | None:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    match = re.search(APP_UID_RE_TEMPLATE.format(package=re.escape(package)), out or "") if code == 0 else None
    logger.debug("android logcat: %s uid=%s (dumpsys exit %s)", package, match.group(1) if match else None, code)
    return int(match.group(1)) if match else None


# Return the PIDs of the app's processes, including named ones such as "<package>:remote", from ps.
def app_pids(adb: AdbClient, package: str) -> set[int]:
    # Toybox ps (Android 8 and later) needs -A to list every process; the older toolbox ps lists them all without it
    # and reads -A as a name filter that matches nothing.
    for args in (["shell", "ps", "-A"], ["shell", "ps"]):
        code, out, _ = adb.run(args)
        rows = (out or "").splitlines()[1:] if code == 0 else []
        if rows:
            pids = {int(cols[1]) for cols in (row.split() for row in rows) if len(cols) >= 3 and cols[1].isdigit() and (cols[-1] == package or cols[-1].startswith(f"{package}:"))}
            logger.debug("android logcat: %s pids=%s from %s", package, sorted(pids), " ".join(args[1:]))
            return pids
    return set()


# Return the captured lines the app wrote: by UID where a line carries one and the app's UID is known, otherwise by
# the PIDs its processes were seen with.
def app_lines(lines: Iterable[str], uid: int | None, pids: set[int]) -> list[str]:
    kept = []
    for line in lines:
        match = LINE_RE.match(line)
        if match is None:
            continue
        line_uid = match.group("uid")
        if line_uid is not None and uid is not None:
            if line_uid == str(uid):
                kept.append(line.rstrip("\n"))
        elif int(match.group("pid")) in pids:
            kept.append(line.rstrip("\n"))
    return kept


# Read a capture file and return the lines the app wrote, as app_lines() does.
def read_app_lines(path: Path, uid: int | None, pids: set[int]) -> list[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return app_lines(handle, uid, pids)
