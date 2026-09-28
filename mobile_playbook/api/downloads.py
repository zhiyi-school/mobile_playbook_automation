from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

MEDIA_TYPES = {
    ".ipa": "application/octet-stream",
    ".apk": "application/vnd.android.package-archive",
    ".zip": "application/zip",
    ".md": "text/markdown; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")
logger = logging.getLogger(__name__)


class DownloadPathError(ValueError):
    pass


class DownloadFileMissing(FileNotFoundError):
    pass


def safe_filename(
    name: str,
    fallback: str,
    *,
    basename: bool = True,
    strip_leading_dots: bool = True,
) -> str:
    source = Path(name).name if basename else name
    cleaned = _UNSAFE_FILENAME.sub("_", source)
    if strip_leading_dots:
        cleaned = cleaned.lstrip(".")
    logger.debug("api: download filename %r sanitized to %r (fallback %r).", name, cleaned, fallback)
    return cleaned or fallback


def media_type_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_TYPES:
        logger.debug("api: media type for %r is %s (known suffix).", name, MEDIA_TYPES[suffix])
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    logger.debug("api: media type for %r guessed as %s.", name, guessed or "application/octet-stream")
    return guessed or "application/octet-stream"


def resolve_regular_file(root: Path, relative: str | Path) -> Path:
    candidate = Path(relative)
    logger.debug("api: resolving download %r under root %s.", str(relative), root)
    if candidate.is_absolute() or ".." in candidate.parts:
        logger.debug("api: download %r rejected (absolute or parent traversal).", str(relative))
        raise DownloadPathError(str(relative))
    try:
        resolved_root = root.resolve()
        resolved = (root / candidate).resolve()
    except (OSError, ValueError) as exc:
        logger.debug("api: download %r could not be resolved: %s", str(relative), exc)
        raise DownloadFileMissing(str(relative)) from exc
    if resolved_root not in resolved.parents or not resolved.is_file():
        logger.debug(
            "api: download %r -> %s outside root or not a regular file (contained=%s).",
            str(relative),
            resolved,
            resolved_root in resolved.parents,
        )
        raise DownloadFileMissing(str(relative))
    logger.debug("api: download %r resolved to %s.", str(relative), resolved)
    return resolved
