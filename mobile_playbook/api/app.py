from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mobile_playbook.orchestration.artifact_intake import selected_app_csv, selected_csv, validate_app_selection
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.scheduler import reserve_run_timestamp
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.config import ConfigError, load_config
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks
from mobile_playbook.platforms.ios.runner import IosPlatformRunner
from mobile_playbook.reporting.report_writer import ReportWriter

from mobile_playbook.api.job_registry import registry

Platform = Literal["ios", "android"]

REPORTS_ROOT = Path("reports")

app = FastAPI(
    title="Mobile Playbook Automation API",
    description=(
        "HTTP wrapper around this repo's existing CLI flows (validate, list-risks, run, "
        "reports). Runs are triggered asynchronously — POST /runs returns immediately with "
        "a run_id, poll GET /runs/{run_id} for status. Browse interactively at /docs."
    ),
    version="0.1.0",
)


def _load_config(platform: Platform, config_path: str, dry_run: bool = False):
    path = Path(config_path)
    if platform == "android":
        return load_android_config(path, dry_run=dry_run)
    return load_config(path, dry_run=dry_run)


def _config_error_detail(exc: ConfigError | AndroidConfigError) -> list[str]:
    return list(exc.errors)


def _load_config_or_400(platform: Platform, config_path: str):
    try:
        return _load_config(platform, config_path, dry_run=False)
    except (ConfigError, AndroidConfigError) as exc:
        raise HTTPException(status_code=422, detail=_config_error_detail(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/platforms/{platform}/risks")
def platform_risks(platform: Platform) -> list[dict]:
    return list_android_risks() if platform == "android" else list_ios_risks()


class ValidateRequest(BaseModel):
    platform: Platform
    config_path: str


@app.post("/config/validate")
def validate_config(body: ValidateRequest) -> dict:
    _load_config_or_400(body.platform, body.config_path)
    return {"valid": True}


class RunRequest(BaseModel):
    platform: Platform
    config_path: str
    apps: str | None = None
    risks: str | None = None
    out_dir: str = "reports"


_PLATFORM_RUNNERS = {
    "ios": (IosPlatformRunner, normalize_ios_result),
    "android": (AndroidPlatformRunner, normalize_android_result),
}


def _report_writer_factory(platform: Platform):
    runner_cls, result_adapter = _PLATFORM_RUNNERS[platform]

    def factory(out_dir: Path, run_timestamp: str) -> ReportWriter:
        return ReportWriter(out_dir, run_timestamp, result_adapter=result_adapter, platform=platform)

    return factory


def _execute_run(run_timestamp: str, platform: Platform, config, options: RunOptions) -> None:
    runner_cls, _ = _PLATFORM_RUNNERS[platform]
    try:
        outcome = run_platform(
            config, runner_cls(), options, _report_writer_factory(platform), run_timestamp=run_timestamp
        )
    except Exception as exc:  # background thread: report failure via the registry, don't raise
        registry.mark_failed(run_timestamp, str(exc))
        return
    registry.mark_completed(run_timestamp, outcome.run_dir)


@app.post("/runs", status_code=202)
def create_run(body: RunRequest) -> dict:
    config = _load_config_or_400(body.platform, body.config_path)

    selected_risks = selected_csv(body.risks)
    selected_apps = selected_app_csv(body.apps)
    try:
        validate_app_selection(config.apps, selected_apps)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    out_dir = Path(body.out_dir)
    # run_id *is* the run_timestamp (and the reports/<run_timestamp>/ dir name) —
    # reserved atomically here so it can be handed back in the response
    # immediately, and so two POST /runs in the same second can't be handed
    # the same run_id/directory (see reserve_run_timestamp's docstring).
    run_timestamp = reserve_run_timestamp(out_dir)

    record = registry.create(run_timestamp, body.platform, body.config_path)
    options = RunOptions(out_dir=out_dir, selected_tests=selected_risks, selected_apps=selected_apps)
    thread = threading.Thread(
        target=_execute_run, args=(run_timestamp, body.platform, config, options), daemon=True
    )
    thread.start()
    return {"run_id": record.run_id, "platform": record.platform, "status": record.status}


@app.get("/runs")
def list_runs() -> list[dict]:
    return [vars(record) for record in registry.list()]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    return vars(record)


@app.get("/runs/{run_id}/summary")
def get_run_summary(run_id: str) -> list[dict]:
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    if record.status == "running":
        raise HTTPException(status_code=409, detail="Run is still in progress")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error)
    return _read_dashboard_results(record.run_timestamp)


def _safe_run_dir(run_timestamp: str) -> Path:
    if not run_timestamp or "/" in run_timestamp or "\\" in run_timestamp or run_timestamp in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    run_dir = (REPORTS_ROOT / run_timestamp).resolve()
    reports_root = REPORTS_ROOT.resolve()
    if reports_root not in run_dir.parents and run_dir != reports_root:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No report directory for run_timestamp: {run_timestamp}")
    return run_dir


def _read_dashboard_results(run_timestamp: str) -> list[dict]:
    run_dir = _safe_run_dir(run_timestamp)
    results_path = run_dir / "dashboard_results.json"
    if not results_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard_results.json not found for this run")
    return json.loads(results_path.read_text())


@app.get("/reports")
def list_reports() -> list[str]:
    if not REPORTS_ROOT.is_dir():
        return []
    return sorted((p.name for p in REPORTS_ROOT.iterdir() if p.is_dir()), reverse=True)


@app.get("/reports/{run_timestamp}/summary")
def report_summary(run_timestamp: str) -> list[dict]:
    return _read_dashboard_results(run_timestamp)


@app.get("/reports/{run_timestamp}/files/{file_path:path}")
def report_file(run_timestamp: str, file_path: str) -> FileResponse:
    run_dir = _safe_run_dir(run_timestamp)
    resolved = (run_dir / file_path).resolve()
    if run_dir not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved)
