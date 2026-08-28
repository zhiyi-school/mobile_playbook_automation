from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from mobile_playbook.orchestration.artifact_intake import (
    selected_app_csv,
    selected_csv,
    validate_app_selection,
    validate_risk_selection,
)
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.scheduler import reserve_run_timestamp
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import known_risks as known_android_risks
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.artifacts.intake_ipa import list_intake_ipas
from mobile_playbook.platforms.ios.config import ConfigError, load_config
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks import known_risks as known_ios_risks
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks
from mobile_playbook.platforms.ios.runner import IosPlatformRunner
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events

from mobile_playbook.api import config_editor, playbook_assets, provisioning
from mobile_playbook.api.job_registry import registry

Platform = Literal["ios", "android"]

REPORTS_ROOT = Path("reports")

app = FastAPI(
    title="Mobile Playbook Automation API",
    description=(
        "HTTP wrapper around this repo's existing CLI flows (validate, list-risks, run, "
        "reports). Runs are triggered asynchronously — POST /runs returns immediately with "
        "a run_id; stream GET /runs/{run_id}/events for live progress, or poll GET /runs/{run_id} "
        "for just the coarse status. Browse interactively at /docs."
    ),
    version="0.1.0",
)

_DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
_cors_env = os.environ.get("CORS_ALLOWED_ORIGINS", "")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_env.split(",") if o.strip()] or _DEFAULT_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    risks = list_android_risks() if platform == "android" else list_ios_risks()
    for risk in risks:
        risk.update(config_editor.get_risk_metadata(platform, risk["risk_id"]))
        risk["demonstration"] = playbook_assets.decorate_demonstration(
            platform, config_editor.get_risk_demonstration(platform, risk["risk_id"])
        )
    return risks


@app.put("/platforms/{platform}/risks/{risk_id}")
def put_platform_risk(platform: Platform, risk_id: str, body: dict) -> dict:
    return config_editor.put_risk_metadata(platform, risk_id, body)


@app.put("/platforms/{platform}/risks/{risk_id}/demonstration")
def put_platform_risk_demonstration(platform: Platform, risk_id: str, body: list[dict]) -> list[dict]:
    stored = config_editor.put_risk_demonstration(platform, risk_id, playbook_assets.strip_derived(body))
    return playbook_assets.decorate_demonstration(platform, stored)


