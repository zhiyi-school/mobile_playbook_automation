from __future__ import annotations

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
    return cleaned or fallback


def media_type_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_TYPES:
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def resolve_regular_file(root: Path, relative: str | Path) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DownloadPathError(str(relative))
    try:
        resolved_root = root.resolve()
        resolved = (root / candidate).resolve()
    except (OSError, ValueError) as exc:
        raise DownloadFileMissing(str(relative)) from exc
    if resolved_root not in resolved.parents or not resolved.is_file():
        raise DownloadFileMissing(str(relative))
    return resolved
