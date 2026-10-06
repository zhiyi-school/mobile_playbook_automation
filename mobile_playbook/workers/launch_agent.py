"""
Starts and stops the assessment worker's macOS launch agent so the worker runs alongside the API.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from mobile_playbook.orchestration.appium_server import tcp_reachable

logger = logging.getLogger(__name__)

WORKER_AGENT_LABEL = "com.mobile-playbook.assessment-worker"
LAUNCHCTL_TIMEOUT_SECONDS = 30.0
LISTEN_WAIT_SECONDS = 60.0
LISTEN_POLL_SECONDS = 0.5


# Return the installed plist of the worker's launch agent.
def agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{WORKER_AGENT_LABEL}.plist"


# Return the launchd domain of the current user's GUI session.
def _domain() -> str:
    return f"gui/{os.getuid()}"


# Run one launchctl command and return its exit code and combined output.
def _launchctl(*args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("worker agent: launchctl %s failed to run", args[0], exc_info=True)
        return 1, str(exc)
    output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
    logger.debug("worker agent: launchctl %s exited %s", " ".join(args), completed.returncode)
    return completed.returncode, output


# Report whether this host can manage the worker: macOS with launchctl and the agent's plist installed.
def worker_agent_installed() -> bool:
    return sys.platform == "darwin" and shutil.which("launchctl") is not None and agent_plist().is_file()


# Load the agent if needed and restart the worker so it runs the current code; returns a short status.
def start_worker_agent() -> str:
    if not worker_agent_installed():
        logger.info("Assessment worker launch agent is not installed; start the worker separately.")
        return "not_installed"
    service = f"{_domain()}/{WORKER_AGENT_LABEL}"
    if _launchctl("print", service)[0] != 0:
        code, output = _launchctl("bootstrap", _domain(), str(agent_plist()))
        if code != 0:
            logger.warning("Could not load the assessment worker launch agent: %s", output or f"exit {code}")
            return "failed"
    # -k stops a worker left running from an earlier start, so the new one runs the current code.
    code, output = _launchctl("kickstart", "-k", service)
    if code != 0:
        logger.warning("Could not start the assessment worker: %s", output or f"exit {code}")
        return "failed"
    logger.info("Started the assessment worker (%s).", WORKER_AGENT_LABEL)
    return "started"


# Unload the agent, stopping the worker; its every-five-minute schedule would otherwise restart it.
def stop_worker_agent() -> str:
    if not worker_agent_installed():
        return "not_installed"
    service = f"{_domain()}/{WORKER_AGENT_LABEL}"
    if _launchctl("print", service)[0] != 0:
        logger.debug("worker agent: %s is not loaded; nothing to stop", service)
        return "not_running"
    code, output = _launchctl("bootout", service)
    if code != 0:
        logger.warning("Could not stop the assessment worker: %s", output or f"exit {code}")
        return "failed"
    logger.info("Stopped the assessment worker (%s).", WORKER_AGENT_LABEL)
    return "stopped"


# Start the worker on a background thread once the API accepts connections, unless `cancelled` is set first.
def start_worker_agent_when_listening(api_url: str, cancelled: threading.Event) -> threading.Thread:
    # Starting before the API listens would send the worker's first readiness check into a backoff.
    def wait_then_start() -> None:
        deadline = time.monotonic() + LISTEN_WAIT_SECONDS
        while not cancelled.is_set():
            if tcp_reachable(api_url, timeout=1):
                start_worker_agent()
                return
            if time.monotonic() >= deadline:
                logger.warning("The API did not start listening on %s; the assessment worker was not started.", api_url)
                return
            cancelled.wait(LISTEN_POLL_SECONDS)

    thread = threading.Thread(target=wait_then_start, name="start-assessment-worker", daemon=True)
    thread.start()
    return thread
