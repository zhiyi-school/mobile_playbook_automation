from __future__ import annotations

import pytest

from mobile_playbook.core import network

LABEL = "example.host"


def test_auto_resolves_to_the_detected_lan_address(monkeypatch):
    monkeypatch.setattr(network, "detect_lan_ip", lambda: "10.0.0.8")

    assert network.resolve_lan_host("auto", label=LABEL) == "10.0.0.8"
    assert network.resolve_lan_host("  AUTO  ", label=LABEL) == "10.0.0.8"


def test_a_detected_address_is_itself_rejected_when_it_is_not_routable(monkeypatch):
    monkeypatch.setattr(network, "detect_lan_ip", lambda: "127.0.0.1")

    with pytest.raises(ValueError, match="routable"):
        network.resolve_lan_host("auto", label=LABEL)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "[::1]"])
def test_rejects_a_loopback_address(host):
    with pytest.raises(ValueError, match="routable"):
        network.resolve_lan_host(host, label=LABEL)


def test_rejects_the_unspecified_address():
    with pytest.raises(ValueError, match="routable"):
        network.resolve_lan_host("0.0.0.0", label=LABEL)


@pytest.mark.parametrize("host", ["169.254.1.1", "fe80::1"])
def test_rejects_a_link_local_address(host):
    with pytest.raises(ValueError, match="routable"):
        network.resolve_lan_host(host, label=LABEL)


@pytest.mark.parametrize("host", ["localhost", "LocalHost", "mac.localhost"])
def test_rejects_a_loopback_hostname(host):
    with pytest.raises(ValueError, match="loopback"):
        network.resolve_lan_host(host, label=LABEL)


@pytest.mark.parametrize("host", ["mac.local", "build-host.example.test", "10.0.0.8", "192.168.1.16"])
def test_passes_a_routable_hostname_or_address_through_unchanged(host):
    assert network.resolve_lan_host(host, label=LABEL) == host


@pytest.mark.parametrize("configured", ["", "   ", None])
def test_an_empty_value_names_the_setting_that_is_missing(configured):
    with pytest.raises(ValueError, match=f"{LABEL} is required"):
        network.resolve_lan_host(configured, label=LABEL)


def test_every_refusal_names_the_setting_it_came_from():
    for host in ("", "localhost", "127.0.0.1"):
        with pytest.raises(ValueError) as exc:
            network.resolve_lan_host(host, label="collection.advertised_host")
        assert "collection.advertised_host" in str(exc.value)


def test_detection_failure_is_reported_rather_than_returning_a_loopback_address(monkeypatch):
    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def connect(self, address):
            raise OSError("no route to host")

    monkeypatch.setattr(network.socket, "socket", lambda *args, **kwargs: _Socket())

    with pytest.raises(ValueError, match="could not detect"):
        network.detect_lan_ip()


def test_detection_returns_the_socket_s_own_address(monkeypatch):
    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def connect(self, address):
            assert address == ("8.8.8.8", 80)

        def getsockname(self):
            return ("10.0.0.8", 54321)

    monkeypatch.setattr(network.socket, "socket", lambda *args, **kwargs: _Socket())

    assert network.detect_lan_ip() == "10.0.0.8"
