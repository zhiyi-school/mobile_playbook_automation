"""
Resolve and decorate playbook screenshots for risk demonstrations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mobile_playbook.playbook import source

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
DERIVED_IMAGE_KEYS = ("url", "exists")
logger = logging.getLogger(__name__)


# Return the platform's configured playbook root, or None.
def playbook_dir(platform: str) -> Path | None:
    return source.configured_root(platform)


# Resolve an image path inside the playbook root to an existing image file, or None.
def resolve_image(platform: str, image_path: str) -> Path | None:
    root = playbook_dir(platform)
    if root is None:
        logger.debug("api: no playbook root configured for %s; image %r unresolved.", platform, image_path)
        return None
    root = root.resolve()
    resolved = (root / image_path).resolve()
    if root != resolved and root not in resolved.parents:
        logger.debug("api: playbook image %r resolves outside %s; rejected.", image_path, root)
        return None
    if resolved.suffix.lower() not in IMAGE_SUFFIXES or not resolved.is_file():
        logger.debug("api: playbook image %s is not an image file (suffix %s).", resolved, resolved.suffix.lower())
        return None
    logger.debug("api: playbook image %r resolved to %s.", image_path, resolved)
    return resolved


# Return the API URL that serves a playbook image.
def image_url(platform: str, image_path: str) -> str:
    return f"/platforms/{platform}/playbook/images/{image_path}"


# Return a step's image list plus its ordered image content blocks.
def _step_images(step: dict) -> list:
    blocks = [block for block in step.get("content") or [] if isinstance(block, dict) and block.get("type") == "image"]
    return list(step.get("images") or []) + blocks


# Add derived url and exists fields to every image in a demonstration's steps.
def decorate_demonstration(platform: str, demonstration: list) -> list:
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
    logger.debug("api: decorated %d %s demonstration item(s) with image fields.", len(demonstration), platform)
    return demonstration


# Remove derived image fields from a demonstration before writing YAML.
def strip_derived(demonstration: Any) -> Any:
    if not isinstance(demonstration, list):
        logger.debug("api: demonstration is %s, not a list; nothing to strip.", type(demonstration).__name__)
        return demonstration
    logger.debug("api: stripping derived image fields from %d demonstration item(s).", len(demonstration))
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


# Return a block without its derived image fields.
def _undecorated(block: Any) -> Any:
    if not isinstance(block, dict):
        return block
    return {key: value for key, value in block.items() if key not in DERIVED_IMAGE_KEYS}
