"""Locate and safely read the external developer playbook directory."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from mobile_playbook.api import settings

logger = logging.getLogger(__name__)

PLAYBOOK_DIR_ENV = {"ios": "IOS_PLAYBOOK_DIR", "android": "ANDROID_PLAYBOOK_DIR"}
RISK_CONFIG_FILES = {
    "ios": Path("configs/split/ios/risks.yaml"),
    "android": Path("configs/split/android/risks.yaml"),
}
RISK_CONFIG_KEY = "playbook_dir"

IGNORED_NAMES = frozenset({".DS_Store", "__MACOSX", ".obsidian", ".git", ".gitkeep", ".Spotlight-V100"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
ARCHIVE_SUFFIXES = frozenset({".zip"})

ATTACHMENTS_DIR = "attachments"
ARCHIVES_DIR = "implemented_controls"


class PlaybookUnavailableError(RuntimeError):
    pass


def playbook_dir_env_key(platform: str) -> str | None:
    return PLAYBOOK_DIR_ENV.get(platform)


def configured_root(platform: str) -> Path | None:
    """The configured directory, whether or not it exists: env var first, then the platform risk config."""
    key = PLAYBOOK_DIR_ENV.get(platform)
    configured = settings.env_setting(key) if key else None
    if configured:
        logger.debug("playbook source: %s playbook dir from env %s: %s", platform, key, configured)
    if not configured:
        configured = _risk_config_playbook_dir(platform)
        logger.debug("playbook source: %s playbook dir from risk config %s: %s", platform, RISK_CONFIG_FILES.get(platform), configured)
    if not configured:
        logger.debug("playbook source: no playbook dir configured for platform %s", platform)
        return None
    return Path(str(configured).strip()).expanduser()


def playbook_root(platform: str) -> Path | None:
    root = configured_root(platform)
    if root is None:
        return None
    try:
        resolved = root.resolve() if root.is_dir() else None
        logger.debug("playbook source: %s playbook root %s resolved to %s", platform, root, resolved)
        return resolved
    except OSError as exc:
        logger.debug("playbook source: could not inspect %s playbook root %s: %s", platform, root, exc, exc_info=True)
        return None


def require_root(platform: str) -> Path:
    root = configured_root(platform)
    if root is None:
        logger.debug("playbook source: require_root failed, no playbook dir configured for %s", platform)
        key = PLAYBOOK_DIR_ENV.get(platform, "the playbook directory setting")
        raise PlaybookUnavailableError(
            f"No developer playbook directory is configured for platform {platform}. "
            f"Set {key} or {RISK_CONFIG_KEY} in {RISK_CONFIG_FILES.get(platform, 'the platform risk config')}."
        )
    if not root.is_dir():
        logger.debug("playbook source: require_root failed, %s playbook root %s is not a directory", platform, root)
        raise PlaybookUnavailableError(
            f"The developer playbook directory for platform {platform} is not readable: {root}"
        )
    resolved = root.resolve()
    logger.debug("playbook source: %s playbook root is %s", platform, resolved)
    return resolved


def _risk_config_playbook_dir(platform: str) -> str | None:
    path = RISK_CONFIG_FILES.get(platform)
    if path is None or not path.exists():
        logger.debug("playbook source: no risk config file for %s at %s", platform, path)
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("playbook source: could not read risk config %s: %s", path, exc, exc_info=True)
        return None
    configured = data.get(RISK_CONFIG_KEY) if isinstance(data, dict) else None
    logger.debug("playbook source: risk config %s %s=%s", path, RISK_CONFIG_KEY, configured)
    return str(configured) if configured else None


def is_ignored(relative: Path | str) -> bool:
    return any(part in IGNORED_NAMES for part in Path(relative).parts)


def resolve_within(root: Path, relative: str) -> Path | None:
    """Resolve a playbook-relative path, or `None` if it escapes the root or names an ignored file."""
    if not relative:
        return None
    root = root.resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError) as exc:
        logger.debug("playbook source: could not resolve %s under %s: %s", relative, root, exc, exc_info=True)
        return None
    if candidate != root and root not in candidate.parents:
        logger.debug("playbook source: rejected %s, resolves outside root %s", relative, root)
        return None
    if is_ignored(candidate.relative_to(root)):
        logger.debug("playbook source: rejected %s, names an ignored path", relative)
        return None
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def markdown_files(root: Path) -> list[Path]:
    """Every playbook document, skipping the shadow copies an unzip leaves in `__MACOSX`."""
    found = sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not is_ignored(path.relative_to(root))
    )
    logger.debug("playbook source: found %s markdown files under %s", len(found), root)
    return found


def asset_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in (ATTACHMENTS_DIR, ARCHIVES_DIR):
        target = root / directory
        if not target.is_dir():
            logger.debug("playbook source: asset directory %s is absent", target)
            continue
        for path in target.rglob("*"):
            if path.is_file() and not is_ignored(path.relative_to(root)):
                found.append(path)
    logger.debug("playbook source: found %s asset files under %s", len(found), root)
    return sorted(found)


def source_signature(root: Path) -> tuple:
    """A fingerprint of everything the catalogue is built from, for cache invalidation."""
    entries: list[tuple[str, int, int]] = []
    for path in [*markdown_files(root), *asset_files(root)]:
        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("playbook source: skipped %s in signature, stat failed: %s", path, exc, exc_info=True)
            continue
        entries.append((relative_to_root(root, path), stat.st_mtime_ns, stat.st_size))
    logger.debug("playbook source: source signature for %s has %s entries", root, len(entries))
    return tuple(entries)
