"""Background worker that turns durable assessment run requests into runs.

The request queue lives in the dashboard database; readiness and run start are
asked of the automation API, so the API's own per-platform lock stays the single
authority on whether a device is free.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mobile_playbook.storage import reports_root
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error, parse, request

from mobile_playbook.dashboard_syncing.contracts import SupabaseRestError
from mobile_playbook.dashboard_syncing.supabase import SupabaseRestStore
from mobile_playbook.env_file import load_env_file

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_LEASE_SECONDS = 900
DEFAULT_POLL_SECONDS = 15.0
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 900
MAX_ATTEMPTS = 20

ACTIVE_REQUEST_STATUSES = ("queued", "waiting", "claimed", "running")

# Blockers the automation host can report that no retry will clear on its own.
NON_RETRYABLE_BLOCKERS = {"configuration_incomplete", "no_tests_enabled", "invalid_run_request"}


class Store(Protocol):
    def recover_expired_assessment_run_leases(self) -> int: ...

    def claim_assessment_run_request(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None: ...

    def update_assessment_run_request(self, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None: ...

    def get_application(self, application_id: str) -> dict[str, Any] | None: ...

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...


class AutomationUnavailable(RuntimeError):
    pass


class RunRejected(RuntimeError):
    """The automation host refused the run. `blocker` says whether to retry."""

    def __init__(self, blocker: str, detail: str):
        super().__init__(detail)
        self.blocker = blocker
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _at(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def backoff_seconds(attempts: int) -> float:
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))))


class AutomationApi:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def readiness(self, platform: str, app_external_id: str) -> dict[str, Any]:
        path = f"/config/{parse.quote(platform)}/apps/{parse.quote(app_external_id)}/provisioning"
        return self._request("GET", path)

    def start_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/runs", payload)

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                content = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 409:
                raise RunRejected("platform_busy", "Another run is already using the test device.") from exc
            if 400 <= exc.code < 500:
                raise RunRejected("invalid_run_request", f"The automation host rejected the run: {detail}") from exc
            raise AutomationUnavailable(f"The automation host returned {exc.code}.") from exc
        except (error.URLError, socket.timeout) as exc:
            raise AutomationUnavailable(f"The automation host could not be reached: {exc}") from exc
        return json.loads(content) if content else {}


def _config_path(platform: str) -> str:
    return f"configs/{platform}.yaml"


def _finish(store: Store, request_id: str, status: str, **fields: Any) -> None:
    store.update_assessment_run_request(
        request_id,
        {
            "status": status,
            "lease_expires_at": None,
            "worker_id": None,
            "completed_at": _now(),
            **fields,
        },
    )


def _set_assessment_status(store: Store, assessment: Mapping[str, Any], status: str) -> None:
    if assessment.get("status") == status:
        return
    try:
        store.update_assessment(assessment["id"], {"status": status, "updated_at": _now()})
    except SupabaseRestError as exc:
        # A refused transition means a newer backend state already won; the
        # request outcome below still stands.
        logger.warning("assessment worker: assessment %s stayed at %s (%s).", assessment["id"], assessment.get("status"), exc)


def _wait(store: Store, req: Mapping[str, Any], assessment: Mapping[str, Any], blocker: str, detail: str) -> str:
    delay = backoff_seconds(int(req.get("attempts") or 1))
    store.update_assessment_run_request(
        req["id"],
        {
            "status": "waiting",
            "blocker_code": blocker,
            "last_error": detail,
            "next_attempt_at": _at(delay),
            "lease_expires_at": None,
            "worker_id": None,
        },
    )
    _set_assessment_status(store, assessment, "waiting")
    logger.info(
        "assessment worker: assessment %s waiting on %s, next attempt in %.0fs.",
        assessment["id"],
        blocker,
        delay,
    )
    return "waiting"


def process_once(
    store: Store,
    api: AutomationApi,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    risk_ids: Callable[[str], Sequence[str]] | None = None,
    reports_dir: str | None = None,
) -> str:
    """Claim at most one request and act on it. Returns what happened."""
    store.recover_expired_assessment_run_leases()

    req = store.claim_assessment_run_request(worker_id, lease_seconds)
    if req is None:
        return "idle"

    assessment = store.get_assessment(req["assessment_id"])
    if assessment is None:
        _finish(store, req["id"], "cancelled", blocker_code="assessment_missing", last_error=None)
        return "cancelled"
    if assessment.get("status") == "completed":
        _finish(store, req["id"], "completed", blocker_code=None, last_error=None)
        return "already_completed"

    application = store.get_application(req["application_id"])
    external_id = (application or {}).get("external_id")
    if not external_id:
        _finish(
            store,
            req["id"],
            "failed",
            blocker_code="configuration_incomplete",
            last_error="This application is not registered with the automation host.",
        )
        _set_assessment_status(store, assessment, "failed")
        return "failed"

    if int(req.get("attempts") or 0) > MAX_ATTEMPTS:
        _finish(
            store,
            req["id"],
            "failed",
            blocker_code=req.get("blocker_code") or "retry_limit_reached",
            last_error="Automated testing could not start after repeated attempts.",
        )
        _set_assessment_status(store, assessment, "failed")
        return "exhausted"

    try:
        readiness = api.readiness(req["platform"], external_id)
    except AutomationUnavailable as exc:
        return _wait(store, req, assessment, "automation_unavailable", str(exc))

    if not readiness.get("runnable"):
        blocker = readiness.get("blocker_code") or "not_ready"
        detail = readiness.get("detail") or "This assessment cannot start yet."
        if readiness.get("retryable") is False or blocker in NON_RETRYABLE_BLOCKERS:
            _finish(store, req["id"], "failed", blocker_code=blocker, last_error=detail)
            _set_assessment_status(store, assessment, "failed")
            return "blocked"
        return _wait(store, req, assessment, blocker, detail)

    store.update_assessment_run_request(
        req["id"],
        {"status": "running", "started_at": _now(), "blocker_code": None, "last_error": None},
    )
    _set_assessment_status(store, assessment, "running")

    payload: dict[str, Any] = {
        "platform": req["platform"],
        "config_path": _config_path(req["platform"]),
        "apps": external_id,
        "out_dir": reports_dir or str(reports_root()),
    }
    selected = list(risk_ids(req["platform"])) if risk_ids else []
    if selected:
        payload["risks"] = ",".join(selected)

    try:
        started = api.start_run(payload)
    except RunRejected as exc:
        if exc.blocker in NON_RETRYABLE_BLOCKERS:
            _finish(store, req["id"], "failed", blocker_code=exc.blocker, last_error=exc.detail)
            _set_assessment_status(store, assessment, "failed")
            return "failed"
        _set_assessment_status(store, assessment, "queued")
        return _wait(store, req, assessment, exc.blocker, exc.detail)
    except AutomationUnavailable as exc:
        _set_assessment_status(store, assessment, "queued")
        return _wait(store, req, assessment, "automation_unavailable", str(exc))

    # Starting the run is the request's whole job: the dashboard sync worker
    # imports the result and moves the assessment to completed or failed.
    _finish(store, req["id"], "completed", blocker_code=None, last_error=None)
    logger.info(
        "assessment worker: started run %s for assessment %s.",
        started.get("run_id"),
        assessment["id"],
    )
    return "started"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run queued assessment execution requests.")
    parser.add_argument("--api-url", default=os.environ.get("AUTOMATION_API_URL", DEFAULT_API_URL))
    parser.add_argument("--reports-dir", default=str(reports_root()))
    parser.add_argument("--worker-id", default=os.environ.get("ASSESSMENT_WORKER_ID"))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("ASSESSMENT_WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=int(os.environ.get("ASSESSMENT_WORKER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)),
    )
    parser.add_argument("--once", action="store_true", help="Process at most one request and exit.")
    args = parser.parse_args(argv)

    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be greater than 0")
    if args.lease_seconds < 30:
        parser.error("--lease-seconds must be at least 30")

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

    worker_id = args.worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    api = AutomationApi(args.api_url)
    logger.info("assessment worker %s polling every %.0fs.", worker_id, args.poll_seconds)

    while True:
        try:
            outcome = process_once(
                store,
                api,
                worker_id=worker_id,
                lease_seconds=args.lease_seconds,
                reports_dir=args.reports_dir,
            )
        except SupabaseRestError as exc:
            logger.error("assessment worker: %s", exc)
            outcome = "error"
        except Exception:
            logger.exception("assessment worker: unexpected failure")
            outcome = "error"
        if args.once:
            return 0 if outcome not in ("error",) else 1
        time.sleep(args.poll_seconds if outcome in ("idle", "error") else 0.5)


if __name__ == "__main__":
    raise SystemExit(main())
