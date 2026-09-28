"""
Android app artifact descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AndroidArtifact:
    """An APK on disk for one configured app, with its package name when known."""

    app_id: str
    apk_path: Path
    package_name: str | None = None
