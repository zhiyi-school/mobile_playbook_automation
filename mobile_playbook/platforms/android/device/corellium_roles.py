"""
Connects adb to each role's claimed and prepared Corellium instance and returns the run's role device pool.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectPolicy, connect_ready
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient
from mobile_playbook.platforms.android.device.corellium import adb_serial
from mobile_playbook.platforms.android.device.debugging_prompt import DebuggingAuthorizer, WordRecognizer
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool, RoleDevice
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumError, CorelliumInstance
from mobile_playbook.platforms.corellium.selection import ROLES, ResolvedDevice

logger = logging.getLogger(__name__)


# Connect every claimed role to its own prepared instance, approving the debugging prompt when adb is unauthorized,
# and disconnect the roles already connected when a later one fails.
def connect_claimed(
    config: Any,
    devices: Sequence[ResolvedDevice],
    prepared: Mapping[str, CorelliumInstance],
    client: CorelliumClient,
    *,
    diagnostics_dir: Path,
    role_settings: Mapping[str, Mapping[str, Any]] | None = None,
    policy: AdbConnectPolicy | None = None,
    recognizer: WordRecognizer | None = None,
) -> AndroidDevicePool:
    adb_path = config.device.adb_path
    planned: dict[str, tuple[CorelliumInstance, str]] = {}
    for device in devices:
        role = device.role
        if role not in ROLES or role in planned:
            raise CorelliumError(f"Unknown or repeated device role {role!r}")
        instance = prepared.get(role)
        if instance is None:
            raise CorelliumError(f"No prepared instance for the {role} role")
        if instance.id != device.instance.id:
            raise CorelliumError(f"The prepared {role} instance {instance.id} is not the claimed instance {device.instance.id}")
        if instance.state != "on":
            raise CorelliumError(f"The {role} instance {instance.id} is {instance.state}, not on")
        if instance.rooted != ROLES[role]:
            raise CorelliumError(f"The {role} role cannot use instance {instance.describe()}")
        planned[role] = (instance, adb_serial(instance, dict((role_settings or {}).get(role) or {})))
    if len({serial for _, serial in planned.values()}) != len(planned):
        raise CorelliumError("Device roles resolve to the same adb address")

    connected: dict[str, RoleDevice] = {}
    try:
        for role, (instance, serial) in planned.items():
            authorize = DebuggingAuthorizer(client, instance.id, serial, diagnostics_dir=diagnostics_dir, recognizer=recognizer)
            build_type = connect_ready(adb_path, serial, rooted=ROLES[role], policy=policy, authorize=authorize)
            logger.info("Connected the %s device: Corellium instance %s at %s (%s build).", role, instance.id, serial, build_type)
            connected[role] = RoleDevice(role, instance.id, serial, ROLES[role],
                                         AndroidDeviceClient(config, AdbClient(adb_path, serial)), authorize)
    except BaseException:
        host = AdbClient(adb_path)
        for _, serial in planned.values():
            host.run(["disconnect", serial])
        raise
    return AndroidDevicePool(connected, adb_path=adb_path, policy=policy)
