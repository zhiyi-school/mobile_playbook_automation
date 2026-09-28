from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from mobile_playbook import sync_status
from mobile_playbook.dashboard_syncing.contracts import AmbiguousApplicationError, DashboardSyncStore, SyncSummary
from mobile_playbook.dashboard_syncing.mapping import sync_report_dir, sync_retest
from mobile_playbook.reporting.run_manifest import artifact_checksums, is_completed, read_manifest
from mobile_playbook.sync_state import is_processed, mark_processed, report_digest

logger = logging.getLogger(__name__)


def fail_report_lifecycle(run_dir: Path, manifest: Mapping[str, Any], store: DashboardSyncStore) -> None:
    detail = str(manifest.get("error") or "The automation run failed before completing.")
    sync_retest(Path(run_dir).name, store, "failed", detail)


def sync_reports(
    reports_dir: Path,
    store: DashboardSyncStore,
    run_timestamps: Iterable[str] | None = None,
    triggered_by: str | None = None,
    risk_counts: Mapping[str, int] | None = None,
    allow_legacy_report: bool = False,
    force: bool = False,
) -> SyncSummary:
    summary = SyncSummary()
    logger.debug(
        "dashboard sync: pass over %s (runs=%s, triggered_by=%s, legacy=%s, force=%s).",
        reports_dir,
        list(run_timestamps) if isinstance(run_timestamps, (list, tuple)) else run_timestamps,
        triggered_by,
        allow_legacy_report,
        force,
    )
    for run_dir in report_dirs(reports_dir, run_timestamps):
        manifest = read_manifest(run_dir)
        logger.debug(
            "dashboard sync: considering %s (manifest status %s).",
            run_dir.name,
            manifest.get("status") if manifest is not None else None,
        )
        skip_reason = report_skip_reason(manifest, allow_legacy_report)
        if skip_reason is not None:
            if manifest is not None:
                logger.debug("dashboard sync: failing the reassessment lifecycle for skipped run %s.", run_dir.name)
                fail_report_lifecycle(run_dir, manifest, store)
            sync_status.mark_not_required(run_dir, skip_reason)
            logger.info("dashboard sync: skipping %s (%s).", run_dir.name, skip_reason)
            summary = summary.plus(SyncSummary(skipped_reports=1))
            continue
        digest = report_digest(run_dir)
        if not force and is_processed(reports_dir, run_dir.name, digest):
            logger.debug(
                "dashboard sync: %s unchanged since last sync (digest %s); skipping.", run_dir.name, digest[:12]
            )
            sync_status.mark_completed(run_dir)
            summary = summary.plus(SyncSummary(unchanged_reports=1))
            continue
        logger.debug("dashboard sync: processing %s (digest %s, force=%s).", run_dir.name, digest[:12], force)
        sync_status.mark_running(run_dir)
        try:
            current = sync_report_dir(
                run_dir,
                store,
                triggered_by=triggered_by,
                risk_counts=risk_counts,
                artifacts=artifact_checksums(manifest),
            )
        except Exception as exc:
            logger.exception("dashboard sync: failed to sync report %s: %s", run_dir.name, exc)
            sync_status.mark_failed(run_dir, str(exc), retryable=not isinstance(exc, AmbiguousApplicationError))
            summary = summary.plus(SyncSummary(failed_reports=1))
            continue
        mark_processed(reports_dir, run_dir.name, digest)
        logger.debug("dashboard sync: ledger updated for %s; counts %s.", run_dir.name, current.counts())
        sync_status.mark_completed(run_dir, current.counts())
        logger.info(
            "dashboard sync: synced %s (%d app(s), %d finding(s)).",
            run_dir.name,
            current.applications,
            current.findings,
        )
        summary = summary.plus(current)
    logger.debug("dashboard sync: pass finished with %s.", summary)
    return summary


def report_dirs(reports_dir: Path, run_timestamps: Iterable[str] | None) -> Iterable[Path]:
    root = Path(reports_dir)
    if run_timestamps is not None:
        for timestamp in run_timestamps:
            logger.debug("dashboard sync: explicitly requested run %s.", timestamp)
            yield root / timestamp
        return
    if not root.is_dir():
        logger.info("dashboard sync: no reports directory at %s; nothing to sync.", root)
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "dashboard_results.json").exists():
            yield child
        else:
            logger.debug("dashboard sync: ignoring %s (not a report directory).", child.name)


def report_skip_reason(manifest: Mapping[str, Any] | None, allow_legacy_report: bool) -> str | None:
    if manifest is None:
        return None if allow_legacy_report else "no run manifest; pass --allow-legacy-report to import historical runs"
    if not is_completed(manifest):
        status = manifest.get("status") or "unknown"
        return f"run manifest status is {status!r}, not completed"
    return None
