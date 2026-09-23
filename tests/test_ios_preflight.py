from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.burp_health import health_record_path, write_health_record
from mobile_playbook.platforms.ios.config import ConfigError, validate_config
from mobile_playbook.platforms.ios.preflight import (
    _parse_connected_udids,
    check_ios_preflight,
    check_traffic_interception_preflight,
)
from mobile_playbook.platforms.ios.runner import IosPlatformRunner

XCTRACE_OUTPUT = """\
== Devices ==
user's MacBook Air (06316618-98B3-5149-9423-BB729F8163A5)
CSEC's iPhone (iOS 17.6) (00008120-0001110834E1A01E)

== Simulators ==
iPhone 16 Simulator (26.5) (F3A1191D-9506-4E83-832F-8035F19748CE)
"""


def _config(udid: str = "00008120-0001110834E1A01E"):
    device = SimpleNamespace(udid=udid, team_id="TEAM", appium_server_url="http://127.0.0.1:4723")
    return SimpleNamespace(device=device)


def test_parse_connected_udids_only_includes_online_devices_section():
    udids = _parse_connected_udids(XCTRACE_OUTPUT)

    assert udids == {"06316618-98B3-5149-9423-BB729F8163A5", "00008120-0001110834E1A01E"}
    assert "F3A1191D-9506-4E83-832F-8035F19748CE" not in udids  # simulator, not a device


def test_check_ios_preflight_passes_when_udid_is_connected(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008120-0001110834E1A01E"},
    )
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: True)

    result = check_ios_preflight(_config())

    assert result.ok
    assert result.errors == []


def test_check_ios_preflight_gives_a_one_line_error_for_a_disconnected_device(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008150-000E03321493401C"},  # a different device is connected instead
    )

    result = check_ios_preflight(_config("00008120-0001110834E1A01E"))

    assert not result.ok
    assert len(result.errors) == 1
    assert "00008120-0001110834E1A01E" in result.errors[0]
    assert "xcrun xctrace list devices" in result.errors[0]


def test_check_ios_preflight_skips_connectivity_check_when_xctrace_is_unusable(monkeypatch):
    # An empty set means we couldn't determine connected devices at all (xcrun missing,
    # timed out, etc.) — this must not be treated as "zero devices connected".
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight.connected_device_udids", lambda: set())
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: True)

    result = check_ios_preflight(_config())

    assert result.ok


def test_check_ios_preflight_gives_a_one_line_error_when_appium_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008120-0001110834E1A01E"},
    )
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: False)

    result = check_ios_preflight(_config())

    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0] == "appium: Appium server not reachable at http://127.0.0.1:4723. Start it with 'appium'."


def test_connect_device_raises_a_clean_error_without_opening_an_appium_session(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"some-other-device"},
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("AppiumDeviceClient.connect should not be called when preflight fails")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect", fail_if_called)

    with pytest.raises(RuntimeError, match="00008120-0001110834E1A01E"):
        IosPlatformRunner().connect_device(_config())


def _traffic_config(capture_path, *, expected_hosts=("api.example.com",), max_age=300):
    return SimpleNamespace(
        device=SimpleNamespace(udid="device-1"),
        traffic_interception={
            "burp": {
                "proxy_url": "HTTP://LOCALHOST:8080/",
                "capture_path": str(capture_path),
                "health_max_age_seconds": max_age,
            },
            "expected_hosts": list(expected_hosts),
        },
    )


def _traffic_app(app_id="app-one", *, enabled=True, override=None):
    risk = {"enabled": enabled}
    risk.update(override or {})
    return SimpleNamespace(id=app_id, risks={"ios-feature-02-risk-01": risk})


def _warning_codes(warnings):
    return {warning.code for warning in warnings}


def _write_fresh_health(capture_path, proxy_url="http://localhost:8080", device_udid="device-1"):
    capture_path.write_text('{"host":"example.com"}\n')
    return write_health_record(
        capture_path=capture_path,
        proxy_url=proxy_url,
        canary_host="example.com",
        valid_entry_count=1,
        device_udid=device_udid,
    )


def test_traffic_preflight_ignores_unselected_and_disabled_risks(tmp_path):
    config = _traffic_config(tmp_path / "capture.jsonl")
    enabled = _traffic_app()
    disabled = _traffic_app(enabled=False)

    assert check_traffic_interception_preflight(config, [(enabled, "ios-feature-01-risk-01")]) == []
    assert check_traffic_interception_preflight(config, [(disabled, "ios-feature-02-risk-01")]) == []


