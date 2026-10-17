"""
Picks and connects adb to an Android virtual device on Corellium for `device.provider: corellium`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import boot_completed
from mobile_playbook.platforms.corellium.client import (
    DEFAULT_BOOT_TIMEOUT_SECONDS,
    CorelliumClient,
    CorelliumError,
    CorelliumInstance,
    ensure_instance_on,
    find_instance,
    stop_instance,
)

logger = logging.getLogger(__name__)

PLATFORM = "android"
# Corellium serves adb for Android instances on this port of the instance's services address, over the project VPN.
DEFAULT_ADB_PORT = 5001
POLL_SECONDS = 2.0


@dataclass(frozen=True)
class CorelliumConnection:
    """The Corellium instance a device client uses, its adb serial, and whether to power it off when testing ends."""

    instance: CorelliumInstance
    serial: str
    auto_stop: bool


# Pick the configured instance and return its connection; with start, power it on when auto_start allows and wait
# for Android to boot, otherwise only read its state.
def connect_corellium(
    adb_path: str,
    settings: dict[str, Any],
    *,
    start: bool = True,
    client: CorelliumClient | None = None,
    poll: float = POLL_SECONDS,
) -> CorelliumConnection:
    client = client or CorelliumClient.from_env()
    instance = find_instance(client, settings, PLATFORM)
    logger.info("Using Corellium instance %s.", instance.describe())
    timeout = float(settings.get("boot_timeout_seconds") or DEFAULT_BOOT_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    auto_stop = bool(settings.get("auto_stop", False))
    started = False
    try:
        if start and instance.state != "on":
            if instance.state in ("off", "paused") and not settings.get("auto_start", True):
                raise CorelliumError(
                    f"Corellium instance {instance.describe()} is not on and device.corellium.auto_start is false; "
                    "start it in Corellium first."
                )
            started = instance.state in ("off", "paused")
            instance = ensure_instance_on(client, instance.id, timeout=timeout, poll=poll)
        connection = CorelliumConnection(instance=instance, serial=adb_serial(instance, settings), auto_stop=auto_stop)
        # An instance that is not on cannot answer adb; callers that did not start it see it as not connected.
        if instance.state == "on":
            _connect_adb(adb_path, connection.serial, deadline, wait=start, poll=poll)
        return connection
    except CorelliumError:
        # An instance this call started would otherwise keep running, and billing, with no run left to stop it.
        if started and auto_stop:
            stop_instance(client, instance)
        raise


# Power the connection's instance off when its settings ask for that at the end of testing.
def stop_corellium(connection: CorelliumConnection, client: CorelliumClient | None = None) -> None:
    if not connection.auto_stop:
        logger.debug("android corellium: auto_stop is off; leaving %s running", connection.instance.name)
        return
    try:
        client = client or CorelliumClient.from_env()
    except CorelliumError as exc:
        logger.warning("Could not power off Corellium instance %s: %s", connection.instance.name, exc)
        return
    stop_instance(client, connection.instance)


# Reconnect adb to the connection's own instance and wait for it to boot, without selecting or starting an instance.
def reconnect_corellium(adb_path: str, connection: CorelliumConnection, settings: dict[str, Any], *, poll: float = POLL_SECONDS) -> None:
    timeout = float(settings.get("boot_timeout_seconds") or DEFAULT_BOOT_TIMEOUT_SECONDS)
    _connect_adb(adb_path, connection.serial, time.monotonic() + timeout, wait=True, poll=poll)


# Return the adb address to use: device.corellium.adb_address, else the instance's services address with the adb
# port it reports, device.corellium.adb_port, or the default port, in that order.
def adb_serial(instance: CorelliumInstance, settings: dict[str, Any]) -> str:
    configured = str(settings.get("adb_address") or "").strip()
    if configured:
        return configured
    if not instance.service_ip:
        raise CorelliumError(
            f"Corellium instance {instance.id} reports no services address; set device.corellium.adb_address."
        )
    port = instance.adb_port or int(settings.get("adb_port") or DEFAULT_ADB_PORT)
    return f"{instance.service_ip}:{port}"


# Connect adb to the serial and, when waiting, retry until Android has booted or the deadline passes.
def _connect_adb(adb_path: str, serial: str, deadline: float, *, wait: bool, poll: float) -> None:
    host = AdbClient(adb_path)
    phone = AdbClient(adb_path, serial)
    while True:
        if serial not in host.connected_devices():
            code, out, err = host.run(["connect", serial])
            logger.debug("android corellium: connect %s exited %s (%s)", serial, code, (out or err)[:200])
        if serial in host.connected_devices() and boot_completed(phone):
            logger.debug("android corellium: adb connected to %s", serial)
            return
        if not wait:
            return
        if time.monotonic() >= deadline:
            raise CorelliumError(
                f"Could not reach the Corellium instance over adb at {serial} before the boot timeout. "
                "Check that this host is connected to the VPN of the instance's project."
            )
        time.sleep(poll)
