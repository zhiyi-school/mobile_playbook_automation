"""Locate and safely read the external developer playbook directory."""

from __future__ import annotations

from pathlib import Path

import yaml

from mobile_playbook.api import settings

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
    if not configured:
        configured = _risk_config_playbook_dir(platform)
    if not configured:
        return None
    return Path(str(configured).strip()).expanduser()


def playbook_root(platform: str) -> Path | None:
    root = configured_root(platform)
    if root is None:
        return None
    try:
        return root.resolve() if root.is_dir() else None
    except OSError:
        return None


def require_root(platform: str) -> Path:
    root = configured_root(platform)
    if root is None:
        key = PLAYBOOK_DIR_ENV.get(platform, "the playbook directory setting")
        raise PlaybookUnavailableError(
            f"No developer playbook directory is configured for platform {platform}. "
            f"Set {key} or {RISK_CONFIG_KEY} in {RISK_CONFIG_FILES.get(platform, 'the platform risk config')}."
        )
    if not root.is_dir():
        raise PlaybookUnavailableError(
            f"The developer playbook directory for platform {platform} is not readable: {root}"
        )
    return root.resolve()


def _risk_config_playbook_dir(platform: str) -> str | None:
    path = RISK_CONFIG_FILES.get(platform)
    if path is None or not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    configured = data.get(RISK_CONFIG_KEY) if isinstance(data, dict) else None
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
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    if is_ignored(candidate.relative_to(root)):
        return None
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def markdown_files(root: Path) -> list[Path]:
    """Every playbook document, skipping the shadow copies an unzip leaves in `__MACOSX`."""
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not is_ignored(path.relative_to(root))
    )


def asset_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in (ATTACHMENTS_DIR, ARCHIVES_DIR):
        target = root / directory
        if not target.is_dir():
            continue
        for path in target.rglob("*"):
            if path.is_file() and not is_ignored(path.relative_to(root)):
                found.append(path)
    return sorted(found)


def source_signature(root: Path) -> tuple:
    """A fingerprint of everything the catalogue is built from, for cache invalidation."""
    entries: list[tuple[str, int, int]] = []
    for path in [*markdown_files(root), *asset_files(root)]:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((relative_to_root(root, path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)
