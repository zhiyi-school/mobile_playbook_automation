# Backend Integration

How to use this backend from another project. Four shapes are supported; pick
the one that matches what you are building, then read the contract sections
that apply.

All examples use placeholders — `Example App`, `example-app`,
`com.example.placeholder`, `<RUN_ID>`, `<RUN_TIMESTAMP>`, `<APP_ID>`,
`<RISK_ID>`, `<PLATFORM>`. Substitute your own.

> This is not plug-and-play. A working integration still requires a physical
> test device, Appium, per-app YAML configuration, and — if you want dashboard
> synchronisation — a Supabase project with the dashboard's migrations applied.
> Those are set up by hand. Everything below assumes you have done that first;
> see [setup.md](setup.md).

## Four integration shapes

**1. Standalone automation service.** Run the CLI on a host with a device
attached, read `reports/<RUN_TIMESTAMP>/` off disk. No HTTP, no dashboard.
Simplest and least coupled.

**2. An API behind your own web application.** Run
`python -m mobile_playbook.api` and drive it over HTTP. Your application starts
runs, watches progress and serves reports. This is what the bundled dashboard
does.

**3. Embedded Python component.** Import the orchestration directly:
`mobile_playbook.orchestration.scan_runner.run_platform` executes a run and
returns a `RunOutcome`, and `mobile_playbook.reporting.*` gives you the report
writers. You own scheduling, storage and any UI.

**4. A worker that feeds someone else's dashboard.** Run automation, then read
`dashboard_results.json` (or `results.sarif`) and write it wherever you like.
The bundled Supabase worker is one implementation of this shape, not a
requirement.

## Requirements

| | Requirement |
| --- | --- |
| Runtime | Python 3.11+ |
| Install | `python -m pip install -e .` |
| Device | one physical iOS or Android device per platform, connected and authorised |
| iOS | Xcode, XCUITest driver, Apple Team ID, device with no passcode/biometric |
| Android | platform-tools (`adb`), UiAutomator2 driver |
| Android repackaging risk | `apktool`, `apksigner`, `keytool` |
| Optional | MobSF (iOS static analysis, `analyzer.provider: mobsf`) |
| Optional | Supabase project + service-role key (dashboard sync only) |

One run per platform at a time — the device is the constraint, and the API
enforces it with `409`.

## Minimum configuration

One entry-point config per platform, carrying `device` and `runner` inline and
including the app roster:

```yaml
# configs/<PLATFORM>.yaml
device:
  udid: "<DEVICE_UDID>"
  team_id: "<APPLE_TEAM_ID>"
  appium_server_url: "http://127.0.0.1:4723"

runner:
  work_dir: "work/<PLATFORM>"

include:
  apps: split/<PLATFORM>/apps.yaml
```

```yaml
# configs/split/<PLATFORM>/apps.yaml
apps:
  - id: example-app
    name: Example App
    bundle_id: com.example.placeholder
    artifact:
      source: local_ipa
      ipa: intake/ios/ipas/example-app.ipa
      expected_bundle_id: com.example.placeholder
    risks:
      <RISK_ID>:
        enabled: true
```

