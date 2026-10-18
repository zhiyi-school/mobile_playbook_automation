"""
Prepares a run's Corellium role devices from their baselines, installs the intended builds and confirms shutdown.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mobile_playbook.artifact_store.store import artifact_digest
from mobile_playbook.platforms.android.acquisition import apk as apk_install
from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome, installer_of
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.install import install_apks
from mobile_playbook.platforms.android.device.permissions import is_installed
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool, RoleUnavailable
from mobile_playbook.platforms.android.device_roles import PlannedRisk
from mobile_playbook.platforms.android.models import CORELLIUM_PROVIDER
from mobile_playbook.platforms.corellium.claims import ClaimSet, ClaimStore
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumError
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle, LifecyclePolicy
from mobile_playbook.platforms.corellium.profile import is_baseline_profile, load_profile
from mobile_playbook.platforms.corellium.selection import ROLES, ResolvedDevice, resolve_roles
from mobile_playbook.reporting.run_events import append_event

logger = logging.getLogger(__name__)

PLATFORM = "android"
Connect = Callable[[Sequence[ResolvedDevice], Mapping[str, Any], CorelliumClient, Mapping[str, Mapping[str, Any]]], AndroidDevicePool]


class BuildUnavailable(RuntimeError):
    """An app the run tests on a device has no intake APK to install."""


class BuildInstallError(RuntimeError):
    """The intended build could not be installed on a role device, or the device reports a different build."""

    # Keep the apk install-failure state alongside the message.
    def __init__(self, message: str, state: str = "error"):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class IntendedBuild:
    """The intake APK a run installs for one app, with its manifest version and SHA-256."""

    app_id: str
    package: str
    apk: Path
    version_name: str | None
    version_code: str | None
    sha256: str
    split_apks: tuple[Path, ...] = ()


@dataclass(frozen=True)
class InstalledBuild:
    """An intended build verified on one role device."""

    app_id: str
    role: str
    instance_id: str
    package: str
    version_name: str | None
    version_code: str | None
    sha256: str
    installer: str | None
    seconds: float


# Report whether an Android config uses the role-based Corellium baseline lifecycle.
def uses_baselines(config: Any) -> bool:
    device = config.device
    return device.provider == CORELLIUM_PROVIDER and is_baseline_profile(device.corellium)


# Return a fresh claim run ID tied to the run it belongs to; a new attempt never reuses an earlier claim.
def claim_run_id(prefix: str) -> str:
    return f"{PLATFORM}-{prefix}-{uuid.uuid4().hex[:12]}"


# Return the app's intake APK as the build to install, raising BuildUnavailable when there is none.
def intended_build(app: Any) -> IntendedBuild:
    found = find_intake_apk(app.package_name)
    if found is None:
        raise BuildUnavailable(f"No APK of {app.package_name} is in the intake folder, so {app.id} cannot be installed "
                               "on the restored test devices.")
    return IntendedBuild(app.id, app.package_name, found.path, found.version_name, found.version_code,
                         artifact_digest(found.path), found.split_paths)


# Return each role's builds in plan order, one per app that has a risk on that role, before any device is touched.
def intended_builds(plan: Iterable[PlannedRisk]) -> dict[str, list[IntendedBuild]]:
    builds: dict[str, IntendedBuild] = {}
    by_role: dict[str, list[IntendedBuild]] = {}
    for item in plan:
        if item.role is None:
            continue
        app_id = str(item.app.id)
        if app_id not in builds:
            builds[app_id] = intended_build(item.app)
        if builds[app_id] not in by_role.setdefault(item.role, []):
            by_role[item.role].append(builds[app_id])
    return by_role


class BaselineSession:
    """One run's claimed and prepared role devices; closing it quits sessions and then confirms every VM off."""

    # Hold the lifecycle and claims; the pool is set once the prepared devices connect.
    def __init__(self, lifecycle: CorelliumLifecycle, claims: ClaimSet, event_dir: Path):
        self.lifecycle = lifecycle
        self.claims = claims
        self.event_dir = event_dir
        self.pool: AndroidDevicePool | None = None
        self.installed: dict[tuple[str, str], InstalledBuild] = {}
        self.outcomes: dict[str, dict] | None = None

    # Return the claimed identity of a role device and the build installed on it for an app, for result metadata.
    def result_device(self, role: str, app_id: str) -> dict[str, Any]:
        device = next(item for item in self.claims.devices if item.role == role)
        identity: dict[str, Any] = {"role": role, "instance_id": device.instance.id, "baseline_id": device.baseline_id}
        build = self.installed.get((role, app_id))
        if build is not None:
            identity.update(artifact_sha256=build.sha256, version=build.version_name, build_number=build.version_code)
        return identity

    # Install the intended build on the role device, replacing any copy, and verify its package and version.
    def install(self, build: IntendedBuild, role: str) -> InstalledBuild:
        device = self.pool.device(role)
        adb = device.client.adb
        if apk_install.adb_installs_need_confirmation(adb):
            raise BuildInstallError(apk_install.FAILURES["confirmation_required"][2], "confirmation_required")
        started = time.monotonic()
        logger.info("Installing %s from %s on the %s device (instance %s).", build.package, build.apk.name, role, device.instance_id)
        installed, reason = install_apks(adb, [build.apk, *build.split_apks], grant_permissions=False, timeout=apk_install.INSTALL_TIMEOUT_SECONDS)
        if not installed:
            raise BuildInstallError(f"Could not install {build.apk.name} on the {role} device: {reason}",
                                    "rejected" if reason.startswith("Failure [") else "error")
        if not is_installed(adb, build.package):
            raise BuildInstallError(f"{build.apk.name} did not install {build.package} on the {role} device", "wrong_app")
        version = installed_version(adb, build.package)
        reported = (version.version_name, version.version_code) if version else (None, None)
        if version is None or any(want and got != want for want, got in zip((build.version_name, build.version_code), reported)):
            raise BuildInstallError(
                f"The {role} device reports {build.package} version {reported[0]} (build {reported[1]}), not the intended "
                f"{build.version_name} (build {build.version_code}) from {build.apk.name}"
            )
        record = InstalledBuild(build.app_id, role, device.instance_id, build.package, reported[0], reported[1],
                                build.sha256, installer_of(adb, build.package) or None, time.monotonic() - started)
        self.installed[(role, build.app_id)] = record
        append_event(self.event_dir, "app_installed", app_id=build.app_id, role=role, instance_id=device.instance_id,
                     package=build.package, version=record.version_name, build_number=record.version_code,
                     artifact_sha256=build.sha256, apk=build.apk.name)
        return record

    # Quit sessions and disconnect adb, then confirm every VM off independently; idempotent, never raises for a
    # shutdown failure, which stays recorded as pending cleanup for recovery.
    def close(self) -> dict[str, dict]:
        if self.outcomes is not None:
            return self.outcomes
        try:
            if self.pool is not None:
                try:
                    self.pool.release()
                except Exception as exc:
                    logger.warning("Could not close the role device sessions before shutdown: %s", exc)
        finally:
            self.outcomes = self.lifecycle.cleanup(self.claims)
        for role, outcome in self.outcomes.items():
            append_event(self.event_dir, "device_cleanup", role=role, **outcome)
            if outcome["status"] != "off":
                logger.error("The %s device (instance %s) did not confirm shutdown and stays pending cleanup: %s",
                             role, outcome["instance_id"], outcome.get("error"))
        return self.outcomes


