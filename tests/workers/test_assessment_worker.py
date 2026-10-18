from __future__ import annotations

from typing import Any, Mapping

import pytest

from mobile_playbook.common.storage_paths import reports_root

from mobile_playbook.workers.assessment_worker import (
    AutomationUnavailable,
    RunRejected,
    backoff_seconds,
    process_once,
)
from mobile_playbook.dashboard_sync.contracts import SupabaseRestError

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
    def __init__(self, ready: Any = None, start: Any = None, install: Any = None):
        self.ready = ready if ready is not None else readiness()
        self.start = start
        self.install = install if install is not None else {"status": "installed"}
        self.readiness_calls = 0
        self.started: list[dict[str, Any]] = []
        self.installs: list[tuple[str, str]] = []

    def readiness(self, platform: str, app_external_id: str) -> dict[str, Any]:
        self.readiness_calls += 1
        ready = self.ready.pop(0) if isinstance(self.ready, list) else self.ready
        if isinstance(ready, Exception):
            raise ready
        return ready

    def install_app(self, platform: str, app_external_id: str) -> dict[str, Any]:
        self.installs.append((platform, app_external_id))
        if isinstance(self.install, Exception):
            raise self.install
        return self.install

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
    assert store.request["run_id"] == "2026-01-01_00-00-00"
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


def android_store() -> FakeStore:
    store = FakeStore()
    store.applications[APPLICATION]["platform"] = "android"
    store.request["platform"] = "android"
    return store


NOT_INSTALLED = readiness(runnable=False, blocker_code="app_not_installed", detail="Not installed yet.")


def test_a_missing_android_app_is_installed_then_its_run_starts():
    store = android_store()
    api = FakeApi([NOT_INSTALLED, readiness()])

    assert run(store, api) == "started"

    assert api.installs == [("android", "example_app")]
    assert api.readiness_calls == 2
    assert api.started[0]["apps"] == "example_app"
    assert store.request["status"] == "completed"


def test_a_paid_android_app_fails_the_request_for_a_person_to_handle():
    store = android_store()
    paid = {"status": "failed", "blocker_code": "app_paid", "retryable": False, "detail": "This app is paid."}
    api = FakeApi([NOT_INSTALLED], install=paid)

    assert run(store, api) == "blocked"

    assert store.request["status"] == "failed"
    assert store.request["blocker_code"] == "app_paid"
    assert store.request["last_error"] == "This app is paid."
    assert store.assessment["status"] == "failed"
    assert api.started == []


def test_a_slow_android_install_waits_and_retries():
    store = android_store()
    slow = {"status": "failed", "blocker_code": "app_install_timeout", "retryable": True, "detail": "Too slow."}
    api = FakeApi([NOT_INSTALLED], install=slow)

    assert run(store, api) == "waiting"

    assert store.request["status"] == "waiting"
    assert store.request["blocker_code"] == "app_install_timeout"
    assert store.assessment["status"] == "waiting"


@pytest.mark.parametrize(
    ("error", "blocker"),
    [
        (RunRejected("platform_busy", "Another run is already using the test device."), "platform_busy"),
        (AutomationUnavailable("The automation host could not be reached."), "automation_unavailable"),
    ],
)
def test_an_install_that_cannot_reach_the_device_waits(error, blocker):
    store = android_store()
    api = FakeApi([NOT_INSTALLED], install=error)

    assert run(store, api) == "waiting"

    assert store.request["blocker_code"] == blocker


def test_an_ios_app_not_installed_is_never_sent_to_the_play_store():
    store = FakeStore()
    api = FakeApi(NOT_INSTALLED)

    assert run(store, api) == "waiting"

    assert api.installs == []


def test_the_selected_risks_are_passed_through_when_the_caller_names_them():
    store, api = FakeStore(), FakeApi()

    run(store, api, risk_ids=lambda _platform: ["example-feature-01-risk-01"])

    assert api.started[0]["risks"] == "example-feature-01-risk-01"


def test_the_worker_loads_the_repository_env_file_whatever_the_working_directory(tmp_path, monkeypatch):
    from mobile_playbook.common.storage_paths import REPOSITORY_ROOT
    from mobile_playbook.workers import assessment_worker

    seen = []

    def no_credentials():
        raise SupabaseRestError("SUPABASE_URL is not set")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(assessment_worker, "load_env_file", lambda path: seen.append(path))
    monkeypatch.setattr(assessment_worker.SupabaseRestStore, "from_env", staticmethod(no_credentials))

    assert assessment_worker.main([]) == 2
    assert seen == [REPOSITORY_ROOT / ".env"]


def test_the_worker_targets_the_api_default_port_when_no_url_is_given(monkeypatch):
    from mobile_playbook.workers import assessment_worker

    urls = []
    monkeypatch.delenv("AUTOMATION_API_URL", raising=False)
    monkeypatch.setattr(assessment_worker, "load_env_file", lambda path: None)
    monkeypatch.setattr(assessment_worker.SupabaseRestStore, "from_env", staticmethod(lambda: object()))
    monkeypatch.setattr(assessment_worker, "AutomationApi", lambda base_url: urls.append(base_url))
    monkeypatch.setattr(assessment_worker, "process_once", lambda *args, **kwargs: "idle")

    assert assessment_worker.main(["--once"]) == 0
    assert urls == ["http://127.0.0.1:8080"]


def test_the_requested_build_version_is_sent_with_the_run():
    store, api = FakeStore(), FakeApi()
    store.request["build_version"] = "3.9.9"

    assert run(store, api) == "started"

    assert api.started[0]["build_version"] == "3.9.9"


def test_a_request_without_a_version_lets_the_host_choose():
    store, api = FakeStore(), FakeApi()

    run(store, api)

    assert "build_version" not in api.started[0]


