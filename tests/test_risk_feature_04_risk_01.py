from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.core import network
from mobile_playbook.report import ReportWriter
from mobile_playbook.platforms.ios import keyboard_resign, keyboard_setup
from mobile_playbook.platforms.ios.models import InstallResult
from mobile_playbook.platforms.ios.risks.feature_04_risk_01 import Feature04Risk01
from tests.conftest import MockDevice


class CollectionServer:
    def __init__(self, host="0.0.0.0", port=0, token=None, enqueue_requires_token=False):
        self.host = host
        self.port = 12345 if int(port) == 0 else int(port)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.state = SimpleNamespace(token=token or "test-token")
        self.stopped = False
        self.events = [{"payload": {"text": "hello123"}}]

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def wait_for_pair(self, timeout_seconds):
        return True

    def snapshot(self):
        return {
            "base_url": self.base_url,
            "paired": True,
            "events_count": len(self.events),
            "events": list(self.events),
            "requests": [{"method": "POST", "path": "/events", "status": 200}],
            "errors": [],
        }


class EmptyCollectionServer(CollectionServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []


class LocalLogDevice(MockDevice):
    def launch_app(self, bundle_id: str) -> dict:
        if bundle_id == "com.example.keyboard":
            self.source = "<App><Text>Collected: hello</Text></App>"
        return {"ok": True, "bundle_id": bundle_id}


class SplitLocalLogDevice(MockDevice):
    def launch_app(self, bundle_id: str) -> dict:
        if bundle_id == "com.example.keyboard":
            self.source = "<App><Cell>h</Cell><Cell>e</Cell><Cell>l</Cell><Cell>l</Cell><Cell>o</Cell></App>"
        return {"ok": True, "bundle_id": bundle_id}


def test_feature_04_risk_01_reports_risk_when_keyboard_local_log_contains_probe_text(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {
                "bundle_id": "com.example.keyboard",
                "ipa": str(tmp_path / "Keyboard.ipa"),
                "server_setup": {"server_url_input_accessibility_id": "server-url-input"},
            },
            "collection": {
                "port": 0,
                "advertised_host": "192.168.1.9",
                "pair_timeout_seconds": 1,
                "evidence_source": "local_app_ui",
                "evidence_timeout_seconds": 0.01,
                "probe_text": "hello",
                "expected_collected_text": "hello",
                "input": {"method": "keyboard_buttons", "key_accessibility_ids": {"h": "H", "e": "E", "l": "L", "o": "O"}},
            },
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")
    device = LocalLogDevice()

    result = Feature04Risk01(CollectionServer).run(app, global_config, device, writer)

    assert result.final_status == "RISK_EXISTS"
    assert result.verdict == "At Risk"
    assert result.behavior_result.status == "PASS"
    assert device.typed_text[0]["text"] == "hello"
    assert device.text_entries[0]["text"] == "http://192.168.1.9:12345"
    assert result.behavior_result.metadata["evidence_source"] == "local_app_ui"


def test_feature_04_risk_01_advertises_the_detected_lan_address_when_set_to_auto(
    global_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(network, "detect_lan_ip", lambda: "10.0.0.8")
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {
                "bundle_id": "com.example.keyboard",
                "ipa": str(tmp_path / "Keyboard.ipa"),
                "server_setup": {"server_url_input_accessibility_id": "server-url-input"},
            },
            "collection": {
                "port": 0,
                "bind_host": "0.0.0.0",
                "advertised_host": "auto",
                "pair_timeout_seconds": 1,
                "evidence_source": "local_app_ui",
                "evidence_timeout_seconds": 0.01,
                "probe_text": "hello",
                "expected_collected_text": "hello",
                "input": {"method": "keyboard_buttons", "key_accessibility_ids": {"h": "H", "e": "E", "l": "L", "o": "O"}},
            },
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")
    device = LocalLogDevice()
    started: list[CollectionServer] = []

    def server_factory(**kwargs):
        server = CollectionServer(**kwargs)
        started.append(server)
        return server

    result = Feature04Risk01(server_factory).run(app, global_config, device, writer)

    assert device.text_entries[0]["text"] == "http://10.0.0.8:12345"
    assert started[0].host == "0.0.0.0"
    assert result.final_status == "RISK_EXISTS"


def test_feature_04_risk_01_matches_split_local_keystroke_log(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard", "ipa": str(tmp_path / "Keyboard.ipa")},
            "collection": {
                "port": 0,
                "pair_timeout_seconds": 1,
                "evidence_source": "local_app_ui",
                "evidence_timeout_seconds": 0.01,
                "probe_text": "hello",
                "local_log": {"expected_items": ["h", "e", "l", "l", "o"]},
                "input": {"method": "keyboard_buttons", "key_accessibility_ids": {"h": "H", "e": "E", "l": "L", "o": "O"}},
            },
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature04Risk01(CollectionServer).run(app, global_config, SplitLocalLogDevice(), writer)

    assert result.final_status == "RISK_EXISTS"
    assert result.behavior_result.metadata["match"]["mode"] == "ordered_items"


def test_feature_04_risk_01_reports_when_local_log_is_missing_probe_text(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard", "ipa": str(tmp_path / "Keyboard.ipa")},
            "collection": {
                "port": 0,
                "pair_timeout_seconds": 1,
                "evidence_source": "local_app_ui",
                "evidence_timeout_seconds": 0.01,
                "probe_text": "hello123",
            },
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature04Risk01(EmptyCollectionServer).run(app, global_config, MockDevice(), writer)

    assert result.final_status == "KEYSTROKE_COLLECTION_NOT_OBSERVED"
    assert result.verdict == "Reduced Risk"
    assert "local log UI" in result.errors[0]


def test_feature_04_risk_01_can_still_use_server_events_as_collection_evidence(global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard", "ipa": str(tmp_path / "Keyboard.ipa")},
            "collection": {
                "port": 0,
                "pair_timeout_seconds": 1,
                "evidence_source": "server_events",
                "event_timeout_seconds": 0.01,
                "probe_text": "hello123",
            },
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature04Risk01(CollectionServer).run(app, global_config, MockDevice(), writer)

    assert result.final_status == "RISK_EXISTS"
    assert result.behavior_result.metadata["evidence_source"] == "server_events"


def test_feature_04_risk_01_reports_secure_text_field_as_custom_keyboard_unavailable(global_config, tmp_path):
    class SecureFieldDevice(MockDevice):
        def tap_text_field(self, selector=None):
            return {
                "tapped": True,
                "selector": selector or {"auto": True},
                "element_type": "XCUIElementTypeSecureTextField",
            }

    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard", "ipa": str(tmp_path / "Keyboard.ipa")},
            "collection": {"port": 0, "pair_timeout_seconds": 1, "probe_text": "hello123"},
        }
    }
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature04Risk01(CollectionServer).run(app, global_config, SecureFieldDevice(), writer)

    assert result.final_status == "CUSTOM_KEYBOARD_NOT_AVAILABLE"
    assert result.verdict == "Reduced Risk"
    assert "secure text field" in result.errors[0]


class InstallRecordingDevice(MockDevice):
    """Returns a scripted InstallResult per call, so a retry can be told apart from the first attempt."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)
        self.install_calls: list[Path] = []

    def install_app(self, ipa_path, timeout_ms):
        self.install_calls.append(ipa_path)
        status, errors = self.results[min(len(self.install_calls) - 1, len(self.results) - 1)]
        return InstallResult(status=status, ipa_path=ipa_path, errors=list(errors))


def _install_keyboard(global_config, device, resign=None, **overrides):
    keyboard_config = {
        "bundle_id": "com.example.keyboard",
        "ipa": "artifacts/intake/ios/ipas/LocalKeyboard.ipa",
        "install": True,
    }
    if resign is not None:
        keyboard_config["resign"] = resign
    keyboard_config.update(overrides)
    return Feature04Risk01(CollectionServer)._install_or_verify_keyboard_app(
        keyboard_config, global_config, device
    )


def test_an_expired_profile_is_resigned_before_the_install_is_attempted(global_config, monkeypatch):
    calls: list = []
    monkeypatch.setattr(keyboard_resign, "signature_expired", lambda path, **kwargs: True)
    monkeypatch.setattr(
        keyboard_resign,
        "resign",
        lambda path, udid, team_id, timeout_seconds=900: calls.append((path, udid, team_id, timeout_seconds))
        or keyboard_resign.ResignResult(status="RESIGNED"),
    )
    device = InstallRecordingDevice([("INSTALLED", [])])

    outcome = _install_keyboard(global_config, device, resign={"enabled": True, "timeout_seconds": 120})

    assert outcome["resign_attempts"] == [{"trigger": "PROFILE_EXPIRED", "status": "RESIGNED", "errors": []}]
    assert len(device.install_calls) == 1
    assert calls == [(Path("artifacts/intake/ios/ipas/LocalKeyboard.ipa"), "udid", "TEAM", 120)]
    assert outcome["status"] == "INSTALLED"


def test_a_verification_rejection_resigns_once_and_retries_the_install(global_config, monkeypatch):
    monkeypatch.setattr(keyboard_resign, "signature_expired", lambda path, **kwargs: False)
    monkeypatch.setattr(
        keyboard_resign,
        "resign",
        lambda *args, **kwargs: keyboard_resign.ResignResult(status="RESIGNED"),
    )
    device = InstallRecordingDevice(
        [
            ("INSTALL_FAILED", ["ApplicationVerificationFailed: Failed to verify code signature"]),
            ("INSTALLED", []),
        ]
    )

    outcome = _install_keyboard(global_config, device, resign={"enabled": True})

    assert [attempt["trigger"] for attempt in outcome["resign_attempts"]] == ["INSTALL_REJECTED"]
    assert len(device.install_calls) == 2
    assert outcome["status"] == "INSTALLED"
    assert outcome["installed_by_risk"] is True


def test_a_failed_resign_is_recorded_and_the_install_is_not_retried(global_config, monkeypatch):
    monkeypatch.setattr(keyboard_resign, "signature_expired", lambda path, **kwargs: False)
    monkeypatch.setattr(
        keyboard_resign,
        "resign",
        lambda *args, **kwargs: keyboard_resign.ResignResult(status="RESIGN_FAILED", errors=["no identity"]),
    )
    device = InstallRecordingDevice(
        [("INSTALL_FAILED", ["ApplicationVerificationFailed: Failed to verify code signature"])]
    )

    outcome = _install_keyboard(global_config, device, resign={"enabled": True})

    assert outcome["resign_attempts"] == [
        {"trigger": "INSTALL_REJECTED", "status": "RESIGN_FAILED", "errors": ["no identity"]}
    ]
    assert len(device.install_calls) == 1
    assert outcome["status"] == "INSTALL_FAILED"


def test_an_expired_profile_is_not_resigned_a_second_time_when_the_install_still_fails(
    global_config, monkeypatch
):
    monkeypatch.setattr(keyboard_resign, "signature_expired", lambda path, **kwargs: True)
    monkeypatch.setattr(
        keyboard_resign,
        "resign",
        lambda *args, **kwargs: keyboard_resign.ResignResult(status="RESIGNED"),
    )
    device = InstallRecordingDevice(
        [("INSTALL_FAILED", ["ApplicationVerificationFailed: Failed to verify code signature"])]
    )

    outcome = _install_keyboard(global_config, device, resign={"enabled": True})

    assert [attempt["trigger"] for attempt in outcome["resign_attempts"]] == ["PROFILE_EXPIRED"]
    assert len(device.install_calls) == 1


def test_an_unrelated_install_failure_is_never_resigned(global_config, monkeypatch):
    monkeypatch.setattr(keyboard_resign, "signature_expired", lambda path, **kwargs: False)
    monkeypatch.setattr(
        keyboard_resign, "resign", lambda *args, **kwargs: pytest.fail("should not resign")
    )
    device = InstallRecordingDevice([("INSTALL_FAILED", ["Device is locked"])])

    outcome = _install_keyboard(global_config, device, resign={"enabled": True})

    assert outcome["resign_attempts"] == []
    assert len(device.install_calls) == 1


def test_resigning_disabled_neither_checks_the_profile_nor_resigns(global_config, monkeypatch):
    monkeypatch.setattr(
        keyboard_resign, "signature_expired", lambda path, **kwargs: pytest.fail("should not read the profile")
    )
    monkeypatch.setattr(
        keyboard_resign, "resign", lambda *args, **kwargs: pytest.fail("should not resign")
    )
    device = InstallRecordingDevice(
        [("INSTALL_FAILED", ["ApplicationVerificationFailed: Failed to verify code signature"])]
    )

    outcome = _install_keyboard(global_config, device, resign={"enabled": False})

    assert outcome["resign_attempts"] == []
    assert len(device.install_calls) == 1


def test_resigning_is_off_when_the_config_says_nothing(global_config, monkeypatch):
    monkeypatch.setattr(
        keyboard_resign, "signature_expired", lambda path, **kwargs: pytest.fail("should not read the profile")
    )
    device = InstallRecordingDevice(
        [("INSTALL_FAILED", ["ApplicationVerificationFailed: Failed to verify code signature"])]
    )

    outcome = _install_keyboard(global_config, device)

    assert outcome["resign_attempts"] == []


class OrderRecordingDevice(LocalLogDevice):
    """Records the steps that bracket the network-alert sweep; the run sweeps alerts more than once."""

    def __init__(self, order: list[tuple[str, dict | None]]):
        super().__init__()
        self.order = order

    def set_text_by_accessibility_id(self, accessibility_id, value, clear_first=True):
        self.order.append(("server_url", None))
        return super().set_text_by_accessibility_id(accessibility_id, value, clear_first=clear_first)

    def handle_permission_alerts(self, config=None):
        self.order.append(("permission_alerts", dict(config or {})))
        return super().handle_permission_alerts(config)


def _network_alert_app(global_config, tmp_path, collection_overrides=None):
    app = global_config.apps[0]
    collection = {
        "port": 0,
        "advertised_host": "192.168.1.9",
        "pair_timeout_seconds": 1,
        "evidence_source": "local_app_ui",
        "evidence_timeout_seconds": 0.01,
        "probe_text": "hello",
        "expected_collected_text": "hello",
        "input": {"method": "keyboard_buttons", "key_accessibility_ids": {"h": "H", "e": "E", "l": "L", "o": "O"}},
    }
    collection.update(collection_overrides or {})
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {
                "bundle_id": "com.example.keyboard",
                "ipa": str(tmp_path / "Keyboard.ipa"),
                "server_setup": {"server_url_input_accessibility_id": "server-url-input"},
            },
            "collection": collection,
        }
    }
    return app


def _run_with_recorded_order(global_config, tmp_path, monkeypatch, collection_overrides=None):
    order: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        keyboard_setup,
        "prepare_custom_keyboard",
        lambda device, config, report_dir: order.append(("keyboard_setup", None)) or {"status": "SKIPPED"},
    )
    app = _network_alert_app(global_config, tmp_path, collection_overrides)
    device = OrderRecordingDevice(order)
    result = Feature04Risk01(CollectionServer).run(
        app, global_config, device, ReportWriter(tmp_path / "reports", "run1")
    )
    return result, order


def _sweep_config(order: list[tuple[str, dict | None]]) -> dict:
    """The sweep this fix added is the first one after the server URL is entered."""
    labels = [label for label, _ in order]
    server = labels.index("server_url")
    sweep = labels.index("permission_alerts", server)
    assert server < sweep < labels.index("keyboard_setup")
    return order[sweep][1] or {}


def test_the_network_alert_sweep_runs_between_the_server_url_and_the_keyboard_setup(
    global_config, tmp_path, monkeypatch
):
    result, order = _run_with_recorded_order(global_config, tmp_path, monkeypatch)

    labels = [label for label, _ in order]
    server = labels.index("server_url")
    sweep = labels.index("permission_alerts", server)
    assert server < sweep < labels.index("keyboard_setup")
    assert result.launch_result["keyboard_network_alerts"][0]["status"] == "NO_ALERT"


def test_the_sweep_waits_longer_than_the_runner_default_without_losing_the_rest_of_it(
    global_config, tmp_path, monkeypatch
):
    global_config.runner.permission_alerts = {
        "enabled": True,
        "action": "dismiss",
        "accept_if_text_contains": ["local network"],
        "wait_seconds": 2,
        "max_alerts": 3,
    }

    _, order = _run_with_recorded_order(
        global_config, tmp_path, monkeypatch, {"network_alert_wait_seconds": 12}
    )
    sweep = _sweep_config(order)

    assert sweep["wait_seconds"] == 12
    assert sweep["accept_if_text_contains"] == ["local network"]
    assert sweep["action"] == "dismiss"
    assert sweep["max_alerts"] == 3
    assert global_config.runner.permission_alerts["wait_seconds"] == 2


def test_only_the_network_sweep_gets_the_longer_wait(global_config, tmp_path, monkeypatch):
    global_config.runner.permission_alerts = {"action": "dismiss", "wait_seconds": 2}

    _, order = _run_with_recorded_order(
        global_config, tmp_path, monkeypatch, {"network_alert_wait_seconds": 12}
    )

    labels = [label for label, _ in order]
    sweep = labels.index("permission_alerts", labels.index("server_url"))
    others = [config for index, (label, config) in enumerate(order) if label == "permission_alerts" and index != sweep]

    assert others and all((config or {}).get("wait_seconds") == 2 for config in others)


def test_the_sweep_falls_back_to_ten_seconds_when_the_risk_configures_nothing(
    global_config, tmp_path, monkeypatch
):
    _, order = _run_with_recorded_order(global_config, tmp_path, monkeypatch)

    assert _sweep_config(order)["wait_seconds"] == 10.0


def test_a_device_that_cannot_sweep_alerts_does_not_fail_the_run(global_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        keyboard_setup, "prepare_custom_keyboard", lambda *args, **kwargs: {"status": "SKIPPED"}
    )
    app = _network_alert_app(global_config, tmp_path)
    device = OrderRecordingDevice([])
    monkeypatch.setattr(device, "handle_permission_alerts", None, raising=False)

    result = Feature04Risk01(CollectionServer).run(
        app, global_config, device, ReportWriter(tmp_path / "reports", "run1")
    )

    assert result.launch_result["keyboard_network_alerts"][0]["status"] == "UNSUPPORTED"


def _run_capturing_setup_config(global_config, tmp_path, monkeypatch, keyboard_app_extra=None, setup=None):
    captured: list[dict] = []
    monkeypatch.setattr(
        keyboard_setup,
        "prepare_custom_keyboard",
        lambda device, config, report_dir: captured.append(dict(config)) or {"status": "SKIPPED"},
    )
    app = _network_alert_app(global_config, tmp_path, {"keyboard_setup": setup} if setup is not None else None)
    app.risks["ios-feature-04-risk-01"]["keyboard_app"].update(keyboard_app_extra or {})
    Feature04Risk01(CollectionServer).run(
        app, global_config, OrderRecordingDevice([]), ReportWriter(tmp_path / "reports", "run1")
    )
    return captured[0]


def test_the_keyboard_extension_bundle_id_reaches_the_setup_without_being_configured_twice(
    global_config, tmp_path, monkeypatch
):
    config = _run_capturing_setup_config(
        global_config,
        tmp_path,
        monkeypatch,
        keyboard_app_extra={"keyboard_extension_bundle_id": "com.example.keyboard.KeyboardExtension"},
        setup={"mode": "appium_ui", "keyboard_display_names": ["LocalKeyboard"]},
    )

    assert config["keyboard_extension_bundle_id"] == "com.example.keyboard.KeyboardExtension"
    assert config["keyboard_display_names"] == ["LocalKeyboard"]


def test_a_bundle_id_named_in_the_setup_block_itself_is_not_overwritten(global_config, tmp_path, monkeypatch):
    config = _run_capturing_setup_config(
        global_config,
        tmp_path,
        monkeypatch,
        keyboard_app_extra={"keyboard_extension_bundle_id": "com.example.keyboard.KeyboardExtension"},
        setup={"mode": "appium_ui", "keyboard_extension_bundle_id": "com.example.override.Extension"},
    )

    assert config["keyboard_extension_bundle_id"] == "com.example.override.Extension"
