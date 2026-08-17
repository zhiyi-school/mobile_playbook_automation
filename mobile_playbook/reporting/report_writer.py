from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mobile_playbook.reporting.dashboard_export import write_dashboard_results
from mobile_playbook.reporting.serialization import serialize


class ReportWriter:
    def __init__(
        self,
        root: Path,
        run_timestamp: str,
        result_adapter: Callable[[Any], Any] | None = None,
        platform: str = "ios",
    ):
        self.root = Path(root)
        self.run_timestamp = run_timestamp
        self.result_adapter = result_adapter
        self.platform = platform
        self.started_at = datetime.now().astimezone()
        self.completed_at: datetime | None = None
        self.run_dir = self.root / run_timestamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "evidence").mkdir(parents=True, exist_ok=True)
        (self.run_dir / platform).mkdir(parents=True, exist_ok=True)
        self.results: list[Any] = []

    def test_report_dir(self, app_id: str, risk_id: str, case_id: str, platform: str | None = None) -> Path:
        path = self.run_dir / (platform or self.platform) / app_id / risk_id / case_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_result(self, result: Any, report_dir: Path) -> None:
        result_path = Path(report_dir) / "report.json"
        result_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        logs_path = Path(report_dir) / "logs.txt"
        if not logs_path.exists():
            logs_path.write_text("\n".join(result.errors))
        self.results.append(result)

    def write_summary(self) -> None:
        self.completed_at = datetime.now().astimezone()
        duration_seconds = (self.completed_at - self.started_at).total_seconds()
        data: dict[str, Any] = {
            "run_timestamp": self.run_timestamp,
            "run_started_at": self.started_at.isoformat(),
            "run_completed_at": self.completed_at.isoformat(),
            "duration_seconds": round(duration_seconds, 3),
            "results": [r.to_dict() for r in self.results],
        }
        (self.run_dir / "summary.json").write_text(json.dumps(serialize(data), indent=2, sort_keys=True))
        if self.result_adapter is not None:
            normalized = [self.result_adapter(result) for result in self.results]
            write_dashboard_results(self.run_dir, normalized)
        lines = [
            "# Run Summary",
            "",
            f"- Run timestamp: `{self.run_timestamp}`",
            f"- Started: {self.started_at.isoformat()}",
            f"- Completed: {self.completed_at.isoformat()}",
            f"- Duration: {duration_seconds:.2f} seconds",
            "",
            "| App | Risk | Test Case | Artifact Source | Status | Notes |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for result in self.results:
            notes = "; ".join(result.errors[:2])
            if not notes and result.artifact_result and result.artifact_result.errors:
                notes = "; ".join(result.artifact_result.errors[:2])
            lines.append(
                f"| {result.app_id} | {result.risk_id} | {result.test_case_id} | "
                f"{result.artifact_source} | {result.final_status} | {notes} |"
            )
        (self.run_dir / "summary.md").write_text("\n".join(lines) + "\n")
