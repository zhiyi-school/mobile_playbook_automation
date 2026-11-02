from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.traffic_interception.device_proxy import (
    ProxyState,
    TrafficInterceptionSetupError,
    read_device_proxy,
    restore_device_proxy,
    set_device_proxy,
)


class ProxyAdb:
    """Fake adb holding the device's global http_proxy setting."""

    def __init__(self, proxy="null", put_fails=False, stores=None):
        self.proxy = proxy
        self.put_fails = put_fails
        # What the device stores for a value put, for devices that do not keep it as given.
        self.stores = stores or {}
        self.puts: list[str] = []

    def run(self, args, timeout=None):
        if args == ["shell", "settings", "get", "global", "http_proxy"]:
            return 0, f"{self.proxy}\n", ""
        assert args[:5] == ["shell", "settings", "put", "global", "http_proxy"]
        self.puts.append(args[5])
        if self.put_fails:
            return 1, "", "java.lang.SecurityException: Permission denial"
        self.proxy = self.stores.get(args[5], args[5])
        return 0, "", ""


def test_the_proxy_is_set_and_reset_with_colon_zero_as_the_playbook_does():
    adb = ProxyAdb()

    state = set_device_proxy(adb, "192.0.2.5", 8081)
    restored = restore_device_proxy(adb, state)

    assert state == ProxyState(previous=None, applied="192.0.2.5:8081")
    assert adb.puts == ["192.0.2.5:8081", ":0"]
    assert restored == {"status": "RESTORED", "proxy": ":0"}
    assert read_device_proxy(adb) == ":0"


def test_a_proxy_the_device_had_before_is_put_back():
    adb = ProxyAdb(proxy="198.51.100.7:3128")

    restore_device_proxy(adb, set_device_proxy(adb, "192.0.2.5", 8081))

    assert adb.proxy == "198.51.100.7:3128"


def test_a_proxy_left_by_an_unfinished_run_is_reset_not_restored():
    adb = ProxyAdb(proxy="192.0.2.5:8081")

    state = set_device_proxy(adb, "192.0.2.5", 8081)
    restore_device_proxy(adb, state)

    assert state.previous == "192.0.2.5:8081"
    assert adb.proxy == ":0"


def test_colon_zero_reading_back_as_no_proxy_counts_as_reset():
    adb = ProxyAdb(stores={":0": "null"})

    restored = restore_device_proxy(adb, set_device_proxy(adb, "192.0.2.5", 8081))

    assert restored["status"] == "RESTORED"
    assert read_device_proxy(adb) is None


def test_a_refused_proxy_change_fails_setup():
    with pytest.raises(TrafficInterceptionSetupError) as error:
        set_device_proxy(ProxyAdb(put_fails=True), "192.0.2.5", 8081)

    assert error.value.status == "DEVICE_PROXY_SET_FAILED"
    assert "Permission denial" in str(error.value)


def test_a_proxy_the_device_does_not_report_fails_setup():
    with pytest.raises(TrafficInterceptionSetupError, match="reports http_proxy") as error:
        set_device_proxy(ProxyAdb(stores={"192.0.2.5:8081": "null"}), "192.0.2.5", 8081)

    assert error.value.status == "DEVICE_PROXY_SET_FAILED"


def test_a_failed_reset_is_reported_with_the_proxy_left_set():
    adb = ProxyAdb()
    state = set_device_proxy(adb, "192.0.2.5", 8081)
    adb.put_fails = True

    with pytest.raises(TrafficInterceptionSetupError) as error:
        restore_device_proxy(adb, state)

    assert error.value.status == "DEVICE_PROXY_RESTORE_FAILED"
    assert error.value.state == {"previous": None, "applied": "192.0.2.5:8081"}
    assert adb.proxy == "192.0.2.5:8081"


@pytest.mark.parametrize("port", [0, 65536])
def test_a_port_out_of_range_is_refused_before_touching_the_device(port):
    adb = ProxyAdb()

    with pytest.raises(TrafficInterceptionSetupError, match="not between 1 and 65535"):
        set_device_proxy(adb, "192.0.2.5", port)

    assert adb.puts == []
