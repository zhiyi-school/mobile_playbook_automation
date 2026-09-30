from __future__ import annotations

import json

import pytest

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_manifest import MANIFEST_NAME, read_manifest, write_manifest


class FakeRunner:
    platform = "fake"

    def __init__(self, tests=(("app_one", "risk_one"), ("app_one", "risk_two"))):
        self.tests = tests
        self.run_test_calls = []

    def requires_device(self, config, selected_tests, selected_apps):
        return False

    def connect_device(self, config, run_dir=None):
        return "client"

    def close_device(self, device_client):
        pass

    def ensure_device_healthy(self, config, device_client, run_dir=None):
        return device_client

    def iter_enabled_tests(self, config, selected_tests, selected_apps):
        yield from self.tests

    def run_test(self, app, test_id, config, device_client, report_writer):
        self.run_test_calls.append((app, test_id))


def _run(runner, tmp_path):
    return run_platform(
        config=object(),
        platform_runner=runner,
        options=RunOptions(out_dir=tmp_path),
        report_writer_factory=lambda out_dir, run_timestamp: ReportWriter(out_dir, run_timestamp),
    )


def test_completed_run_records_a_completed_manifest(tmp_path):
    outcome = _run(FakeRunner(), tmp_path)

    manifest = read_manifest(outcome.run_dir)

    assert manifest["status"] == "completed"
    assert manifest["error"] is None
    assert manifest["platform"] == "fake"
    assert manifest["run_timestamp"] == outcome.run_timestamp
    assert manifest["apps"] == ["app_one"]
    assert manifest["risks"] == ["risk_one", "risk_two"]
    assert manifest["started_at"] and manifest["completed_at"]


def test_recorded_per_risk_failure_still_completes_the_run(tmp_path):
    class RecordsFailureRunner(FakeRunner):
        def run_test(self, app, test_id, config, device_client, report_writer):
            self.run_test_calls.append((app, test_id))
            try:
                raise RuntimeError(f"{test_id} blew up")
            except RuntimeError as exc:
                self.recorded.append(str(exc))

        recorded: list[str] = []

    runner = RecordsFailureRunner()
    outcome = _run(runner, tmp_path)

    # A platform runner that catches a risk failure and records it as a result row
    # never propagates to run_platform, so the run itself is still completed.
    assert runner.recorded == ["risk_one blew up", "risk_two blew up"]
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert read_manifest(outcome.run_dir)["error"] is None
    assert len(runner.run_test_calls) == 2


def test_fatal_orchestration_failure_records_a_failed_manifest(tmp_path):
    class ExplodingRunner(FakeRunner):
        def run_test(self, app, test_id, config, device_client, report_writer):
            super().run_test(app, test_id, config, device_client, report_writer)
            raise RuntimeError("device fell off the bus")

    runner = ExplodingRunner()
    with pytest.raises(RuntimeError, match="device fell off the bus"):
        _run(runner, tmp_path)

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    manifest = read_manifest(run_dir)

    assert manifest["status"] == "failed"
    assert "device fell off the bus" in manifest["error"]
    assert manifest["attempted"] == [{"app_id": "app_one", "risk_id": "risk_one"}]
    assert (run_dir / "summary.md").exists()


def test_summary_write_failure_does_not_hide_the_run_failure(tmp_path):
    class BrokenSummaryWriter(ReportWriter):
        def write_summary(self):
            raise OSError("disk full")

    class ExplodingRunner(FakeRunner):
        def run_test(self, app, test_id, config, device_client, report_writer):
            raise RuntimeError("original failure")

    # The OSError from write_summary must not surface in place of the run failure.
    with pytest.raises(RuntimeError, match="original failure"):
        run_platform(
            config=object(),
            platform_runner=ExplodingRunner(),
            options=RunOptions(out_dir=tmp_path),
            report_writer_factory=lambda out_dir, run_timestamp: BrokenSummaryWriter(out_dir, run_timestamp),
        )

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    manifest = read_manifest(run_dir)
    assert manifest["status"] == "failed"
    assert "original failure" in manifest["error"]
    assert not (run_dir / "summary.md").exists()


def test_manifest_write_is_atomic(tmp_path):
    write_manifest(
        tmp_path,
        run_timestamp="2026-01-01_00-00-00",
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:02+00:00",
    )

    assert json.loads((tmp_path / MANIFEST_NAME).read_text())["status"] == "completed"
    assert not list(tmp_path.glob(".run_manifest.*.tmp"))


def test_unreadable_manifest_reads_as_absent(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{ not json")

    assert read_manifest(tmp_path) is None
