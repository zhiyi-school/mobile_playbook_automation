from __future__ import annotations

import threading
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.dependencies import load_config_or_400
from mobile_playbook.api.job_registry import registry
from mobile_playbook.api.models import Platform, RunRequest
from mobile_playbook.api.services.reports import read_dashboard_results
from mobile_playbook.dashboard_sync_trigger import trigger_dashboard_sync
from mobile_playbook.orchestration.artifact_intake import (
    selected_app_csv,
    selected_csv,
    validate_app_selection,
    validate_risk_selection,
)
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.scheduler import reserve_run_timestamp
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import known_risks as known_android_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks import known_risks as known_ios_risks
from mobile_playbook.platforms.ios.runner import IosPlatformRunner
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.report_writer import ReportWriter

PLATFORM_RUNNERS = {
    "ios": (IosPlatformRunner, normalize_ios_result),
    "android": (AndroidPlatformRunner, normalize_android_result),
}

KNOWN_RISKS_BY_PLATFORM = {"ios": known_ios_risks, "android": known_android_risks}


def report_writer_factory(platform: Platform):
    _, result_adapter = PLATFORM_RUNNERS[platform]

    def factory(out_dir: Path, run_timestamp: str) -> ReportWriter:
        return ReportWriter(out_dir, run_timestamp, result_adapter=result_adapter, platform=platform)

    return factory


def execute_run(run_timestamp: str, platform: Platform, config, options: RunOptions) -> None:
    runner_cls, _ = PLATFORM_RUNNERS[platform]
    try:
        outcome = run_platform(
            config, runner_cls(), options, report_writer_factory(platform), run_timestamp=run_timestamp
        )
    except Exception as exc:
        registry.mark_failed(run_timestamp, clean_message(str(exc)))
    else:
        registry.mark_completed(run_timestamp, outcome.run_dir)
    finally:
        registry.release_platform(platform)
        trigger_dashboard_sync(options.out_dir, run_timestamp)


def create_run(body: RunRequest) -> dict:
    config = load_config_or_400(body.platform, body.config_path)

    selected_risks = selected_csv(body.risks)
    selected_apps = selected_app_csv(body.apps)
    try:
        validate_app_selection(config.apps, selected_apps)
        validate_risk_selection(KNOWN_RISKS_BY_PLATFORM[body.platform](), selected_risks)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not registry.try_claim_platform(body.platform):
        raise HTTPException(status_code=409, detail=f"A {body.platform} run is already in progress")

    try:
        out_dir = Path(body.out_dir)
        run_timestamp = reserve_run_timestamp(out_dir)
        record = registry.create(
            run_timestamp, body.platform, body.config_path, apps=body.apps, risks=body.risks
        )
        options = RunOptions(out_dir=out_dir, selected_tests=selected_risks, selected_apps=selected_apps)
        thread = threading.Thread(
            target=execute_run, args=(run_timestamp, body.platform, config, options), daemon=True
        )
        thread.start()
    except Exception:
        registry.release_platform(body.platform)
        raise
    return {"run_id": record.run_id, "platform": record.platform, "status": record.status}


def list_runs() -> list[dict]:
    return [vars(record) for record in registry.list()]


def get_run(run_id: str) -> dict:
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    return vars(record)


def run_summary(run_id: str) -> list[dict]:
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    if record.status == "running":
        raise HTTPException(status_code=409, detail="Run is still in progress")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error)
    return read_dashboard_results(record.run_timestamp)
