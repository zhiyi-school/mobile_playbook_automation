from types import SimpleNamespace

from mobile_playbook.platforms.android.device import phone_role, wifi
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectPolicy
from mobile_playbook.platforms.android.device.phone_role import connect_phone, phone_readiness
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool
from mobile_playbook.platforms.android.device.transports import PhoneTransports, attach_transports, open_transports, switch_recorder
from mobile_playbook.reporting.run_events import read_events

USB = "VVY0223411002458"
WIFI = "192.168.1.20:5555"
MDNS = "adb-VVY0223411002458-AbCdEf._adb-tls-connect._tcp"
OTHER = "192.168.1.30:5555"
VM = "10.11.1.3:5001"
HW = "VVY0223411002458"
POLICY = AdbConnectPolicy(timeout=0.3, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)


def phone(adb_host, *, wifi_listening=True, wifi_connected=False):
    packages = {"com.example.one": ("1", "1")}
    usb = adb_host.add(USB, state="device", serialno=HW, wifi_address="192.168.1.20",
                       tcp_port="5555" if wifi_listening else "", packages=packages)
    network = adb_host.add(WIFI, state="device" if wifi_connected else "absent", serialno=HW, packages=packages)
    return usb, network


def transports(*serials, exclude=()):
    return PhoneTransports("adb", HW, serials, exclude=exclude)


def config(serial, backup=True):
    return SimpleNamespace(device=SimpleNamespace(adb_path="adb", adb_serial=serial, adb_backup=backup))


def test_a_phone_on_usb_gets_its_wifi_connection_as_the_backup(adb_host):
    phone(adb_host, wifi_listening=False)

    found = open_transports("adb", USB, wifi_wait=0, poll=0, usb_return=0)

    assert (found.current, found.backups, found.hardware_id) == (USB, [WIFI], HW)
    assert adb_host.sent(USB, "tcpip", "5555") == 1
    assert adb_host.sent(None, "connect", WIFI) == 1


def test_an_existing_wifi_connection_to_the_phone_is_reused_without_switching_adb_to_wifi(adb_host):
    phone(adb_host, wifi_connected=True)

    assert open_transports("adb", USB, wifi_wait=0, poll=0, usb_return=0).backups == [WIFI]
    assert adb_host.sent(USB, "tcpip", "5555") == 0 and adb_host.sent(None, "connect", WIFI) == 0


def test_a_phone_on_wifi_gets_its_usb_connection_as_the_backup(adb_host):
    phone(adb_host, wifi_connected=True)

    assert open_transports("adb", WIFI, wifi_wait=0, poll=0, usb_return=0).backups == [USB]


def test_another_phone_and_a_vm_are_never_taken_as_backups(adb_host):
    adb_host.add(USB, state="device", serialno=HW)
    adb_host.add(OTHER, state="device", serialno="SOMEONE-ELSE")
    adb_host.add(VM, state="device", serialno=HW)

    found = open_transports("adb", USB, exclude={VM}, wifi_wait=0, poll=0, usb_return=0)

    assert found.backups == []
    assert adb_host.sent(VM, "shell", "getprop", "ro.serialno") == 0


def test_a_phone_without_a_hardware_serial_gets_no_backup(adb_host):
    adb_host.add(USB, state="device", wifi_address="192.168.1.20", tcp_port="5555")

    assert open_transports("adb", USB, wifi_wait=0, poll=0, usb_return=0) is None
    assert adb_host.sent(None, "connect", WIFI) == 0


def test_a_command_that_finds_usb_gone_runs_again_over_wifi(adb_host):
    usb, _ = phone(adb_host, wifi_connected=True)
    switched = []
    found = transports(USB, WIFI)
    found.on_switch = lambda previous, current: switched.append((previous, current))
    adb = AdbClient("adb", USB, transports=found)
    usb.state = "absent"

    code, out, _ = adb.run(["shell", "pm", "list", "packages", "-3"])

    assert (code, out) == (0, "package:com.example.one")
    assert adb.target_serial() == WIFI and switched == [(USB, WIFI)]
    assert adb_host.sent(USB, "shell", "pm", "list", "packages", "-3") == 1
    assert adb_host.sent(WIFI, "shell", "pm", "list", "packages", "-3") == 1


def test_a_command_that_finds_wifi_gone_runs_again_over_usb(adb_host):
    _, network = phone(adb_host, wifi_connected=True)
    adb = AdbClient("adb", WIFI, transports=transports(WIFI, USB))
    network.state = "offline"

    assert adb.run(["shell", "getprop", "ro.serialno"])[:2] == (0, HW)
    assert adb.target_serial() == USB


