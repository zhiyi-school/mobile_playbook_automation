"""Prepare claimed baselines once, confirm shutdown, and safely recover abandoned ownership."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .claims import ClaimBusy, ClaimSet, ClaimStore
from .client import (
    CorelliumClient,
    CorelliumError,
    CorelliumInstance,
    bounded_call,
    ensure_instance_state,
    polling_deadline,
    retry_read,
)
from .selection import ResolvedDevice, validate_snapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecyclePolicy:
    """Bounded independent budgets for each device lifecycle operation."""

    restore_timeout: float = 600
    power_timeout: float = 600
    poll: float = 2

    # Reject invalid budgets at construction, before claims or VM operations.
    def __post_init__(self):
        polling_deadline(self.restore_timeout, self.poll)
        polling_deadline(self.power_timeout, self.poll)


# Wait until the accepted restore is reported busy and then idle again, as Corellium's own SDK does, before startup.
def wait_restore_complete(client: CorelliumClient, device: ResolvedDevice, *, timeout: float, poll: float,
                          deadline: float | None = None) -> None:
    deadline = deadline if deadline is not None else polling_deadline(timeout, poll)
    # The restore request only returns 204 Accepted, so an idle VM is indistinguishable from one not yet restoring.
    progressed = False
    while True:
        snapshot = retry_read(client, lambda api: api.get_snapshot(device.instance.id, device.baseline_id), deadline, poll)
        status = snapshot.get("status") or {}
        if snapshot.get("id") != device.baseline_id or snapshot.get("instance") != device.instance.id:
            raise CorelliumError("Restore completion returned a different snapshot identity")
        if status.get("task") in {"error", "failed"}:
            raise CorelliumError(f"Snapshot restoration failed for {device.instance.id}")
        instance = retry_read(client, lambda api: api.get_instance(device.instance.id), deadline, poll)
        if instance.id != device.instance.id:
            raise CorelliumError("Restore completion returned a different instance identity")
        if instance.state in {"error", "deleted", "deleting"}:
            raise CorelliumError(f"Instance {instance.id} entered {instance.state} during restoration")
        if (status.get("task") not in {None, "", "none"} or instance.task_state not in {"none", ""}
                or instance.state == "restoring"):
            progressed = True
        elif progressed and status.get("created") is True and instance.state in {"off", "on", "paused"}:
            return
        time.sleep(min(poll, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            stage = "finish restoring" if progressed else "report restore progress"
            raise CorelliumError(f"Instance {device.instance.id} did not {stage} before the deadline")


class CorelliumLifecycle:
    """Shared VM lifecycle only; platform adapters own device connections and test readiness."""

    # Bind the existing authenticated client to its durable host ownership namespace.
    def __init__(self, client: CorelliumClient, store: ClaimStore, policy: LifecyclePolicy | None = None):
        if client.api_url != store.api_url:
            raise CorelliumError("Client and claim store must use the same Corellium API namespace")
        self.client = client
        self.store = store
        self.policy = policy or LifecyclePolicy()

    # Revalidate the frozen instance/snapshot identities, restore once, then start or resume.
    def prepare(self, claims: ClaimSet) -> dict[str, CorelliumInstance]:
        with claims.mutex:
            claims.prepared = self._prepare(claims)
            return dict(claims.prepared)

    # Persist each irreversible preparation transition before making its corresponding API request.
    def _prepare(self, claims: ClaimSet) -> dict[str, CorelliumInstance]:
        self._check_store(claims)
        instances = {}
        for device in claims.devices:
            instance_id = device.instance.id
            row = claims.record(instance_id)
            if row["baseline_id"] != device.baseline_id or row["role"] != device.role:
                raise CorelliumError("Preparation plan differs from persisted ownership")
            if row["phase"] == "prepared":
                deadline = polling_deadline(self.policy.power_timeout, self.policy.poll)
                current = retry_read(self.client, lambda api: api.get_instance(instance_id), deadline, self.policy.poll)
                if current.id != instance_id or current.state != "on":
                    raise CorelliumError(f"Prepared instance {instance_id} is no longer on; cleanup is required")
                instances[device.role] = current
                continue
            if row["phase"] != "claimed":
                raise CorelliumError(f"Preparation for {instance_id} is already attempted ({row['phase']}); cleanup is required")
            try:
                deadline = polling_deadline(self.policy.restore_timeout, self.policy.poll)
                current = retry_read(self.client, lambda api: api.get_instance(instance_id), deadline, self.policy.poll)
                if current.id != instance_id or current.project != device.instance.project:
                    raise CorelliumError("Claimed instance identity/project changed")
                snapshot = retry_read(self.client, lambda api: api.get_snapshot(instance_id, device.baseline_id),
                                      deadline, self.policy.poll)
                if snapshot.get("id") != device.baseline_id:
                    raise CorelliumError("Claimed baseline identity changed")
                validate_snapshot(snapshot, current, device.baseline)
                claims.store.transition(instance_id, claims.owner, "preparing")
                # Corellium's prepared cold baselines are restored while the VM is off.
                ensure_instance_state(self.client, instance_id, "off", timeout=self.policy.power_timeout, poll=self.policy.poll)
                claims.store.transition(instance_id, claims.owner, "restore_requested")
                deadline = polling_deadline(self.policy.restore_timeout, self.policy.poll)
                bounded_call(self.client, lambda api: api.restore_snapshot(instance_id, device.baseline_id), deadline)
                wait_restore_complete(self.client, device, timeout=self.policy.restore_timeout, poll=self.policy.poll,
                                      deadline=deadline)
                claims.store.transition(instance_id, claims.owner, "restored")
                current = ensure_instance_state(self.client, instance_id, "on", timeout=self.policy.power_timeout,
                                                poll=self.policy.poll)
                claims.store.transition(instance_id, claims.owner, "prepared")
                instances[device.role] = current
            except BaseException as exc:
                claims.store.transition(instance_id, claims.owner, "cleanup_pending", str(exc))
                raise
        return instances

    # Stop every claimed device independently and release only those confirmed idle and off.
    def cleanup(self, claims: ClaimSet) -> dict[str, dict]:
        with claims.mutex:
            return self._cleanup(claims)

    # Preserve pending rows across shutdown, reporting and cancellation failures.
    def _cleanup(self, claims: ClaimSet) -> dict[str, dict]:
        self._check_store(claims)
        outcomes = {}
        cancellation = None
        try:
            for device in claims.devices:
                instance_id = device.instance.id
                if instance_id not in claims.handles:
                    continue
                try:
                    claims.record(instance_id)
                    self.store.transition(instance_id, claims.owner, "cleanup_pending")
                    ensure_instance_state(self.client, instance_id, "off", timeout=self.policy.power_timeout,
                                          poll=self.policy.poll)
                    self.store.transition(instance_id, claims.owner, "off")
                    self.store.release(instance_id, claims.owner)
                    claims.handles.pop(instance_id).close()
                    outcomes[device.role] = {"instance_id": instance_id, "baseline_id": device.baseline_id, "status": "off"}
                except BaseException as exc:
                    outcomes[device.role] = {"instance_id": instance_id, "baseline_id": device.baseline_id,
                                             "status": "cleanup_pending", "error": str(exc)}
                    try:
                        self.store.transition(instance_id, claims.owner, "cleanup_pending", str(exc))
                    except Exception as reporting_error:
                        outcomes[device.role]["record_error"] = str(reporting_error)
                    if not isinstance(exc, Exception):
                        cancellation = exc
        finally:
            claims.detach()
        if cancellation:
            raise cancellation
        return outcomes

    # Claim everything before preparation and guarantee cleanup even when preparation never returns a device.
    @contextmanager
    def session(self, run_id: str, devices: tuple[ResolvedDevice, ...]):
        claims = self.store.claim_all(run_id, devices)
        try:
            self.prepare(claims)
            yield claims
        finally:
            self.cleanup(claims)

    # Recover abandoned rows independently while holding the same locks required by claim acquisition.
    def recover(self) -> list[dict]:
        outcomes = []
        for original in self.store.pending():
            instance_id = original["instance_id"]
            # A lock on this host proves nothing about an owner on another host sharing the store.
            if original["host_id"] != self.store.host_id:
                outcomes.append({"instance_id": instance_id, "status": "foreign_host"})
                continue
            try:
                handle = self.store.lock(instance_id)
            except ClaimBusy:
                outcomes.append({"instance_id": instance_id, "status": "active_owner"})
                continue
            try:
                # Recheck after locking: a snapshot of pending rows is never authority to stop a new owner.
                current = next((row for row in self.store.pending() if row["instance_id"] == instance_id), None)
                if current is None or current["owner"] != original["owner"]:
                    continue
                try:
                    self.store.transition(instance_id, current["owner"], "recovering")
                    ensure_instance_state(self.client, instance_id, "off", timeout=self.policy.power_timeout,
                                          poll=self.policy.poll)
                    self.store.transition(instance_id, current["owner"], "off")
                    self.store.release(instance_id, current["owner"])
                    outcomes.append({"instance_id": instance_id, "status": "off"})
                except Exception as exc:
                    outcome = {"instance_id": instance_id, "status": "cleanup_pending", "error": str(exc)}
                    try:
                        self.store.transition(instance_id, current["owner"], "cleanup_pending", str(exc))
                    except Exception as reporting_error:
                        outcome["record_error"] = str(reporting_error)
                    outcomes.append(outcome)
            finally:
                handle.close()
        return outcomes

    # Reject accidentally mixing ownership from different API namespaces or host stores.
    def _check_store(self, claims: ClaimSet) -> None:
        if claims.store.path != self.store.path:
            raise CorelliumError("Claims belong to a different Corellium ownership store")


# Shut down and release claims that ended processes on this host left behind, logging each outcome; without
# Corellium credentials or pending claims nothing is contacted.
def recover_abandoned(policy: LifecyclePolicy | None = None) -> list[dict]:
    try:
        client = CorelliumClient.from_env()
    except CorelliumError:
        logger.debug("corellium recovery: no Corellium credentials; nothing to recover.")
        return []
    store = ClaimStore(client.api_url)
    if not store.pending():
        return []
    outcomes = CorelliumLifecycle(client, store, policy).recover()
    for outcome in outcomes:
        if outcome["status"] == "off":
            logger.info("Recovered Corellium instance %s: confirmed off and released.", outcome["instance_id"])
        elif outcome["status"] == "cleanup_pending":
            logger.warning("Corellium instance %s still awaits cleanup: %s", outcome["instance_id"], outcome.get("error"))
        else:
            logger.info("Left Corellium instance %s alone (%s).", outcome["instance_id"], outcome["status"])
    return outcomes
