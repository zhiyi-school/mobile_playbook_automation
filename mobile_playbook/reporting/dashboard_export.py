from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.serialization import serialize

logger = logging.getLogger(__name__)


def write_dashboard_results(run_dir: Path, results: list[Any]) -> Path:
    """Write the stable JSON feed a future dashboard can consume."""
    path = Path(run_dir) / "dashboard_results.json"
    text = json.dumps(serialize(results), indent=2, sort_keys=True)
    path.write_text(text)
    logger.debug("reporting: wrote %d result(s) to %s (%d characters).", len(results), path, len(text))
    return path
