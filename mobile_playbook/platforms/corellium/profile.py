"""Parse and validate a platform's role-based Corellium baseline profile from plain configuration mappings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .client import ROOTED_ANDROID_BUILDS, CorelliumError
from .lifecycle import LifecyclePolicy
from .selection import ROLES, AutomationBaseline, RoleSelection

SINGLE_INSTANCE_KEYS = ("project", "rooted", "instance", "os_version")
DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class BaselineProfile:
    """Role selectors, the automation inventory keyed by exact instance ID, per-role settings and lifecycle budgets."""

    roles: Mapping[str, RoleSelection]
    baselines: Mapping[str, AutomationBaseline]
    role_settings: Mapping[str, Mapping[str, Any]]
    policy: LifecyclePolicy


# Report whether the settings use the role-based baseline lifecycle rather than the single-instance integration.
def is_baseline_profile(settings: Any) -> bool:
    return isinstance(settings, Mapping) and "roles" in settings


# Return every problem with a baseline profile, prefixed with its configuration path.
def profile_errors(settings: Any, platform: str, label: str = "device.corellium") -> list[str]:
    if not isinstance(settings, Mapping):
        return [f"{label} must be a mapping"]
    errors = [f"{label}.{key} belongs under {label}.roles.<role> when roles are set"
              for key in SINGLE_INSTANCE_KEYS if key in settings]
    for key in ("auto_start", "auto_stop"):
        if settings.get(key) is not True:
            errors.append(f"{label}.{key} must be set to true when roles are set")
    for key in ("boot_timeout_seconds", "restore_timeout_seconds"):
        if key in settings and not _positive(settings[key]):
            errors.append(f"{label}.{key} must be a positive number")
    baselines = settings.get("baselines")
    if not isinstance(baselines, Mapping) or not baselines:
        errors.append(f"{label}.baselines must map each automation instance ID to its baseline")
        baselines = {}
    for instance_id, baseline in baselines.items():
        errors.extend(_baseline_errors(instance_id, baseline, platform, f"{label}.baselines.{instance_id}"))
    roles = settings.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        return errors + [f"{label}.roles must map non_rooted and/or rooted to a device selector"]
    pinned: dict[str, str] = {}
    for role, selector in roles.items():
        path = f"{label}.roles.{role}"
        if role not in ROLES:
            errors.append(f"{path} is not a device role; use non_rooted or rooted")
            continue
        if not isinstance(selector, Mapping):
            errors.append(f"{path} must be a mapping")
            continue
        if not _text(selector.get("project")):
            errors.append(f"{path}.project is required")
        for key in ("instance", "os_version"):
            if selector.get(key) is not None and not isinstance(selector.get(key), str):
                errors.append(f"{path}.{key} must be text")
        rooted = ROLES[role]
        candidates = [item for item in baselines.values() if isinstance(item, Mapping) and item.get("rooted") is rooted]
        if not candidates:
            errors.append(f"{path} has no baseline under {label}.baselines for a {_state(rooted)} instance")
        instance = selector.get("instance")
        if _text(instance):
            baseline = baselines.get(instance)
            if isinstance(baseline, Mapping) and baseline.get("rooted") is not rooted:
                errors.append(f"{path}.instance {instance!r} has a {_state(not rooted)} baseline")
            if instance in pinned:
                errors.append(f"{path}.instance {instance!r} is also the {pinned[instance]} instance; roles need distinct VMs")
            pinned[instance] = role
    return errors


# Build the validated profile, raising CorelliumError with every problem when the settings are invalid.
def load_profile(settings: Any, platform: str) -> BaselineProfile:
    errors = profile_errors(settings, platform)
    if errors:
        raise CorelliumError("; ".join(errors))
    roles = {
        role: RoleSelection(project=selector["project"].strip(), instance=(selector.get("instance") or "").strip(),
                            os_version=(selector.get("os_version") or "").strip())
        for role, selector in settings["roles"].items()
    }
    baselines = {
        instance_id: AutomationBaseline(platform=platform, rooted=item["rooted"], os_version=item["os_version"],
                                        osbuild=item["osbuild"], snapshot_id=item.get("snapshot_id") or "",
                                        snapshot_name=item.get("snapshot_name") or "")
        for instance_id, item in settings["baselines"].items()
    }
    policy = LifecyclePolicy(restore_timeout=float(settings.get("restore_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
                             power_timeout=float(settings.get("boot_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS))
    return BaselineProfile(roles, baselines, {role: dict(selector) for role, selector in settings["roles"].items()}, policy)


# Return the problems with one inventory entry, including a rooted flag that contradicts an Android build variant.
def _baseline_errors(instance_id: Any, baseline: Any, platform: str, path: str) -> list[str]:
    if not _text(instance_id):
        return [f"{path} must be keyed by an exact instance ID"]
    if not isinstance(baseline, Mapping):
        return [f"{path} must be a mapping"]
    errors = []
    if not isinstance(baseline.get("rooted"), bool):
        errors.append(f"{path}.rooted must be true or false")
    for key in ("os_version", "osbuild"):
        if not _text(baseline.get(key)):
            errors.append(f"{path}.{key} is required and must match the instance exactly")
    snapshots = [key for key in ("snapshot_id", "snapshot_name") if baseline.get(key) is not None]
    if len(snapshots) != 1 or not _text(baseline.get(snapshots[0])):
        errors.append(f"{path} needs exactly one of snapshot_id or snapshot_name")
    if platform == "android" and isinstance(baseline.get("rooted"), bool) and _text(baseline.get("osbuild")):
        variant = baseline["osbuild"].split()[-1]
        if (variant in ROOTED_ANDROID_BUILDS) is not baseline["rooted"]:
            errors.append(f"{path}.rooted contradicts the {variant!r} build in osbuild")
    return errors


# Report whether a value is non-empty text.
def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# Report whether a value is a positive finite number and not a boolean.
def _positive(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


# Describe a rooted state for messages.
def _state(rooted: bool) -> str:
    return "rooted" if rooted else "non-rooted"
