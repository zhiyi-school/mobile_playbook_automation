"""Resolve and decorate playbook screenshots for risk demonstrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mobile_playbook.playbook import source

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
DERIVED_IMAGE_KEYS = ("url", "exists")


def playbook_dir(platform: str) -> Path | None:
    return source.configured_root(platform)


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


def _step_images(step: dict) -> list:
    blocks = [block for block in step.get("content") or [] if isinstance(block, dict) and block.get("type") == "image"]
    return list(step.get("images") or []) + blocks


def decorate_demonstration(platform: str, demonstration: list) -> list:
    """Add derived image fields to a demonstration, in both its ordered content and its image list."""
    for item in demonstration:
        if not isinstance(item, dict) or item.get("type") != "steps":
            continue
        for step in item.get("items") or []:
            if not isinstance(step, dict):
                continue
            for image in _step_images(step):
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
            step["images"] = [_undecorated(image) for image in step.get("images") or []]
            if step.get("content"):
                step["content"] = [_undecorated(block) for block in step["content"]]
    return demonstration


def _undecorated(block: Any) -> Any:
    if not isinstance(block, dict):
        return block
    return {key: value for key, value in block.items() if key not in DERIVED_IMAGE_KEYS}
