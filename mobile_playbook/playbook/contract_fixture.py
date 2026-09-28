"""
Exports or checks one parsed playbook control as the frontend contract fixture.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
from pathlib import Path
from typing import Any

from mobile_playbook.playbook import catalogue

logger = logging.getLogger(__name__)


# Build the catalogue from a playbook root and return one control's payload for the contract fixture.
def export_contract(root: Path, platform: str, control_id: str) -> dict[str, Any]:
    built = catalogue.build(platform, Path(root).expanduser().resolve(), overrides={})
    control = built["controls"].get(catalogue.canonical_id(control_id, platform))
    if control is None:
        logger.debug("playbook contract: control %s not in catalogue with controls %s", control_id, sorted(built["controls"]))
        raise ValueError(f"Control not found: {control_id}")
    payload = dict(control)
    payload["has_source_archive"] = bool(control.get("source_download_url"))
    logger.debug(
        "playbook contract: exported %s revision %s with %s steps, has_source_archive=%s",
        control.get("control_id"),
        control.get("playbook_revision"),
        control.get("step_count"),
        payload["has_source_archive"],
    )
    return payload


# CLI entry point that prints, writes or diff-checks the exported contract fixture.
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export one parsed control as a frontend contract fixture.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--control-id", required=True)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    encoded = json.dumps(export_contract(args.root, args.platform, args.control_id), indent=2, sort_keys=True) + "\n"
    logger.debug("playbook contract: encoded fixture is %s chars", len(encoded))
    if args.check:
        actual = args.check.read_text(encoding="utf-8") if args.check.is_file() else ""
        logger.debug("playbook contract: checking against %s (%s chars, matches=%s)", args.check, len(actual), actual == encoded)
        if actual != encoded:
            print(
                "".join(
                    difflib.unified_diff(
                        actual.splitlines(keepends=True),
                        encoded.splitlines(keepends=True),
                        fromfile=str(args.check),
                        tofile="generated playbook contract",
                    )
                ),
                end="",
            )
            return 1
        print(f"Playbook contract matches {args.check}")
    elif args.output:
        logger.debug("playbook contract: writing fixture to %s", args.output)
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
