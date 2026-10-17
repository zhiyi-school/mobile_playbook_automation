from dataclasses import replace

import pytest

from mobile_playbook.platforms.corellium.claims import ClaimBusy, ClaimStore
from mobile_playbook.platforms.corellium.client import CorelliumError, ensure_instance_state, wait_instance_state
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle, LifecyclePolicy, wait_restore_complete
from tests.platforms.corellium.helpers import FakeAPI, device


@pytest.fixture
def environment(tmp_path):
    stock, root = device(), device("root", "rooted", "on")
    api = FakeAPI(stock, root)
    store = ClaimStore(api.api_url, tmp_path)
    lifecycle = CorelliumLifecycle(api, store, LifecyclePolicy(0.1, 0.03, 0.001))
    return (stock, root), api, store, lifecycle


def mutations(api):
    return [(action, instance) for action, instance in api.data["calls"] if action in {"stop", "start", "restore", "unpause"}]


def test_both_claims_are_durable_before_any_mutation_and_restored_exactly_once(environment):
    devices, api, store, lifecycle = environment
    def assert_claimed():
        assert {row["instance_id"] for row in ClaimStore(api.api_url, store.root.parent).pending()} == {"stock", "root"}
    api.data["assert_mutation"] = assert_claimed
    claims = store.claim_all("mixed", devices)
    try:
        prepared = lifecycle.prepare(claims)
        assert prepared["non_rooted"].state == prepared["rooted"].state == "on"
        assert lifecycle.prepare(claims)["non_rooted"].state == "on"
        assert mutations(api) == [("restore", "stock"), ("start", "stock"), ("stop", "root"), ("restore", "root"), ("start", "root")]
    finally:
        api.data["assert_mutation"] = None
        outcome = lifecycle.cleanup(claims)
    assert all(item["status"] == "off" for item in outcome.values())
    assert store.pending() == []
    assert {row["baseline_id"] for row in store.history("mixed")} == {"baseline-stock", "baseline-root"}
    assert all(api.data["instances"][item.instance.id].state == "off" for item in devices)
    with pytest.raises(ClaimBusy, match="already claimed"):
        store.claim_all("mixed", devices)


def test_session_exposes_powered_on_instance_records(environment):
    devices, api, store, lifecycle = environment
    api.data["instances"]["stock"] = replace(devices[0].instance, adb_port=None)
    original = api.start_instance
    def start(instance_id):
        original(instance_id)
        api.data["instances"][instance_id] = replace(api.data["instances"][instance_id], adb_port=5001)
    api.start_instance = start
    with lifecycle.session("records", devices[:1]) as claims:
        assert claims.devices[0].instance.adb_port is None
        assert (claims.prepared["non_rooted"].state, claims.prepared["non_rooted"].adb_port) == ("on", 5001)


@pytest.mark.parametrize("role", ["non_rooted", "rooted"])
def test_single_role_leaves_other_instance_untouched(environment, role):
    devices, api, store, lifecycle = environment
    selected = tuple(item for item in devices if item.role == role)
    with lifecycle.session("one", selected):
        assert len(store.pending()) == 1
    assert {instance for _, instance in api.data["calls"]} == {selected[0].instance.id}


def test_restore_completion_waits_before_start(environment):
    devices, api, store, lifecycle = environment
    # First snapshot read validates; later reads simulate an accepted asynchronous restore.
    api.data["restore_busy"]["stock"] = ["none", "restoring", "restoring", "none"]
    with lifecycle.session("wait", devices[:1]):
        actions = api.data["calls"]
        restore = actions.index(("restore", "stock"))
        start = actions.index(("start", "stock"))
        assert actions[restore:start].count(("snapshot", "stock")) == 3


def test_restore_that_reports_busy_late_is_not_mistaken_for_complete(environment):
    devices, api, store, lifecycle = environment
    api.data["restore_delay"]["stock"] = 2
    def no_pending_restore_reads():
        assert not api.data["restore_reads"].get("stock"), "VM started before the restore was observed finishing"
    api.data["assert_mutation"] = no_pending_restore_reads
    with lifecycle.session("late", devices[:1]):
        assert ("start", "stock") in mutations(api)


