"""
Writes the dashboard_results.json feed for a run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.serialization import serialize

logger = logging.getLogger(__name__)


# Writes the serialized results to the run's dashboard_results.json feed and returns its path.
def write_dashboard_results(run_dir: Path, results: list[Any]) -> Path:
    path = Path(run_dir) / "dashboard_results.json"
    text = json.dumps(serialize(results), indent=2, sort_keys=True)
    path.write_text(text)
    logger.debug("reporting: wrote %d result(s) to %s (%d characters).", len(results), path, len(text))
    return path
