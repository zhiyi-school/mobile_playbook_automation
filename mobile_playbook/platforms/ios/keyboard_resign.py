"""
Detects expired keyboard IPA signatures and resigns them with the bundled resign tool.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.storage.paths import REPOSITORY_ROOT, resolve_under_repository

RESIGN_SCRIPT = "tools/companion_apps/localkeyboard_resign/resign.py"
VERIFICATION_MARKERS = ("ApplicationVerificationFailed", "Failed to verify code signature")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResignResult:
    status: str
    errors: list[str] = field(default_factory=list)


# Return the ExpirationDate of every embedded provisioning profile in an IPA, as UTC datetimes.
def _expiry_dates(ipa_path: Path) -> list[datetime]:
    dates: list[datetime] = []
    with zipfile.ZipFile(ipa_path) as zf:
        for name in zf.namelist():
            if not name.endswith("embedded.mobileprovision"):
                continue
            logger.debug("ios keyboard resign: decoding %s from %s with security cms", name, ipa_path)
            with tempfile.NamedTemporaryFile(suffix=".mobileprovision") as handle:
                handle.write(zf.read(name))
                handle.flush()
                decoded = subprocess.run(
                    ["security", "cms", "-D", "-i", handle.name], capture_output=True
                )
            if decoded.returncode != 0:
                logger.debug("ios keyboard resign: security cms exited %s for %s; skipping", decoded.returncode, name)
                continue
            expiry = plistlib.loads(decoded.stdout).get("ExpirationDate")
            logger.debug("ios keyboard resign: %s ExpirationDate %s", name, expiry)
            if isinstance(expiry, datetime):
                dates.append(expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc))
    logger.debug("ios keyboard resign: %s has %s profile expiry dates", ipa_path, len(dates))
    return dates


# Return whether the earliest profile expiry falls within the margin; no profile counts as unexpired.
def signature_expired(ipa_path: Path, margin_seconds: int = 3600) -> bool:
    dates = _expiry_dates(ipa_path)
    if not dates:
        logger.debug("ios keyboard resign: no embedded profile expiry in %s; treating as not expired", ipa_path)
        return False
    remaining = (min(dates) - datetime.now(timezone.utc)).total_seconds()
    logger.debug(
        "ios keyboard resign: earliest expiry %s, %.0fs remaining, margin %ss -> expired=%s",
        min(dates),
        remaining,
        margin_seconds,
        remaining <= margin_seconds,
    )
    return remaining <= margin_seconds


# Return whether install errors contain a code-signature verification failure marker.
def is_verification_failure(errors: list[str]) -> bool:
    joined = " ".join(errors)
    matched = [marker for marker in VERIFICATION_MARKERS if marker in joined]
    logger.debug("ios keyboard resign: verification failure markers found: %s", matched)
    return bool(matched)


# Run the resign script for an IPA and device, mapping a missing script, timeout or failure to a status.
def resign(ipa_path: Path, udid: str, team_id: str, timeout_seconds: int = 900) -> ResignResult:
    script = resolve_under_repository(RESIGN_SCRIPT)
    if not script.exists():
        logger.debug("ios keyboard resign: resign script %s not found", script)
        return ResignResult(status="RESIGN_UNAVAILABLE", errors=[f"{script} not found"])
    command = [
        sys.executable, str(script),
        "--ipa", str(ipa_path),
        "--udid", udid,
        "--team-id", team_id,
    ]
    logger.debug("ios keyboard resign: running %s (cwd=%s, timeout=%ss)", command, REPOSITORY_ROOT, timeout_seconds)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        logger.debug("ios keyboard resign: resign.py timed out after %.2fs: %s", time.monotonic() - started, exc, exc_info=True)
        return ResignResult(status="RESIGN_TIMED_OUT", errors=[f"resign.py exceeded {timeout_seconds}s"])
    logger.debug(
        "ios keyboard resign: resign.py exited %s in %.2fs (stdout %s chars, stderr %s chars)",
        completed.returncode,
        time.monotonic() - started,
        len(completed.stdout or ""),
        len(completed.stderr or ""),
    )
    if completed.returncode != 0:
        logger.debug("ios keyboard resign: resign.py output head: %s", (completed.stderr.strip() or completed.stdout.strip())[:200])
        return ResignResult(status="RESIGN_FAILED", errors=[completed.stderr.strip() or completed.stdout.strip()])
    return ResignResult(status="RESIGNED")
