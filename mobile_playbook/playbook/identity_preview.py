from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mobile_playbook.playbook import catalogue


def preview(old_root: Path, new_root: Path, platform: str) -> dict[str, Any]:
    old = catalogue.build(platform, Path(old_root).resolve(), overrides={})
    new = catalogue.build(platform, Path(new_root).resolve(), overrides={})
    control_ids = sorted(set(old["controls"]) | set(new["controls"]))
    controls = []
    for control_id in control_ids:
        before = old["controls"].get(control_id)
        after = new["controls"].get(control_id)
        old_steps = _steps(before)
        new_steps = _steps(after)
        preserved = sorted(set(old_steps) & set(new_steps))
        controls.append(
            {
                "control_id": control_id,
                "status": "preserved" if before and after else "removed" if before else "added",
                "preserved": [
                    {
                        "step_key": key,
                        "old_source": old_steps[key],
                        "new_source": new_steps[key],
                        "content_changed": _content_hash(before, key) != _content_hash(after, key),
                    }
                    for key in preserved
                ],
                "removed": sorted(set(old_steps) - set(new_steps)),
                "added": sorted(set(new_steps) - set(old_steps)),
            }
        )
    return {
        "platform": platform,
        "old_root": str(Path(old_root).resolve()),
        "new_root": str(Path(new_root).resolve()),
        "controls": controls,
        "errors": [
            warning
            for warning in [*old["warnings"], *new["warnings"]]
            if warning.get("code") in {"duplicate_document_id", "duplicate_step_id", "duplicate_generated_step_id"}
        ],
    }


def _steps(control: dict[str, Any] | None) -> dict[str, str]:
    return {
        str(step["step_key"]): str(step.get("step_id_source") or "auto")
        for step in (control or {}).get("steps", [])
    }


def _content_hash(control: dict[str, Any] | None, key: str) -> str | None:
    step = next((item for item in (control or {}).get("steps", []) if item.get("step_key") == key), None)
    return str(step.get("content_hash")) if step else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview exact playbook step identities across an edit.")
    parser.add_argument("--old-root", required=True, type=Path)
    parser.add_argument("--new-root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    result = preview(args.old_root, args.new_root, args.platform)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for control in result["controls"]:
            print(f"{control['control_id']}: {control['status']}")
            for item in control["preserved"]:
                changed = " (content changed)" if item["content_changed"] else ""
                print(f"  = {item['step_key']}{changed}")
            for key in control["removed"]:
                print(f"  - {key}")
            for key in control["added"]:
                print(f"  + {key}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
