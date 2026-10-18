import time

import pytest

from mobile_playbook.platforms.android.device.adb_connection import AdbConnectionError, AdbConnectPolicy, connect_ready

SERIAL = "10.11.1.1:5001"
FAST = AdbConnectPolicy(timeout=0.5, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)


def test_a_ready_device_is_used_without_connecting_again(adb_host):
    adb_host.add(SERIAL, state="device")

    assert connect_ready("adb", SERIAL, rooted=False, policy=FAST) == "user"
    assert adb_host.sent(None, "connect", SERIAL) == 0


def test_an_absent_device_is_connected_and_awaited_until_booted(adb_host):
    adb_host.add(SERIAL, boots_after=2)

    connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    assert adb_host.sent(None, "connect", SERIAL) == 1
    assert adb_host.sent(SERIAL, "shell", "getprop", "sys.boot_completed") == 3


def test_an_offline_device_is_disconnected_before_reconnecting(adb_host):
    adb_host.add(SERIAL, state="offline")

    connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    disconnect = adb_host.commands.index((None, ["disconnect", SERIAL]))
    assert adb_host.commands.index((None, ["connect", SERIAL])) > disconnect


def test_a_device_unreachable_for_more_than_five_attempts_connects_within_the_budget(adb_host):
    adb_host.add(SERIAL, unreachable_connects=7)

    connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    assert adb_host.sent(None, "connect", SERIAL) == 8


def test_a_device_behind_a_reconnecting_vpn_connects_once_the_gap_ends(adb_host):
    # Scaled from qualification: the VPN can stay down about 180s of the 600s budget after every VM was off.
    adb_host.add(SERIAL, reachable_at=time.monotonic() + 0.18)
    policy = AdbConnectPolicy(timeout=0.6, reconnect_pause=0.005, unauthorized_grace=0, poll=0.001)

    connect_ready("adb", SERIAL, rooted=False, policy=policy)

    assert adb_host.sent(None, "connect", SERIAL) > 5


def test_reconnection_ends_with_the_time_budget_and_pauses_between_attempts(adb_host):
    adb_host.add(SERIAL, connects_to="absent")
    paced = AdbConnectPolicy(timeout=0.3, reconnect_pause=0.05, unauthorized_grace=0, poll=0.001)

    with pytest.raises(AdbConnectionError, match=r"after \d+ attempts in 0.3s; it is absent") as error:
        connect_ready("adb", SERIAL, rooted=False, policy=paced)

    assert error.value.state == "absent"
    attempts = adb_host.sent(None, "connect", SERIAL)
    assert f"after {attempts} attempts" in str(error.value)
    assert 2 <= attempts <= 7


def test_each_connect_is_capped_by_the_remaining_budget(adb_host, monkeypatch):
    adb_host.add(SERIAL, connects_to="absent")
    timeouts = []
    run = adb_host.run

    def record(argv, **kwargs):
        if "connect" in argv:
            timeouts.append(kwargs["timeout"])
        return run(argv, **kwargs)

    monkeypatch.setattr("mobile_playbook.platforms.android.device.adb.subprocess.run", record)

    with pytest.raises(AdbConnectionError, match="it is absent"):
        connect_ready("adb", SERIAL, rooted=False, policy=AdbConnectPolicy(timeout=0.2, reconnect_pause=0.05, poll=0.001))

    assert timeouts and all(timeout <= 0.2 for timeout in timeouts)


def test_a_device_that_stays_offline_exhausts_its_reconnections(adb_host):
    adb_host.add(SERIAL, state="offline", connects_to="offline")

    with pytest.raises(AdbConnectionError, match="it is offline") as error:
        connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    reconnections = adb_host.sent(None, "disconnect", SERIAL)
    assert reconnections > 1
    assert f"after {reconnections} attempts" in str(error.value)


def test_an_unauthorized_device_fails_clearly_without_an_authorizer(adb_host):
    adb_host.add(SERIAL, state="unauthorized")

    with pytest.raises(AdbConnectionError, match="has not approved this host's adb key") as error:
        connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    assert error.value.state == "unauthorized"


def test_a_brief_unauthorized_handshake_is_not_treated_as_a_prompt(adb_host):
    adb_host.add(SERIAL, script=["unauthorized", "unauthorized", "device"])
    patient = AdbConnectPolicy(timeout=1, unauthorized_grace=0.5, poll=0.001)

    connect_ready("adb", SERIAL, rooted=False, policy=patient, authorize=lambda host, phone: pytest.fail("no prompt"))


def test_an_unauthorized_device_is_authorized_once_and_then_used(adb_host):
    device = adb_host.add(SERIAL, state="unauthorized")
    calls = []

    def authorize(host, phone):
        calls.append((host.serial, phone.serial))
        device.state = "device"

    connect_ready("adb", SERIAL, rooted=False, policy=FAST, authorize=authorize)

    assert calls == [(None, SERIAL)]


def test_a_device_still_unauthorized_after_approval_fails(adb_host):
    adb_host.add(SERIAL, state="unauthorized")
    calls = []

    with pytest.raises(AdbConnectionError, match="still unauthorized after approving"):
        connect_ready("adb", SERIAL, rooted=False, policy=FAST, authorize=lambda host, phone: calls.append(1))

    assert calls == [1]


def test_a_device_that_never_boots_times_out(adb_host):
    adb_host.add(SERIAL, state="device", boots_after=10**6)

    with pytest.raises(AdbConnectionError, match="still booting"):
        connect_ready("adb", SERIAL, rooted=False, policy=AdbConnectPolicy(timeout=0.05, poll=0.001))


@pytest.mark.parametrize(
    ("build_type", "rooted", "message"),
    [
        ("user", True, "user build, which is not rooted, but a rooted device"),
        ("userdebug", False, "userdebug build, which is rooted, but a non-rooted device"),
        ("", False, "root state is unknown"),
    ],
)
def test_the_device_must_have_the_expected_root_state(adb_host, build_type, rooted, message):
    adb_host.add(SERIAL, state="device", build_type=build_type)

    with pytest.raises(AdbConnectionError, match=message):
        connect_ready("adb", SERIAL, rooted=rooted, policy=FAST)


@pytest.mark.parametrize("build_type", ["userdebug", "eng"])
def test_userdebug_and_eng_builds_are_rooted(adb_host, build_type):
    adb_host.add(SERIAL, state="device", build_type=build_type)

    assert connect_ready("adb", SERIAL, rooted=True, policy=FAST) == build_type


@pytest.mark.parametrize(
    "settings",
    [{"timeout": 0}, {"poll": 0}, {"timeout": float("inf")}, {"unauthorized_grace": -1}, {"reconnect_pause": -1},
     {"reconnect_pause": float("nan")}, {"reconnect_pause": True}],
)
def test_unbounded_policies_are_rejected(settings):
    with pytest.raises(ValueError):
        AdbConnectPolicy(**settings)
