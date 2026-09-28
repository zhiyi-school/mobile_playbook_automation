from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def inspect_apk_metadata(apk_path: Path) -> dict:
    logger.debug("android apk tools: inspecting metadata of %s", apk_path)
    metadata = _parse_metadata(_badging(apk_path))
    logger.debug("android apk tools: metadata of %s: %s", apk_path, metadata)
    return metadata


def icon_resource_paths(apk_path: Path) -> list[str]:
    """Icon resources the manifest declares, densest first. Empty when no SDK tool is on PATH."""
    try:
        badging = _badging(apk_path)
    except RuntimeError:
        logger.debug("android apk tools: no SDK tool could read icons of %s", apk_path, exc_info=True)
        return []
    icons = _parse_icon_resources(badging)
    logger.debug("android apk tools: %s icon resources in %s: %s", len(icons), apk_path, icons)
    return icons


def _badging(apk_path: Path) -> str:
    apk_path = Path(apk_path)
    if not apk_path.is_file():
        logger.debug("android apk tools: %s is not a file", apk_path)
        raise FileNotFoundError(apk_path)

    return _run_first_available([
        ["aapt", "dump", "badging", str(apk_path)],
        ["aapt2", "dump", "badging", str(apk_path)],
        ["apkanalyzer", "manifest", "print", str(apk_path)],
    ])


def _run_first_available(commands: list[list[str]]) -> str:
    errors = []
    for command in commands:
        logger.debug("android apk tools: running argv=%s", command)
        started = time.monotonic()
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            logger.debug(
                "android apk tools: %s exited 0 in %.2fs (stdout %s chars)",
                command[0],
                time.monotonic() - started,
                len(completed.stdout or ""),
            )
            return completed.stdout
        except FileNotFoundError as exc:
            logger.debug("android apk tools: %s not found on PATH", command[0], exc_info=True)
            errors.append(str(exc))
        except subprocess.CalledProcessError as exc:
            logger.debug(
                "android apk tools: %s exited %s in %.2fs (stderr head=%r)",
                command[0],
                exc.returncode,
                time.monotonic() - started,
                (exc.stderr or "")[:200],
                exc_info=True,
            )
            errors.append((exc.stderr or exc.stdout or str(exc)).strip())
    logger.debug("android apk tools: no SDK tool succeeded (%s attempts)", len(commands))
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
