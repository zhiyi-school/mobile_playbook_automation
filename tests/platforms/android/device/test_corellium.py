from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import adb as adb_module
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient, open_device
from mobile_playbook.platforms.android.device.corellium import (
    CorelliumConnection,
    adb_serial,
    connect_corellium,
    stop_corellium,
)
from mobile_playbook.platforms.corellium.client import CorelliumError, CorelliumInstance, CorelliumProject

SERIAL = "10.11.1.1:5001"
SETTINGS = {"project": "testing", "rooted": False}


class FakeCorellium:
    """One project holding one Android instance that comes on once started."""

    def __init__(self, state="on", rooted=False):
        self.state = state
        self.rooted = rooted
        self.calls: list[str] = []

    def list_projects(self):
        return [CorelliumProject(id="p1", name="testing")]

    def list_instances(self):
        return [self.get_instance("abc")]

    def get_instance(self, instance_id):
        return CorelliumInstance(
            id=instance_id,
            name="pixel",
            state=self.state,
            service_ip="10.11.1.1",
            project="p1",
            type="android",
            os="16.0.0",
            patches=("jailbroken",),
            osbuild="r2 userdebug" if self.rooted else "r2 user",
        )

    def start_instance(self, instance_id):
        self.calls.append("start")
        self.state = "on"

    def unpause_instance(self, instance_id):
        self.calls.append("unpause")
        self.state = "on"

    def stop_instance(self, instance_id):
        self.calls.append("stop")
        self.state = "off"


