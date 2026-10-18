from dataclasses import replace
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectionError, AdbConnectPolicy
from mobile_playbook.platforms.android.device.corellium_roles import connect_claimed
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool, RoleDevice, RoleUnavailable
from mobile_playbook.platforms.corellium.client import CorelliumError
from tests.platforms.corellium.helpers import device

STOCK, ROOTED = "10.11.1.1:5001", "10.11.1.2:5001"
POLICY = AdbConnectPolicy(timeout=1, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)
CONFIG = SimpleNamespace(device=SimpleNamespace(adb_path="adb"))


class NoLifecycleCorellium:
    def __getattr__(self, name):
        raise AssertionError(f"connecting a claimed device must not call Corellium {name}")


def claimed(*roles):
    devices = tuple(device(f"{role}-vm", role) for role in roles)
    addresses = {"non_rooted": "10.11.1.1", "rooted": "10.11.1.2"}
    prepared = {item.role: replace(item.instance, state="on", service_ip=addresses[item.role], adb_port=5001)
                for item in devices}
    return devices, prepared


def add_devices(adb_host, rooted_build="userdebug"):
    adb_host.add(STOCK, build_type="user")
    adb_host.add(ROOTED, build_type=rooted_build)


def test_each_claimed_role_connects_to_its_own_instance(adb_host, tmp_path):
    add_devices(adb_host)
    devices, prepared = claimed("non_rooted", "rooted")

    pool = connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path, policy=POLICY)

    assert pool.roles == ("non_rooted", "rooted")
    assert pool.identities() == {
        "non_rooted": {"instance_id": "non_rooted-vm", "serial": STOCK},
        "rooted": {"instance_id": "rooted-vm", "serial": ROOTED},
    }
    assert pool.device("rooted").client.adb.serial == ROOTED
    assert pool.device("non_rooted").client.corellium is None


def test_a_non_rooted_only_run_connects_only_the_non_rooted_device(adb_host, tmp_path):
    add_devices(adb_host)
    devices, prepared = claimed("non_rooted")

    pool = connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path, policy=POLICY)

    assert pool.roles == ("non_rooted",)
    assert all(serial in (None, STOCK) and ROOTED not in args for serial, args in adb_host.commands)
    with pytest.raises(RoleUnavailable, match="no prepared rooted device .* never run on another device"):
        pool.device("rooted")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda prepared: prepared.update(rooted=replace(prepared["rooted"], id="other-vm")), "not the claimed instance"),
        (lambda prepared: prepared.update(rooted=replace(prepared["rooted"], state="off")), "is off, not on"),
        (lambda prepared: prepared.update(rooted=replace(prepared["rooted"], osbuild="r2 user")), "cannot use instance"),
        (lambda prepared: prepared.pop("rooted"), "No prepared instance for the rooted role"),
    ],
)
def test_a_prepared_instance_that_does_not_match_its_claim_is_rejected_before_adb(adb_host, tmp_path, change, message):
    add_devices(adb_host)
    devices, prepared = claimed("non_rooted", "rooted")
    change(prepared)

    with pytest.raises(CorelliumError, match=message):
        connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path, policy=POLICY)

    assert adb_host.commands == []


def test_two_roles_on_one_adb_address_are_rejected(adb_host, tmp_path):
    devices, prepared = claimed("non_rooted", "rooted")
    same = {"adb_address": STOCK}

    with pytest.raises(CorelliumError, match="same adb address"):
        connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path,
                        role_settings={"non_rooted": same, "rooted": same}, policy=POLICY)


def test_a_rooted_vm_running_a_stock_build_fails_and_disconnects_every_role(adb_host, tmp_path):
    add_devices(adb_host, rooted_build="user")
    devices, prepared = claimed("non_rooted", "rooted")

    with pytest.raises(AdbConnectionError, match="not rooted, but a rooted device"):
        connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path, policy=POLICY)

    assert adb_host.sent(None, "disconnect", STOCK) == 1
    assert adb_host.sent(None, "disconnect", ROOTED) == 1