@app.get("/platforms/{platform}/playbook/images/{image_path:path}")
def playbook_image(platform: Platform, image_path: str) -> FileResponse:
    resolved = playbook_assets.resolve_image(platform, image_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(resolved)


# Domains iOS itself needs for Developer App certificate verification — these
# must bypass Burp or WebDriverAgent can fail to launch with the device
# reporting it can't verify/trust the app. See docs/ios/configuration.md#traffic-interception.
_PAC_DIRECT_HOST_PATTERNS = ["*.apple.com", "*.icloud.com", "ocsp.apple.com", "*.push.apple.com"]

_TRAFFIC_INTERCEPTION_RISK_ID = {"ios": "ios-feature-02-risk-01"}


def _detect_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


@app.get("/platforms/{platform}/traffic-interception/proxy.pac")
def traffic_interception_pac(platform: Platform, proxy_host: str | None = None) -> PlainTextResponse:
    risk_id = _TRAFFIC_INTERCEPTION_RISK_ID.get(platform)
    if risk_id is None:
        raise HTTPException(status_code=404, detail=f"No traffic-interception proxy config for platform {platform}")
    settings = config_editor.get_risk_settings(platform, risk_id)
    proxy_url = str((settings.get("burp") or {}).get("proxy_url") or "")
    if not proxy_url:
        raise HTTPException(status_code=400, detail=f"{risk_id}.burp.proxy_url is not configured")

    parsed = urlparse(proxy_url)
    host = proxy_host or parsed.hostname or "127.0.0.1"
    if not proxy_host and host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        # burp.proxy_url is written from this server's own point of view, but a PAC
        # file is evaluated on the phone — "127.0.0.1" there means the phone itself,
        # not this Mac, so swap in this machine's LAN-facing IP instead.
        host = _detect_lan_ip()
    port = parsed.port or 8080

    direct_conditions = " ||\n      ".join(f'shExpMatch(host, "{pattern}")' for pattern in _PAC_DIRECT_HOST_PATTERNS)
    pac = (
        "function FindProxyForURL(url, host) {\n"
        f"  if ({direct_conditions}) {{\n"
        "    return \"DIRECT\";\n"
        "  }\n"
        f'  return "PROXY {host}:{port}";\n'
        "}\n"
    )
    return PlainTextResponse(pac, media_type="application/x-ns-proxy-autoconfig")


@app.get("/platforms/{platform}/features")
def platform_features(platform: Platform) -> list[dict]:
    return config_editor.list_features(platform)


class FeatureUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


@app.put("/platforms/{platform}/features/{feature_id}")
def put_platform_feature(platform: Platform, feature_id: str, body: FeatureUpdateRequest) -> dict:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return config_editor.put_feature(platform, feature_id, updates)


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

_KNOWN_RISKS_BY_PLATFORM = {"ios": known_ios_risks, "android": known_android_risks}


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
        registry.mark_failed(run_timestamp, clean_message(str(exc)))
        return
    finally:
        registry.release_platform(platform)
    registry.mark_completed(run_timestamp, outcome.run_dir)


@app.post("/runs", status_code=202)
def create_run(body: RunRequest) -> dict:
    config = _load_config_or_400(body.platform, body.config_path)

    selected_risks = selected_csv(body.risks)
    selected_apps = selected_app_csv(body.apps)
    try:
        validate_app_selection(config.apps, selected_apps)
        validate_risk_selection(_KNOWN_RISKS_BY_PLATFORM[body.platform](), selected_risks)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # One physical device per platform — a second concurrent run for the same
    # platform would fight the first over that device, so claim it before
    # reserving anything. iOS and Android are separate devices and can run
    # concurrently, same as the CLI's run-all already assumes.
    if not registry.try_claim_platform(body.platform):
        raise HTTPException(status_code=409, detail=f"A {body.platform} run is already in progress")

    try:
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
    except Exception:
        registry.release_platform(body.platform)
        raise
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


EVENT_POLL_SECONDS = 0.5


@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-Sent Events stream of this run's progress, in place of polling GET /runs/{run_id}.

    Tails reports/<run_id>/events.jsonl — the same file a risk_started/
    risk_completed/appium_recovery event is appended to as the run actually
    progresses — rather than an in-memory queue, so this survives an API
    server restart and any number of clients can read it independently.
    A late-connecting client still gets every event from the start, since
    each poll re-reads from `since` rather than only forwarding new writes.
    """
    if registry.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    run_dir = _resolved_run_dir(run_id)

    async def event_stream():
        since = 0
        while True:
            if await request.is_disconnected():
                break
            events, since = read_events(run_dir, since)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
            record = registry.get(run_id)
            if record is not None and record.status != "running" and not events:
                yield f"data: {json.dumps({'type': 'done', 'status': record.status, 'error': record.error})}\n\n"
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(EVENT_POLL_SECONDS)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _resolved_run_dir(run_timestamp: str) -> Path:
    """Resolve run_timestamp to its reports/ directory, rejecting path traversal.

    Does not require the directory to already exist — callers that need that
    (reading a finished run's files) check `.is_dir()` themselves; the events
    stream deliberately doesn't, since it may be polled before the run's
    first file is written.
    """
    if not run_timestamp or "/" in run_timestamp or "\\" in run_timestamp or run_timestamp in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    run_dir = (REPORTS_ROOT / run_timestamp).resolve()
    reports_root = REPORTS_ROOT.resolve()
    if reports_root not in run_dir.parents and run_dir != reports_root:
        raise HTTPException(status_code=400, detail="Invalid run_timestamp")
    return run_dir


def _safe_run_dir(run_timestamp: str) -> Path:
    run_dir = _resolved_run_dir(run_timestamp)
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


_INTAKE_DIRS: dict[Platform, Path] = {"ios": Path("intake/ios/ipas"), "android": Path("intake/android/apks")}
_ARTIFACT_SUFFIXES: dict[Platform, str] = {"ios": ".ipa", "android": ".apk"}


def _inspect_uploaded_artifact(platform: Platform, path: Path) -> dict:
    try:
        if platform == "android":
            return inspect_apk_metadata(path)
        return inspect_ipa_metadata(path)
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/artifacts/{platform}")
def list_artifacts(platform: Platform) -> list[dict]:
    """Builds sitting in the intake directory, newest first.

    Lets a dashboard offer "which app are you assessing?" as a pick-list read
    out of the builds themselves, instead of asking someone to type a bundle
    ID they'd need the IPA (or to be the developer) to know.

    iOS entries carry `bundle_id`/`display_name`/`version` read from each
    IPA's Info.plist. Android returns filenames only — `inspect_apk_metadata`
    is still a stub, and Android identifies apps by package name off the
    device rather than from a stored APK.
    """
    directory = _INTAKE_DIRS[platform]
    if platform == "ios":
        return [build.as_dict() for build in list_intake_ipas(directory)]
    if not directory.is_dir():
        return []
    files = sorted(directory.glob(f"*{_ARTIFACT_SUFFIXES[platform]}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "file": path.name,
            "path": str(path),
            "bundle_id": None,
            "display_name": None,
            "version": None,
            "modified_at": path.stat().st_mtime,
        }
        for path in files
    ]


@app.post("/artifacts/{platform}", status_code=201)
async def upload_artifact(platform: Platform, file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "").name
    expected_suffix = _ARTIFACT_SUFFIXES[platform]
    if not filename or Path(filename).suffix.lower() != expected_suffix:
        raise HTTPException(
            status_code=400, detail=f"Expected a {expected_suffix} file for platform {platform}, got {file.filename!r}"
        )

    dest_dir = _INTAKE_DIRS[platform]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    contents = await file.read()
    dest_path.write_bytes(contents)

    return {"path": str(dest_path), "metadata": _inspect_uploaded_artifact(platform, dest_path)}


# ---------------------------------------------------------------------------
# Config editing — CRUD over apps.yaml / risk-settings / device / runner.
#
# Every write here re-runs the real config loader/validator against what's
# now on disk and reverts the file if that fails, so an edit can never leave
# the config in a state `python -m mobile_playbook validate` would reject.
# See mobile_playbook/api/config_editor.py for why iOS apps.yaml (which
# depends on templates.yaml's YAML anchors) is handled differently from
# every other file here.
# ---------------------------------------------------------------------------

_APPS_BY_PLATFORM = {
    "ios": (config_editor.list_ios_apps, config_editor.add_ios_app, config_editor.edit_ios_app, config_editor.delete_ios_app),
    "android": (
        config_editor.list_android_apps,
        config_editor.add_android_app,
        config_editor.edit_android_app,
        config_editor.delete_android_app,
    ),
}


def _apps_ops(platform: Platform):
    return _APPS_BY_PLATFORM[platform]


@app.get("/config/{platform}/apps")
def list_config_apps(platform: Platform) -> list[dict]:
    list_fn, _, _, _ = _apps_ops(platform)
    return list_fn()


@app.get("/config/{platform}/apps/{app_id}")
def get_config_app(platform: Platform, app_id: str) -> dict:
    list_fn, _, _, _ = _apps_ops(platform)
    for app_entry in list_fn():
        if app_entry.get("id") == app_id:
            return app_entry
    raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")


@app.post("/config/{platform}/apps", status_code=201)
def add_config_app(platform: Platform, body: dict) -> dict:
    _, add_fn, _, _ = _apps_ops(platform)
    return add_fn(body)


@app.put("/config/{platform}/apps/{app_id}")
def edit_config_app(platform: Platform, app_id: str, body: dict) -> dict:
    _, _, edit_fn, _ = _apps_ops(platform)
    return edit_fn(app_id, body)


@app.delete("/config/{platform}/apps/{app_id}", status_code=204)
def delete_config_app(platform: Platform, app_id: str) -> None:
    _, _, _, delete_fn = _apps_ops(platform)
    delete_fn(app_id)


@app.get("/config/{platform}/apps/{app_id}/provisioning")
def get_app_provisioning(platform: Platform, app_id: str) -> dict:
    """Whether this app is actually ready to be tested, stage by stage.

    Cheap enough to poll: config + filesystem + at most one `adb` call, never
    an Appium session. See mobile_playbook/api/provisioning.py for what each
    stage means, and why an unregistered app comes back as a 200 with
    status="failed" rather than a 404.
    """
    return provisioning.describe(platform, app_id)


@app.get("/config/{platform}/risk-settings/{risk_id}")
def get_config_risk_settings(platform: Platform, risk_id: str) -> dict:
    return config_editor.get_risk_settings(platform, risk_id)


@app.put("/config/{platform}/risk-settings/{risk_id}")
def put_config_risk_settings(platform: Platform, risk_id: str, body: dict) -> dict:
    return config_editor.put_risk_settings(platform, risk_id, body)


@app.get("/config/{platform}/device")
def get_config_device(platform: Platform) -> dict:
    return config_editor.get_section(platform, "device")


@app.put("/config/{platform}/device")
def put_config_device(platform: Platform, body: dict) -> dict:
    return config_editor.put_section(platform, "device", body)


@app.get("/config/{platform}/runner")
def get_config_runner(platform: Platform) -> dict:
    return config_editor.get_section(platform, "runner")


@app.put("/config/{platform}/runner")
def put_config_runner(platform: Platform, body: dict) -> dict:
    return config_editor.put_section(platform, "runner", body)