def test_restore_without_reported_progress_fails_without_starting(environment, monkeypatch):
    devices, api, store, lifecycle = environment
    original = api.restore_snapshot
    def silent_restore(instance_id, snapshot_id):
        original(instance_id, snapshot_id)
        api.data["restore_reads"][instance_id] = []
    monkeypatch.setattr(api, "restore_snapshot", silent_restore)
    with pytest.raises(CorelliumError, match="report restore progress"):
        with lifecycle.session("silent", devices[:1]):
            pytest.fail("Preparation must not return")
    assert ("start", "stock") not in mutations(api)
    assert store.history("silent")[-1]["phase"] == "released"


def test_live_restore_can_resume_paused_instance(environment, monkeypatch):
    devices, api, store, lifecycle = environment
    original = api.restore_snapshot
    def restore(instance_id, snapshot_id):
        original(instance_id, snapshot_id)
        api.data["instances"][instance_id] = replace(api.data["instances"][instance_id], state="paused")
    monkeypatch.setattr(api, "restore_snapshot", restore)
    with lifecycle.session("paused", devices[:1]):
        assert ("unpause", "stock") in mutations(api)


def test_ambiguous_restore_failure_never_replayed_and_context_cleans_before_device_return(environment):
    devices, api, store, lifecycle = environment
    api.data["failures"][("restore", "stock")] = [CorelliumError("lost response", transient=True)]
    with pytest.raises(CorelliumError, match="lost response"):
        with lifecycle.session("lost", devices):
            pytest.fail("Preparation must not return")
    assert mutations(api).count(("restore", "stock")) == 1
    assert ("restore", "root") not in mutations(api)
    assert all(item.state == "off" for item in api.data["instances"].values())
    assert store.pending() == []


