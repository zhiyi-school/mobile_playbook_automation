from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.api.job_registry import JobRegistry
from mobile_playbook.api.models import RunRequest
from mobile_playbook.api.services import runs as runs_service


class Outcome:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    registry = JobRegistry(persist_path=None)
    monkeypatch.setattr(runs_service, "registry", registry)
    monkeypatch.setattr(
        runs_service, "load_config_or_400", lambda platform, config_path: SimpleNamespace(apps=[])
    )
    monkeypatch.setattr(runs_service, "validate_app_selection", lambda apps, selected: None)
    monkeypatch.setattr(runs_service, "validate_risk_selection", lambda risks, selected: None)
    monkeypatch.setattr(runs_service, "reserve_run_timestamp", lambda out_dir: "2026-01-01_00-00-00")
    monkeypatch.setattr(runs_service, "trigger_dashboard_sync", lambda *args: None)
    return registry


def test_in_flight_run_reports_the_apps_and_risks_it_covers(isolated_registry, monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking_run_platform(config, runner, options, factory, run_timestamp):
        started.set()
        release.wait(timeout=5)
        return Outcome(tmp_path / run_timestamp)

    monkeypatch.setattr(runs_service, "run_platform", blocking_run_platform)

    runs_service.create_run(
        RunRequest(
            platform="ios",
            config_path="configs/ios.yaml",
            apps="parents_gateway",
            risks="ios-feature-01-risk-01",
            out_dir=str(tmp_path),
        )
    )
    assert started.wait(timeout=5)

    try:
        in_flight = [run for run in runs_service.list_runs() if run["status"] == "running"]
        assert len(in_flight) == 1
        assert in_flight[0]["apps"] == "parents_gateway"
        assert in_flight[0]["risks"] == "ios-feature-01-risk-01"
    finally:
        release.set()


def test_run_without_a_selection_records_no_apps_or_risks(isolated_registry, monkeypatch, tmp_path):
    monkeypatch.setattr(
        runs_service, "run_platform", lambda *args, **kwargs: Outcome(tmp_path / "2026-01-01_00-00-00")
    )

    runs_service.create_run(
        RunRequest(platform="ios", config_path="configs/ios.yaml", out_dir=str(tmp_path))
    )

    record = runs_service.get_run("2026-01-01_00-00-00")
    assert record["apps"] is None
    assert record["risks"] is None
