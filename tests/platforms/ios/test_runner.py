from __future__ import annotations

import json
from types import SimpleNamespace

from mobile_playbook.platforms.ios.runner import LEFTOVER_PROMPT_WAIT_SECONDS, IosPlatformRunner

PROMPT = {"status": "HANDLED", "action": "dismiss", "text": "“App” Would Like to Send You Notifications", "button": "Don't Allow"}


class SweepDevice:
    def __init__(self, results=None, recovered=None):
        self.results = list(results or [])
        self.alert_calls: list[dict] = []
        self.recovered = list(recovered or [])
        self.calls: list[str] = []
        self.prompt_recovery_config = {"action": "dismiss"}

    def handle_permission_alerts(self, config=None):
        self.calls.append("prompts")
        self.alert_calls.append(config)
        return [self.results.pop(0)] if self.results else [{"status": "NO_ALERT"}]

    def drain_recovered_prompts(self):
        recovered, self.recovered = self.recovered, []
        return recovered

    def unlock(self):
        return {"was_locked": False}


class Writer:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.run_timestamp = "2026-01-01_00-00-00"
        self.written = []

    def test_report_dir(self, app_id, test_id, case_id, platform=None):
        return self.run_dir / app_id / test_id / case_id

    def write_result(self, result, report_dir):
        self.written.append(result)


def _config():
    return SimpleNamespace(
        runner=SimpleNamespace(permission_alerts={"action": "dismiss", "wait_seconds": 2, "foreground_bundle_id": "com.example.app"})
    )


def _app():
    return SimpleNamespace(id="app", name="App", bundle_id="com.example.app", test_bundle_id="", artifact={}, risks={})


def _risk(device, error=None, requires_device=True):
    def run(app, config, device_client, report_writer):
        device.calls.append("risk")
        if error is not None:
            raise error

    return SimpleNamespace(run=run, test_case_id="case", requires_device=requires_device)


def _events(run_dir):
    path = run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_prompts_are_swept_before_and_after_each_test(monkeypatch, tmp_path):
    device = SweepDevice(results=[{"status": "NO_ALERT"}, PROMPT])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _risk(device))

    IosPlatformRunner().run_test(_app(), "risk_id", _config(), device, Writer(tmp_path))

    assert device.calls == ["prompts", "risk", "prompts"]
    assert device.alert_calls == [{"action": "dismiss", "wait_seconds": LEFTOVER_PROMPT_WAIT_SECONDS}] * 2
    events = _events(tmp_path)
    assert [(event["type"], event["risk_id"], event["stage"], event["prompts"]) for event in events] == [
        ("permission_prompts_answered", "risk_id", "after_test", [PROMPT])
    ]
    assert events[0]["message"] == "ios: answered 1 system prompt(s) left on screen after test risk_id."


def test_prompts_are_swept_after_a_test_that_raises(monkeypatch, tmp_path):
    device = SweepDevice(results=[{"status": "NO_ALERT"}, PROMPT])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _risk(device, RuntimeError("boom")))
    writer = Writer(tmp_path)

    IosPlatformRunner().run_test(_app(), "risk_id", _config(), device, writer)

    assert device.calls == ["prompts", "risk", "prompts"]
    assert writer.written[0].errors == ["boom"]
    assert [event["stage"] for event in _events(tmp_path)] == ["after_test"]


def test_prompts_answered_during_a_test_are_recorded(monkeypatch, tmp_path):
    recovered = [{"operation": "tap_label", **PROMPT}]
    device = SweepDevice(recovered=recovered)
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _risk(device))

    IosPlatformRunner().run_test(_app(), "risk_id", _config(), device, Writer(tmp_path))

    assert [(event["stage"], event["prompts"], event["message"]) for event in _events(tmp_path)] == [
        ("during_test", recovered, "ios: answered 1 system prompt(s) that blocked steps during risk_id.")
    ]


def test_tests_without_a_device_are_not_swept(monkeypatch, tmp_path):
    device = SweepDevice(results=[PROMPT])
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _risk(device, requires_device=False))

    IosPlatformRunner().run_test(_app(), "risk_id", _config(), device, Writer(tmp_path))
    IosPlatformRunner().run_test(_app(), "risk_id", _config(), None, Writer(tmp_path))

    assert device.calls == ["risk", "risk"]
    assert _events(tmp_path) == []


