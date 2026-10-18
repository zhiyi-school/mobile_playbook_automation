from __future__ import annotations

import subprocess

import pytest

from mobile_playbook.platforms.android.device import adb as adb_module
from mobile_playbook.platforms.android.device import wifi
from mobile_playbook.platforms.android.device.adb import AdbClient

USB = "VVY0000000000001"
ADDRESS = "10.0.0.5"


class FakePhone:
    """Simulates adb on the host and one phone that can be switched from USB to Wi-Fi."""

    def __init__(self, *, usb=True, wifi_connected=False, tcp_port="", address=ADDRESS, vms=()):
        self.vms = list(vms)
        self.usb = usb
        self.wifi_connected = wifi_connected
        self.tcp_port = tcp_port
        self.address = address
        self.commands: list[list[str]] = []

    def run(self, argv, **kwargs):
        reply = self.reply(argv[1:])
        return subprocess.CompletedProcess(argv, reply[0], stdout=reply[1], stderr=reply[2])

    def reply(self, argv):
        self.commands.append(list(argv))
        target, args = (argv[1], argv[2:]) if argv[:1] == ["-s"] else (None, argv)
        if args == ["devices"]:
            rows = ([f"{USB}\tdevice"] if self.usb else []) + ([f"{ADDRESS}:5555\tdevice"] if self.wifi_connected else [])
            rows += [f"{vm}\tdevice" for vm in self.vms]
            return 0, "List of devices attached\n" + "\n".join(rows), ""
        if args[0] == "version":
            return 0, "Android Debug Bridge version 1.0.41", ""
        if args[0] == "connect":
            if self.tcp_port:
                self.wifi_connected = True
                return 0, f"connected to {args[1]}", ""
            return 1, "", f"failed to connect to {args[1]}"
        if target not in (USB, f"{ADDRESS}:5555"):
            return 1, "", f"adb: device '{target}' not found"
        if args[:2] == ["shell", "ip"]:
            return (0, f"47: wlan0: <UP>\n    inet {self.address}/24 brd 10.0.0.255 scope global wlan0", "") if self.address else (0, "", "")
        if args[:3] == ["shell", "getprop", "service.adb.tcp.port"]:
            return 0, self.tcp_port, ""
        if args[0] == "tcpip":
            self.tcp_port = args[1]
            return 0, f"restarting in TCP mode port: {args[1]}", ""
        if args == ["get-state"]:
            return 0, "device", ""
        return 0, "", ""


@pytest.fixture
def phone(monkeypatch):
    def install(**state):
        fake = FakePhone(**state)
        monkeypatch.setattr(adb_module.subprocess, "run", fake.run)
        return fake

    return install


def test_an_existing_wifi_connection_is_used_without_touching_the_phone(phone):
    fake = phone(wifi_connected=True, tcp_port="5555")

    assert wifi.connect_over_wifi() == f"{ADDRESS}:5555"
    assert not any("tcpip" in command for command in fake.commands)


def test_a_phone_on_usb_is_switched_to_wifi_at_its_current_address(phone):
    fake = phone()

    assert wifi.connect_over_wifi(wait=0, poll=0) == f"{ADDRESS}:5555"
    assert ["-s", USB, "tcpip", "5555"] in fake.commands
    assert ["connect", f"{ADDRESS}:5555"] in fake.commands


def test_a_phone_already_listening_is_only_reconnected(phone):
    fake = phone(tcp_port="5555", address="10.0.0.9")
    fake.address = ADDRESS

    assert wifi.connect_over_wifi(wait=0, poll=0) == f"{ADDRESS}:5555"
    assert not any("tcpip" in command for command in fake.commands)


@pytest.mark.parametrize("state", [{"usb": False}, {"address": None}])
def test_no_wifi_connection_is_found_without_one_phone_on_usb_with_an_address(phone, state):
    phone(**state)

    assert wifi.connect_over_wifi(wait=0, poll=0) is None


def test_an_adb_client_set_to_wifi_targets_the_resolved_connection(phone):
    fake = phone()
    adb = AdbClient(serial=wifi.WIFI_SERIAL)

    assert adb.is_available()
    assert adb.run(["get-state"]) == (0, "device", "")
    assert adb.is_device_connected()
    assert ["-s", f"{ADDRESS}:5555", "get-state"] in fake.commands
    assert sum(command == ["-s", USB, "tcpip", "5555"] for command in fake.commands) == 1


def test_an_unresolved_wifi_client_reports_no_device_instead_of_using_usb(phone):
    fake = phone(address=None)
    adb = AdbClient(serial=wifi.WIFI_SERIAL)

    assert adb.run(["get-state"])[0] != 0
    assert not adb.is_device_connected()
    assert ["-s", USB, "get-state"] not in fake.commands


VM = "10.11.1.3:5001"


def test_a_corellium_vm_is_never_taken_for_the_phones_wifi_connection(phone):
    phone(wifi_connected=True, tcp_port="5555", vms=[VM])

    assert wifi.connect_over_wifi(exclude={VM}) == f"{ADDRESS}:5555"


def test_with_only_a_vm_on_the_network_the_phone_on_usb_is_switched_to_wifi(phone):
    fake = phone(vms=[VM])

    assert wifi.connect_over_wifi(wait=0, poll=0, exclude={VM}) == f"{ADDRESS}:5555"
    assert ["-s", USB, "tcpip", "5555"] in fake.commands


def test_without_exclusions_a_lone_vm_would_be_mistaken_for_the_phone(phone):
    phone(usb=False, vms=[VM])

    assert wifi.connect_over_wifi() == VM
    assert wifi.connect_over_wifi(exclude={VM}) is None
