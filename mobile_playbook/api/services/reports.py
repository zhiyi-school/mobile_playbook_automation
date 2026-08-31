from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.reporting.sarif_writer import build_from_run_dir, sarif_path, write_sarif

REPORTS_ROOT = Path("reports")


def resolved_run_dir(run_timestamp: str) -> Path:
    if not run_timestamp or "/" in run_timestamp or "\\" in run_timestamp or run_timestamp in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    run_dir = (REPORTS_ROOT / run_timestamp).resolve()
    reports_root = REPORTS_ROOT.resolve()
    if reports_root not in run_dir.parents and run_dir != reports_root:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    return run_dir


def safe_run_dir(run_timestamp: str) -> Path:
    run_dir = resolved_run_dir(run_timestamp)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No report directory for run_timestamp: {run_timestamp}")
    return run_dir


def read_dashboard_results(run_timestamp: str) -> list[dict]:
    run_dir = safe_run_dir(run_timestamp)
    results_path = run_dir / "dashboard_results.json"
    if not results_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard_results.json not found for this run")
    return json.loads(results_path.read_text())


def read_sarif(run_timestamp: str) -> dict:
    """Serve the run's SARIF, generating it on first request for runs made before this existed."""
    run_dir = safe_run_dir(run_timestamp)
    existing = sarif_path(run_dir)
    if existing.is_file():
        try:
            return json.loads(existing.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    document = build_from_run_dir(run_dir)
    if document is None:
        raise HTTPException(
            status_code=404,
            detail="No SARIF available for this run: it has no completed run manifest or no results feed",
        )
    try:
        write_sarif(run_dir)
    except OSError:
        pass
    return document


def list_report_timestamps() -> list[str]:
    if not REPORTS_ROOT.is_dir():
        return []
    return sorted((p.name for p in REPORTS_ROOT.iterdir() if p.is_dir()), reverse=True)


def report_file_path(run_timestamp: str, file_path: str) -> Path:
    run_dir = safe_run_dir(run_timestamp)
    resolved = (run_dir / file_path).resolve()
    if run_dir not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def safe_evidence_path(run_timestamp: str, file_path: str) -> Path:
    safe_run_dir(run_timestamp)
    candidate = Path(file_path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    allowed_roots = [REPORTS_ROOT.resolve(), Path("work").resolve()]
    if not resolved.is_file() or not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(status_code=404, detail="Evidence file not found")
    return resolved


def detail_verdict(row: dict) -> str:
    if row.get("verdict"):
        return str(row["verdict"])
    report_path = row.get("report_path")
    if not report_path:
        return "Inconclusive"
    try:
        detail_path = safe_run_dir(row["run_timestamp"]) / report_path / "report.json"
        return str(json.loads(detail_path.read_text()).get("verdict") or "Inconclusive")
    except Exception:
        return "Inconclusive"


def app_risk_history(app_id: str, risk_id: str, limit: int) -> list[dict]:
    history = []
    for run_timestamp in list_report_timestamps():
        try:
            rows = read_dashboard_results(run_timestamp)
        except HTTPException:
            continue
        match = next((row for row in rows if row.get("app_id") == app_id and row.get("test_id") == risk_id), None)
        if match is None:
            continue
        history.append({**match, "verdict": detail_verdict(match)})
        if len(history) >= limit:
            break
    return history
