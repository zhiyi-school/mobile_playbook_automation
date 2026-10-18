"""
Reads APK manifest metadata and icon resources with the Android SDK tools.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# Return an APK's package name, version code, version name and display label.
def inspect_apk_metadata(apk_path: Path) -> dict:
    logger.debug("android apk tools: inspecting metadata of %s", apk_path)
    metadata = _parse_metadata(_badging(apk_path))
    logger.debug("android apk tools: metadata of %s: %s", apk_path, metadata)
    return metadata


# Return the manifest's icon resources, densest first, or an empty list when no SDK tool is on PATH.
def icon_resource_paths(apk_path: Path) -> list[str]:
    try:
        badging = _badging(apk_path)
    except RuntimeError:
        logger.debug("android apk tools: no SDK tool could read icons of %s", apk_path, exc_info=True)
        return []
    icons = _parse_icon_resources(badging)
    logger.debug("android apk tools: %s icon resources in %s: %s", len(icons), apk_path, icons)
    return icons


# Return the AndroidManifest.xml element tree of an APK, dumped by aapt or aapt2.
def manifest_tree(apk_path: Path) -> str:
    apk_path = Path(apk_path)
    if not apk_path.is_file():
        logger.debug("android apk tools: %s is not a file", apk_path)
        raise FileNotFoundError(apk_path)
    return _run_first_available([
        ["aapt", "dump", "xmltree", str(apk_path), "AndroidManifest.xml"],
        ["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk_path)],
    ])


# Return the badging or manifest dump of an existing APK from the first SDK tool that works.
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


# Return the stdout of the first command that succeeds, raising RuntimeError when none does.
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
    raise RuntimeError("APK inspection needs aapt, aapt2 or apkanalyzer on PATH: " + "; ".join(errors))


# Extract package, version and label fields from badging output.
def _parse_metadata(text: str) -> dict:
    package_line = next((line for line in text.splitlines() if line.startswith("package:")), "")
    label_line = next((line for line in text.splitlines() if line.startswith("application-label")), "")
    return {
        "package_name": _quoted_field(package_line, "name"),
        "version_code": _quoted_field(package_line, "versionCode"),
        "version_name": _quoted_field(package_line, "versionName"),
        "display_name": _line_label(label_line),
    }


# Return icon references, densest first, from aapt/aapt2 badging or apkanalyzer manifest XML.
def _parse_icon_resources(text: str) -> list[str]:
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


# Return an adaptive icon XML's background and foreground layers, each as {"files": [...], "color": ...}, using
# aapt2; None when aapt2 is unavailable or a layer is not a plain resource reference.
def adaptive_icon_layers(apk_path: Path, icon_xml: str, *, resource_apks: list[Path] | None = None) -> dict[str, dict] | None:
    try:
        tree = _run_first_available([["aapt2", "dump", "xmltree", "--file", icon_xml, str(apk_path)]])
        layer_ids = _adaptive_layer_ids(tree)
        if set(layer_ids) != {"background", "foreground"}:
            logger.debug("android apk tools: %s does not name both layers as resources: %s", icon_xml, layer_ids)
            return None
        resources = _resource_entries(
            "\n".join(_run_first_available([["aapt2", "dump", "resources", str(path)]]) for path in (resource_apks or [apk_path])),
            set(layer_ids.values())
        )
    except (RuntimeError, OSError):
        logger.debug("android apk tools: adaptive icon layers of %s unreadable", apk_path, exc_info=True)
        return None
    layers = {layer: resources.get(resource_id) for layer, resource_id in layer_ids.items()}
    if any(entry is None for entry in layers.values()):
        logger.debug("android apk tools: adaptive icon layer resources missing: %s", layers)
        return None
    logger.debug("android apk tools: adaptive icon layers of %s: %s", icon_xml, layers)
    return layers


# Return the resource ids an adaptive-icon xmltree dump names for its background and foreground drawables.
def _adaptive_layer_ids(tree: str) -> dict[str, str]:
    layer_ids: dict[str, str] = {}
    current: str | None = None
    for line in tree.splitlines():
        stripped = line.strip()
        element = re.match(r"^E: (\S+)", stripped)
        if element:
            name = element.group(1)
            current = name if name in ("background", "foreground") else (current if name == "inset" else None)
            continue
        reference = re.match(r"^A: \S*:drawable\(0x01010199\)=@(0x[0-9a-fA-F]{8})", stripped)
        if current and reference and current not in layer_ids:
            layer_ids[current] = reference.group(1).lower()
    return layer_ids


# Return each wanted resource's PNG files, densest-first order left to the caller, or its literal colour.
def _resource_entries(dump: str, wanted: set[str]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    current: dict | None = None
    for line in dump.splitlines():
        stripped = line.strip()
        resource = re.match(r"^resource (0x[0-9a-fA-F]{8}) ", stripped)
        if resource:
            resource_id = resource.group(1).lower()
            current = entries.setdefault(resource_id, {"files": [], "color": None}) if resource_id in wanted else None
            continue
        if current is None:
            continue
        file_match = re.match(r"^\([^)]*\) \(file\) (res/\S+\.png) type=PNG$", stripped)
        if file_match:
            current["files"].append(file_match.group(1))
            continue
        color_match = re.match(r"^\([^)]*\) (#[0-9a-fA-F]{3,8})$", stripped)
        if color_match and current["color"] is None:
            current["color"] = color_match.group(1)
    return {resource_id: entry for resource_id, entry in entries.items() if entry["files"] or entry["color"]}


# Return the resource name of a mipmap or drawable reference, or None for a literal path.
def resource_reference_name(reference: str) -> str | None:
    match = re.match(r"^@?(?:[\w.]+:)?(?:mipmap|drawable)/([\w.]+)$", reference.strip())
    return match.group(1) if match else None


# Return the single-quoted value of a named field in a badging line.
def _quoted_field(line: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}='([^']*)'", line)
    return match.group(1) if match else None


# Return the first single-quoted value after a colon in a badging line.
def _line_label(line: str) -> str | None:
    if not line:
        return None
    match = re.search(r":'([^']*)'", line)
    return match.group(1) if match else None
