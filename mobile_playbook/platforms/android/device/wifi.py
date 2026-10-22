"""
Finds a phone's Wi-Fi adb connection for `adb_serial: wifi` or as a backup to USB, switching adb on the phone to Wi-Fi
over USB when needed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import ABSENT, READY, UNAUTHORIZED, hardware_serial

logger = logging.getLogger(__name__)

WIFI_SERIAL = "wifi"
WIFI_PORT = 5555
CONNECT_WAIT_SECONDS = 10.0
POLL_SECONDS = 1.0

_IPV4_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")


# Return the serial of the phone's Wi-Fi adb connection, enabling and connecting it through USB when needed, or None.
# A connection still waiting for the phone to approve debugging is returned too, after the wait, so adb reports it as
# unauthorized rather than missing. Serials in exclude, such as Corellium VM addresses, are never taken for the phone.
def connect_over_wifi(adb_path: str = "adb", *, wait: float = CONNECT_WAIT_SECONDS, poll: float = POLL_SECONDS,
                      exclude: Collection[str] = ()) -> str | None:
    host = AdbClient(adb_path)
    states = {serial: state for serial, state in host.device_states().items() if serial not in exclude}
    network = sorted(serial for serial in states if is_network_serial(serial))
    ready = [serial for serial in network if states[serial] == READY]
    if len(ready) > 1 and same_phone(adb_path, ready):
        ready = sorted(ready, key=lambda serial: ("._adb-tls-connect." in serial, serial))[:1]
    if len(ready) == 1:
        logger.debug("android wifi: using existing Wi-Fi connection %s", ready[0])
        return ready[0]
    if ready:
        logger.warning("Several Android devices are connected over Wi-Fi; set device.adb_serial to the one to test.")
        return None
    pending = [serial for serial in network if states[serial] == UNAUTHORIZED]
    if len(pending) == 1:
        return _await_connection(host, pending[0], wait, poll)
    usb = [serial for serial in states if not is_network_serial(serial) and states[serial] == READY]
    if len(usb) != 1:
        logger.warning(
            "No Android Wi-Fi connection exists and %s USB devices are connected; connect exactly one phone by USB "
            "so its Wi-Fi address can be found.",
            len(usb),
        )
        return None

    return wifi_from_usb(adb_path, usb[0], wait=wait, poll=poll)


# Return the Wi-Fi serial of the phone connected over USB, switching adb on it to Wi-Fi when it is not listening there
# yet, once that connection is ready or still awaiting approval; None when the phone has no Wi-Fi connection.
def wifi_from_usb(adb_path: str, usb_serial: str, *, wait: float = CONNECT_WAIT_SECONDS, poll: float = POLL_SECONDS) -> str | None:
    phone = AdbClient(adb_path, usb_serial)
    address = wifi_address(phone)
    if address is None:
        logger.warning("The Android phone on USB has no Wi-Fi address; join it to the same network as this host.")
        return None
    port = listening_port(phone)
    if port is None:
        code, out, err = phone.run(["tcpip", str(WIFI_PORT)])
        logger.debug("android wifi: tcpip %s exited %s (%s)", WIFI_PORT, code, (out or err)[:200])
        if code != 0:
            logger.warning("Could not switch adb on the Android phone to Wi-Fi: %s", (err or out or f"exit {code}").strip())
            return None
        port = WIFI_PORT
    return _await_connection(AdbClient(adb_path), f"{address}:{port}", wait, poll)


# Report whether every serial is a ready connection to one and the same phone, by the hardware serial each reports.
def same_phone(adb_path: str, serials: Collection[str]) -> bool:
    found = {hardware_serial(AdbClient(adb_path, serial)) for serial in serials}
    return len(found) == 1 and None not in found


# Connect to the phone's Wi-Fi serial and wait until it is ready, returning it; one still unauthorized at the end of
# the wait is returned with a warning naming the prompt, and any other failure returns None.
def _await_connection(host: AdbClient, serial: str, wait: float, poll: float) -> str | None:
    # adbd restarts after tcpip, so the first connect attempts can fail until it is listening again. Approval of
    # debugging is only ever waited for and left to the person at the phone.
    deadline = time.monotonic() + wait
    state, reply = host.device_states().get(serial, ABSENT), ""
    while True:
        if state not in (READY, UNAUTHORIZED):
            code, out, err = host.run(["connect", serial])
            reply = (out or err).strip()
            logger.debug("android wifi: connect %s exited %s (%s)", serial, code, reply[:200])
            state = host.device_states().get(serial, ABSENT)
        if state == READY:
            logger.info("Connected to the Android phone over Wi-Fi at %s.", serial)
            return serial
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
        state = host.device_states().get(serial, ABSENT)
    if state == UNAUTHORIZED:
        logger.warning(
            "The Android phone's Wi-Fi adb connection %s is waiting for the phone to allow debugging from this host. "
            "Unlock the phone and tap Allow on its USB debugging prompt, ticking Always allow from this computer.",
            serial,
        )
        return serial
    logger.warning("Could not connect to the Android phone over Wi-Fi at %s: %s", serial, reply or state)
    return None


# Report whether an adb serial is a network connection rather than USB.
def is_network_serial(serial: str) -> bool:
    return (":" in serial and not serial.startswith("emulator-")) or "._adb-tls-connect." in serial


# Return the phone's IPv4 address on wlan0, or None when it has none.
def wifi_address(phone: AdbClient) -> str | None:
    code, out, _ = phone.run(["shell", "ip", "-f", "inet", "addr", "show", "wlan0"])
    match = _IPV4_RE.search(out or "") if code == 0 else None
    return match.group(1) if match else None


# Return the TCP port adb on the phone already listens on, or None when it listens on USB only.
def listening_port(phone: AdbClient) -> int | None:
    code, out, _ = phone.run(["shell", "getprop", "service.adb.tcp.port"])
    value = (out or "").strip() if code == 0 else ""
    return int(value) if value.isdigit() and int(value) > 0 else None
