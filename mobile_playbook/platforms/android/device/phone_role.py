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
from mobile_playbook.platforms.android.device.wifi import WIFI_SERIAL, connect_over_wifi, is_network_serial
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
# its role device; reconnecting later reuses the same serial.
def connect_phone(config: Any, role: str, *, exclude: Collection[str] = (), policy: AdbConnectPolicy | None = None) -> RoleDevice:
    if role not in ROLES:
        raise ValueError(f"Unknown device role {role!r}")
    adb_path = config.device.adb_path
    serial = resolve_phone_serial(adb_path, config.device.adb_serial, exclude)
    ready = partial(connect_ready, adb_path, serial, rooted=ROLES[role], policy=policy or PHONE_POLICY,
                    authorize=_ask_at_the_phone(serial), connect=is_network_serial(serial))
    build_type = ready()
    logger.info("Connected the %s device: physical phone %s (%s build).", role, serial, build_type)
    return RoleDevice(role, serial, serial, ROLES[role], AndroidDeviceClient(config, AdbClient(adb_path, serial)),
                      device_type=PHYSICAL_PHONE, reconnect=ready)

