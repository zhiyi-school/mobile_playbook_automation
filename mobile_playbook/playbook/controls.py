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

DESCRIPTION = "description"
GOAL = "goal"
STEPS = "steps"
REFERENCES = "references"

SECTION_NAMES = {
    "description": DESCRIPTION,
    "goal": GOAL,
    "demonstration": STEPS,
    "remediation": STEPS,
    "references": REFERENCES,
}
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

    parse_warnings: list[dict[str, Any]] = []
    intro, steps, reference_blocks = _partition(blocks, parse_warnings)
    references = _collect_references(reference_blocks, intro, steps)
    archives = _collect_archives(blocks)
    description = _section_text(blocks, "description")
    if not description and split_sections(blocks).has_step_section:
        parse_warnings.append({"code": "missing_description", "message": "no Description section"})

    marker_status, from_marker = infer_status(path.name, heading_id or "")
    declared_status = normalize_status(front_matter.get("status"))
    status = declared_status or marker_status

    record = {
        "control_id": control_id,
        "title": _control_title(front_matter, description, control_id),
        "status": status,
        "status_source": "front_matter" if declared_status else ("naming" if from_marker else "default"),
        "required": bool(front_matter.get("required", status == ACTIVE)),
        "playbook_revision": revision(raw),
        "source_file": source.relative_to_root(root, path),
        "summary": description or _summary(intro),
        "intro": intro,
        "steps": steps,
        "references": references,
        "source_archives": archives,
        "declared_risk_id": document_id(str(front_matter.get("risk_id") or "")) or None,
        "heading_id": heading_id,
        "file_id": file_id,
        "parse_warnings": parse_warnings,
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
        "demonstration": _risk_demonstration(blocks),
        "control_links": _control_links(blocks),
        "references": _collect_references(_partition(blocks)[2], [], []),
        "status": (normalize_status(front_matter.get("status")) or infer_status(path.name, heading_id or "")[0]),
        "heading_id": heading_id,
        "file_id": document.file_id,
    }


class Sections(NamedTuple):
    named: dict[str, list[dict[str, Any]]]
    loose: list[dict[str, Any]]
    has_step_section: bool


def split_sections(blocks: list[dict[str, Any]]) -> Sections:
    named: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    current: list[dict[str, Any]] | None = None
    has_step_section = False

    for block in blocks:
        if block.get("type") == "heading":
            level = int(block.get("level", 2) or 2)
            if level <= 2:
                current = None
                continue
            if level == 3:
                name = SECTION_NAMES.get(str(block.get("text") or "").strip().casefold())
                if name is not None:
                    has_step_section = has_step_section or name == STEPS
                    current = named.setdefault(name, [])
                    continue
                current = None

        if block.get("type") == "paragraph" and REFERENCES_PARAGRAPH.match(str(block.get("text") or "")):
            current = named.setdefault(REFERENCES, [])
            continue

        (current if current is not None else loose).append(block)

    return Sections(named, loose, has_step_section)


