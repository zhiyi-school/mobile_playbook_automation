from __future__ import annotations

from types import SimpleNamespace

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from mobile_playbook.reporting.run_manifest import read_manifest


class DeviceRunner:
    platform = "fake"

    def __init__(self, planned):
        self.planned = planned
        self.order: list[tuple[str, str]] = []

    def requires_device(self, config, selected_tests, selected_apps):
        return True

    def connect_device(self, config, run_dir=None):
        return "client"

    def close_device(self, device_client):
        pass

    def ensure_device_healthy(self, config, device_client, run_dir=None):
        return device_client

    def iter_enabled_tests(self, config, selected_tests, selected_apps):
        yield from self.planned

    def run_test(self, app, test_id, config, device_client, report_writer):
        self.order.append(("risk", f"{app.id}/{test_id}"))


class VersionReadingRunner(DeviceRunner):
    def __init__(self, planned, versions):
        super().__init__(planned)
        self.versions = versions

    def installed_app_version(self, app, device_client):
        self.order.append(("version", app.id))
        value = self.versions[app.id]
        if isinstance(value, Exception):
            raise value
        return value


ONE = SimpleNamespace(id="app_one")
TWO = SimpleNamespace(id="app_two")


def _run(runner, tmp_path):
    return run_platform(
        config=object(),
        platform_runner=runner,
        options=RunOptions(out_dir=tmp_path),
        report_writer_factory=lambda out_dir, run_timestamp: ReportWriter(out_dir, run_timestamp),
    )


def test_each_apps_installed_version_is_read_once_before_its_first_risk(tmp_path):
    runner = VersionReadingRunner(
        [(ONE, "risk_a"), (ONE, "risk_b"), (TWO, "risk_a")],
        {"app_one": ("3.2.1", "321"), "app_two": ("1.0", None)},
    )

    outcome = _run(runner, tmp_path)

    assert runner.order == [
        ("version", "app_one"),
        ("risk", "app_one/risk_a"),
        ("risk", "app_one/risk_b"),
        ("version", "app_two"),
        ("risk", "app_two/risk_a"),
    ]
    assert read_manifest(outcome.run_dir)["artifact_versions"] == {"app_one": "3.2.1", "app_two": "1.0"}
    events, _ = read_events(outcome.run_dir)
    recorded = [event for event in events if event["type"] == "app_version"]
    assert [(e["app_id"], e["version"], e["build_number"]) for e in recorded] == [
        ("app_one", "3.2.1", "321"),
        ("app_two", "1.0", None),
    ]
    assert recorded[0]["message"] == "Testing app_one version 3.2.1 (build 321)."
    assert events.index(recorded[0]) < next(i for i, e in enumerate(events) if e["type"] == "risk_started")


def test_an_unreadable_version_is_left_out_and_the_risks_still_run(tmp_path):
    runner = VersionReadingRunner(
        [(ONE, "risk_a"), (TWO, "risk_a")],
        {"app_one": None, "app_two": RuntimeError("device offline")},
    )

    outcome = _run(runner, tmp_path)

    assert ("risk", "app_one/risk_a") in runner.order
    assert ("risk", "app_two/risk_a") in runner.order
    manifest = read_manifest(outcome.run_dir)
    assert manifest["status"] == "completed"
    assert manifest["artifact_versions"] == {}
    events, _ = read_events(outcome.run_dir)
    assert not [event for event in events if event["type"] == "app_version"]


def test_a_platform_without_the_reader_records_no_installed_version(tmp_path):
    runner = DeviceRunner([(ONE, "risk_a")])

    outcome = _run(runner, tmp_path)

    assert runner.order == [("risk", "app_one/risk_a")]
    assert read_manifest(outcome.run_dir)["artifact_versions"] == {}
    events, _ = read_events(outcome.run_dir)
    assert [event["type"] for event in events if event["type"] == "app_version"] == []
