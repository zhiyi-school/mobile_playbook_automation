from __future__ import annotations

import re
import subprocess
from pathlib import Path


def inspect_apk_metadata(apk_path: Path) -> dict:
    apk_path = Path(apk_path)
    if not apk_path.is_file():
        raise FileNotFoundError(apk_path)

    badging = _run_first_available([
        ["aapt", "dump", "badging", str(apk_path)],
        ["aapt2", "dump", "badging", str(apk_path)],
        ["apkanalyzer", "manifest", "print", str(apk_path)],
    ])
    return _parse_metadata(badging)


def _run_first_available(commands: list[list[str]]) -> str:
    errors = []
    for command in commands:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            return completed.stdout
        except FileNotFoundError as exc:
            errors.append(str(exc))
        except subprocess.CalledProcessError as exc:
            errors.append((exc.stderr or exc.stdout or str(exc)).strip())
    raise RuntimeError("APK metadata inspection needs aapt, aapt2 or apkanalyzer on PATH: " + "; ".join(errors))


def _parse_metadata(text: str) -> dict:
    package_line = next((line for line in text.splitlines() if line.startswith("package:")), "")
    label_line = next((line for line in text.splitlines() if line.startswith("application-label")), "")
    return {
        "package_name": _quoted_field(package_line, "name"),
        "version_code": _quoted_field(package_line, "versionCode"),
        "version_name": _quoted_field(package_line, "versionName"),
        "display_name": _line_label(label_line),
    }


def _quoted_field(line: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}='([^']*)'", line)
    return match.group(1) if match else None


def _line_label(line: str) -> str | None:
    if not line:
        return None
    match = re.search(r":'([^']*)'", line)
    return match.group(1) if match else None
