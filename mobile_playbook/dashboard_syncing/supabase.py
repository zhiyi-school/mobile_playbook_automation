from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib import error, parse, request

from mobile_playbook.dashboard_syncing.contracts import SupabaseRestError
from mobile_playbook.dashboard_syncing.identity import now

logger = logging.getLogger(__name__)


def _is_conflict(exc: Exception) -> bool:
    message = str(exc)
    return "with 409" in message or "duplicate key" in message


def _escape_like(value: str) -> str:
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

    def find_application_by_external_id_and_platform(
        self, external_id: str, platform: str
    ) -> dict[str, Any] | None:
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

    def claim_placeholder_assessment(
        self, assessment_id: str, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None:
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
        return self._update_one("tickets", {"id": f"eq.{ticket_id}"}, {"status": status, "updated_at": now()})

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

    def _patch(
        self, table: str, params: Mapping[str, str], payload: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
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
