"""
Reads iOS asset catalogs with assetutil and picks the primary app icon rendition.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ASSETUTIL_PATH = Path("/usr/bin/assetutil")
ASSETUTIL_TIMEOUT_SECONDS = 20
MAX_CATALOG_BYTES = 64 * 1024 * 1024
MAX_INFO_BYTES = 16 * 1024 * 1024

EXCLUDED_IDIOMS = {"marketing"}
MAX_RENDITION_DIMENSION = 2048


@dataclass(frozen=True)
class Rendition:
    name: str
    rendition_name: str
    width: int
    height: int
    idiom: str
    scale: int

    # Returns the rendition fields as a plain dict.
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rendition_name": self.rendition_name,
            "width": self.width,
            "height": self.height,
            "idiom": self.idiom,
            "scale": self.scale,
        }


# Reports whether the macOS assetutil tool is installed.
def assetutil_available() -> bool:
    return ASSETUTIL_PATH.is_file()


# Returns the `assetutil --info` entries of an asset catalog, or None when it cannot be read within limits.
def read_catalog(car_path: Path) -> list[dict[str, Any]] | None:
    path = Path(car_path)
    if not assetutil_available() or not path.is_file():
        logger.debug(
            "artifact store: asset catalog %s not inspected (assetutil=%s, file=%s).",
            path,
            assetutil_available(),
            path.is_file(),
        )
        return None
    if path.stat().st_size > MAX_CATALOG_BYTES:
        logger.warning("Asset catalog is larger than the inspection limit; skipping it.")
        return None

    logger.debug("artifact store: running assetutil on %s (%d bytes).", path, path.stat().st_size)
    try:
        completed = subprocess.run(
            [str(ASSETUTIL_PATH), "--info", str(path)],
            capture_output=True,
            timeout=ASSETUTIL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("artifact store: assetutil failed for %s.", path, exc_info=True)
        logger.warning("Asset catalog inspection could not run: %s", type(exc).__name__)
        return None

    logger.debug(
        "artifact store: assetutil exited %d with %d bytes of output.",
        completed.returncode,
        len(completed.stdout or b""),
    )
    if completed.returncode != 0 or not completed.stdout:
        return None
    if len(completed.stdout) > MAX_INFO_BYTES:
        logger.warning("Asset catalog inspection produced more output than the limit; skipping it.")
        return None

    try:
        parsed = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        logger.debug("artifact store: assetutil output for %s is not JSON.", path, exc_info=True)
        return None
    logger.debug(
        "artifact store: asset catalog %s has %s entr(ies).", path, len(parsed) if isinstance(parsed, list) else "no"
    )
    return [entry for entry in parsed if isinstance(entry, dict)] if isinstance(parsed, list) else None


# Returns value if it is a non-bool int, else 0.
def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# Returns the largest non-marketing rendition of the app's primary icon within the size limit.
def primary_icon_rendition(entries: list[dict[str, Any]], icon_name: str | None) -> Rendition | None:
    wanted = (icon_name or "").strip().lower()
    candidates: list[Rendition] = []
    for entry in entries:
        name = str(entry.get("Name") or "")
        if not name:
            continue
        lowered = name.lower()
        if wanted:
            if lowered != wanted:
                continue
        elif "appicon" not in lowered.replace("_", "").replace("-", ""):
            continue
        if str(entry.get("Idiom") or "").lower() in EXCLUDED_IDIOMS:
            continue
        width, height = _as_int(entry.get("PixelWidth")), _as_int(entry.get("PixelHeight"))
        if width <= 0 or height <= 0 or width > MAX_RENDITION_DIMENSION or height > MAX_RENDITION_DIMENSION:
            continue
        candidates.append(
            Rendition(
                name=name,
                rendition_name=str(entry.get("RenditionName") or ""),
                width=width,
                height=height,
                idiom=str(entry.get("Idiom") or ""),
                scale=_as_int(entry.get("Scale")),
            )
        )
    if not candidates:
        logger.debug("artifact store: no icon rendition among %d entr(ies) for %r.", len(entries), icon_name)
        return None
    chosen = max(candidates, key=lambda item: (item.width * item.height, item.scale))
    logger.debug(
        "artifact store: chose rendition %s %dx%d@%d from %d candidate(s).",
        chosen.rendition_name,
        chosen.width,
        chosen.height,
        chosen.scale,
        len(candidates),
    )
    return chosen
