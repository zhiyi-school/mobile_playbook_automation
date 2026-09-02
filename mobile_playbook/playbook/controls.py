"""Turn one playbook Markdown document into a normalized risk or control record."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, NamedTuple

from mobile_playbook.playbook import markdown, source

ACTIVE = "active"
DEPRECATED = "deprecated"
DEPRIORITIZED = "deprioritized"
CONTROL_STATUSES = (ACTIVE, DEPRECATED, DEPRIORITIZED)

REFERENCES_HEADING = re.compile(r"^references\b", re.I)
REFERENCES_PARAGRAPH = re.compile(r"^references\s*:?\s*$", re.I)
CONTROL_MEASURES_PARAGRAPH = re.compile(r"control measures\s*:?\s*$", re.I)
DEPRIORITIZED_MARKER = re.compile(r"\(depriorit[iz]s?[ei]d?\)|\bdepriorit[iz]", re.I)
DEPRECATED_MARKER = re.compile(r"\(deprecated\)|\bdeprecated\b", re.I)
CONTROL_ID = re.compile(r"^(?P<platform>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)-control-(?P<control>\d+)", re.I)
RISK_ID = re.compile(r"^(?P<platform>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)\s*$", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
TRAILING_MARKER = re.compile(r"[_\s]*\((?:depriorit|deprecat)[^)]*\)\s*$", re.I)

STEP_TITLE_MAX_CHARS = 90


def document_id(stem_or_heading: str) -> str:
    """Strip a trailing status marker so a filename variation never changes an identity."""
    return TRAILING_MARKER.sub("", stem_or_heading).strip()


def revision(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def infer_status(*candidates: str) -> tuple[str, bool]:
    """Status inferred from naming, and whether it came from a marker rather than the default."""
    for candidate in candidates:
        if candidate and DEPRECATED_MARKER.search(candidate):
            return DEPRECATED, True
    for candidate in candidates:
        if candidate and DEPRIORITIZED_MARKER.search(candidate):
            return DEPRIORITIZED, True
    return ACTIVE, False


def normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in {"deprioritised", "deprioritized", "deprioritise", "deprioritize"}:
        return DEPRIORITIZED
    if text in {"deprecated", "deprecate", "retired"}:
        return DEPRECATED
    if text in {"active", "current", "required"}:
        return ACTIVE
    return None


def heading_of(blocks: list[dict[str, Any]], level: int = 2) -> str | None:
    for block in blocks:
        if block.get("type") == "heading" and block.get("level") == level:
            return str(block.get("text") or "").strip() or None
    return None


class Document(NamedTuple):
    path: Path
    raw: bytes
    front_matter: dict[str, Any]
    blocks: list[dict[str, Any]]
    heading_id: str | None
    file_id: str
    identity: str


def read_document(path: Path) -> Document:
    """Parse a document once and take its identity from the heading, never from the filename alone."""
    raw = path.read_bytes()
    front_matter, body = markdown.split_front_matter(raw.decode("utf-8", errors="replace"))
    blocks = markdown.parse_blocks(body)
    heading = heading_of(blocks)
    heading_id = document_id(heading) if heading else None
    file_id = document_id(path.stem)
    return Document(path, raw, front_matter, blocks, heading_id, file_id, heading_id or file_id)


def parse_control(document: Document, root: Path) -> dict[str, Any]:
    path, raw, front_matter, blocks = document.path, document.raw, document.front_matter, document.blocks
    heading_id, file_id = document.heading_id, document.file_id
    control_id = document.identity

    intro, steps, reference_blocks = _partition(blocks)
    references = _collect_references(reference_blocks, intro, steps)
    archives = _collect_archives(blocks)

    marker_status, from_marker = infer_status(path.name, heading_id or "")
    declared_status = normalize_status(front_matter.get("status"))
    status = declared_status or marker_status

    record = {
        "control_id": control_id,
        "title": _control_title(front_matter, control_id),
        "status": status,
        "status_source": "front_matter" if declared_status else ("naming" if from_marker else "default"),
        "required": bool(front_matter.get("required", status == ACTIVE)),
        "playbook_revision": revision(raw),
        "source_file": source.relative_to_root(root, path),
        "summary": _summary(intro),
        "intro": intro,
        "steps": steps,
        "references": references,
        "source_archives": archives,
        "declared_risk_id": document_id(str(front_matter.get("risk_id") or "")) or None,
        "heading_id": heading_id,
        "file_id": file_id,
    }
    return record


def parse_risk(document: Document, root: Path) -> dict[str, Any]:
    path, raw, front_matter, blocks = document.path, document.raw, document.front_matter, document.blocks
    heading_id = document.heading_id

    return {
        "risk_id": document.identity,
        "playbook_revision": revision(raw),
        "source_file": source.relative_to_root(root, path),
        "description": _section_text(blocks, "description"),
        "goal": _section_text(blocks, "goal"),
        "control_links": _control_links(blocks),
        "references": _collect_references(_partition(blocks)[2], [], []),
        "status": (normalize_status(front_matter.get("status")) or infer_status(path.name, heading_id or "")[0]),
        "heading_id": heading_id,
        "file_id": document.file_id,
    }


def _partition(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a control document into its intro, its ordered steps, and its trailing references."""
    intro: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    in_references = False
    current: dict[str, Any] | None = None
    used_keys: set[str] = set()

    for block in blocks:
        if block.get("type") == "heading":
            if block.get("level", 2) >= 3 and REFERENCES_HEADING.match(str(block.get("text") or "")):
                in_references = True
                current = None
                continue
            if block.get("level", 2) <= 2:
                continue

        if block.get("type") == "paragraph" and REFERENCES_PARAGRAPH.match(str(block.get("text") or "")):
            in_references = True
            current = None
            continue

        if in_references:
            references.append(block)
            continue

        if block.get("type") == "paragraph" and CONTROL_MEASURES_PARAGRAPH.search(str(block.get("text") or "")):
            current = None
            continue

        if block.get("type") == "list" and block.get("ordered"):
            for item in block.get("items") or []:
                text = item.get("text") or ""
                declared = str(item.get("step_id") or "").strip()
                current = {
                    "step_key": _unique(declared or _auto_step_id(text), used_keys),
                    "step_id_source": "declared" if declared else "auto",
                    "step_index": len(steps),
                    "number": item.get("number"),
                    "step_title": _step_title(text, len(steps)),
                    "text": text,
                    "content": [],
                }
                steps.append(current)
            continue

        if current is not None:
            current["content"].append(block)
        else:
            intro.append(block)

    return intro, steps, references


