from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

INCLUDE_KEYS = ("include", "includes")


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_yaml_config(path: Path) -> dict[str, Any]:
    """Load a YAML config and resolve optional section includes.

    Supported shape:

    include:
      device: device.yaml
      runner: runner.yaml
      apps: apps.yaml

    Included files may contain either the raw section value or a mapping wrapped
    under the section name, for example both `device: {...}` and `{...}` work
    for the device section. Inline values in the parent config win over included
    values so teams can override a small field without copying the full file.

    A section's include value may also be a list of paths instead of one path,
    for example `apps: [templates.yaml, apps.yaml]`. Listed files are read and
    concatenated as raw text, in order, before being parsed as a single YAML
    document. This is what lets a `templates.yaml` file define reusable YAML
    anchors (`&name`) that a later file in the list (e.g. `apps.yaml`) can
    reference via aliases (`*name`) — anchors only resolve within one parsed
    document, so loading each file separately and merging the results afterward
    would not work for cross-file anchor references.
    """
    path = Path(path)
    with path.open("r") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping: {path}")
    return resolve_config_includes(raw, path.parent)


def resolve_config_includes(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    include_spec = _include_spec(raw)
    if not include_spec:
        return {key: deepcopy(value) for key, value in raw.items() if key not in INCLUDE_KEYS}
    if not isinstance(include_spec, dict):
        raise ValueError("include must be a mapping of config section to YAML file")

    resolved = {key: deepcopy(value) for key, value in raw.items() if key not in INCLUDE_KEYS}
    for section, include_path in include_spec.items():
        section_name = str(section)
        section_value = _load_include_section(Path(base_dir), section_name, include_path)
        if section_name in resolved:
            resolved[section_name] = merge_dicts(section_value, resolved[section_name])
        else:
            resolved[section_name] = section_value
    return resolved


def _include_spec(raw: dict[str, Any]) -> Any:
    for key in INCLUDE_KEYS:
        if key in raw:
            return raw[key]
    return None


def _load_include_section(base_dir: Path, section: str, include_path: Any) -> Any:
    paths = _section_include_paths(section, include_path)
    resolved_paths = [_resolve_include_path(base_dir, p) for p in paths]
    for path, original in zip(resolved_paths, paths):
        if not path.exists():
            raise ValueError(f"included config file does not exist for {section} ({original}): {path}")
    combined_text = "\n".join(path.read_text() for path in resolved_paths)
    loaded = yaml.safe_load(combined_text)
    if loaded is None:
        loaded = {}
    if isinstance(loaded, dict) and section in loaded:
        return deepcopy(loaded[section])
    return deepcopy(loaded)


def _section_include_paths(section: str, include_path: Any) -> list[str]:
    if isinstance(include_path, str):
        if not include_path.strip():
            raise ValueError(f"include.{section} must be a non-empty path")
        return [include_path]
    if isinstance(include_path, list) and include_path and all(isinstance(p, str) and p.strip() for p in include_path):
        return list(include_path)
    raise ValueError(f"include.{section} must be a non-empty path or a non-empty list of paths")


def _resolve_include_path(base_dir: Path, include_path: str) -> Path:
    path = Path(include_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def merge_dicts(base: Any, override: Any) -> Any:
    """Recursively merge `override` onto `base`, without mutating either.

    Dicts are merged key by key (recursing into nested dicts); any other
    value in `override` replaces the corresponding value from `base`
    entirely, unless it is empty/None, in which case `base`'s value wins.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = deepcopy(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = merge_dicts(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged
    if override in (None, {}, []):
        return deepcopy(base)
    return deepcopy(override)
