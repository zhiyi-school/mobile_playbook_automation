"""
Holds a run's Android device clients by role, each bound to one exact claimed instance and adb serial.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import READY, AdbConnectPolicy, Authorizer, connect_ready
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient

logger = logging.getLogger(__name__)

CORELLIUM_VM = "corellium_vm"
PHYSICAL_PHONE = "physical_phone"


class RoleUnavailable(RuntimeError):
    """The run has no prepared device for a role, and another role's device never stands in for it."""


@dataclass(frozen=True)
class RoleDevice:
    """One role's device: its exact instance (a phone's serial), adb serial, expected root state, client and type."""

    role: str
    instance_id: str
    serial: str
    rooted: bool
    client: AndroidDeviceClient
    authorize: Authorizer | None = None
    device_type: str = CORELLIUM_VM
    reconnect: Callable[[], object] | None = None


class AndroidDevicePool:
    """The device clients of one run keyed by role; recovery reconnects a role to its own instance only."""

    # Keep the role devices, rejecting a role bound to the wrong entry or two roles sharing an instance or serial.
    def __init__(self, devices: Mapping[str, RoleDevice], *, adb_path: str = "adb", policy: AdbConnectPolicy | None = None):
        if any(device.role != role for role, device in devices.items()):
            raise ValueError("Each role device must be stored under its own role")
        if len({device.instance_id for device in devices.values()}) != len(devices) or len(
            {device.serial for device in devices.values()}
        ) != len(devices):
            raise ValueError("Device roles must use distinct instances and adb serials")
        self._devices = dict(devices)
        self.adb_path = adb_path
        self.policy = policy

    # Return the roles this pool holds devices for.
    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self._devices)

    # Return the role's device, or fail without substituting another role's device.
    def device(self, role: str) -> RoleDevice:
        device = self._devices.get(role)
        if device is None:
            prepared = ", ".join(self._devices) or "none"
            raise RoleUnavailable(
                f"This run has no prepared {role} device (prepared: {prepared}); risks that need it never run on another device."
            )
        return device

    # Return the role's client after confirming adb still lists its device as ready, reconnecting it when not.
    def ready(self, role: str) -> AndroidDeviceClient:
        device = self.device(role)
        state = AdbClient(self.adb_path).device_states().get(device.serial)
        if state != READY:
            logger.warning("The %s device %s (instance %s) is %s; reconnecting to the same instance.",
                           role, device.serial, device.instance_id, state or "not connected")
            self.recover(role)
        return device.client

    # Reconnect adb to the role's own serial and recheck readiness; this never selects, restores or stops a device.
    def recover(self, role: str) -> RoleDevice:
        device = self.device(role)
        if device.reconnect is not None:
            device.reconnect()
        else:
            connect_ready(self.adb_path, device.serial, rooted=device.rooted, policy=self.policy, authorize=device.authorize)
        return device

    # Return each role's instance_id, serial and device type, for events and reports.
    def identities(self) -> dict[str, dict[str, str]]:
        return {role: {"instance_id": device.instance_id, "serial": device.serial, "device_type": device.device_type}
                for role, device in self._devices.items()}

    # Quit every role's session, continuing past failures so one role cannot block another.
    def quit(self) -> None:
        for role, device in self._devices.items():
            try:
                device.client.quit()
            except Exception as exc:
                logger.warning("Could not quit the %s device session: %s", role, exc)

    # Quit every session and disconnect adb from every VM's serial; a phone stays connected for its next user, and
    # powering off belongs to the VM lifecycle.
    def release(self) -> None:
        self.quit()
        host = AdbClient(self.adb_path)
        for role, device in self._devices.items():
            if device.device_type == PHYSICAL_PHONE:
                continue
            code, out, err = host.run(["disconnect", device.serial])
            logger.debug("android pool: disconnect %s (%s) exited %s (%s)", device.serial, role, code, (out or err)[:200])