def test_failed_prepare_is_not_repeated_and_ownership_survives_detachment(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("failed", devices[:1])
    api.data["failures"][("restore", "stock")] = [CorelliumError("restore refused")]
    try:
        with pytest.raises(CorelliumError, match="restore refused"):
            lifecycle.prepare(claims)
        with pytest.raises(CorelliumError, match="already attempted"):
            lifecycle.prepare(claims)
    finally:
        claims.detach()
    assert store.pending()[0]["phase"] == "cleanup_pending"
    with pytest.raises(ClaimBusy, match="pending"):
        store.claim_all("next", devices[:1])
    assert lifecycle.recover() == [{"instance_id": "stock", "status": "off"}]


def test_shutdown_timeout_retains_only_unresolved_ownership_and_recovery_retries(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("timeout", devices)
    lifecycle.prepare(claims)
    api.data["stuck"].add("stock")
    outcomes = lifecycle.cleanup(claims)
    assert outcomes["non_rooted"]["status"] == "cleanup_pending"
    assert outcomes["rooted"]["status"] == "off"
    assert [row["instance_id"] for row in store.pending()] == ["stock"]
    assert lifecycle.recover()[0]["status"] == "cleanup_pending"
    api.data["stuck"].clear()
    assert lifecycle.recover() == [{"instance_id": "stock", "status": "off"}]
    assert lifecycle.recover() == []


def test_api_outage_preserves_claim_then_connectivity_allows_recovery(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("outage", devices)
    lifecycle.prepare(claims)
    api.data["failures"][("get", "stock")] = [CorelliumError("unreachable", transient=True)] * 500
    assert lifecycle.cleanup(claims)["non_rooted"]["status"] == "cleanup_pending"
    assert store.pending()[0]["instance_id"] == "stock"
    api.data["failures"].clear()
    assert lifecycle.recover()[0]["status"] == "off"


def test_recovery_ignores_active_owner_even_when_timestamp_is_expired(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("live", devices)
    with store.connection() as db:
        db.execute("UPDATE claims SET claimed_at=0")
    try:
        assert {item["status"] for item in lifecycle.recover()} == {"active_owner"}
        assert api.data["calls"] == []
    finally:
        lifecycle.cleanup(claims)


def test_recovery_leaves_claims_from_another_host_untouched(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("elsewhere", devices[:1])
    claims.detach()
    with store.connection() as db:
        db.execute("UPDATE claims SET host_id='other-machine'")
    assert lifecycle.recover() == [{"instance_id": "stock", "status": "foreign_host"}]
    assert api.data["calls"] == []
    assert store.pending()[0]["phase"] == "claimed"


def test_recovery_rechecks_owner_after_acquiring_lock(environment, monkeypatch):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("old", devices[:1])
    claims.detach()
    lock = store.lock
    def race(instance_id):
        handle = lock(instance_id)
        with store.connection() as db:
            db.execute("UPDATE claims SET owner='new-owner'")
        return handle
    monkeypatch.setattr(store, "lock", race)
    assert lifecycle.recover() == []
    assert api.data["calls"] == []
    assert store.pending()[0]["owner"] == "new-owner"


@pytest.mark.parametrize("failure", [RuntimeError("test failed"), KeyboardInterrupt(), SystemExit(1)])
def test_context_cleanup_on_test_failure_or_supported_cancellation(environment, failure):
    devices, api, store, lifecycle = environment
    with pytest.raises(type(failure)):
        with lifecycle.session("cancel", devices):
            raise failure
    assert store.pending() == []
    assert all(item.state == "off" for item in api.data["instances"].values())


def test_cancellation_during_cleanup_does_not_suppress_second_shutdown(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("cancel-cleanup", devices)
    lifecycle.prepare(claims)
    api.data["failures"][("stop", "stock")] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        lifecycle.cleanup(claims)
    assert api.data["instances"]["root"].state == "off"
    assert [row["instance_id"] for row in store.pending()] == ["stock"]


def test_snapshot_or_instance_changes_fail_before_restore(environment):
    devices, api, store, lifecycle = environment
    api.data["instances"]["stock"] = replace(devices[0].instance, os="17.0.0")
    with pytest.raises(CorelliumError, match="incompatible"):
        with lifecycle.session("changed", devices[:1]):
            pytest.fail("Should not prepare")
    assert ("restore", "stock") not in mutations(api)
    assert store.pending() == []


def test_static_session_has_no_claim_records_or_vm_calls(environment):
    _, api, store, lifecycle = environment
    with lifecycle.session("static", ()):
        pass
    assert store.pending() == store.history("static") == api.data["calls"] == []


def test_transient_get_and_stop_errors_retry_but_permission_errors_fail_immediately(environment):
    devices, api, _, _ = environment
    api.data["failures"][("get", "root")] = [CorelliumError("503", transient=True)]
    api.data["failures"][("stop", "root")] = [CorelliumError("503", transient=True)]
    assert ensure_instance_state(api, "root", "off", timeout=0.1, poll=0.001).state == "off"
    assert mutations(api).count(("stop", "root")) == 2
    api.data["failures"][("get", "stock")] = [CorelliumError("403")]
    with pytest.raises(CorelliumError, match="403"):
        ensure_instance_state(api, "stock", "on", timeout=1, poll=0.001)
    assert ("start", "stock") not in mutations(api)


def test_wait_checks_task_state_and_snapshot_timeout(environment):
    devices, api, _, _ = environment
    api.data["instances"]["stock"] = replace(devices[0].instance, state="off", task_state="restoring")
    with pytest.raises(CorelliumError):
        wait_instance_state(api, "stock", {"off"}, timeout=0.01, poll=0.001)
    with pytest.raises(CorelliumError):
        wait_restore_complete(api, devices[0], timeout=0.01, poll=0.001)


def test_wrong_instance_response_never_confirms_shutdown(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("wrong-response", devices[:1])
    api.data["instances"]["stock"] = replace(devices[0].instance, id="foreign", state="off")
    assert lifecycle.cleanup(claims)["non_rooted"]["status"] == "cleanup_pending"
    assert store.pending()[0]["instance_id"] == "stock"


def test_changed_plan_is_rejected_before_restoration(environment):
    devices, api, store, lifecycle = environment
    claims = store.claim_all("changed-plan", devices[:1])
    claims.devices = (replace(devices[0], baseline_id="different-baseline"),)
    try:
        with pytest.raises(CorelliumError, match="differs from persisted"):
            lifecycle.prepare(claims)
        assert mutations(api) == []
    finally:
        lifecycle.cleanup(claims)


def test_concurrent_preparation_submits_only_one_restore(environment):
    from concurrent.futures import ThreadPoolExecutor
    devices, api, store, lifecycle = environment
    claims = store.claim_all("concurrent-prepare", devices[:1])
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: lifecycle.prepare(claims), range(2)))
        assert all(result["non_rooted"].state == "on" for result in results)
        assert mutations(api).count(("restore", "stock")) == 1
    finally:
        lifecycle.cleanup(claims)


@pytest.mark.parametrize("timeout,poll", [(0, 1), (-1, 1), (float("inf"), 1), (1, -1), (1, 0), (1, float("nan")), (True, 1)])
def test_bounded_policy_validation(timeout, poll):
    with pytest.raises(ValueError):
        LifecyclePolicy(timeout, 1, poll)