# Resolve, claim, restore and start every required role device once, connect it and install its builds; any failure
# shuts down what was claimed before the error propagates.
def open_session(config: Any, roles: Sequence[str], builds: Mapping[str, Sequence[IntendedBuild]], event_dir: Path,
                 run_id: str, connect: Connect, *, client: CorelliumClient | None = None, store: ClaimStore | None = None,
                 policy: LifecyclePolicy | None = None) -> BaselineSession:
    profile = load_profile(config.device.corellium, PLATFORM)
    missing = [role for role in roles if role not in profile.roles]
    if missing:
        raise RoleUnavailable(f"This run needs the {' and '.join(missing)} device, but device.corellium.roles has no "
                              "entry for it; another role's device is never used instead.")
    client = client or CorelliumClient.from_env()
    devices = resolve_roles(client, {role: profile.roles[role] for role in roles}, profile.baselines, platform=PLATFORM)
    lifecycle = CorelliumLifecycle(client, store or ClaimStore(client.api_url), policy or profile.policy)
    claims = lifecycle.store.claim_all(run_id, devices)
    session = BaselineSession(lifecycle, claims, event_dir)
    for device in devices:
        append_event(event_dir, "device_claimed", role=device.role, instance_id=device.instance.id,
                     baseline_id=device.baseline_id, claim_run_id=run_id)
    try:
        prepared = lifecycle.prepare(claims)
        for device in devices:
            append_event(event_dir, "device_prepared", role=device.role, instance_id=device.instance.id,
                         baseline_id=device.baseline_id)
        session.pool = connect(claims.devices, prepared, client, profile.role_settings)
        for role, identity in session.pool.identities().items():
            append_event(event_dir, "device_connected", role=role, instance_id=identity["instance_id"])
        for role in roles:
            for build in builds.get(role, ()):
                session.install(build, role)
    except BaseException as exc:
        logger.debug("android baseline: setup of %s failed: %s", run_id, exc, exc_info=True)
        try:
            session.close()
        except Exception as cleanup_error:
            logger.error("Could not shut down the role devices after a setup failure: %s", cleanup_error)
        raise
    return session


