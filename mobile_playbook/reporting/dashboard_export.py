from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.serialization import serialize


def write_dashboard_results(run_dir: Path, results: list[Any]) -> Path:
    """Write the stable JSON feed a future dashboard can consume."""
    path = Path(run_dir) / "dashboard_results.json"
    path.write_text(json.dumps(serialize(results), indent=2, sort_keys=True))
    return path
