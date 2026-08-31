from __future__ import annotations

import re
import subprocess
from pathlib import Path


def inspect_apk_metadata(apk_path: Path) -> dict:
    return _parse_metadata(_badging(apk_path))


def icon_resource_paths(apk_path: Path) -> list[str]:
    """Icon resources the manifest declares, densest first. Empty when no SDK tool is on PATH."""
    try:
        badging = _badging(apk_path)
    except RuntimeError:
        return []
    return _parse_icon_resources(badging)


def _badging(apk_path: Path) -> str:
    apk_path = Path(apk_path)
    if not apk_path.is_file():
        raise FileNotFoundError(apk_path)

    return _run_first_available([
        ["aapt", "dump", "badging", str(apk_path)],
        ["aapt2", "dump", "badging", str(apk_path)],
        ["apkanalyzer", "manifest", "print", str(apk_path)],
    ])


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


def _parse_icon_resources(text: str) -> list[str]:
    """Densest first. Handles `aapt`/`aapt2` badging and `apkanalyzer`'s manifest XML alike."""
    densities: list[tuple[int, str]] = []
    fallback: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        density_match = re.match(r"application-icon-(\d+):'([^']+)'", stripped)
        if density_match:
            densities.append((int(density_match.group(1)), density_match.group(2)))
            continue
        if stripped.startswith("application:"):
            declared = _quoted_field(stripped, "icon")
            if declared:
                fallback.append(declared)
            continue
        for reference in re.findall(r'android:icon\s*=\s*"([^"]+)"', stripped):
            fallback.append(reference)
    ordered = [path for _, path in sorted(densities, key=lambda item: item[0], reverse=True)]
    for path in fallback:
        if path not in ordered:
            ordered.append(path)
    return ordered


def resource_reference_name(reference: str) -> str | None:
    """`@mipmap/ic_launcher` -> `ic_launcher`. Returns `None` for a literal resource path."""
    match = re.match(r"^@?(?:[\w.]+:)?(?:mipmap|drawable)/([\w.]+)$", reference.strip())
    return match.group(1) if match else None


def _quoted_field(line: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}='([^']*)'", line)
    return match.group(1) if match else None


def _line_label(line: str) -> str | None:
    if not line:
        return None
    match = re.search(r":'([^']*)'", line)
    return match.group(1) if match else None
