"""Index the external playbook into a cached platform → risk → control catalogue."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from mobile_playbook.playbook import controls as control_parser
from mobile_playbook.playbook import source

CONTROL_OVERRIDE_FILES = {
    "ios": Path("configs/split/ios/controls.yaml"),
    "android": Path("configs/split/android/controls.yaml"),
}

RISK_DOCUMENT = re.compile(r"^(?P<prefix>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)$", re.I)
CONTROL_DOCUMENT = re.compile(
    r"^(?P<prefix>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)-control-(?P<control>\d+)$", re.I
)

_lock = threading.Lock()
_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}


def canonical_id(document_id: str, platform: str) -> str:
    """Playbook documents are written with a generic `platform-` prefix; the catalogue keys on the real one."""
    for pattern in (CONTROL_DOCUMENT, RISK_DOCUMENT):
        match = pattern.match(document_id)
        if match is not None:
            return f"{platform}{document_id[len(match.group('prefix')) :]}".lower()
    return document_id.lower()


def risk_id_of_control(control_id: str) -> str | None:
    match = CONTROL_DOCUMENT.match(control_id)
    if match is None:
        return None
    return control_id[: control_id.rindex("-control-")].lower()


def asset_url(platform: str, control_id: str, asset_path: str, version: str | None = None) -> str:
    """The version query defeats the browser cache when a screenshot is replaced in place."""
    url = f"/platforms/{platform}/controls/{control_id}/assets/{asset_path}"
    return f"{url}?v={version}" if version else url


def source_url(platform: str, control_id: str) -> str:
    return f"/platforms/{platform}/controls/{control_id}/source"


def get(platform: str, refresh: bool = False) -> dict[str, Any]:
    """The catalogue for a platform, rebuilt when the playbook files change on disk."""
    root = source.require_root(platform)
    signature = source.source_signature(root)
    with _lock:
        cached = _cache.get(platform)
        if not refresh and cached is not None and cached[0] == signature:
            return cached[1]
        built = build(platform, root)
        _cache[platform] = (signature, built)
        return built


def reload(platform: str) -> dict[str, Any]:
    return get(platform, refresh=True)


def clear_cache(platform: str | None = None) -> None:
    with _lock:
        if platform is None:
            _cache.clear()
        else:
            _cache.pop(platform, None)


def build(platform: str, root: Path) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    overrides = _load_overrides(platform)

    risk_records: dict[str, dict[str, Any]] = {}
    control_records: dict[str, dict[str, Any]] = {}
    documents: dict[str, str] = {}

    for path in source.markdown_files(root):
        relative = source.relative_to_root(root, path)
        try:
            document = control_parser.read_document(path)
        except (OSError, ValueError) as exc:
            warnings.append(
                {"code": "document_unreadable", "file": relative, "message": f"Could not read {relative}: {exc}"}
            )
            continue

        documents[relative] = document.identity
        if CONTROL_DOCUMENT.match(document.identity):
            record = control_parser.parse_control(document, root)
            key = canonical_id(record["control_id"], platform)
            _note_identity(warnings, relative, document, key, platform)
            control_records[key] = _finalize_control(record, key, platform, root, overrides, warnings)
        elif RISK_DOCUMENT.match(document.identity):
            record = control_parser.parse_risk(document, root)
            key = canonical_id(record["risk_id"], platform)
            _note_identity(warnings, relative, document, key, platform)
            record["risk_id"] = key
            record["platform"] = platform
            risk_records[key] = record

    _link_controls(risk_records, control_records, platform, root, warnings)

    return {
        "platform": platform,
        "root": str(root),
        "revision": _catalogue_revision(risk_records, control_records),
        "risks": risk_records,
        "controls": control_records,
        "warnings": warnings,
        "document_count": len(documents),
    }


def _finalize_control(
    record: dict[str, Any],
    key: str,
    platform: str,
    root: Path,
    overrides: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    override = overrides.get(key, {})
    override_status = control_parser.normalize_status(override.get("status"))
    if override_status:
        record["status"] = override_status
        record["status_source"] = "config_override"
        record["required"] = bool(override.get("required", override_status == control_parser.ACTIVE))
    if "required" in override:
        record["required"] = bool(override["required"])
    if override.get("title"):
        record["title"] = str(override["title"])

    record["control_id"] = key
    record["platform"] = platform
    record["risk_id"] = risk_id_of_control(key)
    record["source_download_url"] = None

    for block in _iter_blocks(record):
        if block.get("type") == "image":
            _decorate_image(block, platform, key, root, record["source_file"], warnings)

    for step in record.get("steps") or []:
        step["content_hash"] = _step_content_hash(step)
        if not str(step.get("text") or "").strip():
            warnings.append(
                {
                    "code": "empty_step",
                    "control_id": key,
                    "file": record["source_file"],
                    "path": step["step_key"],
                    "message": f"{record['source_file']} step {step['step_key']} has no instruction text.",
                }
            )

    resolved_archives = []
    for archive in record.get("source_archives") or []:
        resolved = source.resolve_within(root, archive["target"])
        exists = resolved is not None and resolved.is_file()
        if not exists:
            warnings.append(
                {
                    "code": "missing_source_archive",
                    "control_id": key,
                    "file": record["source_file"],
                    "path": archive["target"],
                    "message": (
                        f"{record['source_file']} references source archive {archive['target']}, which is not in the playbook."
                    ),
                }
            )
        elif canonical_id(control_parser.document_id(Path(archive["target"]).stem), platform) != key:
            warnings.append(
                {
                    "code": "mismatched_source_archive",
                    "control_id": key,
                    "file": record["source_file"],
                    "path": archive["target"],
                    "message": (
                        f"{record['source_file']} offers source archive {archive['target']}, "
                        "which was built for a different control."
                    ),
                }
            )
        resolved_archives.append(
            {
                "label": archive["label"],
                "path": archive["target"],
                "exists": exists,
                "file_name": Path(archive["target"]).name,
                "size_bytes": resolved.stat().st_size if exists else None,
                "sha256": _digest(resolved) if exists else None,
                "url": source_url(platform, key) if exists else None,
            }
        )
    record["source_archives"] = resolved_archives
    record["source_download_url"] = next((a["url"] for a in resolved_archives if a["exists"]), None)
    record["step_count"] = len(record.get("steps") or [])
    return record


def _decorate_image(
    block: dict[str, Any],
    platform: str,
    control_id: str,
    root: Path,
    source_file: str,
    warnings: list[dict[str, Any]],
) -> None:
    path = str(block.get("path") or "")
    if path.lower().startswith(("http://", "https://", "data:")):
        block["url"] = path
        block["exists"] = True
        return
    resolved = source.resolve_within(root, path)
    exists = resolved is not None and resolved.is_file() and resolved.suffix.lower() in source.IMAGE_SUFFIXES
    block["url"] = asset_url(platform, control_id, path, _short_digest(resolved)) if exists else None
    block["exists"] = exists
    if not exists:
        warnings.append(
            {
                "code": "missing_image",
                "control_id": control_id,
                "file": source_file,
                "path": path,
                "message": f"{source_file} references image {path}, which is not in the playbook.",
            }
        )


def _link_controls(
    risk_records: dict[str, dict[str, Any]],
    control_records: dict[str, dict[str, Any]],
    platform: str,
    root: Path,
    warnings: list[dict[str, Any]],
) -> None:
    for risk_id, risk in risk_records.items():
        owned = sorted(
            (control for control in control_records.values() if control["risk_id"] == risk_id),
            key=lambda control: control["control_id"],
        )
        risk["controls"] = [control["control_id"] for control in owned]
        _validate_links(risk, control_records, platform, root, warnings)
        if not owned:
            warnings.append(
                {
                    "code": "risk_without_controls",
                    "risk_id": risk_id,
                    "file": risk["source_file"],
                    "message": f"{risk['source_file']} has no developer control documents.",
                }
            )

    for control in control_records.values():
        if control["risk_id"] and control["risk_id"] not in risk_records:
            warnings.append(
                {
                    "code": "control_without_risk",
                    "control_id": control["control_id"],
                    "file": control["source_file"],
                    "message": (
                        f"{control['source_file']} belongs to risk {control['risk_id']}, "
                        "which has no playbook document."
                    ),
                }
            )


def _validate_links(
    risk: dict[str, Any],
    control_records: dict[str, dict[str, Any]],
    platform: str,
    root: Path,
    warnings: list[dict[str, Any]],
) -> None:
    for link in risk.get("control_links") or []:
        target = link["target"]
        resolved = source.resolve_within(root, target)
        if resolved is None or not resolved.is_file():
            warnings.append(
                {
                    "code": "missing_control_file",
                    "risk_id": risk["risk_id"],
                    "file": risk["source_file"],
                    "path": target,
                    "message": f"{risk['source_file']} links to {target}, which is not in the playbook.",
                }
            )
            continue
        target_id = canonical_id(control_parser.document_id(Path(target).stem), platform)
        label_id = canonical_id(control_parser.document_id(link["label"]), platform)
        if label_id and CONTROL_DOCUMENT.match(label_id) and label_id != target_id:
            warnings.append(
                {
                    "code": "malformed_control_link",
                    "risk_id": risk["risk_id"],
                    "file": risk["source_file"],
                    "path": target,
                    "message": (
                        f"{risk['source_file']} links {link['label']!r} to {target}, "
                        f"which is control {target_id}."
                    ),
                }
            )
        if target_id in control_records and control_records[target_id]["risk_id"] != risk["risk_id"]:
            warnings.append(
                {
                    "code": "cross_risk_control_link",
                    "risk_id": risk["risk_id"],
                    "file": risk["source_file"],
                    "path": target,
                    "message": (
                        f"{risk['source_file']} links to {target}, which belongs to risk "
                        f"{control_records[target_id]['risk_id']}."
                    ),
                }
            )


def _note_identity(
    warnings: list[dict[str, Any]],
    relative: str,
    document: control_parser.Document,
    key: str,
    platform: str,
) -> None:
    heading_id, file_id = document.heading_id, document.file_id
    if heading_id and canonical_id(heading_id, platform) != canonical_id(file_id, platform):
        warnings.append(
            {
                "code": "heading_filename_mismatch",
                "file": relative,
                "message": (
                    f"{relative} declares heading {heading_id!r}; the catalogue uses the heading as the identity."
                ),
            }
        )
    if not heading_id:
        warnings.append(
            {
                "code": "missing_heading",
                "file": relative,
                "message": f"{relative} has no level-2 heading; the identity fell back to the filename ({key}).",
            }
        )


def _load_overrides(platform: str) -> dict[str, dict[str, Any]]:
    path = CONTROL_OVERRIDE_FILES.get(platform)
    if path is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key).lower(): value for key, value in data.items() if isinstance(value, dict)}


def _iter_blocks(record: dict[str, Any]):
    yield from record.get("intro") or []
    for step in record.get("steps") or []:
        yield from step.get("content") or []


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _short_digest(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return _digest(path)[7:19]
    except OSError:
        return None


def _step_content_hash(step: dict[str, Any]) -> str:
    """Covers the instruction and everything rendered under it, so a rewritten step is detectable."""
    digest = hashlib.sha256()
    digest.update(json.dumps(step.get("text") or "", sort_keys=True).encode())
    digest.update(json.dumps(step.get("content") or [], sort_keys=True, default=str).encode())
    return f"sha256:{digest.hexdigest()[:32]}"


def _catalogue_revision(risks: dict[str, Any], controls: dict[str, Any]) -> str:
    """Changes when any document, screenshot or archive changes, so a client can poll one value."""
    digest = hashlib.sha256()
    for key in sorted(risks):
        digest.update(f"{key}:{risks[key]['playbook_revision']}\n".encode())
    for key in sorted(controls):
        control = controls[key]
        digest.update(f"{key}:{control['playbook_revision']}\n".encode())
        for step in control.get("steps") or []:
            digest.update(f"{step['step_key']}:{step.get('content_hash')}\n".encode())
        for archive in control.get("source_archives") or []:
            digest.update(f"{archive['path']}:{archive.get('sha256')}\n".encode())
    return f"sha256:{digest.hexdigest()}"
