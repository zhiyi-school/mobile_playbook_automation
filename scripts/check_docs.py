#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REFERENCE_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
HTML_TAG = re.compile(r"<[^>]+>")
MACHINE_PATH = re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")
SOURCE_PREFIXES = (
    ".github/",
    "configs/",
    "docs/",
    "mobile_playbook/",
    "scripts/",
    "src/",
    "supabase/",
    "tests/",
)


def visible_lines(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    marker: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            token = match.group(1)
            if marker is None:
                marker = token[0]
            elif token[0] == marker:
                marker = None
            continue
        if marker is None:
            visible.append((number, line))
    return visible


def heading_anchors(lines: list[tuple[int, str]]) -> set[str]:
    anchors: set[str] = set()
    counts: Counter[str] = Counter()
    for _, line in lines:
        match = HEADING.match(line)
        if not match:
            continue
        label = HTML_TAG.sub("", match.group(2))
        label = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", label)
        label = label.replace("`", "").strip().lower()
        slug = re.sub(r"[^\w\- ]", "", label, flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug)
        suffix = counts[slug]
        counts[slug] += 1
        anchors.add(f"{slug}-{suffix}" if suffix else slug)
    for _, line in lines:
        anchors.update(re.findall(r"<(?:a|span)\s+(?:name|id)=[\"']([^\"']+)", line, re.IGNORECASE))
    return anchors


def link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def required_documents(root: Path) -> tuple[str, ...]:
    if (root / "pyproject.toml").is_file():
        return (
            "README.md",
            "docs/README.md",
            "docs/setup.md",
            "docs/architecture.md",
            "docs/testing.md",
            "docs/developer-playbook.md",
        )
    if (root / "package.json").is_file():
        return (
            "README.md",
            "docs/setup.md",
            "docs/architecture.md",
            "docs/testing.md",
            "docs/frontend-integration.md",
            "docs/database-maintenance.md",
        )
    return ("README.md",)


def validate(root: Path) -> list[str]:
    root = root.resolve()
    markdown_files = sorted({root / "README.md", *(root / "docs").rglob("*.md")})
    markdown_files = [path for path in markdown_files if path.is_file()]
    parsed = {path.resolve(): visible_lines(path.read_text(encoding="utf-8")) for path in markdown_files}
    anchors = {path: heading_anchors(lines) for path, lines in parsed.items()}
    errors: list[str] = []
    allowlist_path = root / "docs" / "doc-validation-allowlist.txt"
    allowed = {
        tuple(line.split("|", 1))
        for line in (allowlist_path.read_text(encoding="utf-8").splitlines() if allowlist_path.is_file() else [])
        if line and not line.startswith("#") and "|" in line
    }
    used_allowlist: set[tuple[str, str]] = set()

    for required in required_documents(root):
        if not (root / required).is_file():
            errors.append(f"missing required documentation entry point: {required}")

    for path, lines in parsed.items():
        relative = path.relative_to(root)
        for number, line in lines:
            machine = MACHINE_PATH.search(line)
            if machine:
                errors.append(f"{relative}:{number}: machine-specific path: {machine.group(0)}")

            targets = [match.group(1) for match in INLINE_LINK.finditer(line)]
            definition = REFERENCE_DEFINITION.match(line)
            if definition:
                targets.append(definition.group(1))
            for raw in targets:
                target = link_target(raw)
                parsed_url = urlsplit(target)
                if parsed_url.scheme:
                    if parsed_url.scheme in {"http", "https"} and not parsed_url.netloc:
                        errors.append(f"{relative}:{number}: malformed external URL: {target}")
                    continue
                if target.startswith("/"):
                    continue
                target_path, _, fragment = target.partition("#")
                destination = path if not target_path else (path.parent / unquote(target_path)).resolve()
                if not destination.exists():
                    errors.append(f"{relative}:{number}: missing local link target: {target}")
                    continue
                if fragment and destination.is_file() and destination.suffix.lower() == ".md":
                    destination = destination.resolve()
                    if unquote(fragment).lower() not in anchors.get(destination, set()):
                        errors.append(f"{relative}:{number}: missing heading anchor: {target}")

            for candidate in INLINE_CODE.findall(line):
                candidate = candidate.rstrip(".,:;")
                if not candidate.startswith(SOURCE_PREFIXES) or any(char in candidate for char in "<>*{}"):
                    continue
                source_path = candidate.partition("#")[0]
                if not (root / source_path).exists():
                    exception = (str(relative), candidate)
                    if exception in allowed:
                        used_allowlist.add(exception)
                    else:
                        errors.append(f"{relative}:{number}: missing referenced source path: {candidate}")
    for document, reference in sorted(allowed - used_allowlist):
        errors.append(f"unused documentation validation exception: {document}|{reference}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate repository Markdown without network access.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    errors = validate(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Documentation checks passed for {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
