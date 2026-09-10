from __future__ import annotations

from typing import Any, Mapping

import pytest

from mobile_playbook.storage import reports_root

from mobile_playbook.assessment_worker import (
    AutomationUnavailable,
    RunRejected,
    backoff_seconds,
    process_once,
)
from mobile_playbook.dashboard_sync import SupabaseRestError

ASSESSMENT = "assessment-1"
APPLICATION = "application-1"
REQUEST = "request-1"

ACTIVE = ("queued", "waiting", "claimed", "running")


class FakeStore:
    """Enforces the rules migration 0023 puts in the database."""

    def __init__(self, assessment_status: str = "queued", app_external_id: str | None = "example_app"):
        self.assessments = {
            ASSESSMENT: {"id": ASSESSMENT, "application_id": APPLICATION, "status": assessment_status}
        }
        self.applications = {
            APPLICATION: {"id": APPLICATION, "external_id": app_external_id, "platform": "ios"}
        }
        self.requests: dict[str, dict[str, Any]] = {
            REQUEST: {
                "id": REQUEST,
                "assessment_id": ASSESSMENT,
                "application_id": APPLICATION,
                "platform": "ios",
                "status": "queued",
                "attempts": 0,
                "next_attempt_at": None,
                "lease_expires_at": None,
                "worker_id": None,
                "blocker_code": None,
                "last_error": None,
            }
        }
        self.recoveries = 0
        self.refuse_assessment_transitions: set[tuple[str, str]] = set()

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[REQUEST]

    @property
    def assessment(self) -> dict[str, Any]:
        return self.assessments[ASSESSMENT]

    def recover_expired_assessment_run_leases(self) -> int:
        self.recoveries += 1
        recovered = 0
        for row in self.requests.values():
            if row["status"] in ("claimed", "running") and row.get("lease_expires_at") == "expired":
                row.update(
                    status="queued",
                    lease_expires_at=None,
                    worker_id=None,
                    blocker_code="lease_expired",
                )
                recovered += 1
        return recovered

    def claim_assessment_run_request(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        for row in self.requests.values():
            if row["status"] not in ("queued", "waiting"):
                continue
            row.update(
                status="claimed",
                attempts=row["attempts"] + 1,
                worker_id=worker_id,
                lease_expires_at=f"+{lease_seconds}",
            )
            return dict(row)
        return None

    def update_assessment_run_request(self, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        row = self.requests[request_id]
        if fields.get("status") in ACTIVE and row["status"] not in ACTIVE:
            others = [
                other
                for other in self.requests.values()
                if other is not row and other["status"] in ACTIVE
            ]
            if others:
                raise SupabaseRestError("duplicate key value violates unique constraint")
        row.update(fields)
        return dict(row)

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        row = self.assessments.get(assessment_id)
        return dict(row) if row else None

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        row = self.applications.get(application_id)
        return dict(row) if row else None

    def update_assessment(self, assessment_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        row = self.assessments[assessment_id]
        target = fields.get("status")
        if target and (row["status"], target) in self.refuse_assessment_transitions:
            raise SupabaseRestError(f"an assessment cannot move from {row['status']} to {target}")
        row.update(fields)
        return dict(row)


def readiness(**overrides: Any) -> dict[str, Any]:
    base = {
        "configuration_ready": True,
        "device_required": True,
        "device_ready": True,
        "platform_available": True,
        "runnable": True,
        "blocker_code": None,
        "retryable": True,
        "detail": None,
    }
    base.update(overrides)
    return base


class FakeApi:
    def __init__(self, ready: dict[str, Any] | None = None, start: Any = None):
        self.ready = ready if ready is not None else readiness()
        self.start = start
        self.readiness_calls = 0
        self.started: list[dict[str, Any]] = []

    def readiness(self, platform: str, app_external_id: str) -> dict[str, Any]:
        self.readiness_calls += 1
        if isinstance(self.ready, Exception):
            raise self.ready
        return self.ready

    def start_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(self.start, Exception):
            raise self.start
        self.started.append(dict(payload))
        return {"run_id": "2026-01-01_00-00-00", "status": "running"}


def run(store: FakeStore, api: FakeApi, **kwargs: Any) -> str:
    return process_once(store, api, worker_id="worker-a", **kwargs)


def test_a_ready_assessment_is_started_and_the_request_is_finished():
    store, api = FakeStore(), FakeApi()

    assert run(store, api) == "started"

    assert store.assessment["status"] == "running"
    assert store.request["status"] == "completed"
    assert store.request["blocker_code"] is None
    assert store.request["started_at"]
    assert api.started == [
        {
            "platform": "ios",
            "config_path": "configs/ios.yaml",
            "apps": "example_app",
            "out_dir": str(reports_root()),
        }
    ]


def test_an_idle_queue_does_nothing():
    store = FakeStore()
    store.request["status"] = "completed"
    api = FakeApi()

    assert run(store, api) == "idle"
    assert api.readiness_calls == 0


def test_no_device_leaves_the_assessment_waiting_and_retryable():
    store = FakeStore()
    api = FakeApi(readiness(runnable=False, device_ready=False, blocker_code="no_device", detail="Waiting for a device."))

    assert run(store, api) == "waiting"

    assert store.assessment["status"] == "waiting"
    assert store.request["status"] == "waiting"
    assert store.request["blocker_code"] == "no_device"
    assert store.request["next_attempt_at"]
    assert api.started == []


def test_a_waiting_request_is_retried_and_backs_off_further_each_time():
    store = FakeStore()
    api = FakeApi(readiness(runnable=False, blocker_code="no_device", detail="Waiting for a device."))

    run(store, api)
    first = store.request["next_attempt_at"]
    run(store, api)

    assert store.request["attempts"] == 2
    assert store.request["next_attempt_at"] != first
    assert backoff_seconds(2) > backoff_seconds(1)


def test_backoff_is_bounded():
    assert backoff_seconds(1) == 30
    assert backoff_seconds(30) == 900


def test_a_device_that_arrives_later_lets_the_same_assessment_run():
    store = FakeStore()
    blocked = FakeApi(readiness(runnable=False, device_ready=False, blocker_code="no_device", detail="No device."))
    assert run(store, blocked) == "waiting"

    connected = FakeApi()
    assert run(store, connected) == "started"

    assert store.assessment["id"] == ASSESSMENT
    assert len(store.requests) == 1
    assert store.request["status"] == "completed"
    assert connected.started


def test_a_configuration_problem_is_not_retried():
    store = FakeStore()
    api = FakeApi(
        readiness(
            runnable=False,
            configuration_ready=False,
            blocker_code="configuration_incomplete",
            retryable=False,
            detail="Configuration needs attention.",
        )
    )

    assert run(store, api) == "blocked"

    assert store.request["status"] == "failed"
    assert store.request["blocker_code"] == "configuration_incomplete"
    assert store.assessment["status"] == "failed"


def test_a_platform_already_running_is_retried_rather_than_failed():
    store = FakeStore()
    api = FakeApi(start=RunRejected("platform_busy", "Another run is using the device."))

    assert run(store, api) == "waiting"

    assert store.request["status"] == "waiting"
    assert store.request["blocker_code"] == "platform_busy"
    assert store.assessment["status"] == "waiting"


def test_a_rejected_run_request_is_not_retried():
    store = FakeStore()
    api = FakeApi(start=RunRejected("invalid_run_request", "The host rejected the run."))

    assert run(store, api) == "failed"

    assert store.request["status"] == "failed"
    assert store.assessment["status"] == "failed"


def test_an_unreachable_automation_host_is_retried():
    store = FakeStore()
    api = FakeApi(ready=AutomationUnavailable("host down"))

    assert run(store, api) == "waiting"

    assert store.request["blocker_code"] == "automation_unavailable"
    assert store.assessment["status"] == "waiting"


def test_a_completed_assessment_is_never_restarted():
    store = FakeStore(assessment_status="completed")
    api = FakeApi()

    assert run(store, api) == "already_completed"

    assert api.readiness_calls == 0
    assert api.started == []
    assert store.request["status"] == "completed"


def test_a_deleted_assessment_cancels_its_request():
    store = FakeStore()
    store.assessments.clear()
    api = FakeApi()

    assert run(store, api) == "cancelled"

    assert store.request["status"] == "cancelled"
    assert api.started == []


def test_an_unregistered_application_fails_without_retrying():
    store = FakeStore(app_external_id=None)
    api = FakeApi()

    assert run(store, api) == "failed"

    assert store.request["status"] == "failed"
    assert store.request["blocker_code"] == "configuration_incomplete"
    assert api.readiness_calls == 0


def test_every_pass_recovers_expired_leases_first():
    store = FakeStore()
    store.request.update(status="running", lease_expires_at="expired", worker_id="worker-gone")
    api = FakeApi()

    assert run(store, api) == "started"

    assert store.recoveries == 1
    assert store.request["worker_id"] is None
    assert api.started


def test_a_second_worker_finds_nothing_to_claim_while_one_holds_the_request():
    store = FakeStore()
    held = store.claim_assessment_run_request("worker-a", 900)
    assert held is not None

    assert process_once(store, FakeApi(), worker_id="worker-b") == "idle"


def test_retrying_forever_eventually_stops():
    store = FakeStore()
    store.request["attempts"] = 40
    api = FakeApi()

    assert run(store, api) == "exhausted"

    assert store.request["status"] == "failed"
    assert api.readiness_calls == 0


def test_a_refused_assessment_transition_does_not_lose_the_run():
    store = FakeStore()
    store.refuse_assessment_transitions.add(("queued", "running"))
    api = FakeApi()

    assert run(store, api) == "started"

    assert store.assessment["status"] == "queued"
    assert store.request["status"] == "completed"
    assert api.started


@pytest.mark.parametrize("blocker", ["no_device", "device_unreachable", "app_not_installed", "app_build_missing"])
def test_every_temporary_blocker_keeps_the_assessment_recoverable(blocker: str):
    store = FakeStore()
    api = FakeApi(readiness(runnable=False, blocker_code=blocker, detail="Temporary."))

    assert run(store, api) == "waiting"

    assert store.assessment["status"] == "waiting"
    assert store.request["status"] == "waiting"


def test_the_selected_risks_are_passed_through_when_the_caller_names_them():
    store, api = FakeStore(), FakeApi()

    run(store, api, risk_ids=lambda _platform: ["example-feature-01-risk-01"])

    assert api.started[0]["risks"] == "example-feature-01-risk-01"
