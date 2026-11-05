"""
Prepares a run's role devices, Corellium VMs restored from their baselines and a physical phone where configured,
installs the intended builds, and afterwards removes what the run added to the phone and confirms each VM off.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mobile_playbook.artifact_store.store import artifact_digest
from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.acquisition import apk as apk_install
from mobile_playbook.platforms.android.acquisition.intake_apk import find_intake_apk
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome, installer_of
from mobile_playbook.platforms.android.acquisition.store import APP_STORES, install_method_for
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.apks import installed_apk_paths
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device import fingerprint
from mobile_playbook.platforms.android.device.install import disable_background_dexopt, install_apks, wait_for_dexopt
from mobile_playbook.platforms.android.device.corellium import adb_serial
from mobile_playbook.platforms.android.device.permissions import is_installed
from mobile_playbook.platforms.android.device.phone_lock import PhoneBusy, PhoneLock, acquire_phone_lock, phone_lock_holder
from mobile_playbook.platforms.android.device.phone_role import (
    connect_phone,
    phone_model,
    phone_readiness,
    ready_phone_serial,
    third_party_packages,
    uninstall,
)
from mobile_playbook.platforms.android.device.pool import (
    CORELLIUM_VM,
    PHYSICAL_PHONE,
    AndroidDevicePool,
    RoleDevice,
    RoleUnavailable,
)
from mobile_playbook.platforms.android.device.transports import switch_recorder
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
ConnectPhone = Callable[[str, Collection[str]], RoleDevice]


class BuildUnavailable(RuntimeError):
    """An app the run tests on a device has no intake APK to install."""


class BuildInstallError(RuntimeError):
    """The intended build could not be installed on a role device, or the device reports a different build."""

    # Keep the apk install-failure state, or a store's own failed outcome, alongside the message.
    def __init__(self, message: str, state: str = "error", outcome: InstallOutcome | None = None):
        super().__init__(message)
        self.state = state
        self.outcome = outcome


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
class StoreBuild:
    """An app a run installs on the phone from its store, which chooses the build."""

    app_id: str
    package: str
    store: str


@dataclass(frozen=True)
class InstalledBuild:
    """An intended build verified on one role device."""

    app_id: str
    role: str
    instance_id: str
    package: str
    version_name: str | None
    version_code: str | None
    sha256: str | None
    installer: str | None
    seconds: float
    source: str = "apk"
    action: str = "installed"


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


# Return each role's builds in plan order, one per app that has a risk on that role, before any device is touched;
# only roles that install intake APKs need one.
def intended_builds(plan: Iterable[PlannedRisk], config: Any) -> dict[str, list[IntendedBuild | StoreBuild]]:
    apks: dict[str, IntendedBuild] = {}
    by_role: dict[str, list[IntendedBuild | StoreBuild]] = {}
    for item in plan:
        if item.role is None:
            continue
        app_id = str(item.app.id)
        method = install_method_for(config, item.role)
        if method.key == "apk":
            if app_id not in apks:
                apks[app_id] = intended_build(item.app)
            build: IntendedBuild | StoreBuild = apks[app_id]
        else:
            build = StoreBuild(app_id, item.app.package_name, method.key)
        if build not in by_role.setdefault(item.role, []):
            by_role[item.role].append(build)
    return by_role


class BaselineSession:
    """One run's role devices; closing it quits sessions, removes what the run installed on the phone, confirms every
    VM off and frees the phone."""

    # Hold the event folder; the VM lifecycle and claims, the phone and the pool are set as setup reaches them.
    def __init__(self, lifecycle: CorelliumLifecycle | None, claims: ClaimSet | None, event_dir: Path):
        self.lifecycle = lifecycle
        self.claims = claims
        self.event_dir = event_dir
        self.pool: AndroidDevicePool | None = None
        self.phones: dict[str, RoleDevice] = {}
        self.phone_lock: PhoneLock | None = None
        self.phone_packages: dict[str, set[str]] = {}
        self.phone_models: dict[str, str | None] = {}
        self.replaced: dict[str, set[str]] = {}
        self.store_installed: dict[str, dict[str, str | None]] = {}
        self.clean_phone = True
        self.installed: dict[tuple[str, str], InstalledBuild] = {}
        self.outcomes: dict[str, dict] | None = None

    # Return a role device's type and identity and the build installed on it for an app, for result metadata.
    def result_device(self, role: str, app_id: str) -> dict[str, Any]:
        phone = self.phones.get(role)
        if phone is not None:
            identity: dict[str, Any] = {"role": role, "device_type": PHYSICAL_PHONE, "serial": phone.serial,
                                        "model": self.phone_models.get(role)}
        else:
            device = next(item for item in self.claims.devices if item.role == role)
            identity = {"role": role, "device_type": CORELLIUM_VM, "instance_id": device.instance.id,
                        "baseline_id": device.baseline_id, "serial": self.pool.device(role).serial if self.pool else None}
        build = self.installed.get((role, app_id))
        if build is not None:
            identity.update(artifact_sha256=build.sha256, version=build.version_name, build_number=build.version_code)
        return identity

    # Install the intended build on the role device and verify its package and version; on a phone any copy already
    # there is uninstalled first, except one its store installed.
    def install(self, build: IntendedBuild | StoreBuild, role: str) -> InstalledBuild:
        if isinstance(build, StoreBuild):
            return self._install_from_store(build, role)
        device = self.pool.device(role)
        adb = device.client.adb
        if apk_install.adb_installs_need_confirmation(adb):
            raise BuildInstallError(apk_install.FAILURES["confirmation_required"][2], "confirmation_required")
        if device.device_type == CORELLIUM_VM:
            # A VM restored from its snapshot may recompile apps such as Play services first, and the package manager
            # queues the install behind that work until the install's own timeout expires.
            waiting = time.monotonic()
            still_running = wait_for_dexopt(adb)
            if still_running:
                raise BuildInstallError(
                    f"The {role} device (instance {device.instance_id}) was still compiling apps after "
                    f"{time.monotonic() - waiting:.0f} s ({'; '.join(still_running)}), so {build.apk.name} was not "
                    "installed. Retry once the baseline finishes compiling, or retake its snapshot after compilation ends."
                )
        if device.device_type == PHYSICAL_PHONE and is_installed(adb, build.package):
            self.replaced.setdefault(role, set()).add(build.package)
            logger.info("Uninstalling the copy of %s already on the phone %s before installing the intended build.",
                        build.package, device.serial)
            if not uninstall(adb, build.package):
                raise BuildInstallError(f"Could not uninstall the copy of {build.package} already on the phone {device.serial}")
        started = time.monotonic()
        logger.info("Installing %s from %s on the %s device (%s).", build.package, build.apk.name, role, device.instance_id)
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
        append_event(self.event_dir, "app_installed", app_id=build.app_id, role=role, device_type=device.device_type,
                     instance_id=device.instance_id, serial=device.serial, package=build.package,
                     version=record.version_name, build_number=record.version_code, artifact_sha256=build.sha256,
                     apk=build.apk.name)
        return record

    # Keep the app when the phone's store installed it, otherwise install it from that store, first removing a copy
    # anything else installed, and record the build the phone reports.
    def _install_from_store(self, build: StoreBuild, role: str) -> InstalledBuild:
        device = self.pool.device(role)
        adb = device.client.adb
        store = APP_STORES[build.store]
        started = time.monotonic()
        action = "installed"
        if is_installed(adb, build.package):
            if fingerprint.installer_of(adb, build.package) == store.installer_package:
                action = "kept"
            else:
                action = "replaced"
                logger.info("Uninstalling the copy of %s on the phone %s that %s did not install.", build.package,
                            device.serial, store.name)
                if not uninstall(adb, build.package):
                    message = f"Could not uninstall the copy of {build.package} on the phone {device.serial} that {store.name} did not install"
                    raise BuildInstallError(message, outcome=store.failure("error", message))
        if action != "kept":
            logger.info("Installing %s from %s on the %s device (%s).", build.package, store.name, role, device.serial)
            outcome = store.install_and_read_version(adb, device.client.make_driver, build.package,
                                                     android_work_dir() / store.key / "diagnostics")
            if outcome.status not in ("installed", "already_installed"):
                raise BuildInstallError(f"Could not install {build.package} from {store.name} on the {role} device: "
                                        f"{outcome.detail}", outcome=outcome)
        version = installed_version(adb, build.package)
        if version is None:
            message = f"The {role} device reports no version of {build.package} after installing it from {store.name}"
            raise BuildInstallError(message, outcome=store.failure("error", message))
        installer = fingerprint.installer_of(adb, build.package)
        record = InstalledBuild(build.app_id, role, device.instance_id, build.package, version.version_name,
                                version.version_code, fingerprint.installed_base_apk_sha256(adb, build.package),
                                installer, time.monotonic() - started, source=store.key, action=action)
        self.installed[(role, build.app_id)] = record
        self.store_installed.setdefault(role, {})[build.package] = store.installer_package
        append_event(self.event_dir, "app_installed", app_id=build.app_id, role=role, device_type=device.device_type,
                     instance_id=device.instance_id, serial=device.serial, package=build.package,
                     version=record.version_name, build_number=record.version_code, artifact_sha256=record.sha256,
                     source=store.key, installer=installer, action=action)
        return record

    # Copy the base APK of the app installed on a role device into destination and return it, or None when the device
    # lists no base APK, both attempts fail or the copy is not the installed build; an earlier copy there is replaced.
    def pull_base_apk(self, role: str, package: str, destination: Path, sha256: str | None) -> Path | None:
        adb = self.pool.device(role).client.adb
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "base.apk"
        # A pull that fails as the phone's connection drops reports only its progress, so adb never moves to the
        # backup connection for it; the second attempt's commands find the drop and move.
        for attempt in (1, 2):
            target.unlink(missing_ok=True)
            base = next((path for path in installed_apk_paths(adb, package) if path.endswith("/base.apk")), None)
            if base is None:
                return None
            code, _, err = adb.run(["pull", base, str(target)], timeout=120)
            if code == 0 and target.is_file():
                break
            logger.debug("android baseline: pull %s of %s from the %s device failed (exit %s): %s", attempt, base, role,
                         code, err)
        else:
            return None
        if sha256 is not None and artifact_digest(target) != sha256:
            logger.warning("The copy of %s from the %s device is not the build installed there.", package, role)
            target.unlink(missing_ok=True)
            return None
        return target

    # Quit sessions, remove what the run installed on the phone, disconnect the VMs and confirm each VM off
    # independently, then free the phone; idempotent, and a failure on one device never skips another.
    def close(self) -> dict[str, dict]:
        if self.outcomes is not None:
            return self.outcomes
        outcomes: dict[str, dict] = {}
        try:
            if self.pool is not None:
                try:
                    # Sessions end first, so removing Appium's own apps from the phone cannot break a live session.
                    self.pool.quit()
                except Exception as exc:
                    logger.warning("Could not close the role device sessions: %s", exc)
            for role, phone in self.phones.items() if self.clean_phone else ():
                self._reach_phone(role, phone)
                outcomes[role] = self._clean_phone(role, phone)
            if self.pool is not None:
                try:
                    self.pool.release()
                except Exception as exc:
                    logger.warning("Could not close the role device sessions before shutdown: %s", exc)
        finally:
            try:
                if self.claims is not None:
                    outcomes.update(self.lifecycle.cleanup(self.claims))
            finally:
                if self.phone_lock is not None:
                    self.phone_lock.release()
                self.outcomes = outcomes
        for role, outcome in outcomes.items():
            append_event(self.event_dir, "device_cleanup", role=role, **outcome)
            if outcome["status"] == "cleanup_pending":
                logger.error("The %s device (instance %s) did not confirm shutdown and stays pending cleanup: %s",
                             role, outcome["instance_id"], outcome.get("error"))
            elif outcome["status"] == "cleanup_incomplete":
                logger.error("Could not remove %s from the phone %s after the run: %s", ", ".join(outcome["left"]),
                             outcome["serial"], outcome.get("error"))
        return outcomes

    # Reconnect the phone before cleanup, moving to its backup connection when the one in use dropped; never raises,
    # so an unreachable phone still gets a cleanup outcome.
    def _reach_phone(self, role: str, phone: RoleDevice) -> None:
        try:
            if self.pool is not None and role in self.pool.roles:
                self.pool.ready(role)
            elif phone.reconnect is not None:
                phone.reconnect()
        except Exception as exc:
            logger.warning("Could not reconnect the phone %s before removing what the run installed: %s", phone.serial, exc)

    # Uninstall every package the run added to the phone, including builds it replaced, but keep apps its store
    # installed while that store is still their installer; never raises.
    def _clean_phone(self, role: str, phone: RoleDevice) -> dict:
        adb = phone.client.adb
        outcome: dict[str, Any] = {"device_type": PHYSICAL_PHONE, "serial": phone.serial}
        wanted = {build.package for (built_role, _), build in self.installed.items() if built_role == role}
        wanted |= self.replaced.get(role, set())
        try:
            wanted |= third_party_packages(adb) - self.phone_packages.get(role, set())
        except Exception as exc:
            outcome["error"] = f"could not list the phone's packages: {exc}"
        kept = []
        for package, installer_package in sorted(self.store_installed.get(role, {}).items()):
            try:
                if fingerprint.installer_of(adb, package) == installer_package:
                    kept.append(package)
            except Exception as exc:
                logger.debug("android phone: reading the installer of %s failed: %s", package, exc, exc_info=True)
        wanted -= set(kept)
        left = []
        for package in sorted(wanted):
            try:
                removed = uninstall(adb, package)
            except Exception as exc:
                logger.debug("android phone: uninstall of %s failed: %s", package, exc, exc_info=True)
                removed = False
            if not removed:
                left.append(package)
        outcome.update(status="cleanup_incomplete" if left or "error" in outcome else "cleaned",
                       removed=sorted(set(wanted) - set(left)), left=left, kept=kept)
        return outcome


# Return every adb address a Corellium VM of the run could use, so the phone is never resolved to one of them.
def _vm_addresses(devices: Sequence[ResolvedDevice], role_settings: Mapping[str, Mapping[str, Any]]) -> set[str]:
    addresses = {str(settings.get("adb_address")).strip() for settings in role_settings.values()
                 if isinstance(settings, Mapping) and settings.get("adb_address")}
    for device in devices:
        try:
            addresses.add(adb_serial(device.instance, dict(role_settings.get(device.role) or {})))
        except CorelliumError:
            logger.debug("android baseline: %s has no adb address yet", device.instance.id)
    return addresses


# Prepare every required role device once: the phone for physical roles, checked before any VM is touched, and for
# the others resolve, claim, restore and start each Corellium VM, connect it and install the builds. Any failure
# cleans up what was set up before the error propagates.
def open_session(config: Any, roles: Sequence[str], builds: Mapping[str, Sequence[IntendedBuild | StoreBuild]], event_dir: Path,
                 run_id: str, connect: Connect, *, client: CorelliumClient | None = None, store: ClaimStore | None = None,
                 policy: LifecyclePolicy | None = None, phone: ConnectPhone | None = None,
                 clean_phone: bool = True) -> BaselineSession:
    profile = load_profile(config.device.corellium, PLATFORM)
    phone_roles = [role for role in roles if role in config.device.physical_roles]
    vm_roles = [role for role in roles if role not in phone_roles]
    missing = [role for role in vm_roles if role not in profile.roles]
    if missing:
        raise RoleUnavailable(f"This run needs the {' and '.join(missing)} device, but device.corellium.roles has no "
                              "entry for it; another role's device is never used instead.")
    devices: tuple[ResolvedDevice, ...] = ()
    if vm_roles:
        client = client or CorelliumClient.from_env()
        devices = resolve_roles(client, {role: profile.roles[role] for role in vm_roles}, profile.baselines, platform=PLATFORM)
    session = BaselineSession(None, None, event_dir)
    session.clean_phone = clean_phone
    if phone_roles:
        session.phone_lock = acquire_phone_lock(f"Android run {run_id}")
    try:
        exclude = _vm_addresses(devices, profile.role_settings)
        for role in phone_roles:
            device = phone(role, exclude) if phone else connect_phone(config, role, exclude=exclude)
            session.phones[role] = device
            session.phone_packages[role] = third_party_packages(device.client.adb)
            session.phone_models[role] = phone_model(device.client.adb)
            transports = device.client.adb.transports
            if transports is not None:
                transports.on_switch = switch_recorder(event_dir, role)
            append_event(event_dir, "device_connected", role=role, device_type=PHYSICAL_PHONE, serial=device.serial,
                         model=session.phone_models[role], backup_serials=transports.backups if transports else [])
        pool_devices = dict(session.phones)
        if devices:
            session.lifecycle = CorelliumLifecycle(client, store or ClaimStore(client.api_url), policy or profile.policy)
            session.claims = session.lifecycle.store.claim_all(run_id, devices)
            for device in devices:
                append_event(event_dir, "device_claimed", role=device.role, instance_id=device.instance.id,
                             baseline_id=device.baseline_id, claim_run_id=run_id)
            prepared = session.lifecycle.prepare(session.claims)
            for device in devices:
                append_event(event_dir, "device_prepared", role=device.role, instance_id=device.instance.id,
                             baseline_id=device.baseline_id)
            vm_pool = connect(session.claims.devices, prepared, client, profile.role_settings)
            for role in vm_pool.roles:
                pool_devices[role] = vm_pool.device(role)
                append_event(event_dir, "device_connected", role=role, device_type=CORELLIUM_VM,
                             instance_id=vm_pool.device(role).instance_id, serial=vm_pool.device(role).serial)
                # The daily background job compiles Play services for longer than artd allows, and installs would
                # queue behind it; stopping it lasts until the VM's next boot, so a refusal never blocks the run.
                refused = disable_background_dexopt(vm_pool.device(role).client.adb)
                if refused:
                    logger.warning("Could not stop background app compilation on the %s device (instance %s): %s",
                                   role, vm_pool.device(role).instance_id, refused)
            session.pool = AndroidDevicePool({role: pool_devices[role] for role in roles}, adb_path=vm_pool.adb_path,
                                             policy=vm_pool.policy)
        else:
            session.pool = AndroidDevicePool({role: pool_devices[role] for role in roles}, adb_path=config.device.adb_path)
        for role in roles:
            for build in builds.get(role, ()):
                session.install(build, role)
        _warn_on_differing_versions(session.installed.values(), event_dir)
    except BaseException as exc:
        logger.debug("android baseline: setup of %s failed: %s", run_id, exc, exc_info=True)
        try:
            session.close()
        except Exception as cleanup_error:
            logger.error("Could not clean up the role devices after a setup failure: %s", cleanup_error)
        raise
    return session


# Record a warning for each app whose role devices report different versions, as when the phone's store offers
# another build than the intake APK on the VMs; each device still tests the build it has.
def _warn_on_differing_versions(installed: Iterable[InstalledBuild], event_dir: Path) -> None:
    by_app: dict[str, list[InstalledBuild]] = {}
    for build in installed:
        by_app.setdefault(build.app_id, []).append(build)
    for app_id, builds in by_app.items():
        if len({(build.version_name, build.version_code) for build in builds}) < 2:
            continue
        described = "; ".join(f"{build.role} has {build.version_name} (build {build.version_code or 'unknown'}) from "
                              f"{APP_STORES[build.source].name}" for build in builds)
        message = f"The devices test different versions of {app_id}: {described}."
        logger.warning(message)
        append_event(event_dir, "app_versions_differ", app_id=app_id, message=message, devices=[
            {"role": build.role, "version": build.version_name, "build_number": build.version_code,
             "source": build.source, "artifact_sha256": build.sha256} for build in builds])


# Install apps on one role's device and return each app's outcome: a VM is restored first and shut down again, and a
# phone keeps the installed apps. The next assessment still prepares its own devices and installs its own build;
# PhoneBusy propagates so callers can report the phone as busy.
def install_apps(config: Any, apps: Sequence[Any], role: str, event_dir: Path, connect: Connect, *,
                 client: CorelliumClient | None = None, store: ClaimStore | None = None,
                 policy: LifecyclePolicy | None = None, phone: ConnectPhone | None = None) -> list[tuple[Any, InstallOutcome]]:
    outcomes: dict[str, InstallOutcome] = {}
    builds: list[IntendedBuild | StoreBuild] = []
    method = install_method_for(config, role)
    for app in apps:
        if method.key != "apk":
            builds.append(StoreBuild(app.id, app.package_name, method.key))
            continue
        try:
            builds.append(intended_build(app))
        except BuildUnavailable:
            outcomes[app.id] = apk_install.failure("missing")
    if builds:
        try:
            session = open_session(config, (role,), {}, event_dir, claim_run_id("install"), connect,
                                   client=client, store=store, policy=policy, phone=phone, clean_phone=False)
        except PhoneBusy:
            raise
        except Exception as exc:
            logger.warning("The %s device could not be prepared for installs: %s", role, exc)
            for build in builds:
                outcomes[build.app_id] = method.failure("error", f"The {role} test device is not available: {exc}")
        else:
            try:
                for build in builds:
                    outcomes[build.app_id] = _install_outcome(session, build, role)
            finally:
                session.close()
    return [(app, outcomes[app.id]) for app in apps]


# Install one build through the session and describe the result as an install outcome.
def _install_outcome(session: BaselineSession, build: IntendedBuild | StoreBuild, role: str) -> InstallOutcome:
    try:
        record = session.install(build, role)
    except BuildInstallError as exc:
        return exc.outcome or apk_install.failure(exc.state, str(exc))
    status = "already_installed" if record.action == "kept" else "installed"
    return InstallOutcome(status=status, installer=record.installer, seconds=record.seconds,
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
    """Whether a role device could serve a run now: ready, startable, busy, unavailable, unreachable or unconfigured.

    cause is a machine-readable reason when one is known, such as `unauthorized` for a phone awaiting debugging approval.
    """

    role: str
    state: str
    detail: str = ""
    cause: str = ""


# Report each required role's readiness from read-only API calls and the claim store, never claiming, restoring,
# starting, authorizing or installing; only the roles asked for are looked up.
def role_readiness(config: Any, roles: Sequence[str], *, client: CorelliumClient | None = None,
                   store: ClaimStore | None = None, timeout: float = 10.0) -> list[RoleReadiness]:
    try:
        profile = load_profile(config.device.corellium, PLATFORM)
    except CorelliumError as exc:
        return [RoleReadiness(role, "unconfigured", str(exc)) for role in roles]
    phone_roles = [role for role in roles if role in config.device.physical_roles]
    readiness = [_phone_role_readiness(config, profile, role) for role in phone_roles]
    vm_roles = [role for role in roles if role not in phone_roles]
    if not vm_roles:
        return readiness
    try:
        client = client or CorelliumClient.from_env()
        pending = (store or ClaimStore(client.api_url)).pending()
    except (CorelliumError, OSError) as exc:
        return readiness + [RoleReadiness(role, "unreachable", str(exc)) for role in vm_roles]
    owned = {row["instance_id"] for row in pending}
    for role in vm_roles:
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


# Report the phone serving a role: busy while another run or install holds it, otherwise its read-only adb state.
def _phone_role_readiness(config: Any, profile: Any, role: str) -> RoleReadiness:
    holder = phone_lock_holder()
    if holder is not None:
        return RoleReadiness(role, "busy", f"the phone is in use by {holder.get('purpose')}")
    state, detail, cause = phone_readiness(config.device.adb_path, config.device.adb_serial,
                                           _vm_addresses((), profile.role_settings))
    return RoleReadiness(role, state, detail, cause)


# Report whether the app is on the phone serving the physical roles, from read-only adb calls that never connect or
# install anything; None when the phone is not ready to be asked.
def phone_has_app(config: Any, package: str) -> bool | None:
    profile = load_profile(config.device.corellium, PLATFORM)
    serial = ready_phone_serial(config.device.adb_path, config.device.adb_serial, _vm_addresses((), profile.role_settings))
    if serial is None:
        return None
    return is_installed(AdbClient(config.device.adb_path, serial), package)


# Report whether an inventory instance is one the role could select, so its ownership explains an empty selection.
def _could_serve(profile: Any, role: str, instance_id: str) -> bool:
    baseline = profile.baselines.get(instance_id)
    pinned = profile.roles[role].instance
    return baseline is not None and baseline.rooted is ROLES[role] and (not pinned or pinned == instance_id)