def _auto_step_id(text: str) -> str:
    """Derived from the instruction itself, so reordering keeps progress but a reworded step is a new step."""
    normalized = " ".join(text.split()).casefold()
    return f"auto-{hashlib.sha256(normalized.encode()).hexdigest()[:12]}"


def _unique(key: str, used: set[str]) -> str:
    candidate, ordinal = key, 1
    while candidate in used:
        ordinal += 1
        candidate = f"{key}-{ordinal}"
    used.add(candidate)
    return candidate


def _step_title(text: str, position: int) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return f"Step {position + 1}"
    first = SENTENCE_END.split(cleaned, maxsplit=1)[0].rstrip(".")
    if len(first) <= STEP_TITLE_MAX_CHARS:
        return first
    return f"{first[: STEP_TITLE_MAX_CHARS - 1].rstrip()}…"


def _control_title(front_matter: dict[str, Any], control_id: str) -> str:
    declared = str(front_matter.get("title") or "").strip()
    if declared:
        return declared
    match = CONTROL_ID.match(control_id)
    return f"Control {int(match.group('control'))}" if match else control_id


def _summary(intro: list[dict[str, Any]]) -> str:
    for block in intro:
        if block.get("type") == "paragraph":
            return str(block.get("text") or "")
    return ""


def _section_text(blocks: list[dict[str, Any]], heading: str) -> str:
    collecting = False
    parts: list[str] = []
    for block in blocks:
        if block.get("type") == "heading":
            if collecting:
                break
            collecting = str(block.get("text") or "").strip().lower() == heading
            continue
        if collecting and block.get("type") == "paragraph":
            parts.append(str(block.get("text") or ""))
    return " ".join(parts).strip()


def _control_links(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in blocks:
        for text in _link_sources(block):
            for link in markdown.iter_links(text):
                if link.is_image or not link.target.lower().endswith(".md"):
                    continue
                if link.target in seen:
                    continue
                seen.add(link.target)
                links.append({"label": link.label, "target": link.target})
    return links


def _collect_archives(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    archives: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in blocks:
        for text in _link_sources(block):
            for link in markdown.iter_links(text):
                suffix = Path(link.target).suffix.lower()
                if link.is_image or suffix not in source.ARCHIVE_SUFFIXES or link.target in seen:
                    continue
                seen.add(link.target)
                archives.append({"label": link.label, "target": link.target})
    return archives


def _collect_references(
    reference_blocks: list[dict[str, Any]],
    intro: list[dict[str, Any]],
    steps: list[dict[str, Any]],
) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in reference_blocks:
        for text in _link_sources(block):
            for url, label in _urls_in(text):
                if url in seen or Path(url).suffix.lower() in source.ARCHIVE_SUFFIXES:
                    continue
                seen.add(url)
                references.append({"label": label, "url": url})
    return references


def _link_sources(block: dict[str, Any]) -> list[str]:
    kind = block.get("type")
    if kind in {"paragraph", "caption"}:
        return [str(block.get("text") or "")]
    if kind == "list":
        return [str(item.get("text") or "") for item in block.get("items") or []]
    if kind == "table":
        return [value for row in block.get("rows") or [] for value in row.values()]
    return []


BARE_URL = re.compile(r"https?://[^\s<>()\[\]]+")


def _urls_in(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    consumed = text
    for link in markdown.iter_links(text):
        if link.is_image or not link.target.lower().startswith(("http://", "https://")):
            continue
        found.append((link.target, link.label or link.target))
        consumed = consumed.replace(f"[{link.label}]({link.target})", "")
    found.extend((url, url) for url in BARE_URL.findall(consumed))
    return found