def test_fresh_matching_health_record_has_no_health_warning(tmp_path, monkeypatch):
    capture_path = tmp_path / "capture.jsonl"
    config = _traffic_config(capture_path)
    _write_fresh_health(capture_path)
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda *args, **kwargs: True)

    warnings = check_traffic_interception_preflight(config, [(_traffic_app(), "ios-feature-02-risk-01")])

    assert not (_warning_codes(warnings) & {"BURP_HEALTH_MISSING", "BURP_HEALTH_STALE", "BURP_HEALTH_MISMATCH"})


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("missing", "BURP_HEALTH_MISSING"),
        ("stale", "BURP_HEALTH_STALE"),
        ("proxy_mismatch", "BURP_HEALTH_MISMATCH"),
        ("device_mismatch", "BURP_HEALTH_MISMATCH"),
        ("invalid", "BURP_HEALTH_MISMATCH"),
    ],
)
def test_traffic_preflight_reports_health_record_state(tmp_path, monkeypatch, state, expected_code):
    capture_path = tmp_path / "capture.jsonl"
    config = _traffic_config(capture_path)
    capture_path.write_text('{"host":"example.com"}\n')
    if state != "missing":
        record_path = _write_fresh_health(capture_path)
        record = json.loads(record_path.read_text())
        if state == "stale":
            record["verified_at"] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        elif state == "proxy_mismatch":
            record["proxy_url"] = "http://other-proxy:8080"
        elif state == "device_mismatch":
            record["device_udid_sha256"] = "incorrect"
        elif state == "invalid":
            record["valid_entry_count"] = 0
        record_path.write_text(json.dumps(record))
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda *args, **kwargs: True)

    warnings = check_traffic_interception_preflight(config, [(_traffic_app(), "ios-feature-02-risk-01")])

    assert expected_code in _warning_codes(warnings)


@pytest.mark.parametrize(
    ("condition", "expected_code"),
    [
        ("proxy", "BURP_PROXY_UNREACHABLE"),
        ("directory", "BURP_CAPTURE_PATH_IS_DIRECTORY"),
        ("unreadable", "BURP_CAPTURE_UNREADABLE"),
        ("missing_parent", "BURP_CAPTURE_PARENT_MISSING"),
        ("unwritable_parent", "BURP_CAPTURE_PARENT_UNWRITABLE"),
    ],
)
def test_traffic_preflight_reports_environment_warnings(
    tmp_path,
    monkeypatch,
    condition,
    expected_code,
):
    capture_path = tmp_path / "capture.jsonl"

    if condition == "directory":
        capture_path.mkdir()
    elif condition == "missing_parent":
        capture_path = tmp_path / "missing" / "capture.jsonl"
    elif condition != "unwritable_parent":
        capture_path.write_text("")

    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight._tcp_reachable",
        lambda *args, **kwargs: condition != "proxy",
    )
    if condition == "unreadable":
        monkeypatch.setattr(
            "mobile_playbook.platforms.ios.preflight._capture_readable",
            lambda path: False,
        )
    if condition == "unwritable_parent":
        monkeypatch.setattr(
            "mobile_playbook.platforms.ios.preflight._path_writable",
            lambda path: False,
        )

    warnings = check_traffic_interception_preflight(
        _traffic_config(capture_path),
        [(_traffic_app(), "ios-feature-02-risk-01")],
    )

    assert expected_code in _warning_codes(warnings)


def test_empty_expected_hosts_warns_without_failing_preflight(tmp_path, monkeypatch):
    capture_path = tmp_path / "capture.jsonl"
    config = _traffic_config(capture_path, expected_hosts=())
    _write_fresh_health(capture_path)
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight.connected_device_udids", lambda: {"device-1"})

    warnings = check_traffic_interception_preflight(config, [(_traffic_app(), "ios-feature-02-risk-01")])
    result = check_ios_preflight(
        SimpleNamespace(device=SimpleNamespace(udid="device-1", team_id="TEAM", appium_server_url="http://appium"))
    )

    assert "BURP_EXPECTED_HOSTS_EMPTY" in _warning_codes(warnings)
    assert result.ok


def test_shared_effective_capture_configuration_is_checked_once(tmp_path, monkeypatch):
    capture_path = tmp_path / "capture.jsonl"
    config = _traffic_config(capture_path)
    _write_fresh_health(capture_path)
    calls = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight._tcp_reachable",
        lambda *args, **kwargs: calls.append(args[0]) or True,
    )

    warnings = check_traffic_interception_preflight(
        config,
        [
            (_traffic_app("app-one"), "ios-feature-02-risk-01"),
            (_traffic_app("app-two"), "ios-feature-02-risk-01"),
        ],
    )

    assert calls == ["HTTP://LOCALHOST:8080/"]
    assert warnings == []


def test_per_app_capture_overrides_are_checked_separately(tmp_path, monkeypatch):
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    config = _traffic_config(first_path)
    _write_fresh_health(first_path)
    _write_fresh_health(second_path)
    calls = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight._tcp_reachable",
        lambda *args, **kwargs: calls.append(args[0]) or True,
    )

    warnings = check_traffic_interception_preflight(
        config,
        [
            (_traffic_app("app-one"), "ios-feature-02-risk-01"),
            (
                _traffic_app("app-two", override={"burp": {"capture_path": str(second_path)}}),
                "ios-feature-02-risk-01",
            ),
        ],
    )

    assert len(calls) == 2
    assert warnings == []


def test_negative_health_max_age_is_rejected(global_config):
    global_config.traffic_interception = {
        "burp": {"proxy_url": "http://127.0.0.1:8080", "health_max_age_seconds": -1}
    }
    global_config.apps[0].risks = {"ios-feature-02-risk-01": {"enabled": True}}

    with pytest.raises(ConfigError, match="health_max_age_seconds must be non-negative"):
        validate_config(global_config, dry_run=True)
