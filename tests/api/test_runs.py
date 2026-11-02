from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from mobile_playbook.api.routes import runs as runs_routes
from mobile_playbook.api.run_registry import JobRegistry
from mobile_playbook.api.schemas import RunRequest
from mobile_playbook.api.services import runs as runs_service
from tests.asgi_helpers import call


class Outcome:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir


def _app(app_id: str, **risks: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=app_id, name=app_id, package_name="", bundle_id="", risks={risk: {"enabled": on} for risk, on in risks.items()}
    )


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
    monkeypatch.setattr(runs_service, "REPORTS_ROOT", tmp_path)
    return registry


def test_in_flight_run_reports_the_apps_and_risks_it_covers(isolated_registry, monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking_run_platform(config, runner, options, factory, run_timestamp):
        started.set()
        release.wait(timeout=5)
        return Outcome(tmp_path / run_timestamp)

    monkeypatch.setattr(runs_service, "run_platform", blocking_run_platform)
    monkeypatch.setattr(
        runs_service,
        "load_config_or_400",
        lambda platform, config_path: SimpleNamespace(apps=[_app("parents_gateway", **{"ios-feature-01-risk-01": True})]),
    )

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


def test_a_run_naming_an_app_id_runs_only_that_app(isolated_registry, monkeypatch, tmp_path):
    parking = SimpleNamespace(id="parking", name="Parking", package_name="sg.parking.streetsmart", bundle_id="")
    registered = SimpleNamespace(
        id="sg_parking_streetsmart", name="Parking", package_name="sg.parking.streetsmart", bundle_id=""
    )
    monkeypatch.setattr(
        runs_service, "load_config_or_400", lambda platform, config_path: SimpleNamespace(apps=[parking, registered])
    )
    ran: list[list[str]] = []

    def recording_run_platform(config, runner, options, factory, run_timestamp):
        ran.append([app.id for app in config.apps])
        return Outcome(tmp_path / run_timestamp)

    monkeypatch.setattr(runs_service, "run_platform", recording_run_platform)

    runs_service.create_run(
        RunRequest(platform="android", config_path="configs/android.yaml", apps="sg_parking_streetsmart")
    )
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=5)

    assert ran == [["sg_parking_streetsmart"]]


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


def test_api_rejects_an_output_directory_it_cannot_read(isolated_registry, tmp_path):
    with pytest.raises(HTTPException) as exc_info:
        runs_service.create_run(
            RunRequest(
                platform="ios",
                config_path="configs/ios.yaml",
                out_dir=str(tmp_path / "elsewhere"),
            )
        )

    assert exc_info.value.status_code == 422
    assert "configured report root" in exc_info.value.detail


def test_default_api_output_is_the_configured_root_from_any_working_directory(
    isolated_registry, monkeypatch, tmp_path
):
    seen: list[Path] = []
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(
        runs_service,
        "reserve_run_timestamp",
        lambda out_dir: seen.append(out_dir) or "2026-01-01_00-00-00",
    )
    monkeypatch.setattr(
        runs_service,
        "run_platform",
        lambda *args, **kwargs: Outcome(tmp_path / "2026-01-01_00-00-00"),
    )

    runs_service.create_run(RunRequest(platform="ios", config_path="configs/ios.yaml"))

    assert seen == [tmp_path]


@pytest.fixture
def runs_http(isolated_registry, monkeypatch, tmp_path):
    apps = [
        _app("parking", **{"ios-feature-01-risk-01": True, "ios-feature-01-risk-02": False}),
        _app("care", **{"ios-feature-01-risk-02": True}),
    ]
    monkeypatch.setattr(runs_service, "load_config_or_400", lambda platform, config_path: SimpleNamespace(apps=apps))
    started: list[object] = []
    monkeypatch.setattr(runs_service, "execute_run", lambda *args: started.append(args))
    app = FastAPI()
    app.include_router(runs_routes.router)
    return app, isolated_registry, started


def test_a_risk_disabled_for_the_selected_app_is_refused_without_claiming_the_device(runs_http):
    app, registry, started = runs_http

    status, body = asyncio.run(
        call(app, "POST", "/runs", {"platform": "ios", "config_path": "configs/ios.yaml", "apps": "parking", "risks": "ios-feature-01-risk-02"})
    )

    assert status == 422
    assert body["detail"]["blocker_code"] == "risk_disabled"
    assert "ios-feature-01-risk-02" in body["detail"]["message"]
    assert started == []
    assert registry.try_claim_platform("ios")


def test_a_risk_enabled_for_the_selected_app_starts(runs_http):
    app, _, _ = runs_http

    status, body = asyncio.run(
        call(app, "POST", "/runs", {"platform": "ios", "config_path": "configs/ios.yaml", "apps": "parking", "risks": "ios-feature-01-risk-01"})
    )

    assert status == 202, body
