from __future__ import annotations

from types import SimpleNamespace

from mobile_playbook.report import ReportWriter
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