def test_a_failing_sweep_does_not_fail_the_test(monkeypatch, tmp_path):
    device = SweepDevice()

    def broken(config=None):
        device.calls.append("prompts")
        raise RuntimeError("session gone")

    device.handle_permission_alerts = broken
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _risk(device))
    writer = Writer(tmp_path)

    IosPlatformRunner().run_test(_app(), "risk_id", _config(), device, writer)

    assert device.calls == ["prompts", "risk", "prompts"]
    assert writer.written == []


def test_the_connected_client_recovers_with_the_configured_prompt_rules(monkeypatch, tmp_path):
    from mobile_playbook.orchestration.appium_server import AppiumStartResult

    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.runner.ensure_appium_running",
        lambda url, cfg, log_path: AppiumStartResult(status="ALREADY_RUNNING"),
    )
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.check_ios_preflight", lambda config: SimpleNamespace(ok=True, errors=[]))
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect", lambda self: self)
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.AppiumDeviceClient.unlock", lambda self: {"was_locked": False})
    rules = {"action": "dismiss", "accept_if_text_contains": ["find and connect to devices on your local network"]}
    config = SimpleNamespace(
        device=SimpleNamespace(appium_server_url="http://127.0.0.1:4723", appium_auto_start=None),
        runner=SimpleNamespace(permission_alerts=rules),
    )

    client = IosPlatformRunner().connect_device(config, tmp_path)

    assert client.prompt_recovery_config == rules


LOCAL_NETWORK_RULE = {"accept_if_text_contains": ["find and connect to devices on your local network"]}


def _recording_risk(device, seen):
    def run(app, config, device_client, report_writer):
        seen["config_rules"] = config.runner.permission_alerts
        seen["recovery_rules"] = dict(device_client.prompt_recovery_config)
        device.calls.append("risk")

    return SimpleNamespace(run=run, test_case_id="case", requires_device=True)


def test_a_risk_with_its_own_prompt_rules_uses_them_for_the_whole_test(monkeypatch, tmp_path, global_config):
    global_config.runner.permission_alerts = {"action": "dismiss", "wait_seconds": 2}
    global_config.keystroke_collection = {"permission_alerts": LOCAL_NETWORK_RULE}
    device = SweepDevice()
    seen = {}
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _recording_risk(device, seen))
    app = global_config.apps[0]

    IosPlatformRunner().run_test(app, "ios-feature-04-risk-01", global_config, device, Writer(tmp_path))

    expected = {"action": "dismiss", "wait_seconds": 2, **LOCAL_NETWORK_RULE}
    assert seen == {"config_rules": expected, "recovery_rules": expected}
    assert device.alert_calls == [{**expected, "wait_seconds": LEFTOVER_PROMPT_WAIT_SECONDS}] * 2
    assert device.prompt_recovery_config == {"action": "dismiss"}
    assert global_config.runner.permission_alerts == {"action": "dismiss", "wait_seconds": 2}


def test_other_risks_keep_the_runner_prompt_rules(monkeypatch, tmp_path, global_config):
    global_config.runner.permission_alerts = {"action": "dismiss", "wait_seconds": 2}
    global_config.keystroke_collection = {"permission_alerts": LOCAL_NETWORK_RULE}
    device = SweepDevice()
    seen = {}
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _recording_risk(device, seen))
    app = global_config.apps[0]

    IosPlatformRunner().run_test(app, "ios-feature-03-risk-01", global_config, device, Writer(tmp_path))

    assert seen == {"config_rules": {"action": "dismiss", "wait_seconds": 2}, "recovery_rules": {"action": "dismiss", "wait_seconds": 2}}
    assert all("accept_if_text_contains" not in call for call in device.alert_calls)


def test_an_app_can_override_a_risks_prompt_rules(monkeypatch, tmp_path, global_config):
    global_config.runner.permission_alerts = {"action": "dismiss"}
    global_config.keystroke_collection = {"permission_alerts": LOCAL_NETWORK_RULE}
    app = global_config.apps[0]
    app.risks["ios-feature-04-risk-01"] = {"enabled": True, "permission_alerts": {"accept_if_text_contains": ["custom prompt"]}}
    device = SweepDevice()
    seen = {}
    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.get_risk", lambda test_id: _recording_risk(device, seen))

    IosPlatformRunner().run_test(app, "ios-feature-04-risk-01", global_config, device, Writer(tmp_path))

    assert seen["config_rules"] == {"action": "dismiss", "accept_if_text_contains": ["custom prompt"]}
