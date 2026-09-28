"""
Evidence item type and per-run evidence path allocation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    path: Path
    label: str = ""


class EvidenceStore:
    """Creates per-app, per-risk evidence paths under a run's evidence directory."""

    # Creates the run's evidence directory.
    def __init__(self, run_dir: Path):
        self.root = Path(run_dir) / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)
        logger.debug("reporting: evidence store at %s.", self.root)

    # Returns the evidence file path for an app and risk, creating its directory.
    def path_for(self, app_id: str, risk_id: str, filename: str) -> Path:
        path = self.root / app_id / risk_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("reporting: evidence path %s.", path)
        return path
