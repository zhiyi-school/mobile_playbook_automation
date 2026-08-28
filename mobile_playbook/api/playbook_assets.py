"""Resolve and decorate playbook screenshots for risk demonstrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mobile_playbook.api import config_editor

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
DERIVED_IMAGE_KEYS = ("url", "exists")


def playbook_dir(platform: str) -> Path | None:
    path = config_editor.RISK_FILES.get(platform)
    if path is None or not path.exists():
        return None
    data = config_editor._plain(config_editor._rt_yaml.load(path.read_text()) or {})
    configured = data.get("playbook_dir")
    return Path(str(configured)).expanduser() if configured else None


def resolve_image(platform: str, image_path: str) -> Path | None:
    root = playbook_dir(platform)
    if root is None:
        return None
    root = root.resolve()
    resolved = (root / image_path).resolve()
    if root != resolved and root not in resolved.parents:
        return None
    if resolved.suffix.lower() not in IMAGE_SUFFIXES or not resolved.is_file():
        return None
    return resolved


def image_url(platform: str, image_path: str) -> str:
    return f"/platforms/{platform}/playbook/images/{image_path}"


def decorate_demonstration(platform: str, demonstration: list) -> list:
    """Add derived image fields to a demonstration."""
    for item in demonstration:
        if not isinstance(item, dict) or item.get("type") != "steps":
            continue
        for step in item.get("items") or []:
            for image in step.get("images") or []:
                if not isinstance(image, dict) or not image.get("path"):
                    continue
                image["url"] = image_url(platform, str(image["path"]))
                image["exists"] = resolve_image(platform, str(image["path"])) is not None
    return demonstration


def strip_derived(demonstration: Any) -> Any:
    """Drop derived image fields before writing YAML."""
    if not isinstance(demonstration, list):
        return demonstration
    for item in demonstration:
        if not isinstance(item, dict) or item.get("type") != "steps":
            continue
        for step in item.get("items") or []:
            if not isinstance(step, dict):
                continue
            step["images"] = [
                {key: value for key, value in image.items() if key not in DERIVED_IMAGE_KEYS}
                if isinstance(image, dict)
                else image
                for image in step.get("images") or []
            ]
    return demonstration
