from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from mobile_playbook.dashboard_sync import sync_reports
from mobile_playbook.orchestration import scheduler
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.test_dashboard_sync import FakeStore

FROZEN = datetime(2026, 1, 1, 12, 0, 0).astimezone()


@pytest.fixture
def frozen_second(monkeypatch):
    """Every timestamp request in the test resolves to the same second."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN

    monkeypatch.setattr(scheduler, "datetime", FrozenDatetime)


class PlatformRunner:
    def __init__(self, platform: str):
        self.platform = platform
        self.started = threading.Event()

    def requires_device(self, config, selected_tests, selected_apps):
        return False

    def connect_device(self, config, run_dir=None):
        return None

    def close_device(self, device_client):
        pass

    def ensure_device_healthy(self, config, device_client, run_dir=None):
        return device_client

    def iter_enabled_tests(self, config, selected_tests, selected_apps):
        yield f"{self.platform}_app", f"{self.platform}_risk"

    def run_test(self, app, test_id, config, device_client, report_writer):
        report_writer.results.append(_Result(app, test_id, self.platform))


class _Result:
    def __init__(self, app_id: str, risk_id: str, platform: str):
        self.app_id = app_id
        self.risk_id = risk_id
        self.platform = platform
        self.test_case_id = "case"
        self.artifact_source = "local_ipa"
        self.artifact_result = None
        self.verdict = "At Risk"
        self.errors: list[str] = []

    def to_dict(self):
        return {
            "app_id": self.app_id,
            "app_name": f"Example {self.platform} App",
            "category": self.platform,
            "completed_at": "2026-01-01T12:00:02+00:00",
            "duration_seconds": 2,
            "evidence": [],
            "package_or_bundle_id": "com.example.app",
            "platform": self.platform,
            "raw": {"test_case_id": self.test_case_id},
            "report_path": f"{self.platform}/{self.app_id}/{self.risk_id}/{self.test_case_id}",
            "run_timestamp": "2026-01-01_12-00-00",
            "severity": "high",
            "started_at": "2026-01-01T12:00:00+00:00",
            "status": "RISK_EXISTS",
            "summary": "Example summary",
            "test_id": self.risk_id,
            "test_name": self.risk_id,
            "verdict": self.verdict,
        }


def _writer(platform: str):
    def factory(out_dir, run_timestamp):
        return ReportWriter(out_dir, run_timestamp, result_adapter=lambda r: r.to_dict(), platform=platform)

    return factory


def _run_concurrently(tmp_path, runners):
    outcomes: dict[str, object] = {}
    barrier = threading.Barrier(len(runners))

    def invoke(runner):
        barrier.wait()
        outcomes[runner.platform] = run_platform(
            config=object(),
            platform_runner=runner,
            options=RunOptions(out_dir=tmp_path),
            report_writer_factory=_writer(runner.platform),
        )

    threads = [threading.Thread(target=invoke, args=(runner,)) for runner in runners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_same_second_run_all_threads_get_distinct_run_directories(tmp_path, frozen_second):
    outcomes = _run_concurrently(tmp_path, [PlatformRunner("ios"), PlatformRunner("android")])

    ios_dir = outcomes["ios"].run_dir
    android_dir = outcomes["android"].run_dir

    assert ios_dir != android_dir
    assert {ios_dir.name, android_dir.name} == {"2026-01-01_12-00-00", "2026-01-01_12-00-00-2"}


def test_each_platform_keeps_its_own_top_level_report_files(tmp_path, frozen_second):
    outcomes = _run_concurrently(tmp_path, [PlatformRunner("ios"), PlatformRunner("android")])

    for platform, outcome in outcomes.items():
        run_dir = outcome.run_dir
        feed = json.loads((run_dir / "dashboard_results.json").read_text())

        assert [row["platform"] for row in feed] == [platform]
        assert (run_dir / "summary.md").exists()
        assert read_manifest(run_dir)["platform"] == platform
        assert read_manifest(run_dir)["status"] == "completed"
        assert read_manifest(run_dir)["apps"] == [f"{platform}_app"]


def test_worker_discovers_and_syncs_both_platform_runs(tmp_path, frozen_second):
    _run_concurrently(tmp_path, [PlatformRunner("ios"), PlatformRunner("android")])
    store = FakeStore()

    summary = sync_reports(tmp_path, store, risk_counts={"ios": 1, "android": 1})

    assert summary.reports == 2
    assert summary.skipped_reports == 0
    assert summary.failed_reports == 0
    assert sorted(row["external_id"] for row in store.applications) == ["android_app", "ios_app"]
    assert len(store.assessments) == 2


def test_reserve_run_timestamp_is_unique_under_concurrency(tmp_path, frozen_second):
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        timestamp = scheduler.reserve_run_timestamp(tmp_path)
        with lock:
            claimed.append(timestamp)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 8
    assert len(set(claimed)) == 8
