"""Resolve automation roles to immutable instance and compatible baseline identities before claiming devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .client import CorelliumClient, CorelliumError, CorelliumInstance, polling_deadline, retry_read

ROLES = {"non_rooted": False, "rooted": True}
ELIGIBLE_STATES = {"on": 0, "paused": 1, "off": 2}


@dataclass(frozen=True)
class RoleSelection:
    """A role's project and optional exact instance/name and OS selectors."""

    project: str
    instance: str = ""
    os_version: str = ""


@dataclass(frozen=True)
class AutomationBaseline:
    """Explicit operator-approved automation eligibility and snapshot compatibility manifest."""

    platform: str
    rooted: bool
    os_version: str
    osbuild: str
    snapshot_id: str = ""
    snapshot_name: str = ""

    # Validate the manifest before accepting it as evidence of baseline compatibility.
    def validate(self) -> None:
        if (any(not isinstance(value, str) for value in (self.platform, self.os_version, self.osbuild,
                                                        self.snapshot_id, self.snapshot_name))
                or not isinstance(self.rooted, bool) or not self.platform.strip() or not self.os_version.strip() or not self.osbuild.strip()
                or bool(self.snapshot_id) == bool(self.snapshot_name)):
            raise CorelliumError("Automation baseline requires platform, boolean rooted, exact OS/build and exactly one snapshot ID/name")


@dataclass(frozen=True)
class ResolvedDevice:
    """Frozen identities retained throughout preparation, execution and cleanup."""

    role: str
    instance: CorelliumInstance
    baseline_id: str
    baseline: AutomationBaseline


# Reject unavailable, foreign or incompatible snapshots, including unfinished snapshot tasks.
def validate_snapshot(snapshot: dict, instance: CorelliumInstance, baseline: AutomationBaseline) -> None:
    baseline.validate()
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("status"), dict):
        raise CorelliumError("Snapshot response must contain availability status")
    status = snapshot.get("status") or {}
    if (not snapshot.get("id") or snapshot.get("instance") != instance.id
            or status.get("created") is not True or status.get("task") not in {None, "", "none"}):
        raise CorelliumError(f"Snapshot for {instance.id} is unavailable, busy or belongs to another instance")
    if (baseline.platform != instance.type or baseline.rooted != instance.rooted
            or baseline.os_version != instance.os or baseline.osbuild != instance.osbuild):
        raise CorelliumError(f"Baseline manifest is incompatible with instance {instance.id}")
    for key, expected in (("type", instance.type), ("os", instance.os), ("osbuild", instance.osbuild),
                          ("model", instance.model), ("rooted", instance.rooted)):
        if expected != "" and key in snapshot and snapshot[key] is not None and snapshot[key] != expected:
            raise CorelliumError(f"Snapshot {snapshot['id']} has incompatible {key}")


# Resolve a unique snapshot inside its selected instance; never fall back from ID to name.
def resolve_baseline(client: CorelliumClient, instance: CorelliumInstance, baseline: AutomationBaseline,
                     *, deadline: float, poll: float) -> str:
    if not isinstance(baseline, AutomationBaseline):
        raise CorelliumError("Automation eligibility must declare a baseline compatibility manifest")
    baseline.validate()
    snapshots = retry_read(client, lambda api: api.list_snapshots(instance.id), deadline, poll)
    if not isinstance(snapshots, list) or any(not isinstance(item, dict) for item in snapshots):
        raise CorelliumError("Snapshot listing must return a list of snapshot objects")
    key, wanted = ("id", baseline.snapshot_id) if baseline.snapshot_id else ("name", baseline.snapshot_name)
    matches = [snapshot for snapshot in snapshots if snapshot.get(key) == wanted]
    if len(matches) != 1:
        raise CorelliumError(f"Baseline {wanted!r} must uniquely match a snapshot within {instance.id}; found {len(matches)}")
    validate_snapshot(matches[0], instance, baseline)
    return matches[0]["id"]


# Plan only requested roles from explicitly registered automation instances without mutating anything.
def resolve_roles(client: CorelliumClient, requests: Mapping[str, RoleSelection],
                  automation: Mapping[str, AutomationBaseline], *, platform: str = "android",
                  timeout: float = 60, poll: float = 1) -> tuple[ResolvedDevice, ...]:
    if not requests:
        return ()
    if set(requests) - ROLES.keys():
        raise CorelliumError("Unknown Corellium device role")
    for request in requests.values():
        if not isinstance(request, RoleSelection) or not isinstance(request.project, str) or not request.project.strip():
            raise CorelliumError("Each role needs a project selector")
        if not isinstance(request.instance, str) or not isinstance(request.os_version, str):
            raise CorelliumError("Instance and OS selectors must be strings")
    deadline = polling_deadline(timeout, poll)
    projects = retry_read(client, lambda api: api.list_projects(), deadline, poll)
    instances = retry_read(client, lambda api: api.list_instances(), deadline, poll)
    resolved = []
    for role, request in requests.items():
        projects_matching = [project for project in projects if request.project in (project.id, project.name)]
        if len(projects_matching) != 1:
            raise CorelliumError(f"Project selector {request.project!r} is missing or ambiguous; use an exact ID")
        in_project = [instance for instance in instances if instance.project == projects_matching[0].id]
        if request.instance:
            in_project = [instance for instance in in_project if request.instance in (instance.id, instance.name)]
            if len(in_project) != 1:
                raise CorelliumError(f"Instance selector {request.instance!r} is missing or ambiguous; use an exact ID")
        candidates = [instance for instance in in_project if instance.id in automation
                      and instance.type == platform and instance.rooted is ROLES[role]
                      and instance.state in ELIGIBLE_STATES and instance.task_state in {"none", ""}
                      and (not request.os_version or instance.os == request.os_version
                           or instance.os.startswith(request.os_version + "."))]
        failures = []
        for instance in sorted(candidates, key=lambda item: (ELIGIBLE_STATES[item.state], item.name, item.id)):
            try:
                baseline_id = resolve_baseline(client, instance, automation[instance.id], deadline=deadline, poll=poll)
            except CorelliumError as exc:
                # An outage is not evidence that this VM is ineligible; do not switch devices on an outage.
                if exc.transient:
                    raise
                failures.append(str(exc))
                continue
            resolved.append(ResolvedDevice(role, instance, baseline_id, automation[instance.id]))
            break
        else:
            raise CorelliumError(f"No eligible {role} automation instance with a compatible baseline. {'; '.join(failures)}")
    if len({device.instance.id for device in resolved}) != len(resolved):
        raise CorelliumError("Device roles must resolve to distinct instances")
    return tuple(resolved)
