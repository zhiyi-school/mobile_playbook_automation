from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.storage import resolve_under_repository

RESIGN_SCRIPT = "tools/localkeyboard_resign/resign.py"
VERIFICATION_MARKERS = ("ApplicationVerificationFailed", "Failed to verify code signature")


@dataclass(frozen=True)
class ResignResult:
    status: str
    errors: list[str] = field(default_factory=list)


def _expiry_dates(ipa_path: Path) -> list[datetime]:
    dates: list[datetime] = []
    with zipfile.ZipFile(ipa_path) as zf:
        for name in zf.namelist():
            if not name.endswith("embedded.mobileprovision"):
                continue
            with tempfile.NamedTemporaryFile(suffix=".mobileprovision") as handle:
                handle.write(zf.read(name))
                handle.flush()
                decoded = subprocess.run(
                    ["security", "cms", "-D", "-i", handle.name], capture_output=True
                )
            if decoded.returncode != 0:
                continue
            expiry = plistlib.loads(decoded.stdout).get("ExpirationDate")
            if isinstance(expiry, datetime):
                dates.append(expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc))
    return dates


def signature_expired(ipa_path: Path, margin_seconds: int = 3600) -> bool:
    dates = _expiry_dates(ipa_path)
    if not dates:
        return False
    return (min(dates) - datetime.now(timezone.utc)).total_seconds() <= margin_seconds


def is_verification_failure(errors: list[str]) -> bool:
    joined = " ".join(errors)
    return any(marker in joined for marker in VERIFICATION_MARKERS)


def resign(ipa_path: Path, udid: str, team_id: str, timeout_seconds: int = 900) -> ResignResult:
    script = resolve_under_repository(RESIGN_SCRIPT)
    if not script.exists():
        return ResignResult(status="RESIGN_UNAVAILABLE", errors=[f"{script} not found"])
    command = [
        sys.executable, str(script),
        "--ipa", str(ipa_path),
        "--udid", udid,
        "--team-id", team_id,
    ]
    try:
        completed = subprocess.run(
            command, cwd=script.parents[2], capture_output=True, text=True, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired:
        return ResignResult(status="RESIGN_TIMED_OUT", errors=[f"resign.py exceeded {timeout_seconds}s"])
    if completed.returncode != 0:
        return ResignResult(status="RESIGN_FAILED", errors=[completed.stderr.strip() or completed.stdout.strip()])
    return ResignResult(status="RESIGNED")
