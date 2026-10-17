from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.preflight import check_requirements

SERIAL = "PHONE1"


def test_root_passes_when_the_device_runs_commands_as_root(adb_host):
    adb_host.add(SERIAL, state="device", root_uid=True)

    assert check_requirements(["root"], AdbClient("adb", SERIAL), None) == [("root", True, "device grants root")]


def test_root_fails_when_neither_adbd_nor_su_grants_it(adb_host):
    adb_host.add(SERIAL, state="device")

    [(name, ok, message)] = check_requirements(["root"], AdbClient("adb", SERIAL), None)

    assert (name, ok) == ("root", False)
    assert "does not grant root" in message
    assert adb_host.sent(SERIAL, "shell", "su", "0", "id", "-u") == 1


def test_root_fails_without_a_device():
    assert check_requirements(["root"], None, None)[0][1] is False
