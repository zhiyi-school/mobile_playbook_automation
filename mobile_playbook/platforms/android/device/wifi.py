"""
Finds a phone's Wi-Fi adb connection for `adb_serial: wifi`, switching adb on the phone to Wi-Fi over USB when needed.
"""

from __future__ import annotations

import logging
import re
import time

from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

WIFI_SERIAL = "wifi"
WIFI_PORT = 5555
CONNECT_WAIT_SECONDS = 10.0
POLL_SECONDS = 1.0

_IPV4_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")


# Return the serial of the phone's Wi-Fi adb connection, enabling and connecting it through USB when needed, or None.
def connect_over_wifi(adb_path: str = "adb", *, wait: float = CONNECT_WAIT_SECONDS, poll: float = POLL_SECONDS) -> str | None:
    host = AdbClient(adb_path)
    devices = host.connected_devices()
    network = sorted(serial for serial in devices if is_network_serial(serial))
    if len(network) == 1:
        logger.debug("android wifi: using existing Wi-Fi connection %s", network[0])
        return network[0]
    if network:
        logger.warning("Several Android devices are connected over Wi-Fi; set device.adb_serial to the one to test.")
        return None
    usb = [serial for serial in devices if not is_network_serial(serial)]
    if len(usb) != 1:
        logger.warning(
            "No Android Wi-Fi connection exists and %s USB devices are connected; connect exactly one phone by USB "
            "so its Wi-Fi address can be found.",
            len(usb),
        )
        return None

    phone = AdbClient(adb_path, usb[0])
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
    serial = f"{address}:{port}"

    # adbd restarts after tcpip, so the first connect attempts can fail until it is listening again.
    deadline = time.monotonic() + wait
    while True:
        code, out, err = host.run(["connect", serial])
        logger.debug("android wifi: connect %s exited %s (%s)", serial, code, (out or err)[:200])
        if serial in host.connected_devices():
            logger.info("Connected to the Android phone over Wi-Fi at %s.", serial)
            return serial
        if time.monotonic() >= deadline:
            logger.warning("Could not connect to the Android phone over Wi-Fi at %s: %s", serial, (out or err).strip())
            return None
        time.sleep(poll)


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
