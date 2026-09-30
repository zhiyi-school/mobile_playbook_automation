"""
Services that locate report directories and serve results, SARIF, files and evidence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.downloads import (
    DownloadFileMissing,
    DownloadPathError,
    resolve_regular_file,
    safe_filename,
)
from mobile_playbook.api.settings import REPORTS_ROOT, REPOSITORY_ROOT, WORK_ROOT
from mobile_playbook.reporting.evidence import decode_ref, normalize_evidence
from mobile_playbook.reporting.run_manifest import read_manifest
from mobile_playbook.reporting.sarif_writer import build_from_run_dir, sarif_path, write_sarif

logger = logging.getLogger(__name__)


# Return the named roots evidence refs may point into.
def evidence_roots() -> dict[str, Path]:
    return {"reports": REPORTS_ROOT, "work": WORK_ROOT}


# Sanitize an evidence filename for download.
def safe_download_name(name: str) -> str:
    return safe_filename(name, "evidence")


# Resolve a run timestamp to a directory inside the report root, or raise 400.
def resolved_run_dir(run_timestamp: str) -> Path:
    if not run_timestamp or "/" in run_timestamp or "\\" in run_timestamp or run_timestamp in {".", ".."}:
        logger.debug("api: run_timestamp %r rejected (empty, separator or dot name).", run_timestamp)
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    run_dir = (REPORTS_ROOT / run_timestamp).resolve()
    reports_root = REPORTS_ROOT.resolve()
    if reports_root not in run_dir.parents and run_dir != reports_root:
        logger.debug("api: run_timestamp %r resolves to %s outside %s; rejected.", run_timestamp, run_dir, reports_root)
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    logger.debug("api: run_timestamp %r resolved to %s.", run_timestamp, run_dir)
    return run_dir


# Return an existing report directory for a run, or raise 404.
def safe_run_dir(run_timestamp: str) -> Path:
    run_dir = resolved_run_dir(run_timestamp)
    if not run_dir.is_dir():
        logger.debug("api: report directory %s does not exist; responding 404.", run_dir)
        raise HTTPException(status_code=404, detail=f"No report directory for run_timestamp: {run_timestamp}")
    return run_dir


# Return a result row with its evidence normalized relative to its report directory.
def _with_evidence(run_dir: Path, row: dict) -> dict:
    report_path = str(row.get("report_path") or "").strip()
    report_dir = None
    if report_path:
        candidate = (run_dir / report_path).resolve()
        if run_dir.resolve() in candidate.parents:
            report_dir = candidate
        else:
            logger.debug(
                "api: report_path %r escapes %s; evidence resolved without a report dir.", report_path, run_dir
            )
    return {**row, "evidence": normalize_evidence(row.get("evidence"), report_dir, evidence_roots(), REPOSITORY_ROOT)}


# Read a run's dashboard results, enriching evidence on read so older runs still carry it.
def read_dashboard_results(run_timestamp: str) -> list[dict]:
    run_dir = safe_run_dir(run_timestamp)
    results_path = run_dir / "dashboard_results.json"
    if not results_path.is_file():
        logger.debug("api: %s missing; responding 404.", results_path)
        raise HTTPException(status_code=404, detail="dashboard_results.json not found for this run")
    rows = json.loads(results_path.read_text())
    logger.debug("api: read %d row(s) from %s.", len(rows) if isinstance(rows, list) else 0, results_path)
    return [_with_evidence(run_dir, row) for row in rows if isinstance(row, dict)]


# Serve the run's SARIF, generating and saving it on first request for older runs.
def read_sarif(run_timestamp: str) -> dict:
    run_dir = safe_run_dir(run_timestamp)
    existing = sarif_path(run_dir)
    if existing.is_file():
        try:
            document = json.loads(existing.read_text())
            logger.debug("api: serving stored SARIF %s.", existing)
            return document
        except (OSError, json.JSONDecodeError):
            logger.debug("api: stored SARIF %s unreadable; regenerating.", existing, exc_info=True)
    else:
        logger.debug("api: no stored SARIF at %s; generating.", existing)
    document = build_from_run_dir(run_dir)
    if document is None:
        logger.debug("api: SARIF cannot be built for %s; responding 404.", run_dir)
        raise HTTPException(
            status_code=404,
            detail="No SARIF available for this run: it has no completed run manifest or no results feed",
        )
    try:
        write_sarif(run_dir)
    except OSError:
        logger.debug("api: generated SARIF could not be persisted for %s.", run_dir, exc_info=True)
    return document


# List report directory names, newest first, optionally filtered by manifest status.
def list_report_timestamps(status: str | None = None) -> list[str]:
    if not REPORTS_ROOT.is_dir():
        logger.debug("api: report root %s does not exist; no reports.", REPORTS_ROOT)
        return []
    names = sorted((p.name for p in REPORTS_ROOT.iterdir() if p.is_dir()), reverse=True)
    logger.debug("api: found %d report directory(ies) in %s.", len(names), REPORTS_ROOT)
    if status is None:
        return names
    matching = [
        name
        for name in names
        if _manifest_status(REPORTS_ROOT / name) == status
    ]
    logger.debug("api: %d report(s) have manifest status %r.", len(matching), status)
    return matching


# Return a run's manifest status, treating runs without a manifest as completed.
def _manifest_status(run_dir: Path) -> str:
    manifest = read_manifest(run_dir)
    if manifest is None:
        return "completed"
    return str(manifest.get("status") or "unknown")


# Resolve a file inside a report directory, or raise 404.
def report_file_path(run_timestamp: str, file_path: str) -> Path:
    run_dir = safe_run_dir(run_timestamp)
    try:
        return resolve_regular_file(run_dir, file_path)
    except (DownloadPathError, DownloadFileMissing) as exc:
        logger.debug(
            "api: report file %r not served for %s (%s); responding 404.", file_path, run_timestamp, type(exc).__name__
        )
        raise HTTPException(status_code=404, detail="File not found")


# Check that an evidence file belongs to this run, so one run's ref cannot read another's.
def _belongs_to_run(resolved: Path, root_name: str, run_dir: Path, run_timestamp: str) -> bool:
    if root_name == "reports":
        return run_dir == resolved or run_dir in resolved.parents
    return run_timestamp in resolved.parts


# Resolve an evidence ref to a regular file inside its allowed root that belongs to this run.
def safe_evidence_path(run_timestamp: str, ref: str) -> Path:
    run_dir = safe_run_dir(run_timestamp)
    decoded = decode_ref(ref or "")
    if decoded is None:
        logger.debug("api: evidence ref for %s could not be decoded; responding 400.", run_timestamp)
        raise HTTPException(status_code=400, detail="Malformed evidence reference")

    root_name, relative = decoded
    logger.debug("api: evidence ref for %s decoded to root %r, path %r.", run_timestamp, root_name, relative)
    root = evidence_roots().get(root_name)
    if root is None:
        logger.debug("api: evidence root %r is not an allowed root; responding 400.", root_name)
        raise HTTPException(status_code=400, detail="Malformed evidence reference")

    try:
        # resolve() follows symlinks, so a link out of the root fails containment instead of escaping.
        resolved = resolve_regular_file(root, relative)
    except DownloadPathError:
        logger.debug("api: evidence path %r rejected under %s; responding 400.", relative, root)
        raise HTTPException(status_code=400, detail="Malformed evidence reference") from None
    except DownloadFileMissing:
        logger.debug("api: evidence path %r missing under %s; responding 404.", relative, root)
        raise HTTPException(status_code=404, detail="Evidence file not found") from None
    if not _belongs_to_run(resolved, root_name, run_dir, run_timestamp):
        logger.debug("api: evidence %s does not belong to run %s; responding 404.", resolved, run_timestamp)
        raise HTTPException(status_code=404, detail="Evidence file not found for this run")
    logger.debug("api: evidence ref for %s resolved to %s.", run_timestamp, resolved)
    return resolved


# Return a row's verdict, falling back to its report.json, then Inconclusive.
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
        logger.debug("api: verdict unreadable for %r; using Inconclusive.", report_path, exc_info=True)
        return "Inconclusive"


# Collect up to limit results for one app and risk from the newest reports.
def app_risk_history(app_id: str, risk_id: str, limit: int) -> list[dict]:
    history = []
    for run_timestamp in list_report_timestamps():
        try:
            rows = read_dashboard_results(run_timestamp)
        except HTTPException as exc:
            logger.debug("api: history skipping run %s (HTTP %s).", run_timestamp, exc.status_code)
            continue
        match = next((row for row in rows if row.get("app_id") == app_id and row.get("test_id") == risk_id), None)
        if match is None:
            continue
        logger.debug("api: history match for %s/%s in run %s.", app_id, risk_id, run_timestamp)
        history.append({**match, "verdict": detail_verdict(match)})
        if len(history) >= limit:
            logger.debug("api: history limit %d reached.", limit)
            break
    return history
