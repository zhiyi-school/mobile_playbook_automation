"""
Services that validate run requests, start runs on a background thread and read run state.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.dependencies import load_config_or_400
from mobile_playbook.api.run_registry import registry
from mobile_playbook.api.schemas import Platform, RunRequest
from mobile_playbook.api.services.reports import REPORTS_ROOT, read_dashboard_results
from mobile_playbook.api.settings import REPOSITORY_ROOT
from mobile_playbook.dashboard_sync.trigger import trigger_dashboard_sync
from mobile_playbook.orchestration.selection import (
    app_matches_selector,
    narrow_to_app_selection,
    selected_app_csv,
    selected_csv,
    validate_app_selection,
    validate_risk_selection,
)
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.run_timestamps import reserve_run_timestamp
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
logger = logging.getLogger(__name__)


# Return the report root, raising 422 when a requested out_dir names anything else.
def api_out_dir(requested: str | None) -> Path:
    if requested is None:
        logger.debug("api: no out_dir requested; using %s.", REPORTS_ROOT)
        return REPORTS_ROOT
    path = Path(requested).expanduser()
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    resolved = path.resolve()
    if resolved != REPORTS_ROOT.resolve():
        logger.debug(
            "api: out_dir %r resolves to %s, not the report root %s; responding 422.", requested, resolved, REPORTS_ROOT
        )
        raise HTTPException(
            status_code=422,
            detail="out_dir must identify the API's configured report root",
        )
    return resolved


# Return a factory that builds a ReportWriter with the platform's result adapter.
def report_writer_factory(platform: Platform):
    _, result_adapter = PLATFORM_RUNNERS[platform]

    # Build a ReportWriter for one run.
    def factory(out_dir: Path, run_timestamp: str) -> ReportWriter:
        return ReportWriter(out_dir, run_timestamp, result_adapter=result_adapter, platform=platform)

    return factory


# Run one platform scan, record its outcome, then release the platform and trigger dashboard sync.
def execute_run(run_timestamp: str, platform: Platform, config, options: RunOptions) -> None:
    runner_cls, _ = PLATFORM_RUNNERS[platform]
    logger.debug("api: executing %s run %s with %s.", platform, run_timestamp, runner_cls.__name__)
    try:
        outcome = run_platform(
            config, runner_cls(), options, report_writer_factory(platform), run_timestamp=run_timestamp
        )
    except Exception as exc:
        logger.debug("api: %s run %s raised.", platform, run_timestamp, exc_info=True)
        registry.mark_failed(run_timestamp, clean_message(str(exc)))
    else:
        logger.debug("api: %s run %s finished in %s.", platform, run_timestamp, outcome.run_dir)
        registry.mark_completed(run_timestamp, outcome.run_dir)
    finally:
        logger.debug("api: run %s cleanup: releasing %s and triggering dashboard sync.", run_timestamp, platform)
        registry.release_platform(platform)
        trigger_dashboard_sync(options.out_dir, run_timestamp)


# Pin the one selected iOS app to its intake build with this version, refusing the run when there is none.
def pin_build_version(config, platform: str, selected_apps: set[str] | None, version: str) -> None:
    from mobile_playbook.platforms.ios.acquisition.intake_ipa import PINNABLE_IOS_SOURCES, resolve_for_app

    version = version.strip()
    apps = [app for app in config.apps if app_matches_selector(app, selected_apps)]
    if not version or platform != "ios" or len(apps) != 1:
        logger.debug("api: build_version %r refused (platform=%s, %d app(s) selected).", version, platform, len(apps))
        raise HTTPException(
            status_code=422, detail="build_version needs an iOS run with exactly one app selected and a non-empty version"
        )
    app = apps[0]
    if (app.artifact or {}).get("source") not in PINNABLE_IOS_SOURCES:
        logger.debug("api: build_version refused for %s, whose builds are not IPA files.", app.id)
        raise HTTPException(status_code=422, detail=f"build_version needs an app tested from an IPA; {app.id} is not one")
    app.artifact = {**(app.artifact or {}), "build_version": version}
    resolution = resolve_for_app(app)
    if resolution.match is None:
        available = sorted({build.version for build in resolution.candidates if build.version})
        logger.debug("api: no intake build of %s with version %s (available %s).", app.id, version, available)
        raise HTTPException(
            status_code=422,
            detail={
                "blocker_code": "app_build_missing",
                "message": f"No build of {app.name} with version {version} is in intake. "
                f"Available versions: {', '.join(available) or 'none'}.",
            },
        )
    # A local_ipa app reads its fixed `ipa` path, so the chosen build replaces it for this run only.
    app.artifact = {**app.artifact, "ipa": str(resolution.match.path)}
    logger.debug("api: run pinned %s to version %s (%s).", app.id, version, resolution.match.path)


# Validate a run request, claim the platform and start the run on a background thread.
def create_run(body: RunRequest) -> dict:
    logger.debug(
        "api: creating %s run from %s (apps=%r, risks=%r).", body.platform, body.config_path, body.apps, body.risks
    )
    config = load_config_or_400(body.platform, body.config_path)

    selected_risks = selected_csv(body.risks)
    selected_apps = selected_app_csv(body.apps)
    try:
        validate_app_selection(config.apps, selected_apps)
        validate_risk_selection(KNOWN_RISKS_BY_PLATFORM[body.platform](), selected_risks)
    except ValueError as exc:
        logger.debug("api: run selection rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    narrow_to_app_selection(config, body.apps)
    if body.build_version is not None:
        pin_build_version(config, body.platform, selected_apps, body.build_version)

    if not registry.try_claim_platform(body.platform):
        logger.debug("api: %s platform busy; responding 409.", body.platform)
        raise HTTPException(status_code=409, detail=f"A {body.platform} run is already in progress")

    try:
        out_dir = api_out_dir(body.out_dir)
        run_timestamp = reserve_run_timestamp(out_dir)
        record = registry.create(
            run_timestamp, body.platform, body.config_path, apps=body.apps, risks=body.risks
        )
        options = RunOptions(out_dir=out_dir, selected_tests=selected_risks, selected_apps=selected_apps)
        thread = threading.Thread(
            target=execute_run, args=(run_timestamp, body.platform, config, options), daemon=True
        )
        thread.start()
        logger.debug("api: run %s started on thread %s.", run_timestamp, thread.name)
    except Exception:
        logger.debug("api: starting %s run failed; releasing platform.", body.platform, exc_info=True)
        registry.release_platform(body.platform)
        raise
    return {"run_id": record.run_id, "platform": record.platform, "status": record.status}


# Return every registry record as a dict.
def list_runs() -> list[dict]:
    return [vars(record) for record in registry.list()]


# Return one run's registry record, or raise 404.
def get_run(run_id: str) -> dict:
    record = registry.get(run_id)
    if record is None:
        logger.debug("api: run %s unknown; responding 404.", run_id)
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    logger.debug("api: run %s status %s.", run_id, record.status)
    return vars(record)


# Return a completed run's dashboard results, raising for unknown, running or failed runs.
def run_summary(run_id: str) -> list[dict]:
    record = registry.get(run_id)
    if record is None:
        logger.debug("api: run %s unknown; responding 404.", run_id)
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    logger.debug("api: summary for run %s with status %s.", run_id, record.status)
    if record.status == "running":
        raise HTTPException(status_code=409, detail="Run is still in progress")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error)
    return read_dashboard_results(record.run_timestamp)