def test_a_wifi_backup_adb_no_longer_lists_is_reconnected_by_its_address(adb_host):
    usb, _ = phone(adb_host)
    adb = AdbClient("adb", USB, transports=transports(USB, WIFI))
    usb.state = "absent"

    assert adb.run(["shell", "getprop", "ro.serialno"])[0] == 0
    assert adb.target_serial() == WIFI and adb_host.sent(None, "connect", WIFI) == 1


def test_a_connection_made_after_the_run_started_serves_once_it_proves_to_be_the_phone(adb_host):
    adb_host.add(USB, state="absent", serialno=HW)
    adb_host.add(OTHER, state="device", serialno="SOMEONE-ELSE")
    adb_host.add(MDNS, state="device", serialno=HW)
    adb = AdbClient("adb", USB, transports=transports(USB))

    assert adb.run(["shell", "getprop", "ro.serialno"])[:2] == (0, HW)
    assert adb.target_serial() == MDNS and adb.transports.serials == [USB, MDNS]


def test_with_every_connection_gone_the_original_failure_stands(adb_host):
    adb_host.add(USB, state="absent", serialno=HW)
    adb_host.add(WIFI, connects_to="absent", serialno=HW)
    adb = AdbClient("adb", USB, transports=transports(USB, WIFI))

    code, _, err = adb.run(["shell", "getprop", "ro.serialno"])

    assert code == 1 and "not found" in err
    assert adb.target_serial() == USB
    assert adb_host.sent(USB, "shell", "getprop", "ro.serialno") == 1


def test_a_failure_on_the_phone_itself_never_looks_for_another_connection(adb_host):
    phone(adb_host, wifi_connected=True)
    adb = AdbClient("adb", USB, transports=transports(USB, WIFI))

    adb.run(["uninstall", "com.example.absent"])

    assert adb_host.sent(None, "devices") == 0 and adb.target_serial() == USB


def test_the_connection_check_moves_to_the_backup(adb_host):
    usb, _ = phone(adb_host, wifi_connected=True)
    adb = AdbClient("adb", USB, transports=transports(USB, WIFI))
    usb.state = "absent"

    assert adb.is_device_connected() is True and adb.target_serial() == WIFI


def test_attaching_skips_a_client_whose_phone_is_not_ready(adb_host):
    adb_host.add(USB, state="absent", serialno=HW)
    adb = AdbClient("adb", USB)

    assert attach_transports(adb) is None and adb.transports is None


def test_the_role_phone_keeps_running_over_wifi_after_usb_drops(adb_host):
    usb, _ = phone(adb_host, wifi_connected=True)
    device = connect_phone(config(USB), "non_rooted", exclude={VM}, policy=POLICY)
    pool = AndroidDevicePool({"non_rooted": device}, adb_path="adb", policy=POLICY)
    usb.state = "absent"

    client = pool.ready("non_rooted")

    assert client.adb.target_serial() == WIFI
    assert pool.identities()["non_rooted"]["serial"] == USB
    assert adb_host.sent(WIFI, "shell", "getprop", "sys.boot_completed") == 1


def test_the_role_phone_has_no_backup_when_adb_backup_is_off(adb_host):
    phone(adb_host, wifi_listening=False)

    device = connect_phone(config(USB, backup=False), "non_rooted", policy=POLICY)

    assert device.client.adb.transports is None
    assert adb_host.sent(USB, "tcpip", "5555") == 0 and adb_host.sent(None, "connect", WIFI) == 0


def test_two_wifi_connections_to_one_phone_count_as_one_phone(adb_host, monkeypatch):
    adb_host.add(WIFI, state="device", serialno=HW)
    adb_host.add(MDNS, state="device", serialno=HW)

    assert wifi.connect_over_wifi("adb", wait=0, poll=0) == WIFI
    assert phone_readiness("adb", "wifi") == ("ready", "", "")


def test_wifi_connections_to_two_phones_still_need_the_serial_named(adb_host):
    adb_host.add(WIFI, state="device", serialno=HW)
    adb_host.add(OTHER, state="device", serialno="SOMEONE-ELSE")

    assert wifi.connect_over_wifi("adb", wait=0, poll=0) is None
    assert phone_readiness("adb", "wifi")[1] == "several phones are connected"
    assert phone_role.resolve_phone_serial("adb", WIFI) == WIFI


def test_each_move_is_recorded_as_a_run_event(adb_host, tmp_path):
    usb, _ = phone(adb_host, wifi_connected=True)
    adb = AdbClient("adb", USB, transports=transports(USB, WIFI))
    adb.transports.on_switch = switch_recorder(tmp_path)
    usb.state = "absent"

    adb.run(["shell", "getprop", "ro.serialno"])

    recorded, _ = read_events(tmp_path)
    assert [(event["type"], event["previous_serial"], event["serial"]) for event in recorded] == [
        ("device_transport_switched", USB, WIFI)]
    assert "role" not in recorded[0]
