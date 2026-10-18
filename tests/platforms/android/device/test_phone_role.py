from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import phone_role
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectionError, AdbConnectPolicy, connect_ready
from mobile_playbook.platforms.android.device.phone_role import connect_phone, resolve_phone_serial
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool, RoleDevice

USB_PHONE = "VVY0223411002458"
WIFI_PHONE = "192.168.1.20:5555"
VM = "10.11.1.3:5001"
POLICY = AdbConnectPolicy(timeout=0.3, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)


def config(serial):
    return SimpleNamespace(device=SimpleNamespace(adb_path="adb", adb_serial=serial))


def test_a_phone_on_usb_becomes_the_role_device_without_any_adb_connect(adb_host):
    adb_host.add(USB_PHONE, state="device", build_type="user")

    phone = connect_phone(config(USB_PHONE), "non_rooted", exclude={VM}, policy=POLICY)

    assert (phone.role, phone.instance_id, phone.serial, phone.rooted) == ("non_rooted", USB_PHONE, USB_PHONE, False)
    assert phone.device_type == "physical_phone"
    assert phone.client.adb.serial == USB_PHONE
    assert not [args for _, args in adb_host.commands if args[:1] in (["connect"], ["disconnect"])]


def test_a_phone_on_usb_that_appears_late_is_waited_for(adb_host):
    adb_host.add(USB_PHONE, build_type="user", script=["absent", "absent", "device"])

    assert connect_phone(config(USB_PHONE), "non_rooted", policy=POLICY).serial == USB_PHONE
    assert adb_host.sent(None, "connect", USB_PHONE) == 0


def test_a_missing_phone_on_usb_fails_with_what_to_check(adb_host):
    with pytest.raises(AdbConnectionError, match="Check that the phone is connected and that USB debugging is on"):
        connect_phone(config(USB_PHONE), "non_rooted", policy=POLICY)

    assert adb_host.sent(None, "connect", USB_PHONE) == 0


def test_a_phone_over_wifi_is_connected_by_its_address(adb_host):
    adb_host.add(WIFI_PHONE, build_type="user")

    assert connect_phone(config(WIFI_PHONE), "non_rooted", policy=POLICY).serial == WIFI_PHONE
    assert adb_host.sent(None, "connect", WIFI_PHONE) == 1


def test_an_unauthorized_phone_asks_the_person_holding_it(adb_host):
    adb_host.add(USB_PHONE, state="unauthorized")

    with pytest.raises(AdbConnectionError, match="Unlock the phone and tap Allow on its USB debugging prompt") as error:
        connect_phone(config(USB_PHONE), "non_rooted", policy=POLICY)

    assert error.value.state == "unauthorized"


def test_a_rooted_build_cannot_serve_the_non_rooted_role(adb_host):
    adb_host.add(USB_PHONE, state="device", build_type="userdebug")

    with pytest.raises(AdbConnectionError, match="userdebug build, which is rooted, but a non-rooted device"):
        connect_phone(config(USB_PHONE), "non_rooted", policy=POLICY)


def test_wifi_resolves_to_the_phone_with_every_vm_address_excluded(adb_host, monkeypatch):
    adb_host.add(WIFI_PHONE, state="device", build_type="user")
    seen = []
    monkeypatch.setattr(phone_role, "connect_over_wifi", lambda adb_path, exclude: seen.append(set(exclude)) or WIFI_PHONE)

    assert connect_phone(config("wifi"), "non_rooted", exclude={VM}, policy=POLICY).serial == WIFI_PHONE
    assert seen == [{VM}]


def test_wifi_that_finds_no_phone_fails_with_how_to_connect_it(monkeypatch):
    monkeypatch.setattr(phone_role, "connect_over_wifi", lambda adb_path, exclude: None)

    with pytest.raises(AdbConnectionError, match="Connect the phone by USB while it is on the same Wi-Fi network"):
        resolve_phone_serial("adb", " wifi ", {VM})


def test_a_vm_address_configured_as_the_phone_is_refused_before_any_adb_command(adb_host):
    with pytest.raises(AdbConnectionError, match="is a Corellium VM's adb address, not the phone's"):
        connect_phone(config(VM), "non_rooted", exclude={VM}, policy=POLICY)

    assert adb_host.commands == []


def test_recovering_the_phone_waits_for_the_same_serial_and_never_disconnects_it(adb_host):
    phone_device = adb_host.add(USB_PHONE, state="device", build_type="user")
    vm = adb_host.add(VM, state="device", build_type="userdebug")
    phone = connect_phone(config(USB_PHONE), "non_rooted", policy=POLICY)
    rooted = RoleDevice("rooted", "rooted-vm", VM, True, SimpleNamespace(adb=None, quit=lambda: None))
    pool = AndroidDevicePool({"non_rooted": phone, "rooted": rooted}, adb_path="adb", policy=POLICY)
    phone_device.script = ["absent", "device"]
    adb_host.commands.clear()

    assert pool.recover("non_rooted") is phone
    assert pool.identities()["non_rooted"] == {"instance_id": USB_PHONE, "serial": USB_PHONE, "device_type": "physical_phone"}
    assert adb_host.sent(None, "connect", USB_PHONE) == 0
    assert adb_host.sent(USB_PHONE, "shell", "getprop", "sys.boot_completed") == 1

    phone.client.quit = lambda: None
    pool.release()
    assert adb_host.sent(None, "disconnect", USB_PHONE) == 0
    assert adb_host.sent(None, "disconnect", VM) == 1
    assert vm.state == "absent" and phone_device.state == "device"


def test_wait_only_connection_never_connects_or_disconnects_an_offline_device(adb_host):
    adb_host.add(USB_PHONE, state="offline")

    with pytest.raises(AdbConnectionError, match="it is offline"):
        connect_ready("adb", USB_PHONE, rooted=False, policy=POLICY, connect=False)

    assert not [args for _, args in adb_host.commands if args[:1] in (["connect"], ["disconnect"])]
