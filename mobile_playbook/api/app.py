from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mobile_playbook.api.cors import cors_allowed_origins
from mobile_playbook.api.routes import artifacts, catalog, config, playbook, reports, runs, sync
from mobile_playbook.reporting import run_manifest

_STARTED_AT = datetime.now(timezone.utc).isoformat()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Not a CORS-safelisted response header, so the browser cannot read the
    # filename the download endpoint sends without this.
    expose_headers=["Content-Disposition"],
)

app.include_router(catalog.router)
app.include_router(playbook.router)
app.include_router(config.router)
app.include_router(runs.router)
app.include_router(reports.router)
app.include_router(artifacts.router)
app.include_router(sync.router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "code_revision": run_manifest.git_revision(),
        "started_at": _STARTED_AT,
    }
