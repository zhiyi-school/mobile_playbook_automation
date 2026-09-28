"""
Checks, auto-starts and stops the local Appium server process.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Split a URL or host:port into a host and port, defaulting to 127.0.0.1 and the scheme's port.
def _host_port(url_or_hostport: str) -> tuple[str, int]:
    parsed = urlparse(url_or_hostport if "//" in url_or_hostport else f"//{url_or_hostport}")
    return parsed.hostname or "127.0.0.1", parsed.port or (443 if parsed.scheme == "https" else 80)


# Report whether a TCP connection to the URL's host and port succeeds within the timeout.
def tcp_reachable(url_or_hostport: str, timeout: float = 3.0) -> bool:
    host, port = _host_port(url_or_hostport)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            logger.debug("appium: tcp %s:%s reachable (timeout=%s)", host, port, timeout)
            return True
    except OSError as exc:
        logger.debug("appium: tcp %s:%s not reachable (timeout=%s): %s", host, port, timeout, exc)
        return False


@dataclass(frozen=True)
class AppiumStartResult:
    status: str  # "ALREADY_RUNNING" | "STARTED" | "DISABLED" | "FAILED"
    error: str = ""
    log_tail: str = ""
    log_path: Path | None = None
    process: subprocess.Popen | None = field(default=None, compare=False)


# Start Appium when unreachable and auto-start is enabled, appending its output to log_path and polling until it is up.
def ensure_appium_running(appium_server_url: str, auto_start_config: dict[str, Any] | None, log_path: Path) -> AppiumStartResult:
    logger.debug("appium: ensuring server at %s is running (log_path=%s)", appium_server_url, log_path)
    if tcp_reachable(appium_server_url, timeout=2):
        logger.debug("appium: server at %s already reachable; not starting", appium_server_url)
        return AppiumStartResult(status="ALREADY_RUNNING")

    auto_start = auto_start_config or {}
    if not bool(auto_start.get("enabled", False)):
        logger.debug("appium: server at %s unreachable and auto_start disabled (config keys=%s)", appium_server_url, list(auto_start))
        return AppiumStartResult(status="DISABLED")

    command = auto_start.get("command") or ["appium"]
    if isinstance(command, str):
        command = shlex.split(command)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n--- launching {' '.join(command)} at {datetime.now().astimezone().isoformat()} ---\n")
        log_handle.flush()
        logger.debug("appium: launching argv=%s (output appended to %s)", command, log_path)
        try:
            process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            logger.debug("appium: failed to launch %s: %s", command[0], exc, exc_info=True)
            return AppiumStartResult(status="FAILED", error=f"Could not start Appium ({command[0]}): {exc}", log_path=log_path)
    logger.debug("appium: launched pid=%s", getattr(process, "pid", None))

    wait_seconds = float(auto_start.get("wait_seconds", 60))
    poll_interval = float(auto_start.get("poll_interval_seconds", 1))
    started = time.monotonic()
    deadline = started + wait_seconds
    logger.debug("appium: waiting up to %ss for %s (poll_interval=%ss)", wait_seconds, appium_server_url, poll_interval)
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        if process.poll() is not None:
            logger.debug("appium: pid=%s exited early with code %s after %.2fs (%s polls)", getattr(process, "pid", None), getattr(process, "returncode", None), time.monotonic() - started, polls)
            return AppiumStartResult(
                status="FAILED",
                error=f"Appium exited early (code {process.returncode}) before becoming reachable. See {log_path}",
                log_tail=_tail(log_path),
                log_path=log_path,
            )
        if tcp_reachable(appium_server_url, timeout=2):
            logger.debug("appium: pid=%s reachable at %s after %.2fs (%s polls)", getattr(process, "pid", None), appium_server_url, time.monotonic() - started, polls)
            return AppiumStartResult(status="STARTED", process=process, log_path=log_path)
        time.sleep(poll_interval)

    logger.debug("appium: pid=%s not reachable at %s within %ss (%s polls)", getattr(process, "pid", None), appium_server_url, wait_seconds, polls)
    return AppiumStartResult(
        status="FAILED",
        error=f"Appium did not become reachable at {appium_server_url} within {wait_seconds:g}s. See {log_path}",
        log_tail=_tail(log_path),
        log_path=log_path,
    )


# Terminate the Appium process group, escalating to SIGKILL when it does not exit in time.
def stop_appium(process: subprocess.Popen, timeout: float = 10) -> None:
    if process.poll() is not None:
        logger.debug("appium: pid=%s already exited with code %s; nothing to stop", getattr(process, "pid", None), getattr(process, "returncode", None))
        return

    logger.debug("appium: sending SIGTERM to process group of pid=%s (timeout=%ss)", getattr(process, "pid", None), timeout)
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.debug("appium: pid=%s did not exit after SIGTERM; sending SIGKILL: %s", getattr(process, "pid", None), exc, exc_info=True)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)
    logger.debug("appium: pid=%s stopped with code %s", getattr(process, "pid", None), getattr(process, "returncode", None))


# Return the last lines of a log file, or an empty string when it cannot be read.
def _tail(log_path: Path, lines: int = 40) -> str:
    try:
        content = log_path.read_text()
    except OSError as exc:
        logger.debug("appium: could not read log tail from %s: %s", log_path, exc, exc_info=True)
        return ""
    return "\n".join(content.splitlines()[-lines:])
