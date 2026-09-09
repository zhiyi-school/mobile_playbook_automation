#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

HEADER = """# Generated from [project.dependencies] in pyproject.toml.
# Run `python scripts/check_requirements.py --write` after changing dependencies.
# This mirrors direct constraints; it does not lock transitive packages.
"""


def rendered(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = data.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ValueError("pyproject.toml must define project.dependencies as a list of strings")
    return HEADER + "\n".join(dependencies) + "\n"


def synchronized(pyproject: Path, requirements: Path) -> bool:
    actual = requirements.read_text(encoding="utf-8") if requirements.is_file() else ""
    return actual == rendered(pyproject)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Check or regenerate requirements.txt from pyproject.toml.")
    parser.add_argument("--pyproject", type=Path, default=root / "pyproject.toml")
    parser.add_argument("--requirements", type=Path, default=root / "requirements.txt")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    expected = rendered(args.pyproject)
    if args.write:
        args.requirements.write_text(expected, encoding="utf-8")
        print(f"Wrote {args.requirements}")
        return 0
    if not synchronized(args.pyproject, args.requirements):
        print("requirements.txt is out of sync; run python scripts/check_requirements.py --write", file=sys.stderr)
        return 1
    print("Dependency declarations are synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
