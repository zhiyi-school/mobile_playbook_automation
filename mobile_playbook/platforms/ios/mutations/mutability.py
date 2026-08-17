from __future__ import annotations

import re
import subprocess
from pathlib import Path

from mobile_playbook.platforms.ios.ipa.plist_utils import get_bundle_executable
from mobile_playbook.platforms.ios.models import BinaryInspectionResult


def detect_macho_encryption(executable_path: Path) -> BinaryInspectionResult:
    executable_path = Path(executable_path)
    if not executable_path.exists():
        return BinaryInspectionResult(status="EXECUTABLE_NOT_FOUND", executable_path=executable_path)
    try:
        completed = subprocess.run(
            ["otool", "-l", str(executable_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return BinaryInspectionResult(
            status="INSPECTION_FAILED",
            executable_path=executable_path,
            errors=[str(exc)],
        )
    if completed.returncode != 0:
        return BinaryInspectionResult(
            status="INSPECTION_FAILED",
            executable_path=executable_path,
            errors=[completed.stderr.strip() or completed.stdout.strip()],
            metadata={"returncode": completed.returncode},
        )
    cryptids = [int(match.group(1)) for match in re.finditer(r"\bcryptid\s+(\d+)", completed.stdout)]
    cryptid = max(cryptids) if cryptids else 0
    encrypted = cryptid != 0
    return BinaryInspectionResult(
        status="PROTECTED_OR_ENCRYPTED_BINARY" if encrypted else "MUTABLE_AS_PROVIDED",
        executable_path=executable_path,
        encrypted=encrypted,
        cryptid=cryptid,
        metadata={"tool": "otool", "encryption_load_commands": len(cryptids)},
    )


def inspect_main_executable(app_dir: Path) -> BinaryInspectionResult:
    try:
        executable = get_bundle_executable(app_dir)
    except Exception as exc:
        return BinaryInspectionResult(status="INSPECTION_FAILED", errors=[str(exc)])
    if not executable:
        return BinaryInspectionResult(status="EXECUTABLE_NOT_FOUND", errors=["CFBundleExecutable is missing"])
    return detect_macho_encryption(Path(app_dir) / executable)
