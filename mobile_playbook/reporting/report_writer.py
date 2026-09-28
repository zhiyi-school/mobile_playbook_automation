from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mobile_playbook.reporting.dashboard_export import write_dashboard_results
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.run_events import append_event, read_events

logger = logging.getLogger(__name__)


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
        logger.debug("reporting: %s report writer for run %s at %s.", platform, run_timestamp, self.run_dir)

    def test_report_dir(self, app_id: str, risk_id: str, case_id: str, platform: str | None = None) -> Path:
        path = self.run_dir / (platform or self.platform) / app_id / risk_id / case_id
        path.mkdir(parents=True, exist_ok=True)
        logger.debug("reporting: test report directory %s.", path)
        return path

    def write_result(self, result: Any, report_dir: Path) -> None:
        result_path = Path(report_dir) / "report.json"
        text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
        result_path.write_text(text)
        logger.debug(
            "reporting: wrote %s (%d characters) for %s/%s verdict %s.",
            result_path,
            len(text),
            result.app_id,
            result.risk_id,
            result.verdict,
        )
        logs_path = Path(report_dir) / "logs.txt"
        if not logs_path.exists():
            logs_path.write_text("\n".join(result.errors))
            logger.debug("reporting: wrote %d error line(s) to %s.", len(result.errors), logs_path)
        else:
            logger.debug("reporting: %s already exists; leaving it.", logs_path)
        self.results.append(result)
        append_event(
            self.run_dir,
            "risk_completed",
            app_id=result.app_id,
            risk_id=result.risk_id,
            test_case_id=result.test_case_id,
            verdict=result.verdict,
            final_status=result.final_status,
        )

    def write_summary(self) -> None:
        self.completed_at = datetime.now().astimezone()
        duration_seconds = (self.completed_at - self.started_at).total_seconds()
        logger.debug("reporting: writing summary for run %s with %d result(s).", self.run_timestamp, len(self.results))
        if self.result_adapter is not None:
            normalized = [self.result_adapter(result) for result in self.results]
            write_dashboard_results(self.run_dir, normalized)
        else:
            logger.debug("reporting: no result adapter; skipping dashboard_results.json.")
        lines = [
            "# Run Summary",
            "",
            f"- Run timestamp: `{self.run_timestamp}`",
            f"- Started: {self.started_at.isoformat()}",
            f"- Completed: {self.completed_at.isoformat()}",
            f"- Duration: {duration_seconds:.2f} seconds",
            "",
            "| App | Risk | Test Case | Artifact Source | Status | Notes | Report |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for result in self.results:
            notes = "; ".join(clean_message(e) for e in result.errors[:2])
            if not notes and result.artifact_result and result.artifact_result.errors:
                notes = "; ".join(clean_message(e) for e in result.artifact_result.errors[:2])
            report_path = f"{self.platform}/{result.app_id}/{result.risk_id}/{result.test_case_id}"
            lines.append(
                f"| {result.app_id} | {result.risk_id} | {result.test_case_id} | "
                f"{result.artifact_source} | {result.verdict} | {notes} | [{report_path}/]({report_path}/) |"
            )
        warnings = _preflight_warnings(self.run_dir)
        if warnings:
            lines.extend(["", "## Preflight warnings", ""])
            for warning in warnings:
                app_ids = ", ".join(str(value) for value in warning.get("app_ids") or [])
                scope = f" ({app_ids})" if app_ids else ""
                lines.append(f"- `{warning.get('code', '')}`{scope}: {warning.get('message', '')}")
        (self.run_dir / "summary.md").write_text("\n".join(lines) + "\n")
        logger.debug(
            "reporting: wrote %s (%d line(s), %d preflight warning(s)).",
            self.run_dir / "summary.md",
            len(lines),
            len(warnings),
        )


def _preflight_warnings(run_dir: Path) -> list[dict[str, Any]]:
    events, _ = read_events(run_dir)
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for event in events:
        if event.get("type") != "preflight_warning":
            continue
        app_ids = tuple(str(value) for value in (event.get("app_ids") or []))
        key = (str(event.get("code") or ""), str(event.get("risk_id") or ""), app_ids)
        if key in seen:
            continue
        seen.add(key)
        warnings.append(event)
    return warnings
