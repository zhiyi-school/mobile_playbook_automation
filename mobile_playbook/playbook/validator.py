from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from mobile_playbook.playbook import catalogue, controls, markdown, source

ERROR_CODES = frozenset(
    {
        "control_without_risk",
        "cross_risk_control_link",
        "duplicate_document_id",
        "duplicate_generated_step_id",
        "duplicate_step_id",
        "malformed_step_id",
        "missing_control_file",
        "missing_image",
        "missing_local_link",
        "missing_source_archive",
        "reference_outside_root",
        "unrecognized_document_identity",
    }
)
EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto"})


def validate(root: Path, platform: str) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return _result(root, platform, [_diagnostic("playbook_root_unreadable", "error", ".", None, f"Playbook root is not a readable directory: {root}")])

    built = catalogue.build(platform, root, overrides={})
    diagnostics = [_from_catalogue(item) for item in built["warnings"]]
    for path in source.markdown_files(root):
        diagnostics.extend(_validate_document(root, platform, path))
    diagnostics = _deduplicate(diagnostics)
    return _result(root, platform, diagnostics, built)


def _validate_document(root: Path, platform: str, path: Path) -> list[dict[str, Any]]:
    relative = source.relative_to_root(root, path)
    text = path.read_text(errors="replace")
    document = controls.read_document(path)
    diagnostics: list[dict[str, Any]] = []
    identity_looks_managed = "-risk-" in document.identity or "-control-" in document.identity
    if identity_looks_managed and not catalogue.CONTROL_DOCUMENT.match(document.identity) and not catalogue.RISK_DOCUMENT.match(document.identity):
        diagnostics.append(
            _diagnostic(
                "unrecognized_document_identity",
                "error",
                relative,
                1,
                f"Document identity {document.identity!r} is neither a risk nor a control identifier.",
            )
        )

    for line_number, line in enumerate(text.splitlines(), 1):
        if "playbook-step-id" in line.casefold() and markdown.STEP_ID_COMMENT.match(line.strip()) is None:
            diagnostics.append(
                _diagnostic(
                    "malformed_step_id",
                    "error",
                    relative,
                    line_number,
                    "Step id directive must be `<!-- playbook-step-id: stable-id -->` on its own line.",
                )
            )

    anchors = _heading_anchors(document.blocks)
    for line_number, line in enumerate(text.splitlines(), 1):
        for link in markdown.iter_links(line):
            diagnostics.extend(_validate_link(root, path, relative, line_number, link, anchors))
    return diagnostics


def _validate_link(
    root: Path,
    document_path: Path,
    relative: str,
    line_number: int,
    link: markdown.Link,
    current_anchors: set[str],
) -> list[dict[str, Any]]:
    target = link.target.strip().strip("<>")
    if target.startswith("data:"):
        return []
    parsed = urlsplit(target)
    if parsed.scheme in EXTERNAL_SCHEMES:
        if parsed.scheme in {"http", "https"} and not parsed.netloc:
            return [_diagnostic("invalid_external_url", "warning", relative, line_number, f"External URL is malformed: {target}")]
        return []
    if parsed.scheme or target.startswith("//"):
        return [_diagnostic("unsupported_link_scheme", "warning", relative, line_number, f"Link scheme is not validated: {target}")]

    decoded_path = unquote(parsed.path)
    candidate_relative = Path(relative).parent / decoded_path if decoded_path else Path(relative)
    resolved = source.resolve_within(root, candidate_relative.as_posix())
    if resolved is None:
        return [_diagnostic("reference_outside_root", "error", relative, line_number, f"Reference leaves the playbook root: {target}")]
    if not resolved.is_file():
        return [_diagnostic("missing_local_link", "error", relative, line_number, f"Referenced file does not exist: {target}")]
    if parsed.fragment and resolved.suffix.lower() == ".md":
        anchors = current_anchors if resolved == document_path.resolve() else _heading_anchors(controls.read_document(resolved).blocks)
        wanted = unquote(parsed.fragment).casefold()
        if wanted not in anchors:
            return [_diagnostic("missing_heading_anchor", "warning", relative, line_number, f"Heading anchor #{parsed.fragment} does not exist in {source.relative_to_root(root, resolved)}.")]
    return []


def _heading_anchors(blocks: list[dict[str, Any]]) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for block in blocks:
        if block.get("type") != "heading":
            continue
        base = re.sub(r"[^\w\s-]", "", str(block.get("text") or "").casefold())
        base = re.sub(r"[\s-]+", "-", base).strip("-")
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _from_catalogue(item: dict[str, Any]) -> dict[str, Any]:
    code = str(item.get("code") or "catalogue_warning")
    return _diagnostic(
        code,
        "error" if code in ERROR_CODES else "warning",
        str(item.get("file") or "."),
        None,
        str(item.get("message") or code),
        section=str(item.get("path")) if item.get("path") else None,
    )


def _diagnostic(
    code: str,
    severity: str,
    path: str,
    line: int | None,
    message: str,
    *,
    section: str | None = None,
) -> dict[str, Any]:
    return {"code": code, "severity": severity, "path": path, "line": line, "section": section, "message": message}


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        key = (item["code"], item["path"], item["line"], item["section"], item["message"])
        unique[key] = item
    return sorted(unique.values(), key=lambda item: (item["path"], item["line"] or 0, item["code"]))


def _result(root: Path, platform: str, diagnostics: list[dict[str, Any]], built: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "root": str(root),
        "platform": platform,
        "revision": built.get("revision") if built else None,
        "risk_count": len(built.get("risks", {})) if built else 0,
        "control_count": len(built.get("controls", {})) if built else 0,
        "errors": sum(item["severity"] == "error" for item in diagnostics),
        "warnings": sum(item["severity"] == "warning" for item in diagnostics),
        "diagnostics": diagnostics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a developer playbook without modifying it.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as a failing result.")
    args = parser.parse_args(argv)
    result = validate(args.root, args.platform)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for item in result["diagnostics"]:
            location = item["path"] + (f":{item['line']}" if item["line"] else "")
            print(f"{item['severity'].upper()} {item['code']} {location}: {item['message']}")
        print(f"{result['errors']} error(s), {result['warnings']} warning(s)")
    return 1 if result["errors"] or (args.strict and result["warnings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
