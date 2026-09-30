"""
Background worker that turns queued dashboard assessment run requests into automation API runs.
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

from mobile_playbook.common.storage_paths import reports_root
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error, parse, request

from mobile_playbook.dashboard_sync.contracts import SupabaseRestError
from mobile_playbook.dashboard_sync.supabase_store import SupabaseRestStore
from mobile_playbook.common.env_file import load_env_file
from mobile_playbook.common.logging_setup import log_level

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
    """Dashboard database operations the worker needs for leasing requests and updating assessments."""

    # Recovers requests whose leases expired and returns how many were recovered.
    def recover_expired_assessment_run_leases(self) -> int: ...

    # Leases the next claimable request to worker_id, or returns None when there is none.
    def claim_assessment_run_request(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None: ...

    # Updates fields on a run request and returns the stored row.
    def update_assessment_run_request(self, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...

    # Returns the assessment row, or None if it does not exist.
    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None: ...

    # Returns the application row, or None if it does not exist.
    def get_application(self, application_id: str) -> dict[str, Any] | None: ...

    # Updates fields on an assessment and returns the stored row.
    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...


class AutomationUnavailable(RuntimeError):
    pass


class RunRejected(RuntimeError):
    """The automation host refused the run. `blocker` says whether to retry."""

    # Stores the blocker code and detail message.
    def __init__(self, blocker: str, detail: str):
        super().__init__(detail)
        self.blocker = blocker
        self.detail = detail


# Returns the current UTC time as an ISO 8601 string.
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Returns the UTC time `seconds` from now as an ISO 8601 string.
def _at(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# Returns the exponential retry delay for an attempt count, capped at BACKOFF_MAX_SECONDS.
def backoff_seconds(attempts: int) -> float:
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))))


class AutomationApi:
    """Minimal JSON client for the automation API's readiness and run-start endpoints."""

    # Stores the API base URL without a trailing slash and the request timeout.
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # Fetches the provisioning readiness of an app on a platform.
    def readiness(self, platform: str, app_external_id: str) -> dict[str, Any]:
        path = f"/config/{parse.quote(platform)}/apps/{parse.quote(app_external_id)}/provisioning"
        return self._request("GET", path)

    # Asks the automation API to start a run with the given payload.
    def start_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/runs", payload)

    # Sends a JSON request, mapping 409 and other 4xx to RunRejected and other failures to AutomationUnavailable.
    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        logger.debug(
            "assessment worker: API %s %s%s fields=%s timeout=%.0fs.",
            method,
            self.base_url,
            path,
            sorted(payload) if payload is not None else None,
            self.timeout,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                content = response.read()
                logger.debug(
                    "assessment worker: API %s %s -> %s (%d bytes).",
                    method,
                    path,
                    getattr(response, "status", None),
                    len(content or b""),
                )
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            logger.debug("assessment worker: API %s %s -> HTTP %s.", method, path, exc.code)
            if exc.code == 409:
                raise RunRejected("platform_busy", "Another run is already using the test device.") from exc
            if 400 <= exc.code < 500:
                raise RunRejected("invalid_run_request", f"The automation host rejected the run: {detail}") from exc
            raise AutomationUnavailable(f"The automation host returned {exc.code}.") from exc
        except (error.URLError, socket.timeout) as exc:
            logger.debug("assessment worker: API %s %s unreachable: %s", method, path, exc)
            raise AutomationUnavailable(f"The automation host could not be reached: {exc}") from exc
        return json.loads(content) if content else {}


# Returns the relative config path for a platform.
def _config_path(platform: str) -> str:
    return f"configs/{platform}.yaml"


# Marks a request with a terminal status, releases its lease and records extra fields.
def _finish(store: Store, request_id: str, status: str, **fields: Any) -> None:
    logger.debug(
        "assessment worker: request %s -> %s (blocker=%s); releasing lease.", request_id, status, fields.get("blocker_code")
    )
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


# Moves an assessment to status unless it is already there; refused transitions are only logged.
def _set_assessment_status(store: Store, assessment: Mapping[str, Any], status: str) -> None:
    if assessment.get("status") == status:
        logger.debug("assessment worker: assessment %s already %s.", assessment["id"], status)
        return
    logger.debug("assessment worker: assessment %s %s -> %s.", assessment["id"], assessment.get("status"), status)
    try:
        store.update_assessment(assessment["id"], {"status": status, "updated_at": _now()})
    except SupabaseRestError as exc:
        logger.debug("assessment worker: assessment %s transition refused.", assessment["id"], exc_info=True)
        # A refused transition means a newer backend state already won; the request outcome stands.
        logger.warning("assessment worker: assessment %s stayed at %s (%s).", assessment["id"], assessment.get("status"), exc)


# Puts a request back to waiting with a backoff delay and marks its assessment waiting.
def _wait(store: Store, req: Mapping[str, Any], assessment: Mapping[str, Any], blocker: str, detail: str) -> str:
    delay = backoff_seconds(int(req.get("attempts") or 1))
    logger.debug(
        "assessment worker: request %s -> waiting on %s after attempt %s; releasing lease for %.0fs.",
        req["id"],
        blocker,
        req.get("attempts"),
        delay,
    )
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


# Claims at most one request, checks readiness and starts its run, returning a short outcome name.
def process_once(
    store: Store,
    api: AutomationApi,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    risk_ids: Callable[[str], Sequence[str]] | None = None,
    reports_dir: str | None = None,
) -> str:
    logger.debug("assessment worker: poll cycle for %s; recovering expired leases.", worker_id)
    recovered = store.recover_expired_assessment_run_leases()
    if recovered:
        logger.debug("assessment worker: recovered %s expired lease(s).", recovered)

    req = store.claim_assessment_run_request(worker_id, lease_seconds)
    if req is None:
        logger.debug("assessment worker: no request to claim.")
        return "idle"
    logger.debug(
        "assessment worker: claimed request %s (assessment %s, application %s, platform %s, attempt %s, lease %ds).",
        req["id"],
        req.get("assessment_id"),
        req.get("application_id"),
        req.get("platform"),
        req.get("attempts"),
        lease_seconds,
    )

    assessment = store.get_assessment(req["assessment_id"])
    if assessment is None:
        logger.debug("assessment worker: assessment %s no longer exists; cancelling.", req["assessment_id"])
        _finish(store, req["id"], "cancelled", blocker_code="assessment_missing", last_error=None)
        return "cancelled"
    logger.debug("assessment worker: assessment %s is %s.", assessment["id"], assessment.get("status"))
    if assessment.get("status") == "completed":
        logger.debug("assessment worker: assessment %s already completed; closing request.", assessment["id"])
        _finish(store, req["id"], "completed", blocker_code=None, last_error=None)
        return "already_completed"

    application = store.get_application(req["application_id"])
    external_id = (application or {}).get("external_id")
    if not external_id:
        logger.debug(
            "assessment worker: application %s found=%s has no external id.", req["application_id"], application is not None
        )
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
        logger.debug("assessment worker: request %s exceeded %d attempts.", req["id"], MAX_ATTEMPTS)
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
        logger.debug("assessment worker: readiness check failed: %s", exc)
        return _wait(store, req, assessment, "automation_unavailable", str(exc))
    logger.debug(
        "assessment worker: readiness for %s runnable=%s blocker=%s retryable=%s.",
        external_id,
        readiness.get("runnable"),
        readiness.get("blocker_code"),
        readiness.get("retryable"),
    )

    if not readiness.get("runnable"):
        blocker = readiness.get("blocker_code") or "not_ready"
        detail = readiness.get("detail") or "This assessment cannot start yet."
        if readiness.get("retryable") is False or blocker in NON_RETRYABLE_BLOCKERS:
            logger.debug("assessment worker: blocker %s is not retryable; failing request %s.", blocker, req["id"])
            _finish(store, req["id"], "failed", blocker_code=blocker, last_error=detail)
            _set_assessment_status(store, assessment, "failed")
            return "blocked"
        return _wait(store, req, assessment, blocker, detail)

    logger.debug("assessment worker: request %s -> running.", req["id"])
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
    logger.debug(
        "assessment worker: starting %s run for %s (risks=%s, out_dir=%s).",
        payload["platform"],
        external_id,
        payload.get("risks"),
        payload["out_dir"],
    )

    try:
        started = api.start_run(payload)
    except RunRejected as exc:
        logger.debug("assessment worker: run rejected with blocker %s: %s", exc.blocker, exc.detail)
        if exc.blocker in NON_RETRYABLE_BLOCKERS:
            _finish(store, req["id"], "failed", blocker_code=exc.blocker, last_error=exc.detail)
            _set_assessment_status(store, assessment, "failed")
            return "failed"
        _set_assessment_status(store, assessment, "queued")
        return _wait(store, req, assessment, exc.blocker, exc.detail)
    except AutomationUnavailable as exc:
        logger.debug("assessment worker: run start failed: %s", exc)
        _set_assessment_status(store, assessment, "queued")
        return _wait(store, req, assessment, "automation_unavailable", str(exc))

    # The dashboard sync worker, not this request, moves the assessment to completed or failed.
    _finish(store, req["id"], "completed", blocker_code=None, last_error=None)
    logger.info(
        "assessment worker: started run %s for assessment %s.",
        started.get("run_id"),
        assessment["id"],
    )
    return "started"


# Parses worker options, builds the Supabase store and polls for requests until stopped or --once completes.
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

    load_env_file(Path(".env"))
    logging.basicConfig(level=log_level(), format="%(levelname)s %(name)s: %(message)s")
    try:
        store = SupabaseRestStore.from_env()
    except SupabaseRestError as exc:
        logger.debug("assessment worker: store construction failed.", exc_info=True)
        logger.error(
            "%s. Set it in the environment or in .env; the service-role key must never be committed "
            "or exposed to the frontend.",
            exc,
        )
        return 2

    worker_id = args.worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    api = AutomationApi(args.api_url)
    logger.info("assessment worker %s polling every %.0fs.", worker_id, args.poll_seconds)
    logger.debug(
        "assessment worker: api_url=%s reports_dir=%s lease=%ds once=%s.",
        args.api_url,
        args.reports_dir,
        args.lease_seconds,
        args.once,
    )

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
            logger.debug("assessment worker: store error during poll.", exc_info=True)
            logger.error("assessment worker: %s", exc)
            outcome = "error"
        except Exception:
            logger.exception("assessment worker: unexpected failure")
            outcome = "error"
        logger.debug("assessment worker: poll outcome %s.", outcome)
        if args.once:
            return 0 if outcome not in ("error",) else 1
        time.sleep(args.poll_seconds if outcome in ("idle", "error") else 0.5)


if __name__ == "__main__":
    raise SystemExit(main())