def test_a_missing_build_waits_for_it_rather_than_failing():
    store = FakeStore()
    api = FakeApi(start=RunRejected("app_build_missing", "No build of Example with version 9.9 is in intake."))

    assert run(store, api) == "waiting"

    assert store.request["status"] == "waiting"
    assert store.request["blocker_code"] == "app_build_missing"
    assert "version 9.9" in store.request["last_error"]


def _http_error(code: int, body: str):
    import io
    from urllib import error

    return error.HTTPError("http://automation/runs", code, "refused", {}, io.BytesIO(body.encode()))


def test_the_hosts_blocker_is_read_from_a_refused_run(monkeypatch):
    import json

    from mobile_playbook.workers import assessment_worker

    detail = {"detail": {"blocker_code": "app_build_missing", "message": "No build " + "x" * 600}}

    def refuse(*args, **kwargs):
        raise _http_error(422, json.dumps(detail))

    monkeypatch.setattr(assessment_worker.request, "urlopen", refuse)

    with pytest.raises(RunRejected) as exc:
        assessment_worker.AutomationApi("http://automation").start_run({"platform": "ios"})

    assert exc.value.blocker == "app_build_missing"
    assert exc.value.detail.startswith("No build ")


def test_a_refusal_without_a_blocker_is_still_an_invalid_request(monkeypatch):
    from mobile_playbook.workers import assessment_worker

    def refuse(*args, **kwargs):
        raise _http_error(422, '{"detail": "apps did not match"}')

    monkeypatch.setattr(assessment_worker.request, "urlopen", refuse)

    with pytest.raises(RunRejected) as exc:
        assessment_worker.AutomationApi("http://automation").start_run({"platform": "ios"})

    assert exc.value.blocker == "invalid_run_request"


def test_an_install_request_waits_long_enough_for_a_store_download(monkeypatch):
    import io
    import json

    from mobile_playbook.workers import assessment_worker

    seen: list[tuple[str, str, float]] = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def respond(req, timeout):
        seen.append((req.get_method(), req.full_url, timeout))
        return Response(json.dumps({"status": "installed"}).encode())

    monkeypatch.setattr(assessment_worker.request, "urlopen", respond)
    api = assessment_worker.AutomationApi("http://automation")

    assert api.install_app("android", "example_app") == {"status": "installed"}
    api.readiness("android", "example_app")

    assert seen[0] == (
        "POST",
        "http://automation/config/android/apps/example_app/install",
        assessment_worker.INSTALL_REQUEST_TIMEOUT_SECONDS,
    )
    assert seen[1][2] == api.timeout


def baseline_readiness(tmp_path, monkeypatch, *, apk: bool, physical_roles=()) -> dict:
    from mobile_playbook.api.services import provisioning
    from tests.platforms.android.baseline_fakes import PLAIN, BaselineBench

    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN,), physical_roles=physical_roles)
    monkeypatch.setattr(provisioning, "get_android_risk", bench.risks.get)
    if not apk:
        bench.apk.unlink()
    return provisioning.describe("android", "one")


def test_a_corellium_baseline_app_without_its_apk_waits_and_is_never_installed_by_the_worker(tmp_path, monkeypatch):
    store = android_store()
    api = FakeApi(baseline_readiness(tmp_path, monkeypatch, apk=False))

    assert run(store, api) == "waiting"

    assert store.request["blocker_code"] == "app_build_missing"
    assert api.installs == [] and api.started == []


def test_a_ready_corellium_baseline_app_starts_its_run_without_a_pre_run_install(tmp_path, monkeypatch):
    store = android_store()
    api = FakeApi(baseline_readiness(tmp_path, monkeypatch, apk=True))

    assert run(store, api) == "started"

    assert api.installs == []
    assert api.started[0]["apps"] == "example_app"


def test_an_app_tested_on_the_phone_beside_a_vm_starts_its_run_without_a_pre_run_install(tmp_path, monkeypatch):
    store = android_store()
    api = FakeApi(baseline_readiness(tmp_path, monkeypatch, apk=True, physical_roles=("non_rooted",)))

    assert run(store, api) == "started"

    assert api.installs == []


def test_the_worker_loop_shuts_down_and_releases_vms_an_ended_run_left_pending(tmp_path, monkeypatch):
    from mobile_playbook.workers import assessment_worker
    from tests.platforms.android.baseline_fakes import PLAIN, STOCK_VM, BaselineBench
    from tests.platforms.corellium.helpers import device

    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN,))
    bench.store.claim_all("ended-run", (device(STOCK_VM, "non_rooted"),)).detach()
    bench.turn_on(STOCK_VM)
    monkeypatch.setattr(assessment_worker, "load_env_file", lambda path: None)
    monkeypatch.setattr(assessment_worker.SupabaseRestStore, "from_env", staticmethod(lambda: object()))
    monkeypatch.setattr(assessment_worker, "process_once", lambda *args, **kwargs: "idle")
    monkeypatch.setattr(assessment_worker, "CORELLIUM_RECOVERY_POLICY", assessment_worker.LifecyclePolicy(poll=0.001))

    assert assessment_worker.main(["--once"]) == 0

    assert bench.instance_state(STOCK_VM) == "off"
    assert bench.store.pending() == []
    assert bench.actions(STOCK_VM, "restore", "start") == []


def test_a_failed_corellium_recovery_never_stops_the_worker(monkeypatch):
    from mobile_playbook.workers import assessment_worker

    def broken(policy):
        raise OSError("claim store unreadable")

    monkeypatch.setattr(assessment_worker, "recover_abandoned", broken)

    assert assessment_worker.recover_corellium_devices() == []
