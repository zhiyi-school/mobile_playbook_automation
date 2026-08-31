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

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rendition_name": self.rendition_name,
            "width": self.width,
            "height": self.height,
            "idiom": self.idiom,
            "scale": self.scale,
        }


def assetutil_available() -> bool:
    return ASSETUTIL_PATH.is_file()


def read_catalog(car_path: Path) -> list[dict[str, Any]] | None:
    """`assetutil --info` output, or `None` when it cannot be read."""
    path = Path(car_path)
    if not assetutil_available() or not path.is_file():
        return None
    if path.stat().st_size > MAX_CATALOG_BYTES:
        logger.warning("Asset catalog is larger than the inspection limit; skipping it.")
        return None

    try:
        completed = subprocess.run(
            [str(ASSETUTIL_PATH), "--info", str(path)],
            capture_output=True,
            timeout=ASSETUTIL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Asset catalog inspection could not run: %s", type(exc).__name__)
        return None

    if completed.returncode != 0 or not completed.stdout:
        return None
    if len(completed.stdout) > MAX_INFO_BYTES:
        logger.warning("Asset catalog inspection produced more output than the limit; skipping it.")
        return None

    try:
        parsed = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return [entry for entry in parsed if isinstance(entry, dict)] if isinstance(parsed, list) else None


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def primary_icon_rendition(entries: list[dict[str, Any]], icon_name: str | None) -> Rendition | None:
    """The largest home-screen rendition of the app's primary icon."""
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
        return None
    return max(candidates, key=lambda item: (item.width * item.height, item.scale))
