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
            resolved[section_name] = _merge_section(section_value, resolved[section_name])
        else:
            resolved[section_name] = section_value
    return resolved


def _include_spec(raw: dict[str, Any]) -> Any:
    for key in INCLUDE_KEYS:
        if key in raw:
            return raw[key]
    return None


def _load_include_section(base_dir: Path, section: str, include_path: Any) -> Any:
    if not isinstance(include_path, str) or not include_path.strip():
        raise ValueError(f"include.{section} must be a non-empty path")
    path = Path(include_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    if not path.exists():
        raise ValueError(f"included config file does not exist for {section}: {path}")
    with path.open("r") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        loaded = {}
    if isinstance(loaded, dict) and section in loaded:
        return deepcopy(loaded[section])
    return deepcopy(loaded)


def _merge_section(included: Any, inline: Any) -> Any:
    if isinstance(included, dict) and isinstance(inline, dict):
        merged = deepcopy(included)
        for key, value in inline.items():
            if key in merged:
                merged[key] = _merge_section(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged
    if inline in (None, {}, []):
        return deepcopy(included)
    return deepcopy(inline)
