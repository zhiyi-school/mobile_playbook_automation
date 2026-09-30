"""
Maps dashboard result rows onto dashboard applications, assessments, findings and reassessments.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mobile_playbook.dashboard_sync.contracts import (
    AmbiguousApplicationError,
    DashboardSyncStore,
    SupabaseRestError,
    SyncSummary,
)
from mobile_playbook.dashboard_sync.identity import finding_status, is_older_run, now, rows_by_app, sync_key
from mobile_playbook.platforms.android.risks import known_risks as known_android_risks
from mobile_playbook.platforms.ios.risks import known_risks as known_ios_risks

logger = logging.getLogger(__name__)


# Syncs result rows into dashboard applications, assessments and findings per app and returns the counts.
def sync_dashboard_results(
    rows: Sequence[Mapping[str, Any]],
    store: DashboardSyncStore,
    triggered_by: str | None = None,
    risk_counts: Mapping[str, int] | None = None,
    artifacts: Mapping[str, str] | None = None,
) -> SyncSummary:
    grouped = rows_by_app(rows)
    logger.debug("dashboard sync: mapping %d row(s) across %d app(s).", len(rows), len(grouped))
    recorded = 0
    for app_id, app_rows in grouped.items():
        logger.debug(
            "dashboard sync: app %s has %d row(s); artifact checksum known=%s.",
            app_id,
            len(app_rows),
            bool((artifacts or {}).get(app_id)),
        )
        application = sync_application(app_rows[0], store, (artifacts or {}).get(app_id))
        assessment = sync_assessment(app_id, app_rows, application["id"], store, risk_counts)
        for row in app_rows:
            if sync_finding(row, application["id"], assessment["id"], store, triggered_by):
                recorded += 1
    logger.debug("dashboard sync: %d finding status change(s) recorded.", recorded)
    return SyncSummary(
        applications=len(grouped),
        assessments=len(grouped),
        findings=len(rows),
        history=recorded,
        activity=recorded,
    )


# Syncs a run folder's dashboard_results.json and completes any reassessment request linked to the run.
def sync_report_dir(
    run_dir: Path,
    store: DashboardSyncStore,
    triggered_by: str | None = None,
    risk_counts: Mapping[str, int] | None = None,
    artifacts: Mapping[str, str] | None = None,
) -> SyncSummary:
    results_path = Path(run_dir) / "dashboard_results.json"
    rows = json.loads(results_path.read_text())
    if not isinstance(rows, list):
        logger.debug("dashboard sync: %s holds %s, not a list.", results_path, type(rows).__name__)
        raise ValueError(f"{results_path} must contain a JSON list")
    run_timestamp = Path(run_dir).name
    logger.debug("dashboard sync: read %d row(s) from %s.", len(rows), results_path)
    if not rows:
        logger.debug("dashboard sync: run %s has no results; completing its retest only.", run_timestamp)
        sync_retest(run_timestamp, store, "completed", "Retest completed with no recorded results.")
        return SyncSummary(reports=1)
    summary = sync_dashboard_results(
        rows, store, triggered_by=triggered_by, risk_counts=risk_counts, artifacts=artifacts
    )
    sync_retest(run_timestamp, store, "completed", "Retest completed — see finding for updated status.")
    return summary.plus(SyncSummary(reports=1))


# Returns icon fields for an app: the icon derived from the exact artifact when known, else the configured icon.
def icon_reference(platform: str, app_id: str, artifact_sha256: str | None = None) -> dict[str, Any]:
    from mobile_playbook.artifact_store.resolver import app_icon_reference, icon_reference_for_artifact

    if artifact_sha256:
        exact = icon_reference_for_artifact(artifact_sha256)
        if exact is not None:
            logger.debug(
                "dashboard sync: using the icon derived from artifact %s for %s.", artifact_sha256[:12], app_id
            )
            return exact
        logger.info("dashboard sync: no derived icon for the build %s used; leaving the icon unchanged.", app_id)
        return {}
    reference = app_icon_reference(platform, app_id)
    logger.debug(
        "dashboard sync: configured icon reference for %s has status %s.", app_id, reference.get("icon_extraction_status")
    )
    return {} if reference.get("icon_extraction_status") == "failed" else reference


# Updates the linked application, else adopts a single unlinked one by name, else upserts a new one.
def sync_application(
    row: Mapping[str, Any], store: DashboardSyncStore, artifact_sha256: str | None = None
) -> dict[str, Any]:
    app_id = str(row["app_id"])
    platform = str(row["platform"])
    fields = {
        "external_id": app_id,
        "name": str(row["app_name"]),
        "platform": platform,
        "identifier": str(row["package_or_bundle_id"]),
        "provisioning_status": "ready",
        "provisioning_error": None,
        "updated_at": now(),
        **icon_reference(platform, app_id, artifact_sha256),
    }
    linked = store.find_application_by_external_id_and_platform(app_id, platform)
    if linked is not None:
        logger.debug("dashboard sync: app %s already linked to application %s; updating.", app_id, linked["id"])
        return store.update_application(linked["id"], fields)
    candidates = store.find_unlinked_applications(fields["name"], platform)
    logger.debug("dashboard sync: %d unlinked %s application(s) match %r.", len(candidates), platform, fields["name"])
    if len(candidates) > 1:
        raise AmbiguousApplicationError(
            f"{len(candidates)} unlinked {platform} applications match the name {fields['name']!r}. "
            "Link or rename them in the dashboard so this run can be attached to exactly one."
        )
    if candidates:
        logger.debug("dashboard sync: adopting unlinked application %s for app %s.", candidates[0]["id"], app_id)
        return store.update_application(candidates[0]["id"], fields)
    logger.debug("dashboard sync: no existing application for %s; upserting a new one.", app_id)
    return store.upsert_application(fields)


# Updates or creates the run's assessment for an app, claiming a manual placeholder when one exists.
def sync_assessment(
    app_id: str,
    rows: Sequence[Mapping[str, Any]],
    application_id: str,
    store: DashboardSyncStore,
    risk_counts: Mapping[str, int] | None,
) -> dict[str, Any]:
    run_timestamp = str(rows[0]["run_timestamp"])
    run_key = f"{run_timestamp}::{app_id}"
    tested = {str(row["test_id"]) for row in rows if row.get("test_id")}
    tested.update(store.test_ids_for_application(application_id))
    fields = {
        "application_id": application_id,
        "status": "completed",
        "total_tests": total_tests(rows, risk_counts),
        "completed_tests": len(tested),
        "updated_at": now(),
    }
    logger.debug(
        "dashboard sync: assessment %s covers %d tested risk(s) of %d.", run_key, len(tested), fields["total_tests"]
    )
    existing = store.find_assessment_by_external_id(run_key)
    if existing is not None:
        logger.debug("dashboard sync: assessment %s exists as %s; updating.", run_key, existing["id"])
        return store.update_assessment(existing["id"], fields)
    placeholder = store.find_placeholder_assessment(application_id)
    if placeholder is not None:
        logger.debug("dashboard sync: claiming placeholder assessment %s for %s.", placeholder["id"], run_key)
        claimed = store.claim_placeholder_assessment(placeholder["id"], {**fields, "external_id": run_key})
        if claimed is not None:
            return claimed
        logger.debug("dashboard sync: placeholder %s was claimed elsewhere; re-reading %s.", placeholder["id"], run_key)
        existing = store.find_assessment_by_external_id(run_key)
        if existing is not None:
            return store.update_assessment(existing["id"], fields)
    logger.debug("dashboard sync: upserting assessment %s.", run_key)
    return store.upsert_assessment({**fields, "external_id": run_key})


# Creates or updates a finding from a row, skipping older runs; returns whether a status change was recorded.
def sync_finding(
    row: Mapping[str, Any],
    application_id: str,
    assessment_id: str,
    store: DashboardSyncStore,
    triggered_by: str | None,
) -> bool:
    external_id = f"{row['app_id']}::{row['test_id']}"
    run_timestamp = str(row["run_timestamp"])
    new_status = finding_status(str(row.get("verdict") or "Inconclusive"))
    fields = {
        "external_id": external_id,
        "application_id": application_id,
        "assessment_id": assessment_id,
        "test_id": str(row["test_id"]),
        "latest_test_run_id": run_timestamp,
        "title": str(row["test_name"]),
        "description": str(row.get("summary") or ""),
        "severity": row.get("severity"),
        "platform": str(row["platform"]),
        "updated_at": now(),
    }
    existing = store.find_finding_by_external_id(external_id)
    if existing is None:
        logger.debug("dashboard sync: finding %s not found; creating it.", external_id)
        created = store.create_finding({key: value for key, value in fields.items() if key != "latest_test_run_id"})
        if created.get("_conflicted"):
            logger.debug("dashboard sync: finding %s created concurrently; re-reading it.", external_id)
        existing = store.find_finding_by_external_id(external_id) if created.get("_conflicted") else created
    if existing is None:
        raise SupabaseRestError(f"Finding {external_id} could not be created or re-read")
    if is_older_run(run_timestamp, existing.get("latest_test_run_id")):
        logger.info(
            "dashboard sync: keeping finding %s at run %s; %s is older.",
            external_id,
            existing.get("latest_test_run_id"),
            run_timestamp,
        )
        return False
    previous_status = existing.get("status") if existing.get("latest_test_run_id") else None
    recorded_change = previous_status != new_status
    logger.debug(
        "dashboard sync: finding %s run %s status %s -> %s (change recorded=%s).",
        external_id,
        run_timestamp,
        previous_status,
        new_status,
        recorded_change,
    )
    if recorded_change:
        store.create_finding_history(
            {
                "finding_id": existing["id"],
                "previous_status": previous_status,
                "new_status": new_status,
                "changed_by": triggered_by,
                "reason": "Automation result synced",
                "sync_key": sync_key(external_id, run_timestamp, "history"),
            }
        )
        store.log_activity(
            {
                "entity_type": "finding",
                "entity_id": existing["id"],
                "action": "finding_created" if previous_status is None else "finding_status_changed",
                "metadata": {
                    "test_id": row["test_id"],
                    "previous_status": previous_status,
                    "new_status": new_status,
                    "run_timestamp": run_timestamp,
                },
                "sync_key": sync_key(external_id, run_timestamp, "activity"),
            }
        )
    store.update_finding(existing["id"], {**fields, "status": new_status})
    return recorded_change


# Moves a ticket on after a reassessment run only once no other request for it is outstanding.
def reconcile_ticket_after_retest(store: DashboardSyncStore, ticket_id: str | None, status: str) -> None:
    if not ticket_id:
        logger.debug("dashboard sync: retest has no ticket; nothing to reconcile.")
        return
    outstanding = store.outstanding_retests_for_ticket(ticket_id)
    logger.debug("dashboard sync: ticket %s has %d outstanding retest(s).", ticket_id, len(outstanding))
    if any(row.get("status") == "running" for row in outstanding):
        logger.debug("dashboard sync: ticket %s -> retest_in_progress.", ticket_id)
        store.update_ticket_status(ticket_id, "retest_in_progress")
        return
    if outstanding:
        logger.debug("dashboard sync: ticket %s -> retest_requested.", ticket_id)
        store.update_ticket_status(ticket_id, "retest_requested")
        return
    if status == "completed":
        logger.debug("dashboard sync: ticket %s -> under_review.", ticket_id)
        store.update_ticket_status(ticket_id, "under_review")
    else:
        logger.debug("dashboard sync: ticket %s left unchanged after a %s retest.", ticket_id, status)


# Closes the open reassessment request linked to a run and reconciles its ticket; returns whether one was found.
def sync_retest(run_timestamp: str, store: DashboardSyncStore, status: str, result: str) -> bool:
    retest = store.find_retest_by_external_run_id(run_timestamp)
    if retest is None or retest.get("status") in {"completed", "failed", "cancelled"}:
        logger.debug(
            "dashboard sync: no open retest for run %s (found=%s, status=%s).",
            run_timestamp,
            retest is not None,
            retest.get("status") if retest else None,
        )
        return False
    store.update_retest(retest["id"], {"status": status, "result": result, "completed_at": now()})
    if retest.get("conversation_id"):
        logger.debug("dashboard sync: adding a conversation entry for retest %s.", retest["id"])
        store.create_risk_conversation_entry(
            {
                "conversation_id": retest["conversation_id"],
                "kind": "retest_completed" if status == "completed" else "retest_failed",
                "message": result,
                "metadata": {"run_timestamp": run_timestamp},
                "source_ticket_id": retest.get("ticket_id"),
                "sync_key": sync_key(str(retest["id"]), run_timestamp, "retest"),
            }
        )
    reconcile_ticket_after_retest(store, retest.get("ticket_id"), status)
    logger.info("dashboard sync: retest %s marked %s from run %s.", retest["id"], status, run_timestamp)
    return True


# Returns the number of risks in scope for the rows' platform, preferring supplied counts.
def total_tests(rows: Sequence[Mapping[str, Any]], risk_counts: Mapping[str, int] | None) -> int:
    platform = str(rows[0]["platform"])
    if risk_counts is not None and platform in risk_counts:
        logger.debug("dashboard sync: total tests for %s from supplied counts: %d.", platform, risk_counts[platform])
        return risk_counts[platform]
    if platform == "ios":
        return len(known_ios_risks())
    if platform == "android":
        return len(known_android_risks())
    return len({str(row["test_id"]) for row in rows})
