"""Artifacts a completed test left behind, named for a reader rather than by path."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from mobile_playbook.storage.paths import resolve_recorded_path

# Named artifacts first; anything else is still offered, labelled from its suffix.
NAMED_ARTIFACTS: list[tuple[str, str, str]] = [
    ("report.json", "report", "Detailed result report"),
    ("critical_findings.md", "report", "Critical findings"),
    ("critical_findings.json", "report", "Critical findings data"),
    ("ipa_analysis.json", "report", "IPA analysis"),
    ("package_inventory.json", "report", "Package inventory"),
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


def _label_for(name: str) -> tuple[str, str]:
    kind, label = SUFFIX_KINDS.get(Path(name).suffix.lower(), ("file", "Artifact"))
    return kind, f"{label} ({name})"


def encode_ref(root: str, relative_path: str) -> str:
    """An opaque handle for one artifact. Never a host path, and never a URL parameter to resolve directly."""
    raw = f"{root}\n{Path(relative_path).as_posix()}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_ref(ref: str) -> tuple[str, str] | None:
    """The root name and its relative path, or None when the handle is not one we wrote."""
    try:
        padded = ref + "=" * (-len(ref) % 4)
        root, _, relative = base64.urlsafe_b64decode(padded.encode()).decode().partition("\n")
    except (ValueError, UnicodeDecodeError):
        return None
    if not root or not relative:
        return None
    # A NUL or control character reaches the filesystem call as a ValueError,
    # not as a rejection, so it is refused before it gets there.
    if any(ord(ch) < 32 or ch == "\x7f" for ch in root + relative):
        return None
    return root, relative


def _reference(path: Path, roots: dict[str, Path]) -> tuple[str, str] | None:
    """The artifact as (root-relative path, opaque ref), or None when it is outside every root."""
    for name, root in roots.items():
        try:
            relative = path.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        return f"{name}/{relative.as_posix()}", encode_ref(name, str(relative))
    return None


def report_dir_evidence(report_dir: Path) -> list[dict[str, Any]]:
    """Every file the test wrote, named artifacts first. Missing directories yield nothing."""
    try:
        if not report_dir.is_dir():
            return []
        present = {entry.name: entry for entry in report_dir.iterdir() if entry.is_file()}
    except OSError:
        return []

    items: list[dict[str, Any]] = []
    for name, kind, label in NAMED_ARTIFACTS:
        entry = present.pop(name, None)
        if entry is not None:
            items.append({"kind": kind, "source": entry, "label": label})
    for name in sorted(present):
        kind, label = _label_for(name)
        items.append({"kind": kind, "source": present[name], "label": label})
    return items


def normalize_evidence(
    declared: list[dict[str, Any]] | None,
    report_dir: Path | None = None,
    roots: dict[str, Path] | None = None,
    base: Path | None = None,
) -> list[dict[str, Any]]:
    """
    The row's own evidence plus its report directory: existing files only, one per
    path, each carrying the opaque handle the download endpoint accepts. An
    artifact outside every allowed root is dropped rather than served.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = roots or {}

    for item in list(declared or []) + (report_dir_evidence(report_dir) if report_dir else []):
        source = item.get("source")
        raw = str(item.get("path") or "").strip()
        candidate = Path(source) if source is not None else Path(raw) if raw else None
        if candidate is None:
            continue
        # A recorded path is relative to the installation, never to the working
        # directory the API happens to have been started from.
        if base is not None and not candidate.is_absolute():
            candidate = base / candidate
        # A path recorded under the previous repository-root layout names a
        # directory that has since moved under artifacts/; map it onto its file
        # so the artifact is still served instead of silently dropped.
        if candidate.is_absolute() and not candidate.exists():
            candidate = resolve_recorded_path(candidate)
        try:
            if not candidate.is_file():
                continue
            key = str(candidate.resolve())
        except OSError:
            continue
        if key in seen:
            continue

        reference = _reference(candidate, allowed) if allowed else None
        if allowed and reference is None:
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
    return merged
