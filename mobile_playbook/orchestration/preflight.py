"""
Loads YAML configs with section includes and merges inline overrides onto them.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

INCLUDE_KEYS = ("include", "includes")


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Load a YAML config mapping and resolve its section includes relative to the file.
def load_yaml_config(path: Path) -> dict[str, Any]:
    path = Path(path)
    logger.debug("config: loading %s", path)
    with path.open("r") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        logger.debug("config: root of %s is %s, not a mapping", path, type(raw).__name__)
        raise ValueError(f"config root must be a mapping: {path}")
    logger.debug("config: %s top-level sections=%s", path, list(raw))
    return resolve_config_includes(raw, path.parent)


# Replace include entries with the included sections, letting inline values override them.
def resolve_config_includes(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    include_spec = _include_spec(raw)
    if not include_spec:
        logger.debug("config: no includes under %s", base_dir)
        return {key: deepcopy(value) for key, value in raw.items() if key not in INCLUDE_KEYS}
    if not isinstance(include_spec, dict):
        logger.debug("config: include spec is %s, not a mapping", type(include_spec).__name__)
        raise ValueError("include must be a mapping of config section to YAML file")

    resolved = {key: deepcopy(value) for key, value in raw.items() if key not in INCLUDE_KEYS}
    for section, include_path in include_spec.items():
        section_name = str(section)
        section_value = _load_include_section(Path(base_dir), section_name, include_path)
        if section_name in resolved:
            logger.debug("config: merging included section %s under inline overrides", section_name)
            resolved[section_name] = merge_dicts(section_value, resolved[section_name])
        else:
            logger.debug("config: using included section %s as-is", section_name)
            resolved[section_name] = section_value
    return resolved


# Return the value of the first include key present in the config, or None.
def _include_spec(raw: dict[str, Any]) -> Any:
    for key in INCLUDE_KEYS:
        if key in raw:
            return raw[key]
    return None


# Load a section from one or more include files, unwrapping it when nested under its own name.
def _load_include_section(base_dir: Path, section: str, include_path: Any) -> Any:
    paths = _section_include_paths(section, include_path)
    resolved_paths = [_resolve_include_path(base_dir, p) for p in paths]
    logger.debug("config: section %s includes %s", section, [str(p) for p in resolved_paths])
    for path, original in zip(resolved_paths, paths):
        if not path.exists():
            logger.debug("config: include for %s missing: %s (declared %s)", section, path, original)
            raise ValueError(f"included config file does not exist for {section} ({original}): {path}")
    # Listed files are parsed as one document so anchors resolve across them.
    combined_text = "\n".join(path.read_text() for path in resolved_paths)
    loaded = yaml.safe_load(combined_text)
    if loaded is None:
        loaded = {}
    if isinstance(loaded, dict) and section in loaded:
        logger.debug("config: section %s loaded from wrapped mapping (%s chars of YAML)", section, len(combined_text))
        return deepcopy(loaded[section])
    logger.debug("config: section %s loaded as raw %s (%s chars of YAML)", section, type(loaded).__name__, len(combined_text))
    return deepcopy(loaded)


# Normalize a section's include value to a non-empty list of paths, raising ValueError otherwise.
def _section_include_paths(section: str, include_path: Any) -> list[str]:
    if isinstance(include_path, str):
        if not include_path.strip():
            raise ValueError(f"include.{section} must be a non-empty path")
        return [include_path]
    if isinstance(include_path, list) and include_path and all(isinstance(p, str) and p.strip() for p in include_path):
        return list(include_path)
    raise ValueError(f"include.{section} must be a non-empty path or a non-empty list of paths")


# Resolve an include path against the config directory unless it is absolute.
def _resolve_include_path(base_dir: Path, include_path: str) -> Path:
    path = Path(include_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


# Recursively merge override onto base without mutating either; empty override values keep base.
def merge_dicts(base: Any, override: Any) -> Any:
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
