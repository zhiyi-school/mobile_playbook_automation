import pytest

from mobile_playbook.platforms.android.device.adb_connection import AdbConnectionError, AdbConnectPolicy, connect_ready

SERIAL = "10.11.1.1:5001"
FAST = AdbConnectPolicy(timeout=0.5, connect_attempts=3, unauthorized_grace=0, poll=0.001)


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


def test_connection_attempts_are_bounded(adb_host):
    adb_host.add(SERIAL, connects_to="absent")

    with pytest.raises(AdbConnectionError, match="after 3 attempts; it is absent") as error:
        connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    assert error.value.state == "absent"
    assert adb_host.sent(None, "connect", SERIAL) == 3


def test_a_device_that_stays_offline_exhausts_its_reconnections(adb_host):
    adb_host.add(SERIAL, state="offline", connects_to="offline")

    with pytest.raises(AdbConnectionError, match="it is offline"):
        connect_ready("adb", SERIAL, rooted=False, policy=FAST)

    assert adb_host.sent(None, "disconnect", SERIAL) == 3


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
    [{"timeout": 0}, {"poll": 0}, {"timeout": float("inf")}, {"unauthorized_grace": -1}, {"connect_attempts": 0},
     {"connect_attempts": True}],
)
def test_unbounded_policies_are_rejected(settings):
    with pytest.raises(ValueError):
        AdbConnectPolicy(**settings)