def test_an_unauthorized_role_is_approved_on_its_own_instance(adb_host, tmp_path, monkeypatch):
    adb_host.add(STOCK, build_type="user")
    rooted = adb_host.add(ROOTED, build_type="userdebug", connects_to="unauthorized")
    approved = []

    class Authorizer:
        def __init__(self, client, instance_id, serial, **kwargs):
            self.instance_id, self.serial, self.diagnostics_dir = instance_id, serial, kwargs["diagnostics_dir"]

        def __call__(self, host, phone):
            approved.append((self.instance_id, self.serial, self.diagnostics_dir))
            rooted.state = "device"

    monkeypatch.setattr("mobile_playbook.platforms.android.device.corellium_roles.DebuggingAuthorizer", Authorizer)
    devices, prepared = claimed("non_rooted", "rooted")

    pool = connect_claimed(CONFIG, devices, prepared, NoLifecycleCorellium(), diagnostics_dir=tmp_path, policy=POLICY)

    assert approved == [("rooted-vm", ROOTED, tmp_path)]
    assert pool.device("rooted").authorize is not None


def role_pool(adb_host, *roles):
    add_devices(adb_host)
    clients = {}
    for role, serial, rooted in (("non_rooted", STOCK, False), ("rooted", ROOTED, True)):
        if role in roles:
            adb_host.devices[serial].state = "device"
            clients[role] = RoleDevice(role, f"{role}-vm", serial, rooted, SimpleNamespace(adb=AdbClient("adb", serial), quit=lambda: None))
    return AndroidDevicePool(clients, adb_path="adb", policy=POLICY)


def test_ready_returns_a_connected_roles_client_without_reconnecting(adb_host):
    pool = role_pool(adb_host, "non_rooted", "rooted")

    assert pool.ready("rooted").adb.serial == ROOTED
    assert adb_host.sent(None, "connect", ROOTED) == 0


def test_ready_reconnects_only_the_affected_role_to_the_same_serial(adb_host):
    pool = role_pool(adb_host, "non_rooted", "rooted")
    adb_host.devices[ROOTED].state = "offline"

    assert pool.ready("rooted").adb.serial == ROOTED
    assert pool.ready("non_rooted").adb.serial == STOCK
    assert adb_host.sent(None, "connect", ROOTED) == 1
    assert adb_host.sent(None, "connect", STOCK) == 0


def test_a_role_that_cannot_reconnect_fails_rather_than_using_another_device(adb_host):
    pool = role_pool(adb_host, "non_rooted", "rooted")
    adb_host.devices[STOCK].state = "absent"
    adb_host.devices[STOCK].connects_to = "absent"

    with pytest.raises(AdbConnectionError, match=STOCK):
        pool.ready("non_rooted")


def test_roles_must_use_distinct_instances_and_serials():
    client = SimpleNamespace(adb=None)
    with pytest.raises(ValueError, match="distinct"):
        AndroidDevicePool({"non_rooted": RoleDevice("non_rooted", "vm", STOCK, False, client),
                           "rooted": RoleDevice("rooted", "vm", ROOTED, True, client)})
    with pytest.raises(ValueError, match="its own role"):
        AndroidDevicePool({"rooted": RoleDevice("non_rooted", "vm", STOCK, False, client)})


def test_release_quits_every_session_and_disconnects_every_role_despite_failures(adb_host):
    pool = role_pool(adb_host, "non_rooted", "rooted")
    quits = []

    def failing_quit():
        quits.append("non_rooted")
        raise RuntimeError("session already gone")

    pool.device("non_rooted").client.quit = failing_quit
    pool.device("rooted").client.quit = lambda: quits.append("rooted")

    pool.release()

    assert quits == ["non_rooted", "rooted"]
    assert adb_host.sent(None, "disconnect", STOCK) == 1
    assert adb_host.sent(None, "disconnect", ROOTED) == 1
