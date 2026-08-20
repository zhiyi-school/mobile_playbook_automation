from __future__ import annotations

import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def tcp_reachable(url_or_hostport: str, timeout: float = 3.0) -> bool:
    parsed = urlparse(url_or_hostport if "//" in url_or_hostport else f"//{url_or_hostport}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class AppiumStartResult:
    status: str  # "ALREADY_RUNNING" | "STARTED" | "DISABLED" | "FAILED"
    error: str = ""
    log_tail: str = ""
    log_path: Path | None = None
    process: subprocess.Popen | None = field(default=None, compare=False)


def ensure_appium_running(appium_server_url: str, auto_start_config: dict[str, Any] | None, log_path: Path) -> AppiumStartResult:
    """Start Appium if it's not reachable and `auto_start_config.enabled` is true.

    Polls reachability rather than a flat sleep, the same way this repo's
    MobSF auto_start already does. Appium's own stdout/stderr is appended to
    `log_path` (never overwritten) so a run's full Appium history — including
    every restart attempt — stays inspectable from one file, whether this
    call started it or the caller is just finding it already running.
    """
    if tcp_reachable(appium_server_url, timeout=2):
        return AppiumStartResult(status="ALREADY_RUNNING")

    auto_start = auto_start_config or {}
    if not bool(auto_start.get("enabled", False)):
        return AppiumStartResult(status="DISABLED")

    command = auto_start.get("command") or ["appium"]
    if isinstance(command, str):
        command = shlex.split(command)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n--- launching {' '.join(command)} at {datetime.now().astimezone().isoformat()} ---\n")
        log_handle.flush()
        try:
            process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)
        except OSError as exc:
            return AppiumStartResult(status="FAILED", error=f"Could not start Appium ({command[0]}): {exc}", log_path=log_path)

    wait_seconds = float(auto_start.get("wait_seconds", 60))
    poll_interval = float(auto_start.get("poll_interval_seconds", 1))
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return AppiumStartResult(
                status="FAILED",
                error=f"Appium exited early (code {process.returncode}) before becoming reachable. See {log_path}",
                log_tail=_tail(log_path),
                log_path=log_path,
            )
        if tcp_reachable(appium_server_url, timeout=2):
            return AppiumStartResult(status="STARTED", process=process, log_path=log_path)
        time.sleep(poll_interval)

    return AppiumStartResult(
        status="FAILED",
        error=f"Appium did not become reachable at {appium_server_url} within {wait_seconds:g}s. See {log_path}",
        log_tail=_tail(log_path),
        log_path=log_path,
    )


def _tail(log_path: Path, lines: int = 40) -> str:
    try:
        content = log_path.read_text()
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])
