"""
Turns one playbook Markdown document into a normalized risk or control record.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

from mobile_playbook.playbook import markdown, source

logger = logging.getLogger(__name__)

ACTIVE = "active"
DEPRECATED = "deprecated"
DEPRIORITIZED = "deprioritized"
CONTROL_STATUSES = (ACTIVE, DEPRECATED, DEPRIORITIZED)

REFERENCES_HEADING = re.compile(r"^references\b", re.I)
REFERENCES_PARAGRAPH = re.compile(r"^references\s*:?\s*$", re.I)
CONTROL_MEASURES_PARAGRAPH = re.compile(r"control measures\s*:?\s*$", re.I)

TITLE = "title"
DESCRIPTION = "description"
STEPS = "steps"
REFERENCES = "references"

SECTION_NAMES = {
    "title": TITLE,
    "description": DESCRIPTION,
    "demonstration": STEPS,
    "remediation": STEPS,
    "references": REFERENCES,
}
DEPRIORITIZED_MARKER = re.compile(r"\(depriorit[iz]s?[ei]d?\)|\bdepriorit[iz]", re.I)
DEPRECATED_MARKER = re.compile(r"\(deprecated\)|\bdeprecated\b", re.I)
CONTROL_ID = re.compile(r"^(?P<platform>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)-control-(?P<control>\d+)", re.I)
RISK_ID = re.compile(r"^(?P<platform>[a-z0-9]+)-feature-(?P<feature>\d+)-risk-(?P<risk>\d+)\s*$", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MITRE_MARKER = re.compile(r"MITRE\s+ATT&CK", re.I)
MITRE_ANNOTATION = re.compile(
    r"MITRE\s+ATT&CK\s*:\s*(?P<tactic>[^-\u2013\u2014()]+?)\s*[-\u2013\u2014]\s*(?P<tactic_id>TA\d{4})",
    re.I,
)
EMPHASIS_EDGE = re.compile(r"^[\s*_`]+|[\s*_`]+$")
TRAILING_MARKER = re.compile(r"[_\s]*\((?:depriorit|deprecat)[^)]*\)\s*$", re.I)

STEP_TITLE_MAX_CHARS = 90


# Return the single MITRE tactic and TA id named in a Description, or None with a malformed or conflict warning.
def mitre_annotation(description: str) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    if not MITRE_MARKER.search(description or ""):
        return None, []

    found: list[tuple[str, str]] = []
    for match in MITRE_ANNOTATION.finditer(description):
        tactic = EMPHASIS_EDGE.sub("", match.group("tactic")).strip()
        if tactic:
            found.append((tactic, match.group("tactic_id").upper()))

    if not found:
        logger.debug("playbook parser: MITRE ATT&CK named without a tactic and TA identifier")
        return None, [
            {
                "code": "malformed_mitre_annotation",
                "message": "names MITRE ATT&CK without a tactic and TA identifier",
            }
        ]
    distinct = sorted(set(found))
    if len(distinct) > 1:
        detail = ", ".join(f"{tactic} ({tactic_id})" for tactic, tactic_id in distinct)
        logger.debug("playbook parser: conflicting MITRE tactics: %s", detail)
        return None, [{"code": "conflicting_mitre_annotation", "message": detail}]
    tactic, tactic_id = distinct[0]
    logger.debug("playbook parser: MITRE tactic %s (%s)", tactic, tactic_id)
    return {"tactic": tactic, "tactic_id": tactic_id}, []


# Strip a trailing status marker so a filename variation never changes an identity.
def document_id(stem_or_heading: str) -> str:
    return TRAILING_MARKER.sub("", stem_or_heading).strip()


# Return the sha256 revision of a document's raw bytes.
def revision(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# Infer deprecated or deprioritized status from naming markers, returning whether a marker matched.
def infer_status(*candidates: str) -> tuple[str, bool]:
    for candidate in candidates:
        if candidate and DEPRECATED_MARKER.search(candidate):
            return DEPRECATED, True
    for candidate in candidates:
        if candidate and DEPRIORITIZED_MARKER.search(candidate):
            return DEPRIORITIZED, True
    return ACTIVE, False


# Map a declared status and its spelling variants to a control status, or None when unrecognized.
def normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in {"deprioritised", "deprioritized", "deprioritise", "deprioritize"}:
        return DEPRIORITIZED
    if text in {"deprecated", "deprecate", "retired"}:
        return DEPRECATED
    if text in {"active", "current", "required"}:
        return ACTIVE
    return None


# Return the text of the first heading at the given level, or None.
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


# Parse a document once, taking its identity from the level-2 heading and falling back to the filename.
def read_document(path: Path) -> Document:
    raw = path.read_bytes()
    front_matter, body = markdown.split_front_matter(raw.decode("utf-8", errors="replace"))
    blocks = markdown.parse_blocks(body)
    heading = heading_of(blocks)
    heading_id = document_id(heading) if heading else None
    file_id = document_id(path.stem)
    logger.debug(
        "playbook parser: read %s (%s bytes, %s blocks, front matter keys %s): heading id %s, file id %s, identity from %s",
        path,
        len(raw),
        len(blocks),
        sorted(map(str, front_matter)),
        heading_id,
        file_id,
        "heading" if heading_id else "filename",
    )
    return Document(path, raw, front_matter, blocks, heading_id, file_id, heading_id or file_id)


# Build a control record with its status, title, intro, steps, references, archives and parse warnings.
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
        logger.debug("playbook parser: control %s has step sections but no Description", control_id)
        parse_warnings.append({"code": "missing_description", "message": "no Description section"})

    marker_status, from_marker = infer_status(path.name, heading_id or "")
    declared_status = normalize_status(front_matter.get("status"))
    status = declared_status or marker_status

    record = {
        "control_id": control_id,
        "title": _control_title(front_matter, _section_text(blocks, "title"), description, control_id),
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
    logger.debug(
        "playbook parser: control %s status %s (%s) revision %s: %s intro blocks, %s steps, %s references, %s archives",
        control_id,
        status,
        record["status_source"],
        record["playbook_revision"],
        len(intro),
        len(steps),
        len(references),
        len(archives),
    )
    if logger.isEnabledFor(logging.DEBUG):
        for step in steps:
            logger.debug(
                "playbook parser: control %s step %s #%s (%s id) %r",
                control_id,
                step.get("step_key"),
                step.get("number"),
                step.get("step_id_source"),
                str(step.get("step_title") or "")[:80],
            )
        if parse_warnings:
            logger.debug("playbook parser: control %s parse warnings %s", control_id, [note.get("code") for note in parse_warnings])
    return record


# Build a risk record with its title, description, MITRE tactic, demonstration, control links and status.
def parse_risk(document: Document, root: Path) -> dict[str, Any]:
    path, raw, front_matter, blocks = document.path, document.raw, document.front_matter, document.blocks
    heading_id = document.heading_id

    logger.debug("playbook parser: parsing risk %s from %s", document.identity, path)
    description = _section_text(blocks, "description")
    mitre, parse_warnings = mitre_annotation(description)
    return {
        "risk_id": document.identity,
        "playbook_revision": revision(raw),
        "source_file": source.relative_to_root(root, path),
        "title": _section_text(blocks, "title"),
        "description": description,
        "tactic": (mitre or {}).get("tactic"),
        "tactic_id": (mitre or {}).get("tactic_id"),
        "demonstration": _risk_demonstration(blocks),
        "control_links": _control_links(blocks),
        "references": _collect_references(_partition(blocks)[2], [], []),
        "status": (normalize_status(front_matter.get("status")) or infer_status(path.name, heading_id or "")[0]),
        "heading_id": heading_id,
        "file_id": document.file_id,
        "parse_warnings": parse_warnings,
    }


class Sections(NamedTuple):
    named: dict[str, list[dict[str, Any]]]
    loose: list[dict[str, Any]]
    has_step_section: bool


# Group blocks under the recognized level-3 section headings, keeping the rest as loose blocks.
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


# Split a document into intro blocks, steps and reference blocks, supporting sectioned and legacy layouts.
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
        logger.debug("playbook parser: step section with %s blocks, heading steps %s", len(body), "none" if steps is None else len(steps))
        if steps is None:
            steps, leading = _list_steps(body, used_keys, notes)
            logger.debug("playbook parser: step section parsed as list steps: %s", len(steps))
            if not steps:
                notes.append({"code": "empty_step_section", "message": "no numbered steps"})
        intro = _intro_blocks(_after_title(sections.named.get(TITLE) or []))
        intro.extend(_intro_blocks(sections.named.get(DESCRIPTION) or []))
        intro.extend(_intro_blocks(sections.loose))
        intro.extend(_intro_blocks(leading))
        return intro, steps, references

    # Legacy documents name no section: ordered lists before the references are the steps.
    body = _after_title(sections.named.get(TITLE) or []) + sections.loose
    steps, intro = _list_steps(body, used_keys, notes)
    logger.debug("playbook parser: legacy document without step sections, %s list steps", len(steps))
    intro = _intro_blocks(_intro_blocks(sections.named.get(DESCRIPTION) or []) + intro)
    return intro, steps, references


# Return what a Title section holds after its title paragraph.
def _after_title(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, block in enumerate(blocks):
        if block.get("type") == "paragraph":
            return blocks[index + 1 :]
    return blocks


# Drop step id blocks from a list of blocks.
def _intro_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block for block in blocks if block.get("type") != "step_id"]


# Build steps from numbered headings, keeping deeper headings in the step they follow; None when there are none.
def _heading_steps(
    blocks: list[dict[str, Any]],
    used_keys: set[str],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
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

    trailing = False
    for block in blocks:
        if block.get("type") == "step_id":
            declared = str(block.get("value") or "").strip() or None
            continue

        if block.get("type") == "paragraph" and CONTROL_MEASURES_PARAGRAPH.search(str(block.get("text") or "")):
            trailing = True
            continue

        heading = None
        if block.get("type") == "heading" and int(block.get("level", 0) or 0) == step_level:
            heading = markdown.NUMBERED_HEADING.match(str(block.get("text") or ""))

        if heading is None:
            if trailing:
                continue
            if current is None:
                leading.append(block)
            else:
                current["content"].append(block)
            continue

        trailing = False
        number = int(heading.group(1))
        title = " ".join(heading.group(2).split()) or f"Step {len(steps) + 1}"
        if number in seen_numbers:
            logger.debug("playbook parser: duplicate step number %s", number)
            warnings.append({"code": "duplicate_step_number", "path": str(number), "message": f"step number {number}"})
        seen_numbers.add(number)

        generated = _auto_step_id(title)
        candidate = declared or generated
        if candidate in used_keys:
            code = "duplicate_step_id" if declared else "duplicate_generated_step_id"
            logger.debug("playbook parser: %s %s, assigning a suffixed key", code, candidate)
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


# Build steps from top-level ordered-list items, the original format, returning them and the leading blocks.
def _list_steps(
    blocks: list[dict[str, Any]],
    used_keys: set[str],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
                    logger.debug("playbook parser: %s %s, assigning a suffixed key", code, candidate)
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


# Return a risk's Demonstration section as table and steps sections in the manual-testing shape the API serves.
def _risk_demonstration(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    logger.debug("playbook parser: risk demonstration has %s steps and %s sections", len(items), len(demonstration))
    return demonstration


DEMONSTRATION_BLOCK_KEYS = {
    "paragraph": ("text",),
    "caption": ("text",),
    "heading": ("level", "text"),
    "code": ("language", "text"),
    "image": ("path", "alt", "caption", "width"),
    "list": ("ordered", "items"),
    "table": ("columns", "rows"),
}


# Keep a block's served fields in place so a caption still follows its image, or None for other types.
def _demonstration_block(block: dict[str, Any]) -> dict[str, Any] | None:
    keys = DEMONSTRATION_BLOCK_KEYS.get(str(block.get("type") or ""))
    if keys is None:
        return None
    kept = {"type": block["type"], **{key: block[key] for key in keys if key in block}}
    if kept["type"] == "list":
        kept["items"] = [{"text": str(item.get("text") or "")} for item in block.get("items") or []]
    return kept


# Convert a parsed step into a demonstration item with its content, commands and images.
def _demonstration_step(step: dict[str, Any], _label: str | None) -> dict[str, Any]:
    content = [
        normalized
        for normalized in (_demonstration_block(block) for block in step.get("content") or [])
        if normalized is not None
    ]
    return {
        "id": step["step_key"],
        "title": step.get("step_title"),
        "text": str(step.get("text") or ""),
        "content": content,
        "commands": [block["text"] for block in content if block["type"] == "code" and block.get("text")],
        "images": [
            {key: value for key, value in block.items() if key != "type"}
            for block in content
            if block["type"] == "image"
        ],
    }


# Derive a step id from its normalized text, so reordering keeps progress but rewording makes a new step.
def _auto_step_id(text: str) -> str:
    normalized = " ".join(text.split()).casefold()
    return f"auto-{hashlib.sha256(normalized.encode()).hexdigest()[:12]}"


# Return the key, or the first free numbered suffix of it, and mark it used.
def _unique(key: str, used: set[str]) -> str:
    candidate, ordinal = key, 1
    while candidate in used:
        ordinal += 1
        candidate = f"{key}-{ordinal}"
    used.add(candidate)
    return candidate


# Return the first sentence of a step's text, truncated to STEP_TITLE_MAX_CHARS, or a numbered fallback.
def _step_title(text: str, position: int) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return f"Step {position + 1}"
    first = SENTENCE_END.split(cleaned, maxsplit=1)[0].rstrip(".")
    if len(first) <= STEP_TITLE_MAX_CHARS:
        return first
    return f"{first[: STEP_TITLE_MAX_CHARS - 1].rstrip()}…"


# Return the declared or Title-section title, else the description's first sentence, else a numbered fallback.
def _control_title(front_matter: dict[str, Any], titled: str, description: str, control_id: str) -> str:
    declared = str(front_matter.get("title") or "").strip() or titled.strip()
    if declared:
        return declared
    if description:
        return _step_title(description, 0)
    match = CONTROL_ID.match(control_id)
    return f"Control {int(match.group('control'))}" if match else control_id


# Return the text of the first intro paragraph, or an empty string.
def _summary(intro: list[dict[str, Any]]) -> str:
    for block in intro:
        if block.get("type") == "paragraph":
            return str(block.get("text") or "")
    return ""


# Return the paragraph text under the named heading, only the first paragraph for Title.
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
            if heading == TITLE:
                break
    return ("\n\n".join(parts) if heading != TITLE else " ".join(parts)).strip()


# Return the distinct non-image links to Markdown documents in the blocks.
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


# Return the distinct non-image links to source archives in the blocks.
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


# Return the distinct http(s) links in the reference blocks, excluding archives.
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


# Return the texts of a paragraph, caption, list or table block that may contain links.
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


# Return (url, label) pairs for http(s) Markdown links and bare URLs in a text.
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