def _partition(
    blocks: list[dict[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sections = split_sections(blocks)
    notes = warnings if warnings is not None else []
    references = list(sections.named.get(REFERENCES) or [])
    used_keys: set[str] = set()

    if sections.has_step_section:
        body = sections.named.get(STEPS) or []
        steps, leading = _heading_steps(body, used_keys, notes)
        if steps is None:
            steps, leading = _list_steps(body, used_keys, notes)
            if not steps:
                notes.append({"code": "empty_step_section", "message": "no numbered steps"})
        intro = _intro_blocks(sections.named.get(DESCRIPTION) or [])
        intro.extend(_intro_blocks(sections.named.get(GOAL) or []))
        intro.extend(_intro_blocks(sections.loose))
        intro.extend(_intro_blocks(leading))
        return intro, steps, references

    # Legacy documents name no section: ordered lists before the references are the steps.
    steps, intro = _list_steps(sections.loose, used_keys, notes)
    intro = _intro_blocks(_intro_blocks(sections.named.get(DESCRIPTION) or []) + intro)
    return intro, steps, references


def _intro_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block for block in blocks if block.get("type") != "step_id"]


def _heading_steps(
    blocks: list[dict[str, Any]],
    used_keys: set[str],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Steps introduced by numbered headings; deeper headings stay inside the step they follow."""
    levels = [
        int(block.get("level", 0) or 0)
        for block in blocks
        if block.get("type") == "heading" and markdown.NUMBERED_HEADING.match(str(block.get("text") or ""))
    ]
    if not levels:
        return None, blocks

    step_level = min(levels)
    steps: list[dict[str, Any]] = []
    leading: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    declared: str | None = None
    seen_numbers: set[int] = set()

    for block in blocks:
        if block.get("type") == "step_id":
            declared = str(block.get("value") or "").strip() or None
            continue

        heading = None
        if block.get("type") == "heading" and int(block.get("level", 0) or 0) == step_level:
            heading = markdown.NUMBERED_HEADING.match(str(block.get("text") or ""))

        if heading is None:
            if current is None:
                leading.append(block)
            else:
                current["content"].append(block)
            continue

        number = int(heading.group(1))
        title = " ".join(heading.group(2).split()) or f"Step {len(steps) + 1}"
        if number in seen_numbers:
            warnings.append({"code": "duplicate_step_number", "path": str(number), "message": f"step number {number}"})
        seen_numbers.add(number)

        generated = _auto_step_id(title)
        candidate = declared or generated
        if candidate in used_keys:
            code = "duplicate_step_id" if declared else "duplicate_generated_step_id"
            warnings.append(
                {"code": code, "path": candidate, "message": f"step id {candidate}"}
            )
        current = {
            "step_key": _unique(candidate, used_keys),
            "step_id_source": "declared" if declared else "auto",
            "step_index": len(steps),
            "number": number,
            "step_title": title,
            "text": "",
            "content": [],
        }
        steps.append(current)
        declared = None

    for step in steps:
        content = step["content"]
        if content and content[0].get("type") == "paragraph":
            step["text"] = str(content.pop(0).get("text") or "")
    return steps, leading


def _list_steps(
    blocks: list[dict[str, Any]],
    used_keys: set[str],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The original format: each top-level ordered-list item is a step."""
    steps: list[dict[str, Any]] = []
    leading: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for block in blocks:
        if block.get("type") == "step_id":
            continue

        if block.get("type") == "paragraph" and CONTROL_MEASURES_PARAGRAPH.search(str(block.get("text") or "")):
            current = None
            continue

        if block.get("type") == "list" and block.get("ordered"):
            for item in block.get("items") or []:
                text = item.get("text") or ""
                declared = str(item.get("step_id") or "").strip()
                generated = _auto_step_id(text)
                candidate = declared or generated
                if candidate in used_keys:
                    code = "duplicate_step_id" if declared else "duplicate_generated_step_id"
                    warnings.append(
                        {"code": code, "path": candidate, "message": f"step id {candidate}"}
                    )
                current = {
                    "step_key": _unique(candidate, used_keys),
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
            leading.append(block)

    return steps, leading


def _risk_demonstration(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The risk's Demonstration section in the manual-testing shape the risk API already serves."""
    sections = split_sections(blocks)
    body = sections.named.get(STEPS) or []
    if not body:
        return []

    used_keys: set[str] = set()
    steps, leading = _heading_steps(body, used_keys, [])
    if steps is None:
        steps, leading = _list_steps(body, used_keys, [])

    demonstration: list[dict[str, Any]] = []
    label: str | None = None
    for block in leading:
        if block.get("type") == "table":
            demonstration.append(
                {
                    "id": f"table-{len(demonstration) + 1}",
                    "type": "table",
                    "label": label,
                    "rows": block.get("rows") or [],
                }
            )
            label = None
        elif block.get("type") == "paragraph":
            label = str(block.get("text") or "") or None

    items = [_demonstration_step(step, label if index == 0 else None) for index, step in enumerate(steps)]
    if items:
        demonstration.append({"id": "steps-1", "type": "steps", "label": label, "items": items})
    return demonstration


def _demonstration_step(step: dict[str, Any], _label: str | None) -> dict[str, Any]:
    text = [str(step.get("text") or "")]
    commands: list[str] = []
    images: list[dict[str, Any]] = []
    for block in step.get("content") or []:
        kind = block.get("type")
        if kind == "code":
            commands.append(str(block.get("text") or ""))
        elif kind == "image":
            images.append(
                {key: value for key, value in block.items() if key in {"path", "alt", "caption", "width"}}
            )
        elif kind == "paragraph":
            text.append(str(block.get("text") or ""))
        elif kind == "caption":
            text.append(str(block.get("text") or ""))
        elif kind == "heading":
            text.append(str(block.get("text") or ""))
        elif kind == "list":
            text.extend(str(item.get("text") or "") for item in block.get("items") or [])
    return {
        "id": step["step_key"],
        "title": step.get("step_title"),
        "text": "\n\n".join(part for part in text if part.strip()),
        "commands": commands,
        "images": images,
    }


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


def _control_title(front_matter: dict[str, Any], description: str, control_id: str) -> str:
    declared = str(front_matter.get("title") or "").strip()
    if declared:
        return declared
    if description:
        return _step_title(description, 0)
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
