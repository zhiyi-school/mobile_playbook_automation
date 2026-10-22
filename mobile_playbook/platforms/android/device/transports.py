"""
Tracks every adb connection to one physical phone, matched by the hardware serial it reports, so commands move to a
backup connection, such as Wi-Fi beside USB, when the one in use drops.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import READY, hardware_serial
from mobile_playbook.platforms.android.device.wifi import CONNECT_WAIT_SECONDS, POLL_SECONDS, is_network_serial, wifi_from_usb
from mobile_playbook.reporting.run_events import append_event

logger = logging.getLogger(__name__)

# adb restarts on the phone after switching it to Wi-Fi, so its USB connection drops briefly before returning.
USB_RETURN_SECONDS = 15.0

Switched = Callable[[str, str], None]


class PhoneTransports:
    """The adb connections to one phone; commands use the current one until it drops, then another to the same phone."""

    # Keep the phone's hardware serial and its connections, the first being the one in use.
    def __init__(self, adb_path: str, hardware_id: str, serials: Collection[str], *, exclude: Collection[str] = (),
                 on_switch: Switched | None = None):
        self.adb_path = adb_path
        self.hardware_id = hardware_id
        self.serials = list(dict.fromkeys(serials))
        if not self.serials:
            raise ValueError("A phone needs at least one adb connection")
        self.current = self.serials[0]
        self.exclude = frozenset(exclude)
        self.on_switch = on_switch

    # Return the connections other than the one in use.
    @property
    def backups(self) -> list[str]:
        return [serial for serial in self.serials if serial != self.current]

    # Return the connection to use: the current one while adb lists it ready, otherwise another ready connection that
    # reports this phone's hardware serial, reconnecting known Wi-Fi addresses first; None when none is ready.
    def switch(self) -> str | None:
        host = AdbClient(self.adb_path)
        states = host.device_states()
        if states.get(self.current) == READY:
            return self.current
        lost = [serial for serial in self.backups if states.get(serial) != READY and _reconnectable(serial)]
        for serial in lost:
            code, out, err = host.run(["connect", serial])
            logger.debug("android transports: connect %s exited %s (%s)", serial, code, (out or err)[:200])
        if lost:
            states = host.device_states()
        known = [serial for serial in self.backups if states.get(serial) == READY]
        # A connection made after the run started, such as the phone plugged back in, can serve too once it proves to
        # be this phone; a Corellium VM's address never does.
        others = [serial for serial, state in states.items()
                  if state == READY and serial not in self.serials and serial not in self.exclude]
        for serial in known + others:
            if hardware_serial(AdbClient(self.adb_path, serial)) != self.hardware_id:
                continue
            previous, self.current = self.current, serial
            if serial not in self.serials:
                self.serials.append(serial)
            logger.warning("The adb connection %s to the phone dropped; continuing over %s.", previous, serial)
            if self.on_switch is not None:
                self.on_switch(previous, serial)
            return serial
        logger.debug("android transports: no ready connection to the phone %s among %s", self.hardware_id, states)
        return None


# Return the phone's connections with primary in use, finding every other ready connection to the same phone and, for
# a phone on USB with none, switching adb on it to Wi-Fi as the backup; None when the phone reports no hardware serial.
def open_transports(adb_path: str, primary: str, *, exclude: Collection[str] = (),
                    wifi_wait: float = CONNECT_WAIT_SECONDS, poll: float = POLL_SECONDS,
                    usb_return: float = USB_RETURN_SECONDS) -> PhoneTransports | None:
    hardware_id = hardware_serial(AdbClient(adb_path, primary))
    if hardware_id is None:
        logger.warning("The phone %s reports no hardware serial, so no backup adb connection can be matched to it.", primary)
        return None
    skip = set(exclude) | {primary}
    backups = _same_phone(adb_path, hardware_id, skip)
    if not is_network_serial(primary) and not any(is_network_serial(serial) for serial in backups):
        wifi = wifi_from_usb(adb_path, primary, wait=wifi_wait, poll=poll)
        _wait_ready(adb_path, primary, usb_return, poll)
        if wifi is not None and wifi not in skip:
            backups = _same_phone(adb_path, hardware_id, skip)
    transports = PhoneTransports(adb_path, hardware_id, [primary, *backups], exclude=exclude)
    if backups:
        logger.info("The phone %s also has the backup adb connection %s.", primary, ", ".join(backups))
    else:
        logger.warning("The phone %s has no backup adb connection; the run stops if this connection drops. Keep the "
                       "phone on the same Wi-Fi network as this host to add one.", primary)
    return transports


# Give the client failover across the phone's connections, unless its serial is unset, unresolved or not ready yet.
def attach_transports(adb: AdbClient, *, exclude: Collection[str] = ()) -> PhoneTransports | None:
    serial = adb.target_serial()
    if not serial or AdbClient(adb.adb_path).device_states().get(serial) != READY:
        logger.debug("android transports: no ready serial (%s) to add a backup connection to", serial)
        return None
    adb.transports = open_transports(adb.adb_path, serial, exclude=exclude)
    return adb.transports


# Return a callback recording each move between the phone's connections as a device_transport_switched run event.
def switch_recorder(event_dir: Path, role: str | None = None) -> Switched:
    # Append the event naming the dropped connection and the one now in use.
    def record(previous: str, current: str) -> None:
        fields = {"role": role} if role is not None else {}
        append_event(event_dir, "device_transport_switched", device_type="physical_phone", previous_serial=previous,
                     serial=current, **fields)

    return record


# Return the ready serials, other than those in skip, that report the phone's hardware serial.
def _same_phone(adb_path: str, hardware_id: str, skip: Collection[str]) -> list[str]:
    states = AdbClient(adb_path).device_states()
    return [serial for serial, state in states.items()
            if state == READY and serial not in skip and hardware_serial(AdbClient(adb_path, serial)) == hardware_id]


# Report whether adb can reconnect the serial by address; USB and mDNS connections return on their own.
def _reconnectable(serial: str) -> bool:
    return is_network_serial(serial) and ":" in serial


# Wait until adb lists the serial as ready again, up to the timeout.
def _wait_ready(adb_path: str, serial: str, timeout: float, poll: float) -> None:
    host = AdbClient(adb_path)
    deadline = time.monotonic() + timeout
    while host.device_states().get(serial) != READY and time.monotonic() < deadline:
        time.sleep(poll)