Include semantics, overrides and anchor sharing:
[configuration.md](configuration.md#yaml-includes).

### How applications and risks are represented

An **application** is a config entry with a stable `id`. Selectors match against
`id`, `name`, `package_name` or `bundle_id`, case-insensitively and ignoring
non-alphanumerics.

A **risk** is a Python class discovered by the plugin registry, identified by a
platform-prefixed `risk_id` such as `<PLATFORM>-feature-NN-risk-NN`. Its
displayed text (name, description, goal, tactic, demonstration) is authored in
`configs/split/<PLATFORM>/risks.yaml`, not in the class. Enumerate both at
runtime rather than hard-coding:

```bash
curl http://127.0.0.1:8080/platforms/<PLATFORM>/risks
curl http://127.0.0.1:8080/config/<PLATFORM>/apps
```

### Artifacts

iOS risks that need a binary read it from an `artifact` block. `source` is
`local_ipa` (a path you provide), `intake_ipa` (a file dropped in
`intake/ios/ipas/`) or `installed_app_reference` (no binary; device-only
checks). Upload through the API with `POST /artifacts/{platform}` if your
integration needs to accept files.

## The minimum contract

For starting and observing a run, an integration needs exactly these:

```text
POST /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/events
GET  /runs/{run_id}/summary
GET  /reports
GET  /reports/{run_timestamp}/summary
GET  /reports/{run_timestamp}/files/{file_path}
```

Everything else is optional and adds a capability.

### Required vs optional, by feature

| Feature | Endpoints |
| --- | --- |
| **Basic run viewing** | `GET /health`, `GET /runs`, `GET /runs/{run_id}`, `GET /reports`, `GET /reports/{run_timestamp}/summary` |
| **Starting runs** | `POST /runs`, plus `GET /platforms/{platform}/risks` and `GET /config/{platform}/apps` to build the request |
| **Live progress** | `GET /runs/{run_id}/events` (SSE); polling `GET /runs/{run_id}` is the fallback and remains authoritative for completion |
| **Application provisioning** | `POST /config/{platform}/apps`, `GET /config/{platform}/apps/{app_id}`, `GET /config/{platform}/apps/{app_id}/provisioning`, `DELETE /config/{platform}/apps/{app_id}`, `POST /artifacts/{platform}` |
| **Report and evidence viewing** | `GET /reports/{run_timestamp}/files/{file_path}`, `GET /reports/{run_timestamp}/evidence-file?path=…` |
| **Finding history** | `GET /apps/{app_id}/risks/{risk_id}/history` |
| **Dashboard synchronisation** | `GET /runs/{run_id}/sync-status`, `GET /sync/status`, `POST /runs/{run_id}/sync` |
| **SARIF export** | `GET /reports/{run_timestamp}/sarif` |
| **Developer remediation controls** | `GET /platforms/{platform}/risks` (for the `controls` summaries), `GET /platforms/{platform}/risks/{risk_id}/controls`, `GET /platforms/{platform}/controls/{control_id}`, `GET /platforms/{platform}/controls/{control_id}/assets/{asset_path}`, `GET /platforms/{platform}/controls/{control_id}/source`, `GET /platforms/{platform}/playbook/status` |
| **Worker operations** | `GET /sync/status`, plus the CLI worker itself |

Full request and response shapes: [api.md](api.md#endpoints).

## Run lifecycle

```text
Create or select Example App
    ↓
Submit a run request                POST /runs
    ↓
Receive <RUN_ID>                    202, {"run_id", "platform", "status"}
    ↓
Poll status or subscribe to SSE     GET /runs/<RUN_ID>
                                    GET /runs/<RUN_ID>/events
    ↓
Wait for automation completion      status becomes completed | failed
    ↓
Observe dashboard sync status       GET /runs/<RUN_ID>/sync-status
    ↓
Read summary, findings, reports     GET /runs/<RUN_ID>/summary
and evidence                        GET /reports/<RUN_TIMESTAMP>/files/...
```

### Submitting a run

```bash
curl -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{
        "platform": "<PLATFORM>",
        "config_path": "configs/<PLATFORM>.yaml",
        "apps": "example-app",
        "risks": "<RISK_ID>"
      }'
```

```json
{"run_id": "<RUN_ID>", "platform": "<PLATFORM>", "status": "running"}
```

`202`, immediately — the run executes on a background thread. `apps` and `risks`
are optional comma-separated selectors; omitting either means "everything
enabled". `409` if that platform already has a run in progress; `422` for an
unknown app or risk.

**The `run_id` is the run timestamp**, and it is the `reports/<RUN_TIMESTAMP>/`
directory name. It is reserved atomically when the request arrives, so it is
known before the run finishes, and two requests in the same second never
collide (the second gets a `-2` suffix). There is no second ID scheme.

### Watching

Poll, or stream:

```bash
curl http://127.0.0.1:8080/runs/<RUN_ID>
curl -N http://127.0.0.1:8080/runs/<RUN_ID>/events
```

SSE carries `risk_started`, `risk_completed`, `appium_recovery` and
`device_unlocked` events, ending with a `done` event, then closes. The stream
re-reads `reports/<RUN_ID>/events.jsonl` from the start on every connection, so
a client that connects late still receives the whole history, and any number of
clients can watch the same run.

Two important properties for integrators:

- **The registry only knows runs started through this API.** A CLI run writes a
  full report but has no registry entry, so `GET /runs/{run_id}` and the SSE
  stream return `404` for it. Use the `/reports/...` endpoints, which read
  straight off disk and see every run regardless of origin.
- **Giving up on watching changes nothing.** A closed tab or an expired poll
  window does not cancel the run and must not be treated as failure. There is
  no cancel endpoint.

### Consuming results

```bash
curl http://127.0.0.1:8080/runs/<RUN_ID>/summary
curl http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/summary
```

Both return the run's `dashboard_results.json`: one normalised row per
(app, risk, test case), each with `verdict`, `severity`, `summary`, `evidence`
and `report_path`. `/runs/...` returns `409` while the run is still executing
and `500` with the error if it failed; `/reports/...` works for any run on disk.

**`dashboard_results.json` is the canonical normalised feed.** Build your
integration on it.

`run_manifest.json` is the completion signal. Report-file existence is not —
the summary is written from a `finally` block, so a fatal failure still leaves a
partial `dashboard_results.json` behind. Check `status == "completed"` in the
manifest before trusting a feed. A risk that fails is an ordinary result row and
leaves the run `completed`; only an uncaught setup, device or orchestration
failure marks it `failed`.

### Evidence

Evidence paths appear on each result row. Two endpoints serve them:

```bash
curl http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/files/<REPORT_PATH>/report.json
curl "http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/evidence-file?path=<EVIDENCE_PATH>"
```

The first serves anything inside that run's report directory. The second also
reaches artifacts under `work/`. Both refuse paths that resolve outside their
allowed roots. There is no authentication on either — see
[Security](#security-and-deployment).

### SARIF export

```bash
curl http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/sarif
```

SARIF 2.1.0, `application/sarif+json`, sent as a download. Generated from the
same `dashboard_results.json` and written to `reports/<RUN_TIMESTAMP>/results.sarif`.
Only completed runs produce it; `404` otherwise. Deterministic and free of
absolute host paths, with stable per-finding fingerprints. It is an **export**
— nothing in the pipeline reads it back. Field mapping and limitations:
[api.md](api.md#sarif-export).

## Developer remediation controls

Optional. Skip it entirely if your project only reports findings and does not
run a developer remediation workflow.

### Providing an equivalent playbook directory

The control text is not in this repository. Point a platform's directory at your
own and the endpoints work with no code change:

```env
IOS_PLAYBOOK_DIR=/opt/app/playbooks/ios
```

Your directory needs one Markdown file per risk and per control, named
`<prefix>-feature-NN-risk-NN[-control-NN].md`, with the document's identity in a
level-2 heading. Screenshots go in `attachments/`, optional reference archives
in `implemented_controls/`. The full format, including how control status and
step keys are derived, is in
[developer-playbook.md](developer-playbook.md#expected-directory-layout).

The directory is read-only from this backend's point of view: nothing is written
there, nothing is copied out of it into this repository, and nothing is imported
into a database.

### What an integrating project has to supply

The backend serves the control text and does not store progress. A project that
wants a developer workflow owns:

- **Progress storage** keyed by `control_id` and `step_key`, both strings this
  API reports. Store `playbook_revision` alongside them so you can tell that the
  guidance changed after the work was recorded.
- **Authorisation.** This API has no notion of users, teams or applications, so
  every access decision — which developer may see which control, who may record
  progress — belongs to the integrating project.
- **Rendering.** Control content arrives as typed JSON blocks (`paragraph`,
  `heading`, `code`, `list`, `table`, `image`, `caption`). Render only the kinds
  you know and drop the rest; never pass a block through as raw HTML.

### Keeping the two kinds of instruction apart

A risk's `demonstration` is how **security** reproduces the problem. A control's
`steps` are what a **developer** changes to fix it. They come from different
files and different endpoints, and an integrating project should not present
one as the other.

## Dashboard synchronisation

Optional. If you have your own datastore, read `dashboard_results.json` and
skip this entirely.

The bundled worker is a CLI process with no HTTP surface and is the only writer
of dashboard rows from automation results. Deploy it on the automation host:

```bash
SUPABASE_URL=https://dashboard.example.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=<SERVICE_ROLE_KEY> \
python -m mobile_playbook.dashboard_sync --reports-dir reports
```

It is triggered automatically after each run and by a launchd recovery sweep.
Monitoring, locking, ledger and retry semantics are in
[operations.md](operations.md).

**Automation status and dashboard sync status are different questions.** A run
is `completed` when the device finished and the report is on disk; its sync is
`completed` only once every dashboard write succeeded. Poll
`GET /runs/<RUN_ID>/sync-status` if your integration needs to know the dashboard
caught up. Do not infer it from the run's own status.

Retries are idempotent: `POST /runs/<RUN_ID>/sync` queues another pass without
forcing, so the processed ledger still short-circuits anything that already
landed.

## Storage

| Path | Growth | Notes |
| --- | --- | --- |
| `reports/<RUN_TIMESTAMP>/` | one directory per run, retained indefinitely | includes screenshots and recordings; the bulk of the footprint |
| `work/<PLATFORM>/` | per-app unpacked bundles and logs | can be pruned between runs |
| `intake/` | artifacts you supply | yours to manage |

Nothing prunes automatically. Plan retention yourself; report contents are
sensitive assessment data.

## Security and deployment

The API has **no authentication, no authorisation and no rate limiting**. It
binds to `127.0.0.1` by default, and that default is the security model.

- **Localhost**: fine as-is.
- **Trusted LAN**: `--host 0.0.0.0` plus exact `CORS_ALLOWED_ORIGINS`. Anyone
  who can route to the host can start runs and read every report.
- **Anything wider**: put an authenticating reverse proxy or VPN in front,
  terminating TLS, enforcing user auth, limiting request sizes and logging
  access. Do not expose this process directly.

Set `CORS_ALLOWED_ORIGINS` to exact origins; `*` is rejected. The service-role
key belongs to the worker process only and must never reach a browser.
Evidence access is filesystem-based and constrained to `reports/`, `work/` and
configured playbook image directories — a protected deployment should still
treat those as sensitive.

## Extension points

| To add | Do this |
| --- | --- |
| A **risk** | add a `Risk` subclass under `mobile_playbook/platforms/<platform>/risks/` with a unique `risk_id`; the plugin registry discovers it, and `list-risks` and the API pick it up. Author its text in `configs/split/<platform>/risks.yaml`. |
| A **platform** | implement the platform-runner protocol (`requires_device`, `connect_device`, `ensure_device_healthy`, `iter_enabled_tests`, `run_test`, `close_device`) and register it in `PLATFORM_RUNNERS` in `mobile_playbook/api/services/runs.py`, plus a result normaliser to `TestResult`. |
| An **artifact provider** | add a `source` handler in the artifact-intake layer and reference it from an app's `artifact.source`. |
| An **exporter** | write a module in `mobile_playbook/reporting/` that consumes `dashboard_results.json`, as `sarif_writer.py` does. Derive from the normalised feed; do not add a second canonical format. |

## Current limitations and follow-up work

- **No built-in API authentication.** Acceptable only on localhost or a trusted
  network behind a proxy.
- **Filesystem-based evidence access.** Path helpers constrain reads, but there
  is no per-user authorisation.
- **Single-host worker assumptions.** The sync lock, processed ledger and
  status sidecars are local files; two automation hosts sharing one dashboard
  would need shared state.
- **Eventual consistency.** No push notification when a run lands in the
  dashboard; clients poll.
- **No published OpenAPI or JSON Schema contract.** FastAPI serves a generated
  schema at `/openapi.json` and a browser at `/docs`, but there is no versioned,
  reviewed contract file a replacement implementation could be validated
  against.
- **No API versioning.** Endpoint paths are unversioned; a breaking change
  would break clients silently.
- **No retention or pruning** of reports and working files.
- **One run per platform**, single host — no queue, no horizontal scaling.
- **Implemented-control archives have no per-user authorisation.** The only
  host-level control is `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED`, which is
  all-or-nothing; finer-grained access has to be added at a proxy.
- **Only iOS has a playbook today.** The catalogue is platform-agnostic and
  `ANDROID_PLAYBOOK_DIR` is read, but no Android control documents exist yet.


## Application icons

A replacement dashboard consumes one endpoint:

```text
GET /config/{platform}/apps/{app_id}/icon   ->  200 image/png | 404
```

`404` covers an unknown app and an app with no readable icon alike, and its
detail names no path or filename. `ETag` carries the source build's checksum and
`Cache-Control` is `private, max-age=300`. There is no write endpoint: icons are
derived by runs and by `python -m mobile_playbook.icon_backfill`, never by a
browser.

A replacement backend must also record, per app, the SHA-256 of the build a run
executed against, so the dashboard shows the icon for that exact build rather
than whatever was uploaded most recently. See
[api.md](api.md#which-build-an-icon-belongs-to).

Binaries and images never enter the dashboard database. It stores
`artifact_sha256`, a logical `icons/<ARTIFACT_ID>.png` reference and an
extraction status; everything else stays on the automation host.
