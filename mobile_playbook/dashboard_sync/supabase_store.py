"""
Supabase PostgREST transport implementing the dashboard sync store.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib import error, parse, request

from mobile_playbook.dashboard_sync.contracts import SupabaseRestError
from mobile_playbook.dashboard_sync.identity import now
from mobile_playbook.logging_setup import redacted

logger = logging.getLogger(__name__)


# Reports whether a Supabase error is a 409 or duplicate-key conflict.
def _is_conflict(exc: Exception) -> bool:
    message = str(exc)
    conflict = "with 409" in message or "duplicate key" in message
    logger.debug("dashboard sync: Supabase error classified as conflict=%s.", conflict)
    return conflict


# Escapes LIKE wildcards in value and quotes it when PostgREST filter syntax requires.
def _escape_like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f'"{escaped}"' if "," in escaped or "(" in escaped else escaped


class SupabaseRestStore:
    """Dashboard sync store backed by the Supabase PostgREST API using the service-role key."""

    # Stores the Supabase base URL without a trailing slash and the service-role key.
    def __init__(self, supabase_url: str, service_role_key: str):
        self.base_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key

    # Builds a store from SUPABASE_URL (or DASHBOARD_SUPABASE_URL) and SUPABASE_SERVICE_ROLE_KEY.
    @classmethod
    def from_env(cls) -> "SupabaseRestStore":
        supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get("DASHBOARD_SUPABASE_URL")
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        logger.debug(
            "dashboard sync: Supabase URL from %s; service-role key present=%s.",
            "SUPABASE_URL" if os.environ.get("SUPABASE_URL") else "DASHBOARD_SUPABASE_URL",
            bool(service_role_key),
        )
        if not supabase_url:
            raise SupabaseRestError("SUPABASE_URL or DASHBOARD_SUPABASE_URL must be set")
        if not service_role_key:
            raise SupabaseRestError("SUPABASE_SERVICE_ROLE_KEY must be set")
        logger.debug("dashboard sync: Supabase store targets %s.", supabase_url.rstrip("/"))
        return cls(supabase_url, service_role_key)

    # Returns the application with this external id, or None.
    def find_application_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("applications", {"external_id": f"eq.{external_id}", "select": "*"})

    # Returns the application with this external id on this platform, or None.
    def find_application_by_external_id_and_platform(
        self, external_id: str, platform: str
    ) -> dict[str, Any] | None:
        return self._single(
            "applications",
            {"external_id": f"eq.{external_id}", "platform": f"eq.{platform}", "select": "*"},
        )

    # Returns up to five applications without an external id whose name matches case-insensitively.
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

    # Updates one application by id and returns the stored row.
    def update_application(self, application_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("applications", {"id": f"eq.{application_id}"}, fields)

    # Inserts or merges an application keyed on external_id.
    def upsert_application(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._upsert_one("applications", "external_id", fields)

    # Returns the assessment with this external id, or None.
    def find_assessment_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("assessments", {"external_id": f"eq.{external_id}", "select": "*"})

    # Returns the application's oldest manual placeholder assessment, or None.
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

    # Updates the assessment only while it is still a manual placeholder; returns None if already claimed.
    def claim_placeholder_assessment(
        self, assessment_id: str, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        rows = self._patch(
            "assessments",
            {"id": f"eq.{assessment_id}", "external_id": "like.manual::*", "select": "*"},
            fields,
        )
        logger.debug("dashboard sync: placeholder assessment %s claimed=%s.", assessment_id, bool(rows))
        return rows[0] if rows else None

    # Updates one assessment by id and returns the stored row.
    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("assessments", {"id": f"eq.{assessment_id}"}, fields)

    # Inserts or merges an assessment keyed on external_id.
    def upsert_assessment(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._upsert_one("assessments", "external_id", fields)

    # Returns the finding with this external id, or None.
    def find_finding_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return self._single("findings", {"external_id": f"eq.{external_id}", "select": "*"})

    # Returns the test ids of the application's existing findings.
    def test_ids_for_application(self, application_id: str) -> list[str]:
        rows = self._get("findings", {"application_id": f"eq.{application_id}", "select": "test_id"})
        return [str(row["test_id"]) for row in rows if row.get("test_id")]

    # Returns the reassessment request linked to a run, raising ValueError if more than one is linked.
    def find_retest_by_external_run_id(self, run_timestamp: str) -> dict[str, Any] | None:
        # A run must name exactly one request; two matches is a data fault, not a row to pick from.
        rows = self._get("retest_runs", {"external_test_run_id": f"eq.{run_timestamp}", "select": "*", "limit": "2"})
        logger.debug("dashboard sync: %d retest row(s) linked to run %s.", len(rows), run_timestamp)
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(f"run {run_timestamp} is linked to more than one reassessment request")
        return rows[0]

    # Returns the ticket's queued or running reassessment requests.
    def outstanding_retests_for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        return self._get(
            "retest_runs",
            {"ticket_id": f"eq.{ticket_id}", "status": "in.(queued,running)", "select": "id,status"},
        )

    # Updates one reassessment request by id and returns the stored row.
    def update_retest(self, retest_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("retest_runs", {"id": f"eq.{retest_id}"}, fields)

    # Sets a ticket's status and updated_at.
    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        return self._update_one("tickets", {"id": f"eq.{ticket_id}"}, {"status": status, "updated_at": now()})

    # Inserts a finding, returning a `_conflicted` marker instead of raising when it already exists.
    def create_finding(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        try:
            rows = self._post("findings", {"select": "*"}, fields)
        except SupabaseRestError as exc:
            logger.debug("dashboard sync: finding insert failed: %s", exc)
            if not _is_conflict(exc):
                raise
            logger.debug("dashboard sync: finding insert conflicted; treating it as already present.")
            return {"_conflicted": True}
        if not rows:
            raise SupabaseRestError("Supabase returned no finding after insert")
        return rows[0]

    # Updates one finding by id and returns the stored row.
    def update_finding(self, finding_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("findings", {"id": f"eq.{finding_id}"}, fields)

    # Appends a finding history row once per sync key.
    def create_finding_history(self, fields: Mapping[str, Any]) -> None:
        self._append_once("finding_history", fields)

    # Appends a risk conversation entry once per sync key.
    def create_risk_conversation_entry(self, fields: Mapping[str, Any]) -> None:
        self._append_once("risk_conversation_entries", fields)

    # Appends an activity log row once per sync key.
    def log_activity(self, fields: Mapping[str, Any]) -> None:
        self._append_once("activity_log", fields)

    # Returns the assessment with this id, or None.
    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        return self._single("assessments", {"id": f"eq.{assessment_id}", "select": "*"})

    # Returns the application with this id, or None.
    def get_application(self, application_id: str) -> dict[str, Any] | None:
        return self._single("applications", {"id": f"eq.{application_id}", "select": "*"})

    # Leases the next claimable run request to worker_id via RPC, or returns None.
    def claim_assessment_run_request(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        rows = self._post(
            "rpc/claim_assessment_run_request",
            {},
            {"p_worker_id": worker_id, "p_lease_seconds": lease_seconds},
        )
        row = rows[0] if rows else None
        logger.debug(
            "dashboard sync: worker %s claim returned %s.", worker_id, row.get("id") if row and row.get("id") else None
        )
        return row if row and row.get("id") else None

    # Recovers run requests with expired leases via RPC and returns the count it reports.
    def recover_expired_assessment_run_leases(self) -> int:
        rows = self._post("rpc/recover_expired_assessment_run_leases", {}, {})
        if not rows:
            logger.debug("dashboard sync: lease recovery returned no rows.")
            return 0
        recovered = rows[0]
        logger.debug("dashboard sync: lease recovery returned %r.", recovered)
        return recovered if isinstance(recovered, int) else 0

    # Updates one assessment run request by id and returns the stored row.
    def update_assessment_run_request(self, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        return self._update_one("assessment_run_requests", {"id": f"eq.{request_id}"}, fields)

    # Inserts a row, treating a conflict as the row already being present.
    def _append_once(self, table: str, fields: Mapping[str, Any]) -> None:
        try:
            self._post(table, {}, fields, prefer="return=minimal")
        except SupabaseRestError as exc:
            logger.debug("dashboard sync: append to %s failed: %s", table, exc)
            if not _is_conflict(exc):
                raise
            logger.debug("dashboard sync: %s row already present for sync_key.", table)

    # Returns the first row matching params, or None.
    def _single(self, table: str, params: Mapping[str, str]) -> dict[str, Any] | None:
        rows = self._get(table, params)
        return rows[0] if rows else None

    # Patches rows matching filters and returns the first, raising if none matched.
    def _update_one(self, table: str, filters: Mapping[str, str], fields: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._patch(table, {**filters, "select": "*"}, fields)
        if not rows:
            logger.debug("dashboard sync: update of %s matched no row (filters %s).", table, redacted(filters))
            raise SupabaseRestError(f"Supabase returned no {table} row after update")
        return rows[0]

    # Upserts a row merging on conflict_target and returns it, raising if none came back.
    def _upsert_one(self, table: str, conflict_target: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._post(
            table,
            {"on_conflict": conflict_target, "select": "*"},
            fields,
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not rows:
            logger.debug("dashboard sync: upsert into %s on %s returned no row.", table, conflict_target)
            raise SupabaseRestError(f"Supabase returned no {table} row after upsert")
        return rows[0]

    # Sends a GET to a table and returns the rows.
    def _get(self, table: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        return self._request_json("GET", table, params)

    # Sends a POST to a table or RPC and returns the rows.
    def _post(
        self,
        table: str,
        params: Mapping[str, str],
        payload: Mapping[str, Any],
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]]:
        return self._request_json("POST", table, params, payload, prefer=prefer)

    # Sends a PATCH to a table and returns the updated rows.
    def _patch(
        self, table: str, params: Mapping[str, str], payload: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return self._request_json("PATCH", table, params, payload, prefer="return=representation")

    # Sends an authenticated PostgREST request and returns its JSON as rows, raising SupabaseRestError on failure.
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
        logger.debug(
            "dashboard sync: Supabase %s %s params=%s fields=%s body=%d bytes headers=%s.",
            method,
            table,
            redacted(params),
            sorted(payload) if payload is not None else None,
            len(body) if body is not None else 0,
            sorted(headers),
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                content = response.read()
                logger.debug(
                    "dashboard sync: Supabase %s %s -> %s (%d bytes).",
                    method,
                    table,
                    getattr(response, "status", None),
                    len(content or b""),
                )
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.debug(
                "dashboard sync: Supabase %s %s -> HTTP %s (%d-byte error body).", method, table, exc.code, len(detail)
            )
            raise SupabaseRestError(f"Supabase {method} {table} failed with {exc.code}: {detail}") from exc
        except error.URLError as exc:
            logger.debug("dashboard sync: Supabase %s %s transport error: %s", method, table, exc.reason)
            raise SupabaseRestError(f"Supabase {method} {table} failed: {exc.reason}") from exc
        if not content:
            return []
        data = json.loads(content)
        if isinstance(data, list):
            logger.debug("dashboard sync: Supabase %s %s returned %d row(s).", method, table, len(data))
            return data
        if isinstance(data, dict):
            logger.debug("dashboard sync: Supabase %s %s returned one object.", method, table)
            return [data]
        logger.debug("dashboard sync: Supabase %s %s returned %s JSON.", method, table, type(data).__name__)
        raise SupabaseRestError(f"Supabase {method} {table} returned unexpected JSON")
