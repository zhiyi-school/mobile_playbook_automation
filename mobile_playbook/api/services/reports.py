from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.downloads import (
    DownloadFileMissing,
    DownloadPathError,
    media_type_for,
    resolve_regular_file,
    safe_filename,
)
from mobile_playbook.api.settings import REPORTS_ROOT, REPOSITORY_ROOT, WORK_ROOT
from mobile_playbook.reporting.evidence import decode_ref, normalize_evidence
from mobile_playbook.reporting.run_manifest import read_manifest
from mobile_playbook.reporting.sarif_writer import build_from_run_dir, sarif_path, write_sarif

def evidence_roots() -> dict[str, Path]:
    return {"reports": REPORTS_ROOT, "work": WORK_ROOT}


def safe_download_name(name: str) -> str:
    return safe_filename(name, "evidence")


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


def _with_evidence(run_dir: Path, row: dict) -> dict:
    report_path = str(row.get("report_path") or "").strip()
    report_dir = None
    if report_path:
        candidate = (run_dir / report_path).resolve()
        if run_dir.resolve() in candidate.parents:
            report_dir = candidate
    return {**row, "evidence": normalize_evidence(row.get("evidence"), report_dir, evidence_roots(), REPOSITORY_ROOT)}


def read_dashboard_results(run_timestamp: str) -> list[dict]:
    """Rows are enriched as they are served, so runs recorded before this still carry their artifacts."""
    run_dir = safe_run_dir(run_timestamp)
    results_path = run_dir / "dashboard_results.json"
    if not results_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard_results.json not found for this run")
    rows = json.loads(results_path.read_text())
    return [_with_evidence(run_dir, row) for row in rows if isinstance(row, dict)]


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


def list_report_timestamps(status: str | None = None) -> list[str]:
    if not REPORTS_ROOT.is_dir():
        return []
    names = sorted((p.name for p in REPORTS_ROOT.iterdir() if p.is_dir()), reverse=True)
    if status is None:
        return names
    return [
        name
        for name in names
        if _manifest_status(REPORTS_ROOT / name) == status
    ]


def _manifest_status(run_dir: Path) -> str:
    manifest = read_manifest(run_dir)
    if manifest is None:
        return "completed"
    return str(manifest.get("status") or "unknown")


def report_file_path(run_timestamp: str, file_path: str) -> Path:
    run_dir = safe_run_dir(run_timestamp)
    try:
        return resolve_regular_file(run_dir, file_path)
    except (DownloadPathError, DownloadFileMissing):
        raise HTTPException(status_code=404, detail="File not found")


def _belongs_to_run(resolved: Path, root_name: str, run_dir: Path, run_timestamp: str) -> bool:
    """A handle for one run must not read another run's artifacts."""
    if root_name == "reports":
        return run_dir == resolved or run_dir in resolved.parents
    return run_timestamp in resolved.parts


def safe_evidence_path(run_timestamp: str, ref: str) -> Path:
    """
    Resolve one opaque handle to a file. Rejects anything that is not a regular
    file inside the named root for this run: `resolve()` collapses `..` and
    follows symlinks, so a link pointing out of the root fails the containment
    check rather than escaping it.
    """
    run_dir = safe_run_dir(run_timestamp)
    decoded = decode_ref(ref or "")
    if decoded is None:
        raise HTTPException(status_code=400, detail="Malformed evidence reference")

    root_name, relative = decoded
    root = evidence_roots().get(root_name)
    if root is None:
        raise HTTPException(status_code=400, detail="Malformed evidence reference")

    try:
        resolved = resolve_regular_file(root, relative)
    except DownloadPathError:
        raise HTTPException(status_code=400, detail="Malformed evidence reference") from None
    except DownloadFileMissing:
        raise HTTPException(status_code=404, detail="Evidence file not found") from None
    if not _belongs_to_run(resolved, root_name, run_dir, run_timestamp):
        raise HTTPException(status_code=404, detail="Evidence file not found for this run")
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
