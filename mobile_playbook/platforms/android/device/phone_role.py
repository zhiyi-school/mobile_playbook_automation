"""
Connects the physical phone that serves a device role beside Corellium VMs, by its serial or over Wi-Fi, never taking
a VM's adb address for the phone.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from functools import partial
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import (
    ABSENT,
    UNAUTHORIZED,
    AdbConnectionError,
    AdbConnectPolicy,
    Authorizer,
    connect_ready,
)
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient
from mobile_playbook.platforms.android.device.pool import PHYSICAL_PHONE, RoleDevice
from mobile_playbook.platforms.android.device.transports import attach_transports
from mobile_playbook.platforms.android.device.wifi import WIFI_SERIAL, connect_over_wifi, is_network_serial, same_phone
from mobile_playbook.platforms.corellium.client import ROOTED_ANDROID_BUILDS
from mobile_playbook.platforms.corellium.selection import ROLES

logger = logging.getLogger(__name__)

# A phone is expected to be attached already, so a missing one fails within a minute rather than a VM's boot budget.
PHONE_POLICY = AdbConnectPolicy(timeout=60, reconnect_pause=5, unauthorized_grace=5, poll=1)


# Return the phone's adb serial: the configured one, or for "wifi" its Wi-Fi connection, which never resolves to a
# serial in exclude.
def resolve_phone_serial(adb_path: str, configured: str | None, exclude: Collection[str] = ()) -> str:
    configured = (configured or "").strip()
    if configured == WIFI_SERIAL:
        serial = connect_over_wifi(adb_path, exclude=exclude)
        if serial is None:
            raise AdbConnectionError(
                "device.adb_serial is wifi, but no Wi-Fi connection to the phone was found or could be made. Connect "
                "the phone by USB while it is on the same Wi-Fi network as this host, or set device.adb_serial to "
                "its serial.",
                serial=WIFI_SERIAL,
                state=ABSENT,
            )
    else:
        serial = configured
    if serial in exclude:
        raise AdbConnectionError(f"{serial} is a Corellium VM's adb address, not the phone's.", serial=serial, state=ABSENT)
    return serial


# Return an authorizer that fails with what the person at the phone must do; only they can approve its prompt.
def _ask_at_the_phone(serial: str) -> Authorizer:
    # Fail with the steps the person holding the phone must take.
    def ask(host: AdbClient, phone: AdbClient) -> None:
        raise AdbConnectionError(
            f"adb on the phone {serial} is unauthorized. Unlock the phone and tap Allow on its USB debugging prompt, "
            "ticking Always allow from this computer.",
            serial=serial,
            state=UNAUTHORIZED,
        )

    return ask


# Connect the phone serving the role and wait until it is authorized, booted and in the role's root state, returning
# its role device. With device.adb_backup on, its other connections, or a Wi-Fi one made for a phone on USB, serve as
# backups that commands and reconnects move to when the one in use drops.
def connect_phone(config: Any, role: str, *, exclude: Collection[str] = (), policy: AdbConnectPolicy | None = None) -> RoleDevice:
    if role not in ROLES:
        raise ValueError(f"Unknown device role {role!r}")
    adb_path = config.device.adb_path
    serial = resolve_phone_serial(adb_path, config.device.adb_serial, exclude)
    policy = policy or PHONE_POLICY
    build_type = connect_ready(adb_path, serial, rooted=ROLES[role], policy=policy, authorize=_ask_at_the_phone(serial),
                               connect=is_network_serial(serial))
    adb = AdbClient(adb_path, serial)
    if config.device.adb_backup:
        attach_transports(adb, exclude=exclude)
    logger.info("Connected the %s device: physical phone %s (%s build).", role, serial, build_type)
    return RoleDevice(role, serial, serial, ROLES[role], AndroidDeviceClient(config, adb), device_type=PHYSICAL_PHONE,
                      reconnect=partial(_reconnect_phone, adb, ROLES[role], policy))


# Move to any ready connection to the phone, then wait until the connection in use is authorized, booted and in the
# expected root state; without backups this is the phone's one serial.
def _reconnect_phone(adb: AdbClient, rooted: bool, policy: AdbConnectPolicy) -> str:
    if adb.transports is not None:
        adb.transports.switch()
    serial = adb.target_serial()
    return connect_ready(adb.adb_path, serial, rooted=rooted, policy=policy, authorize=_ask_at_the_phone(serial),
                         connect=is_network_serial(serial))


# Return the phone's third-party (user-installed) packages; anything not listed before a run was installed for it.
def third_party_packages(adb: AdbClient) -> set[str]:
    code, out, err = adb.run(["shell", "pm", "list", "packages", "-3"])
    if code != 0:
        raise AdbConnectionError(f"Could not list the phone's packages: {(err or out).strip()}", serial=adb.serial or "",
                                 state=ABSENT)
    return {line.strip().removeprefix("package:") for line in out.splitlines() if line.strip().startswith("package:")}


# Uninstall a package from the phone, returning whether it is gone, including when it was not installed.
def uninstall(adb: AdbClient, package: str) -> bool:
    code, out, err = adb.run(["uninstall", package], timeout=60)
    output = f"{out}\n{err}".lower()
    gone = code == 0 and "failure" not in output
    if not gone:
        # Android 12 answers DELETE_FAILED_INTERNAL_ERROR for a package that is not installed, so a failure is
        # rechecked; only a listing the phone answered can show it gone, never an unreachable phone.
        listed, out, _ = adb.run(["shell", "pm", "list", "packages", package])
        gone = listed == 0 and f"package:{package}" not in {line.strip() for line in out.splitlines()}
    logger.debug("android phone: uninstall %s exited %s gone=%s (%s)", package, code, gone, output.strip()[:200])
    return gone


# Return the phone's model name, to tell several test phones apart in reports.
def phone_model(adb: AdbClient) -> str | None:
    code, out, _ = adb.run(["shell", "getprop", "ro.product.model"])
    model = out.strip() if code == 0 else ""
    return model or None


# Return the configured phone's readiness, why, and its adb state when that is the cause, from read-only adb calls
# that never connect, switch the phone to Wi-Fi or install anything: ready, unavailable or unreachable.
def phone_readiness(adb_path: str, configured: str | None, exclude: Collection[str] = ()) -> tuple[str, str, str]:
    return _phone_status(adb_path, configured, exclude)[:3]


# Return the adb serial of the configured phone when it is ready, from the same read-only calls as phone_readiness.
def ready_phone_serial(adb_path: str, configured: str | None, exclude: Collection[str] = ()) -> str | None:
    state, _, _, serial = _phone_status(adb_path, configured, exclude)
    return serial if state == "ready" else None


# Return the phone's readiness, why, its adb state when that is the cause, and the serial it was found at.
def _phone_status(adb_path: str, configured: str | None, exclude: Collection[str]) -> tuple[str, str, str, str]:
    host = AdbClient(adb_path)
    if not host.is_available():
        return "unreachable", "adb is not available on this host", "", ""
    states = {serial: state for serial, state in host.device_states().items() if serial not in exclude}
    configured = (configured or "").strip()
    if configured == WIFI_SERIAL:
        # A phone on USB alone still counts: the run switches it to Wi-Fi itself.
        network = [serial for serial in states if is_network_serial(serial)]
        if len(network) > 1 and all(states[serial] == "device" for serial in network) and same_phone(adb_path, network):
            network = network[:1]
        candidates = network or [serial for serial in states if not is_network_serial(serial)]
        if len(candidates) != 1:
            return "unavailable", "no phone is connected" if not candidates else "several phones are connected", "", ""
        serial = candidates[0]
    else:
        serial = configured
    state = states.get(serial)
    if state is None:
        return "unavailable", "the phone is not connected", ABSENT, serial
    if state == UNAUTHORIZED:
        transport = "Wi-Fi adb connection" if is_network_serial(serial) else "adb connection"
        return "unavailable", (
            f"the phone's {transport} is waiting for it to allow debugging from this host; unlock the phone and tap "
            "Allow on its USB debugging prompt, ticking Always allow from this computer"
        ), UNAUTHORIZED, serial
    if state != "device":
        return "unavailable", f"the phone is {state}", state, serial
    code, out, _ = AdbClient(adb_path, serial).run(["shell", "getprop", "ro.build.type"])
    if code == 0 and out.strip() in ROOTED_ANDROID_BUILDS:
        return "unavailable", f"the phone runs a rooted {out.strip()} build", "", serial
    return "ready", "", "", serial