# Install apps on one role's freshly restored device and shut it down again, returning each app's outcome; the
# next assessment still restores its own baseline and installs its own build.
def install_apps(config: Any, apps: Sequence[Any], role: str, event_dir: Path, connect: Connect, *,
                 client: CorelliumClient | None = None, store: ClaimStore | None = None,
                 policy: LifecyclePolicy | None = None) -> list[tuple[Any, InstallOutcome]]:
    outcomes: dict[str, InstallOutcome] = {}
    builds = []
    for app in apps:
        try:
            builds.append(intended_build(app))
        except BuildUnavailable:
            outcomes[app.id] = apk_install.failure("missing")
    if builds:
        try:
            session = open_session(config, (role,), {}, event_dir, claim_run_id("install"), connect,
                                   client=client, store=store, policy=policy)
        except Exception as exc:
            logger.warning("The %s Corellium device could not be prepared for installs: %s", role, exc)
            for build in builds:
                outcomes[build.app_id] = apk_install.failure("error", f"The {role} test device is not available: {exc}")
        else:
            try:
                for build in builds:
                    outcomes[build.app_id] = _install_outcome(session, build, role)
            finally:
                session.close()
    return [(app, outcomes[app.id]) for app in apps]


# Install one build through the session and describe the result as an install outcome.
def _install_outcome(session: BaselineSession, build: IntendedBuild, role: str) -> InstallOutcome:
    try:
        record = session.install(build, role)
    except BuildInstallError as exc:
        return apk_install.failure(exc.state, str(exc))
    return InstallOutcome(status="installed", installer=record.installer, seconds=record.seconds,
                          version_name=record.version_name, version_code=record.version_code)


class TaggedReportWriter:
    """Adds a role device's identity and installed build to every result a risk writes through it."""

    # Wrap the run's report writer with the device identity to record.
    def __init__(self, writer: Any, device: Mapping[str, Any]):
        self._writer = writer
        self._device = dict(device)

    # Write the result with its device identity under metadata.device.
    def write_result(self, result: Any, report_dir: Path) -> None:
        self._writer.write_result(replace(result, metadata={**result.metadata, "device": self._device}), report_dir)

    # Forward everything else to the wrapped writer.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


@dataclass(frozen=True)
class RoleReadiness:
    """Whether a role device could serve a run now: ready, startable, busy, unavailable, unreachable or unconfigured."""

    role: str
    state: str
    detail: str = ""


# Report each required role's readiness from read-only API calls and the claim store, never claiming, restoring,
# starting, authorizing or installing; only the roles asked for are looked up.
def role_readiness(config: Any, roles: Sequence[str], *, client: CorelliumClient | None = None,
                   store: ClaimStore | None = None, timeout: float = 10.0) -> list[RoleReadiness]:
    try:
        profile = load_profile(config.device.corellium, PLATFORM)
    except CorelliumError as exc:
        return [RoleReadiness(role, "unconfigured", str(exc)) for role in roles]
    try:
        client = client or CorelliumClient.from_env()
        pending = (store or ClaimStore(client.api_url)).pending()
    except (CorelliumError, OSError) as exc:
        return [RoleReadiness(role, "unreachable", str(exc)) for role in roles]
    owned = {row["instance_id"] for row in pending}
    readiness = []
    for role in roles:
        selection = profile.roles.get(role)
        if selection is None:
            readiness.append(RoleReadiness(role, "unconfigured", f"device.corellium.roles has no {role} entry"))
            continue
        try:
            (device,) = resolve_roles(client, {role: selection}, profile.baselines, platform=PLATFORM, timeout=timeout)
        except CorelliumError as exc:
            if exc.transient:
                readiness.append(RoleReadiness(role, "unreachable", str(exc)))
            elif any(_could_serve(profile, role, instance_id) for instance_id in owned):
                readiness.append(RoleReadiness(role, "busy", "a claimed device is in use or awaiting cleanup"))
            else:
                readiness.append(RoleReadiness(role, "unavailable", str(exc)))
            continue
        if device.instance.id in owned:
            readiness.append(RoleReadiness(role, "busy", "the device is in use or awaiting cleanup"))
        else:
            readiness.append(RoleReadiness(role, "ready" if device.instance.state == "on" else "startable"))
    return readiness


# Report whether an inventory instance is one the role could select, so its ownership explains an empty selection.
def _could_serve(profile: Any, role: str, instance_id: str) -> bool:
    baseline = profile.baselines.get(instance_id)
    pinned = profile.roles[role].instance
    return baseline is not None and baseline.rooted is ROLES[role] and (not pinned or pinned == instance_id)
