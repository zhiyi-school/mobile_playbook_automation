from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

from mobile_playbook.playbook import catalogue


def export_contract(root: Path, platform: str, control_id: str) -> dict[str, Any]:
    built = catalogue.build(platform, Path(root).expanduser().resolve(), overrides={})
    control = built["controls"].get(catalogue.canonical_id(control_id, platform))
    if control is None:
        raise ValueError(f"Control not found: {control_id}")
    payload = dict(control)
    payload["has_source_archive"] = bool(control.get("source_download_url"))
    return payload


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
    if args.check:
        actual = args.check.read_text(encoding="utf-8") if args.check.is_file() else ""
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
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
