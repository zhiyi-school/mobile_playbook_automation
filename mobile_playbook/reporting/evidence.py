"""
Artifacts a completed test left behind, named for a reader rather than by path.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from mobile_playbook.common.storage_paths import resolve_recorded_path

logger = logging.getLogger(__name__)

NAMED_ARTIFACTS: list[tuple[str, str, str]] = [
    ("report.json", "report", "Detailed result report"),
    ("critical_findings.md", "report", "Critical findings"),
    ("critical_findings.json", "report", "Critical findings data"),
    ("ipa_analysis.json", "report", "IPA analysis"),
    ("package_inventory.json", "report", "Package inventory"),
    ("burp_capture.json", "report", "Burp capture results"),
    ("recording.mp4", "screen_recording", "Screen recording (recorder app capture)"),
    ("ocr_matches.json", "report", "Screen capture OCR matches"),
    ("reference_screen.png", "screenshot", "Target screen (Appium reference)"),
    ("target_screen.png", "screenshot", "Target screen"),
    ("target_page_source.xml", "page_source", "Target page source"),
    ("target_text_field_candidates.json", "report", "Text field candidates"),
    ("logs.txt", "log", "Run log"),
]

SUFFIX_KINDS: dict[str, tuple[str, str]] = {
    ".mp4": ("screen_recording", "Screen recording"),
    ".png": ("screenshot", "Screenshot"),
    ".jpg": ("screenshot", "Screenshot"),
    ".jpeg": ("screenshot", "Screenshot"),
    ".xml": ("page_source", "Page source"),
    ".apk": ("package", "Android package"),
    ".ipa": ("ipa", "iOS package"),
    ".json": ("report", "Result data"),
    ".txt": ("log", "Log"),
    ".log": ("log", "Log"),
}


# Returns the evidence kind and a label derived from a file's suffix.
def _label_for(name: str) -> tuple[str, str]:
    kind, label = SUFFIX_KINDS.get(Path(name).suffix.lower(), ("file", "Artifact"))
    return kind, f"{label} ({name})"


# Encodes a root name and relative path as an opaque download handle; never a host path.
def encode_ref(root: str, relative_path: str) -> str:
    raw = f"{root}\n{Path(relative_path).as_posix()}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# Decodes a download handle into its root name and relative path, or None when it is not one we wrote.
def decode_ref(ref: str) -> tuple[str, str] | None:
    try:
        padded = ref + "=" * (-len(ref) % 4)
        root, _, relative = base64.urlsafe_b64decode(padded.encode()).decode().partition("\n")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.debug("reporting: evidence ref is not valid base64 text (%s).", type(exc).__name__)
        return None
    if not root or not relative:
        logger.debug("reporting: evidence ref lacks a root or path.")
        return None
    # A NUL or control character would reach the filesystem as a ValueError, so refuse it here.
    if any(ord(ch) < 32 or ch == "\x7f" for ch in root + relative):
        logger.debug("reporting: evidence ref contains control characters; rejected.")
        return None
    return root, relative


# Returns the artifact's root-relative path and opaque ref, or None when it is outside every root.
def _reference(path: Path, roots: dict[str, Path]) -> tuple[str, str] | None:
    for name, root in roots.items():
        try:
            relative = path.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        logger.debug("reporting: evidence %s is under root %s.", path, name)
        return f"{name}/{relative.as_posix()}", encode_ref(name, str(relative))
    logger.debug("reporting: evidence %s is outside roots %s.", path, sorted(roots))
    return None


# Lists every file in a report directory as evidence, named artifacts first; a missing directory yields nothing.
def report_dir_evidence(report_dir: Path) -> list[dict[str, Any]]:
    try:
        if not report_dir.is_dir():
            logger.debug("reporting: report directory %s missing; no directory evidence.", report_dir)
            return []
        present = {entry.name: entry for entry in report_dir.iterdir() if entry.is_file()}
    except OSError:
        logger.debug("reporting: report directory %s unreadable.", report_dir, exc_info=True)
        return []
    logger.debug("reporting: %d file(s) in %s.", len(present), report_dir)

    items: list[dict[str, Any]] = []
    for name, kind, label in NAMED_ARTIFACTS:
        entry = present.pop(name, None)
        if entry is not None:
            items.append({"kind": kind, "source": entry, "label": label})
    for name in sorted(present):
        kind, label = _label_for(name)
        items.append({"kind": kind, "source": present[name], "label": label})
    return items


# Merges declared and report-directory evidence into unique existing files with refs, dropping any outside allowed roots.
def normalize_evidence(
    declared: list[dict[str, Any]] | None,
    report_dir: Path | None = None,
    roots: dict[str, Path] | None = None,
    base: Path | None = None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = roots or {}

    for item in list(declared or []) + (report_dir_evidence(report_dir) if report_dir else []):
        source = item.get("source")
        raw = str(item.get("path") or "").strip()
        candidate = Path(source) if source is not None else Path(raw) if raw else None
        if candidate is None:
            continue
        # Recorded paths are relative to the installation, never the API's working directory.
        if base is not None and not candidate.is_absolute():
            candidate = base / candidate
        # Paths from the old repository-root layout moved under artifacts/; map them so they are still served.
        if candidate.is_absolute() and not candidate.exists():
            candidate = resolve_recorded_path(candidate)
        try:
            if not candidate.is_file():
                logger.debug("reporting: evidence %s is not a file; skipped.", candidate)
                continue
            key = str(candidate.resolve())
        except OSError:
            logger.debug("reporting: evidence %s could not be inspected; skipped.", candidate, exc_info=True)
            continue
        if key in seen:
            logger.debug("reporting: evidence %s already listed; skipped.", key)
            continue

        reference = _reference(candidate, allowed) if allowed else None
        if allowed and reference is None:
            logger.debug("reporting: evidence %s dropped; outside every allowed root.", candidate)
            continue
        seen.add(key)
        path, ref = reference if reference else (raw or candidate.name, "")
        merged.append(
            {
                "kind": str(item.get("kind") or "file"),
                "path": path,
                "ref": ref,
                "label": str(item.get("label") or Path(path).name),
                "size_bytes": candidate.stat().st_size,
            }
        )
    logger.debug("reporting: normalized %d evidence item(s) (%d declared).", len(merged), len(declared or []))
    return merged
