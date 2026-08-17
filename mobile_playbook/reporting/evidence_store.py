from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    path: Path
    label: str = ""


class EvidenceStore:
    def __init__(self, run_dir: Path):
        self.root = Path(run_dir) / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, app_id: str, risk_id: str, filename: str) -> Path:
        path = self.root / app_id / risk_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
