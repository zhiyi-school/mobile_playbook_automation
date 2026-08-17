from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AndroidArtifact:
    app_id: str
    apk_path: Path
    package_name: str | None = None