class FakeAdb:
    """adb on the host, with the instance reachable once connected and booted after a number of checks."""

    def __init__(self, reachable=True, boots_after=0):
        self.reachable = reachable
        self.boots_after = boots_after
        self.connected = False
        self.commands: list[list[str]] = []

    def run(self, argv, **kwargs):
        args = argv[1:]
        self.commands.append(args)
        if args == ["devices"]:
            rows = f"{SERIAL}\tdevice" if self.connected else ""
            return subprocess.CompletedProcess(argv, 0, stdout=f"List of devices attached\n{rows}", stderr="")
        if args[:1] == ["connect"]:
            self.connected = self.reachable
            return subprocess.CompletedProcess(argv, 0 if self.reachable else 1, stdout="", stderr="")
        if args == ["-s", SERIAL, "shell", "getprop", "sys.boot_completed"]:
            booted = self.boots_after <= 0
            self.boots_after -= 1
            return subprocess.CompletedProcess(argv, 0, stdout="1" if booted else "", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def fake_adb(monkeypatch):
    def install(**state):
        fake = FakeAdb(**state)
        monkeypatch.setattr(adb_module.subprocess, "run", fake.run)
        return fake

    return install


def test_an_off_instance_is_started_connected_and_awaited_until_booted(fake_adb):
    adb = fake_adb(boots_after=2)
    corellium = FakeCorellium(state="off")

    connection = connect_corellium("adb", {**SETTINGS, "auto_stop": True}, client=corellium, poll=0)

    assert connection.serial == SERIAL
    assert connection.instance.state == "on"
    assert connection.auto_stop is True
    assert corellium.calls == ["start"]
    assert ["connect", SERIAL] in adb.commands


def test_auto_start_false_refuses_an_off_instance_without_starting_it(fake_adb):
    corellium = FakeCorellium(state="off")

    with pytest.raises(CorelliumError, match="auto_start is false"):
        connect_corellium("adb", {**SETTINGS, "auto_start": False}, client=corellium, poll=0)
    assert corellium.calls == []


def test_without_start_an_off_instance_is_neither_started_nor_connected(fake_adb):
    adb = fake_adb()
    corellium = FakeCorellium(state="off")

    connection = connect_corellium("adb", SETTINGS, start=False, client=corellium, poll=0)

    assert connection.instance.state == "off"
    assert corellium.calls == []
    assert adb.commands == []


def test_without_start_an_unreachable_instance_is_tried_once(fake_adb):
    adb = fake_adb(reachable=False)

    connect_corellium("adb", SETTINGS, start=False, client=FakeCorellium(), poll=0)

    assert adb.commands.count(["connect", SERIAL]) == 1


def test_an_instance_this_call_started_is_stopped_again_when_adb_never_reaches_it(fake_adb):
    fake_adb(reachable=False)
    corellium = FakeCorellium(state="off")
    settings = {**SETTINGS, "auto_stop": True, "boot_timeout_seconds": 0.01}

    with pytest.raises(CorelliumError, match="VPN"):
        connect_corellium("adb", settings, client=corellium, poll=0)
    assert corellium.calls == ["start", "stop"]


def test_an_instance_that_was_already_on_is_left_running_when_adb_fails(fake_adb):
    fake_adb(reachable=False)
    corellium = FakeCorellium(state="on")
    settings = {**SETTINGS, "auto_stop": True, "boot_timeout_seconds": 0.01}

    with pytest.raises(CorelliumError):
        connect_corellium("adb", settings, client=corellium, poll=0)
    assert corellium.calls == []


def test_the_rooted_setting_must_match_the_instance(fake_adb):
    with pytest.raises(CorelliumError, match="no usable non-rooted android instance"):
        connect_corellium("adb", SETTINGS, client=FakeCorellium(rooted=True), poll=0)


def test_stop_corellium_follows_auto_stop():
    corellium = FakeCorellium(state="on")
    instance = corellium.get_instance("abc")

    stop_corellium(CorelliumConnection(instance=instance, serial=SERIAL, auto_stop=False), client=corellium)
    assert corellium.calls == []
    stop_corellium(CorelliumConnection(instance=instance, serial=SERIAL, auto_stop=True), client=corellium)
    assert corellium.calls == ["stop"]


def test_adb_serial_prefers_the_configured_address_then_the_services_address_and_port():
    instance = CorelliumInstance(id="abc", name="", state="on", service_ip="10.11.1.1")

    assert adb_serial(instance, {"adb_address": "10.11.2.2:6000"}) == "10.11.2.2:6000"
    assert adb_serial(instance, {"adb_port": 5555}) == "10.11.1.1:5555"
    assert adb_serial(instance, {}) == "10.11.1.1:5001"
    reported = CorelliumInstance(id="abc", name="", state="on", service_ip="10.11.1.3", adb_port=5002)
    assert adb_serial(reported, {"adb_port": 5555}) == "10.11.1.3:5002"
    with pytest.raises(CorelliumError, match="adb_address"):
        adb_serial(CorelliumInstance(id="abc", name="", state="on", service_ip=None), {})


def test_open_device_uses_the_physical_serial_unless_the_provider_is_corellium(monkeypatch):
    device = SimpleNamespace(provider="physical", adb_path="adb", adb_serial="PHONE1", corellium={})
    physical = open_device(SimpleNamespace(device=device))
    assert (physical.adb.serial, physical.corellium) == ("PHONE1", None)

    connection = CorelliumConnection(instance=FakeCorellium().get_instance("abc"), serial=SERIAL, auto_stop=True)
    calls = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.device.client.connect_corellium",
        lambda adb_path, settings, start: calls.append(start) or connection,
    )
    device = SimpleNamespace(provider="corellium", adb_path="adb", adb_serial="PHONE1", corellium=SETTINGS)

    virtual = open_device(SimpleNamespace(device=device), start=False)

    assert (virtual.adb.serial, virtual.corellium) == (SERIAL, connection)
    assert calls == [False]


def test_release_stops_the_corellium_instance_but_quit_does_not(monkeypatch):
    stopped = []
    monkeypatch.setattr("mobile_playbook.platforms.android.device.client.stop_corellium", stopped.append)
    connection = CorelliumConnection(instance=FakeCorellium().get_instance("abc"), serial=SERIAL, auto_stop=True)
    client = AndroidDeviceClient(None, corellium=connection)

    client.quit()
    assert stopped == []
    client.release()
    assert stopped == [connection]
