from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import error, parse, request

from mobile_playbook import sync_status
from mobile_playbook.env_file import load_env_file
from mobile_playbook.reporting.run_manifest import artifact_checksums, is_completed, read_manifest
from mobile_playbook.sync_state import SyncBusy, is_processed, mark_processed, report_digest, single_instance
from mobile_playbook.platforms.android.risks import known_risks as known_android_risks
from mobile_playbook.platforms.ios.risks import known_risks as known_ios_risks

logger = logging.getLogger(__name__)


class DashboardSyncStore(Protocol):
    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        ...

    def find_application_by_external_id_and_platform(self, external_id: str, platform: str) -> dict[str, Any] | None:
        ...

    def find_unlinked_applications(self, name: str, platform: str) -> list[dict[str, Any]]:
        ...

    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        ...

    def find_placeholder_assessment(self, application_id: str) -> dict[str, Any] | None:
        ...

    def claim_placeholder_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        ...

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        ...

    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None:
        ...

    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        ...

    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def create_finding_history(self, fields: Mapping[str, Any]) -> None:
        ...

    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None:
        ...

    def log_activity(self, fields: Mapping[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class SyncSummary:
    reports: int = 0
    applications: int = 0
    assessments: int = 0
    findings: int = 0
    history: int = 0
    activity: int = 0
    failed_reports: int = 0
    skipped_reports: int = 0
    unchanged_reports: int = 0

    def plus(self, other: "SyncSummary") -> "SyncSummary":
        return SyncSummary(
            reports=self.reports + other.reports,
            applications=self.applications + other.applications,
            assessments=self.assessments + other.assessments,
            findings=self.findings + other.findings,
            history=self.history + other.history,
            activity=self.activity + other.activity,
            failed_reports=self.failed_reports + other.failed_reports,
            skipped_reports=self.skipped_reports + other.skipped_reports,
            unchanged_reports=self.unchanged_reports + other.unchanged_reports,
        )

    def counts(self) -> dict[str, int]:
        return {
            "applications": self.applications,
            "assessments": self.assessments,
            "findings": self.findings,
            "history": self.history,
            "activity": self.activity,
        }


class SupabaseRestError(RuntimeError):
    pass


class AmbiguousApplicationError(RuntimeError):
    pass


def _is_conflict(exc: Exception) -> bool:
    message = str(exc)
    return "with 409" in message or "duplicate key" in message


def _escape_like(value: str) -> str:
    """Quote a PostgREST ilike pattern so wildcards in an app name match literally."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f'"{escaped}"' if "," in escaped or "(" in escaped else escaped


class SupabaseRestStore:
    def __init__(self, supabase_url: str, service_role_key: str):
        self.base_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key

    @classmethod
    def from_env(cls) -> "SupabaseRestStore":
        supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get("DASHBOARD_SUPABASE_URL")
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not supabase_url:
            raise SupabaseRestError("SUPABASE_URL or DASHBOARD_SUPABASE_URL must be set")
        if not service_role_key:
            raise SupabaseRestError("SUPABASE_SERVICE_ROLE_KEY must be set")
        return cls(supabase_url, service_role_key)

    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("applications", {"external_id": f"eq.{external_id}", "select": "*"})

    def find_application_by_external_id_and_platform(self, external_id: str, platform: str) -> dict[str, Any] | None:
        """`external_id` alone is ambiguous: the same config app id can exist on both platforms."""
        return self._single(
            "applications",
            {"external_id": f"eq.{external_id}", "platform": f"eq.{platform}", "select": "*"},
        )

    def find_unlinked_applications(self, name: str, platform: str) -> list[dict[str, Any]]:
        return self._get(
            "applications",
            {
                "external_id": "is.null",
                "name": f"ilike.{_escape_like(name)}",
                "platform": f"eq.{platform}",
                "select": "*",
                "limit": "5",
            },
        )

    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("applications", {"id": f"eq.{application_id}"}, fields)

    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._upsert_one("applications", "external_id", fields)

    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("assessments", {"external_id": f"eq.{external_id}", "select": "*"})

    def find_placeholder_assessment(self, application_id: str) -> dict[str, Any] | None:
        return self._single(
            "assessments",
            {
                "application_id": f"eq.{application_id}",
                "external_id": "like.manual::*",
                "order": "created_at.asc",
                "select": "*",
                "limit": "1",
            },
        )

    def claim_placeholder_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        rows = self._patch(
            "assessments",
            {"id": f"eq.{assessment_id}", "external_id": "like.manual::*", "select": "*"},
            fields,
        )
        return rows[0] if rows else None

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("assessments", {"id": f"eq.{assessment_id}"}, fields)

    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._upsert_one("assessments", "external_id", fields)

    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("findings", {"external_id": f"eq.{external_id}", "select": "*"})

    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None:
        return self._single(
            "retest_runs",
            {"external_test_run_id": f"eq.{run_timestamp}", "select": "*", "limit": "1"},
        )

    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("retest_runs", {"id": f"eq.{retest_id}"}, fields)

    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        return self._update_one("tickets", {"id": f"eq.{ticket_id}"}, {"status": status, "updated_at": _now()})

    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        try:
            rows = self._post("findings", {"select": "*"}, fields)
        except SupabaseRestError as exc:
            if not _is_conflict(exc):
                raise
            return {"_conflicted": True}
        if not rows:
            raise SupabaseRestError("Supabase returned no finding after insert")
        return rows[0]

    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("findings", {"id": f"eq.{finding_id}"}, fields)

    def create_finding_history(self, fields: Mapping[str, Any]) -> None:
        self._append_once("finding_history", fields)

    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None:
        self._append_once("risk_conversation_entries", fields)

    def log_activity(self, fields: Mapping[str, Any]) -> None:
        self._append_once("activity_log", fields)

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        return self._single("assessments", {"id": f"eq.{assessment_id}", "select": "*"})

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        return self._single("applications", {"id": f"eq.{application_id}", "select": "*"})

    def claim_assessment_run_request(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        rows = self._post(
            "rpc/claim_assessment_run_request",
            {},
            {"p_worker_id": worker_id, "p_lease_seconds": lease_seconds},
        )
        row = rows[0] if rows else None
        return row if row and row.get("id") else None

    def recover_expired_assessment_run_leases(self) -> int:
        rows = self._post("rpc/recover_expired_assessment_run_leases", {}, {})
        if not rows:
            return 0
        recovered = rows[0]
        return recovered if isinstance(recovered, int) else 0

    def update_assessment_run_request(self, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("assessment_run_requests", {"id": f"eq.{request_id}"}, fields)

    def _append_once(self, table: str, fields: Mapping[str, Any]) -> None:
        try:
            self._post(table, {}, fields, prefer="return=minimal")
        except SupabaseRestError as exc:
            if not _is_conflict(exc):
                raise
            logger.debug("dashboard sync: %s row already present for sync_key.", table)

    def _single(self, table: str, params: Mapping[str, str]) -> dict[str, Any] | None:
        rows = self._get(table, params)
        return rows[0] if rows else None

    def _update_one(self, table: str, filters: Mapping[str, str], fields: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._patch(table, {**filters, "select": "*"}, fields)
        if not rows:
            raise SupabaseRestError(f"Supabase returned no {table} row after update")
        return rows[0]

    def _upsert_one(self, table: str, conflict_target: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._post(
            table,
            {"on_conflict": conflict_target, "select": "*"},
            fields,
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not rows:
            raise SupabaseRestError(f"Supabase returned no {table} row after upsert")
        return rows[0]

    def _get(self, table: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        return self._request_json("GET", table, params)

    def _post(
        self,
        table: str,
        params: Mapping[str, str],
        payload: Mapping[str, Any],
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]]:
        return self._request_json("POST", table, params, payload, prefer=prefer)

    def _patch(self, table: str, params: Mapping[str, str], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self._request_json("PATCH", table, params, payload, prefer="return=representation")

    def _request_json(
        self,
        method: str,
        table: str,
        params: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
        prefer: str | None = None,
    ) -> list[dict[str, Any]]:
        query = parse.urlencode(params)
        url = f"{self.base_url}/rest/v1/{parse.quote(table)}"
        if query:
            url = f"{url}?{query}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if prefer is not None:
            headers["Prefer"] = prefer
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=30) as response:
                content = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SupabaseRestError(f"Supabase {method} {table} failed with {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise SupabaseRestError(f"Supabase {method} {table} failed: {exc.reason}") from exc
        if not content:
            return []
        data = json.loads(content)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        raise SupabaseRestError(f"Supabase {method} {table} returned unexpected JSON")


def sync_dashboard_results(
    rows: Sequence[Mapping[str, Any]],
    store: DashboardSyncStore,
    triggered_by: str | None = None,
    risk_counts: Mapping[str, int] | None = None,
    artifacts: Mapping[str, str] | None = None,
) -> SyncSummary:
    grouped = _rows_by_app(rows)
    recorded = 0
    for app_id, app_rows in grouped.items():
        first = app_rows[0]
        application = _sync_application(first, store, (artifacts or {}).get(app_id))
        assessment = _sync_assessment(app_id, app_rows, application["id"], store, risk_counts)
        for row in app_rows:
            if _sync_finding(row, application["id"], assessment["id"], store, triggered_by):
                recorded += 1
    return SyncSummary(
        applications=len(grouped),
        assessments=len(grouped),
        findings=len(rows),
        history=recorded,
        activity=recorded,
    )


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
        raise ValueError(f"{results_path} must contain a JSON list")
    run_timestamp = Path(run_dir).name
    if not rows:
        # A completed run with no result rows still has a lifecycle to close.
        _sync_retest(run_timestamp, store, "completed", "Retest completed with no recorded results.")
        return SyncSummary(reports=1)
    summary = sync_dashboard_results(
        rows, store, triggered_by=triggered_by, risk_counts=risk_counts, artifacts=artifacts
    )
    _sync_retest(run_timestamp, store, "completed", "Retest completed — see finding for updated status.")
    return summary.plus(SyncSummary(reports=1))


def fail_report_lifecycle(run_dir: Path, manifest: Mapping[str, Any], store: DashboardSyncStore) -> None:
    """Resolve a failed run's retest without importing its partial feed."""
    detail = str(manifest.get("error") or "The automation run failed before completing.")
    _sync_retest(Path(run_dir).name, store, "failed", detail)


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
    for run_dir in _report_dirs(reports_dir, run_timestamps):
        manifest = read_manifest(run_dir)
        skip_reason = _skip_reason(manifest, allow_legacy_report)
        if skip_reason is not None:
            if manifest is not None:
                fail_report_lifecycle(run_dir, manifest, store)
            sync_status.mark_not_required(run_dir, skip_reason)
            logger.info("dashboard sync: skipping %s (%s).", run_dir.name, skip_reason)
            summary = summary.plus(SyncSummary(skipped_reports=1))
            continue
        digest = report_digest(run_dir)
        if not force and is_processed(reports_dir, run_dir.name, digest):
            sync_status.mark_completed(run_dir)
            summary = summary.plus(SyncSummary(unchanged_reports=1))
            continue
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
            sync_status.mark_failed(
                run_dir, str(exc), retryable=not isinstance(exc, AmbiguousApplicationError)
            )
            summary = summary.plus(SyncSummary(failed_reports=1))
            continue
        mark_processed(reports_dir, run_dir.name, digest)
        sync_status.mark_completed(run_dir, current.counts())
        logger.info(
            "dashboard sync: synced %s (%d app(s), %d finding(s)).",
            run_dir.name,
            current.applications,
            current.findings,
        )
        summary = summary.plus(current)
    return summary


def _rows_by_app(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        app_id = str(row["app_id"])
        grouped.setdefault(app_id, []).append(row)
    return grouped


def _icon_reference(platform: str, app_id: str, artifact_sha256: str | None = None) -> dict[str, Any]:
    """Prefer the build the run recorded; an empty result leaves the existing reference alone."""
    from mobile_playbook.artifact_store.resolver import app_icon_reference, icon_reference_for_artifact

    if artifact_sha256:
        exact = icon_reference_for_artifact(artifact_sha256)
        if exact is not None:
            return exact
        logger.info("dashboard sync: no derived icon for the build %s used; leaving the icon unchanged.", app_id)
        return {}

    reference = app_icon_reference(platform, app_id)
    return {} if reference.get("icon_extraction_status") == "failed" else reference


def _sync_application(
    row: Mapping[str, Any], store: DashboardSyncStore, artifact_sha256: str | None = None
) -> dict[str, Any]:
    now = _now()
    app_id = str(row["app_id"])
    platform = str(row["platform"])
    fields = {
        "external_id": app_id,
        "name": str(row["app_name"]),
        "platform": platform,
        "identifier": str(row["package_or_bundle_id"]),
        "provisioning_status": "ready",
        "provisioning_error": None,
        "updated_at": now,
        **_icon_reference(platform, app_id, artifact_sha256),
    }
    linked = store.find_application_by_external_id_and_platform(app_id, platform)
    if linked is not None:
        return store.update_application(linked["id"], fields)
    candidates = store.find_unlinked_applications(fields["name"], platform)
    if len(candidates) > 1:
        raise AmbiguousApplicationError(
            f"{len(candidates)} unlinked {platform} applications match the name {fields['name']!r}. "
            "Link or rename them in the dashboard so this run can be attached to exactly one."
        )
    if candidates:
        return store.update_application(candidates[0]["id"], fields)
    return store.upsert_application(fields)


def _sync_assessment(
    app_id: str,
    rows: Sequence[Mapping[str, Any]],
    application_id: str,
    store: DashboardSyncStore,
    risk_counts: Mapping[str, int] | None,
) -> dict[str, Any]:
    run_timestamp = str(rows[0]["run_timestamp"])
    run_key = f"{run_timestamp}::{app_id}"
    fields = {
        "application_id": application_id,
        "status": "completed",
        "total_tests": _total_tests(rows, risk_counts),
        "completed_tests": len({str(row["test_id"]) for row in rows}),
        "updated_at": _now(),
    }
    existing = store.find_assessment_by_external_id(run_key)
    if existing is not None:
        return store.update_assessment(existing["id"], fields)

    placeholder = store.find_placeholder_assessment(application_id)
    if placeholder is not None:
        claimed = store.claim_placeholder_assessment(placeholder["id"], {**fields, "external_id": run_key})
        if claimed is not None:
            return claimed
        existing = store.find_assessment_by_external_id(run_key)
        if existing is not None:
            return store.update_assessment(existing["id"], fields)

    return store.upsert_assessment({**fields, "external_id": run_key})


def _sync_finding(
    row: Mapping[str, Any],
    application_id: str,
    assessment_id: str,
    store: DashboardSyncStore,
    triggered_by: str | None,
) -> bool:
    external_id = f"{row['app_id']}::{row['test_id']}"
    run_timestamp = str(row["run_timestamp"])
    new_status = _finding_status(str(row.get("verdict") or "Inconclusive"))
    fields = {
        "external_id": external_id,
        "application_id": application_id,
        "assessment_id": assessment_id,
        "test_id": str(row["test_id"]),
        "latest_test_run_id": str(row["run_timestamp"]),
        "title": str(row["test_name"]),
        "description": str(row.get("summary") or ""),
        "severity": row.get("severity"),
        "platform": str(row["platform"]),
        "updated_at": _now(),
    }
    existing = store.find_finding_by_external_id(external_id)
    if existing is None:
        # Created without status/latest_test_run_id so a retry still sees the pre-run state.
        created = store.create_finding({key: value for key, value in fields.items() if key != "latest_test_run_id"})
        existing = store.find_finding_by_external_id(external_id) if created.get("_conflicted") else created
    if existing is None:
        raise SupabaseRestError(f"Finding {external_id} could not be created or re-read")

    if _is_older_run(run_timestamp, existing.get("latest_test_run_id")):
        logger.info(
            "dashboard sync: keeping finding %s at run %s; %s is older.",
            external_id,
            existing.get("latest_test_run_id"),
            run_timestamp,
        )
        return False

    previous_status = existing.get("status") if existing.get("latest_test_run_id") else None
    recorded_change = previous_status != new_status
    if recorded_change:
        store.create_finding_history(
            {
                "finding_id": existing["id"],
                "previous_status": previous_status,
                "new_status": new_status,
                "changed_by": triggered_by,
                "reason": "Automation result synced",
                "sync_key": _sync_key(external_id, run_timestamp, "history"),
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
                "sync_key": _sync_key(external_id, run_timestamp, "activity"),
            }
        )
    store.update_finding(existing["id"], {**fields, "status": new_status})
    return recorded_change


def _sync_key(external_id: str, run_timestamp: str, kind: str) -> str:
    return f"{external_id}::{run_timestamp}::{kind}"


def _run_sort_key(run_timestamp: str) -> tuple[str, int]:
    """String ordering would put `..._12-00-00-10` before `..._12-00-00-2`."""
    base, _, suffix = str(run_timestamp).rpartition("-")
    if base and suffix.isdigit() and len(suffix) <= 2 and base.count("-") >= 4:
        return base, int(suffix)
    return str(run_timestamp), 1


def _is_older_run(candidate: str, current: Any) -> bool:
    if not current:
        return False
    return _run_sort_key(candidate) < _run_sort_key(str(current))


def _sync_retest(run_timestamp: str, store: DashboardSyncStore, status: str, result: str) -> bool:
    """Resolve the retest correlated with this run, if there is one."""
    retest = store.find_retest_by_external_run_id(run_timestamp)
    if retest is None:
        return False
    # An already-resolved retest is a repeated sync pass, not a second result.
    if retest.get("status") in {"completed", "failed"}:
        return False
    store.update_retest(retest["id"], {"status": status, "result": result, "completed_at": _now()})
    if retest.get("conversation_id"):
        store.create_risk_conversation_entry(
            {
                "conversation_id": retest["conversation_id"],
                "kind": "retest_completed" if status == "completed" else "retest_failed",
                "message": result,
                "metadata": {"run_timestamp": run_timestamp},
                "source_ticket_id": retest.get("ticket_id"),
                "sync_key": _sync_key(str(retest["id"]), run_timestamp, "retest"),
            }
        )
    if status == "completed" and retest.get("ticket_id"):
        store.update_ticket_status(retest["ticket_id"], "under_review")
    logger.info("dashboard sync: retest %s marked %s from run %s.", retest["id"], status, run_timestamp)
    return True


def _total_tests(rows: Sequence[Mapping[str, Any]], risk_counts: Mapping[str, int] | None) -> int:
    platform = str(rows[0]["platform"])
    if risk_counts is not None and platform in risk_counts:
        return risk_counts[platform]
    if platform == "ios":
        return len(known_ios_risks())
    if platform == "android":
        return len(known_android_risks())
    return len({str(row["test_id"]) for row in rows})


def _finding_status(verdict: str) -> str:
    if verdict == "At Risk":
        return "at_risk"
    if verdict == "Reduced Risk":
        return "reduced_risk"
    return "inconclusive"


def _report_dirs(reports_dir: Path, run_timestamps: Iterable[str] | None) -> Iterable[Path]:
    root = Path(reports_dir)
    if run_timestamps is not None:
        for timestamp in run_timestamps:
            yield root / timestamp
        return
    if not root.is_dir():
        logger.info("dashboard sync: no reports directory at %s; nothing to sync.", root)
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "dashboard_results.json").exists():
            yield child


def _skip_reason(manifest: Mapping[str, Any] | None, allow_legacy_report: bool) -> str | None:
    if manifest is None:
        if allow_legacy_report:
            return None
        return "no run manifest; pass --allow-legacy-report to import historical runs"
    if not is_completed(manifest):
        status = manifest.get("status") or "unknown"
        return f"run manifest status is {status!r}, not completed"
    return None


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync completed automation reports into the dashboard database.")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--run-timestamp", action="append", dest="run_timestamps")
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Development-only in-process loop. Unattended operation uses post-run one-shot workers and a launchd recovery sweep.",
    )
    parser.add_argument(
        "--allow-legacy-report",
        action="store_true",
        help="Import report folders that predate the run manifest. Their completion is unverified.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-sync reports already recorded in the processed ledger, for reconciliation or a rebuild.",
    )
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=0,
        help="Wait this long for another sync pass to release the host lock. Post-run triggers use this to serialize.",
    )
    args = parser.parse_args(argv)

    if args.interval_seconds is not None and args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than 0")
    if args.lock_wait_seconds < 0:
        parser.error("--lock-wait-seconds must be greater than or equal to 0")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_env_file(Path(".env"))
    try:
        store = SupabaseRestStore.from_env()
    except SupabaseRestError as exc:
        logger.error(
            "%s. Set it in the environment or in .env; the service-role key must never be committed "
            "or exposed to the frontend.",
            exc,
        )
        return 2

    reports_dir = Path(args.reports_dir)
    while True:
        try:
            with single_instance(reports_dir, wait_seconds=args.lock_wait_seconds):
                summary = sync_reports(
                    reports_dir,
                    store,
                    run_timestamps=args.run_timestamps,
                    triggered_by=args.triggered_by,
                    allow_legacy_report=args.allow_legacy_report,
                    force=args.force,
                )
        except SyncBusy as exc:
            logger.info("dashboard sync: skipped, %s.", exc)
            if args.interval_seconds is None:
                return 0
            time.sleep(args.interval_seconds)
            continue
        except Exception as exc:
            sync_status.record_worker_pass(reports_dir, succeeded=False, error=str(exc))
            raise
        sync_status.record_worker_pass(
            reports_dir,
            succeeded=not summary.failed_reports,
            error=f"{summary.failed_reports} report(s) failed to sync" if summary.failed_reports else None,
        )
        logger.info(
            "dashboard sync: %d report(s), %d app(s), %d assessment(s), %d finding(s), "
            "%d unchanged report(s), %d skipped report(s), %d failed report(s).",
            summary.reports,
            summary.applications,
            summary.assessments,
            summary.findings,
            summary.unchanged_reports,
            summary.skipped_reports,
            summary.failed_reports,
        )
        if args.interval_seconds is None:
            return 1 if summary.failed_reports else 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
