"""Parse the playbook's Markdown into normalized blocks the API can serve as JSON."""

from __future__ import annotations

import re
from typing import Any, Iterator, NamedTuple

import yaml

FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
ORDERED_ITEM = re.compile(r"^(\d+)[.)]\s*(.*)$")
BULLET_ITEM = re.compile(r"^[-*+]\s+(.*)$")
FENCE = re.compile(r"^(`{3,}|~{3,})\s*([^\s`~]*)\s*$")
TABLE_DIVIDER = re.compile(r"^\|?(?:\s*:?-{1,}:?\s*\|)+\s*:?-{0,}:?\s*\|?$")
HTML_IMG = re.compile(r"<img\b([^>]*?)/?>", re.I)
HTML_TAG = re.compile(r"<[^>]+>")
ATTRIBUTE = re.compile(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
EMPHASIS_ONLY = re.compile(r"\A\*{1,3}([^*].*?)\*{1,3}\Z|\A_{1,3}([^_].*?)_{1,3}\Z", re.S)
STEP_ID_COMMENT = re.compile(r"^<!--\s*playbook-step-id\s*:\s*([A-Za-z0-9][\w.-]*)\s*-->$")
NUMBERED_HEADING = re.compile(r"^(\d{1,3})\s*[.)]\s*(.*)$")


class Link(NamedTuple):
    label: str
    target: str
    is_image: bool


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Optional YAML front matter, so a playbook author can state control status in the source file."""
    match = FRONT_MATTER.match(text)
    if match is None:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, text[match.end() :]
    return (data if isinstance(data, dict) else {}), text[match.end() :]


def iter_links(text: str) -> Iterator[Link]:
    """Markdown links, tolerating balanced parentheses inside the target."""
    index = 0
    while index < len(text):
        start = text.find("[", index)
        if start < 0:
            return
        close = text.find("]", start)
        if close < 0:
            return
        if close + 1 >= len(text) or text[close + 1] != "(":
            index = close + 1
            continue
        depth = 0
        cursor = close + 1
        while cursor < len(text):
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if cursor >= len(text):
            return
        target = text[close + 2 : cursor].strip()
        if target:
            yield Link(text[start + 1 : close], target, start > 0 and text[start - 1] == "!")
        index = cursor + 1


def html_images(text: str) -> list[dict[str, str]]:
    images = []
    for match in HTML_IMG.finditer(text):
        attributes = {
            name.lower(): (double or single or bare or "")
            for name, double, single, bare in ATTRIBUTE.findall(match.group(1))
        }
        src = attributes.get("src", "").strip()
        if src:
            images.append({"path": src, "alt": attributes.get("alt", "").strip(), "width": attributes.get("width", "")})
    return images


def strip_inline_html(text: str) -> str:
    return HTML_TAG.sub("", text).strip()


def caption_text(text: str) -> str | None:
    """An emphasis-only paragraph, which is how the playbook writes screenshot captions."""
    match = EMPHASIS_ONLY.match(text.strip())
    if match is None:
        return None
    return " ".join((match.group(1) or match.group(2) or "").split()) or None


def parse_blocks(text: str) -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal buffer
        if buffer:
            blocks.extend(_paragraph_blocks("\n".join(buffer)))
            buffer = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        fence = FENCE.match(stripped)
        if fence is not None:
            flush()
            marker = fence.group(1)[0]
            body: list[str] = []
            index += 1
            while index < len(lines):
                closing = FENCE.match(lines[index].strip())
                if closing is not None and closing.group(1)[0] == marker:
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            blocks.append({"type": "code", "language": fence.group(2) or None, "text": "\n".join(body).strip("\n")})
            continue

        heading = HEADING.match(stripped)
        if heading is not None:
            flush()
            blocks.append({"type": "heading", "level": len(heading.group(1)), "text": heading.group(2).strip()})
            index += 1
            continue

        if stripped.startswith("|") and _is_table_start(lines, index):
            flush()
            table, index = _consume_table(lines, index)
            blocks.append(table)
            continue

        if ORDERED_ITEM.match(stripped) or BULLET_ITEM.match(stripped) or _opens_list(lines, index):
            flush()
            listing, index = _consume_list(lines, index)
            blocks.append(listing)
            continue

        declared = STEP_ID_COMMENT.match(stripped)
        if declared is not None:
            flush()
            blocks.append({"type": "step_id", "value": declared.group(1)})
            index += 1
            continue

        buffer.append(line)
        index += 1

    flush()
    return _attach_captions(blocks)


def _attach_captions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The playbook writes a screenshot caption as its own italic paragraph under the image."""
    merged: list[dict[str, Any]] = []
    for block in blocks:
        previous = merged[-1] if merged else None
        if block.get("type") == "caption" and previous is not None and previous.get("type") == "image":
            if not previous.get("caption"):
                previous["caption"] = block["text"]
                continue
        merged.append(block)
    return merged


def _paragraph_blocks(raw: str) -> list[dict[str, Any]]:
    """A paragraph, plus any HTML images it contains lifted out as their own blocks."""
    images = html_images(raw)
    text = strip_inline_html(raw)
    blocks: list[dict[str, Any]] = []

    remaining = text
    for label, target, is_image in list(iter_links(text)):
        if not is_image:
            continue
        images.append({"path": target, "alt": label, "width": ""})
        remaining = remaining.replace(f"![{label}]({target})", "").strip()

    if remaining:
        caption = caption_text(remaining)
        if caption is not None and images:
            images[-1]["caption"] = caption
        elif caption is not None:
            blocks.append({"type": "caption", "text": caption})
        else:
            blocks.append({"type": "paragraph", "text": " ".join(remaining.split())})

    blocks.extend({"type": "image", **image} for image in images)
    return blocks


def _is_table_start(lines: list[str], index: int) -> bool:
    return index + 1 < len(lines) and bool(TABLE_DIVIDER.match(lines[index + 1].strip()))


def _consume_table(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    columns = _table_cells(lines[index])
    index += 2
    rows: list[dict[str, str]] = []
    while index < len(lines) and lines[index].strip().startswith("|"):
        cells = _table_cells(lines[index])
        rows.append({column: (cells[position] if position < len(cells) else "") for position, column in enumerate(columns)})
        index += 1
    return {"type": "table", "columns": columns, "rows": rows}, index


def _table_cells(line: str) -> list[str]:
    return [strip_inline_html(cell.strip()) for cell in line.strip().strip("|").split("|")]


def _opens_list(lines: list[str], index: int) -> bool:
    """A step-id comment counts as the start of the list it labels."""
    if STEP_ID_COMMENT.match(lines[index].strip()) is None:
        return False
    following = _skip_step_ids(lines, index)
    return following < len(lines) and ORDERED_ITEM.match(lines[following].strip()) is not None


def _skip_step_ids(lines: list[str], index: int) -> int:
    while index < len(lines) and STEP_ID_COMMENT.match(lines[index].strip()) is not None:
        index += 1
    return index


def _consume_list(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    ordered = ORDERED_ITEM.match(lines[_skip_step_ids(lines, index)].strip()) is not None
    items: list[dict[str, Any]] = []
    step_id: str | None = None
    while index < len(lines):
        stripped = lines[index].strip()
        declared = STEP_ID_COMMENT.match(stripped)
        if declared is not None:
            step_id = declared.group(1)
            index += 1
            continue
        match = ORDERED_ITEM.match(stripped) if ordered else BULLET_ITEM.match(stripped)
        if match is None:
            break
        number = int(match.group(1)) if ordered else None
        parts = [match.group(2) if ordered else match.group(1)]
        index += 1
        while index < len(lines):
            following = lines[index]
            if not following.strip():
                break
            if ORDERED_ITEM.match(following.strip()) or BULLET_ITEM.match(following.strip()):
                break
            if not following.startswith((" ", "\t")):
                break
            parts.append(following.strip())
            index += 1
        items.append({"number": number, "text": " ".join(" ".join(parts).split()), "step_id": step_id})
        step_id = None
    return {"type": "list", "ordered": ordered, "items": items}, index
