# HTTP API

`mobile_playbook/api/` is a thin HTTP wrapper around the same functions the CLI (`python -m mobile_playbook ...`) calls — config loading/validation, risk listing, `run_platform()`, and the `reports/<run_timestamp>/` files each run already writes. It exists so a separate dashboard (or `curl`, or the interactive docs) can trigger runs and read results without shelling out to the CLI.

Nothing about the CLI changes because of this — `python -m mobile_playbook ...` still works exactly as before, and both entry points share the same underlying code.

## Running the server

```bash
python -m mobile_playbook.api --port 8080
```

Add `--reload` during development to restart on code changes, and `--host 0.0.0.0` to accept connections from other machines on the network (leave it on the default `127.0.0.1` for local-only use).

Configuration paths supplied in requests remain process-relative, so starting from the
repository root is the conventional choice. Report storage does not depend on that
choice; see [Report root and evidence contract](#report-root-and-evidence-contract).

## Who owns what

```text
Backend automation   owns execution, IPA/APK files, extracted artifact
                     metadata, application icons, raw reports, evidence,
                     run status, SARIF
Sync worker          translates completed reports into Supabase
Supabase             owns users, roles, teams, applications, assessments,
                     findings, finding history, tickets, retests, risk conversations,
                     activity, and small references (checksums, icon refs)
                     back to backend-owned files — never the files themselves
Frontend             reads backend automation state and Supabase dashboard
                     state; performs no authoritative synchronisation
```

Everything this API serves comes from disk. Nothing here reads or writes the
dashboard database — that is the sync worker's job alone
([operations.md](operations.md)).

## Security model

The automation API is intended for localhost or a trusted lab network. It can start tests on attached devices, write YAML config, accept IPA/APK uploads, and serve report/evidence files from `reports/` and `work/`, so do not expose the FastAPI server directly to the internet.

For anything beyond localhost or a trusted LAN, put it behind a VPN or authenticated reverse proxy that handles user auth, TLS, request-size limits, and access logging. Keep `CORS_ALLOWED_ORIGINS` to the exact dashboard origins that should call it; wildcard origins are rejected at startup.

## Exploring it without a dashboard

FastAPI serves interactive, browsable docs at **http://127.0.0.1:8080/docs** — every endpoint below can be called from there with a form, no client code required. `curl` also works, for example:

```bash
curl http://127.0.0.1:8080/reports
curl http://127.0.0.1:8080/platforms/ios/risks
curl -X POST http://127.0.0.1:8080/config/validate \
  -H "Content-Type: application/json" \
  -d '{"platform": "ios", "config_path": "configs/ios.yaml"}'
```

## Triggering a run

`POST /runs` takes the same platform, config, app and risk selections as the CLI's
`run` command and starts it in a background thread, returning right away with a
`run_id`:

```bash
curl -X POST http://127.0.0.1:8080/runs \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "ios",
    "config_path": "configs/ios.yaml",
    "risks": "ios-feature-01-risk-01"
  }'
```

```json
{"run_id": "<RUN_TIMESTAMP>", "platform": "ios", "status": "running"}
```

`apps`/`risks` are optional comma-separated strings, same as the CLI flags — omit
either to run every enabled app/risk in the config. Omit `out_dir` to use the API's
configured report root. The field remains accepted for older callers only when it
identifies that same root; a different directory is rejected with `422` because the
API could not consistently retrieve, enrich or synchronize such a run. CLI `--out`
remains independently configurable and relative CLI paths still resolve from the
CLI process's working directory.

## Report root and evidence contract

The API has one report root. `REPORTS_DIR` selects it, defaulting to
`<repository>/reports`; a relative value is anchored to the repository and an
absolute value is used as given. Run creation, report listing and lookup, history,
SARIF, sync state and ledgers, and `.job_registry.json` all use this root. The
policy is unchanged when the server starts from another working directory.

`dashboard_results.json` persists each declared evidence item as `kind`, `path`
and `label`. It does not persist a download handle or file size. When summary or
history is served, the API reads that row, discovers files in its `report_path`,
keeps only artifacts that still exist under the configured report root or the
installation's `work/` root, de-duplicates them, and adds `ref` and `size_bytes`
to the response. This enrichment is read-only: historical report files are not
rewritten. A missing artifact is omitted from summary/history; a previously issued
reference to a file that is now missing returns `404`.

`path` is display/report metadata. Downloads use only the opaque `ref` returned by
the API:

```bash
curl -OJ "http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/evidence-file?ref=<OPAQUE_REF>"
```

The reference is an identifier, not an authorization credential. It limits path
resolution to an allowed root and the named run, but the API itself has no user
authentication. A valid response includes the sanitized artifact filename in
`Content-Disposition: attachment`, a media type, and `Content-Length`;
`Content-Disposition` is exposed through CORS. Omitting `ref` is FastAPI request
validation (`422`), a malformed or unknown-root ref is `400`, and a missing file,
wrong-run ref, escaped symlink, or unknown run is `404`. Arbitrary path-based
evidence downloads are not supported.

The `run_id` *is* the run's timestamp and its `<report-root>/<run_id>/`
directory name — reserved atomically the moment the request comes in, so it is
known before the run finishes. Same-second requests receive `-2`/`-3` suffixes;
there is no separate ID scheme.

Poll it for status:

```bash
curl http://127.0.0.1:8080/runs/<RUN_TIMESTAMP>
```

```json
{"run_id": "<RUN_TIMESTAMP>", "platform": "ios", "config_path": "configs/ios.yaml", "status": "completed", "run_timestamp": "<RUN_TIMESTAMP>", "run_dir": "reports/<RUN_TIMESTAMP>", "error": null, "started_at": "...", "completed_at": "...", "apps": "example-app", "risks": "ios-feature-01-risk-01"}
```

`apps` and `risks` echo back the selection the run was started with, so a client
that lost track of a run — a browser tab that navigated away and came back, or a
second person opening the same page — can find it again by listing `GET /runs`
and matching on platform, app and risk rather than having to remember a `run_id`.
Either field is `null` when the run covers everything, matching the CLI's
behaviour when `--apps`/`--risks` are omitted. Runs recorded before these fields
existed read back as `null` on both.

Once `status` is `"completed"`, fetch the results:

```bash
curl http://127.0.0.1:8080/runs/<RUN_TIMESTAMP>/summary
```

This returns the same `dashboard_results.json` content the run wrote to disk. The identical value also works under `/reports` (useful since that path works for CLI-started runs too, not just ones started via `/runs`):

```bash
curl http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/summary
```

## Watching a run's progress live

`GET /runs/{run_id}` only ever reports one of three coarse states (`running`/`completed`/`failed`) — enough to know when a run is done, but nothing about what it's doing while it runs, which can be minutes for a config with several apps and risks. `GET /runs/{run_id}/events` streams that in real time over Server-Sent Events instead of needing to poll:

```bash
curl -N http://127.0.0.1:8080/runs/<RUN_TIMESTAMP>/events
```

```text
data: {"type": "risk_started", "timestamp": "...", "app_id": "example-app", "risk_id": "ios-feature-01-risk-01"}

data: {"type": "risk_completed", "timestamp": "...", "app_id": "example-app", "risk_id": "ios-feature-01-risk-01", "verdict": "At Risk", "final_status": "IPA_ANALYSIS_COMPLETE"}

data: {"type": "appium_recovery", "timestamp": "...", "message": "ios: Appium server at http://127.0.0.1:4723 is no longer reachable mid-run — attempting to recover and resume."}

data: {"type": "done", "status": "completed", "error": null}
```

Every `risk_started`/`risk_completed` event comes from the same run loop that writes each test's `report.json`, and `appium_recovery` fires from the same mid-run health check that restarts Appium after a crash (see [Appium auto-start](ios/configuration.md#appium-auto-start)) — so this is the same information already available in `summary.md`/`appium.log` after the fact, just pushed live instead of read after the run finishes. The stream ends with a `"done"` event once `GET /runs/{run_id}` would report anything other than `"running"`, then closes; a browser can consume it directly with `new EventSource(url)`.

These events are read from `reports/{run_id}/events.jsonl`, appended to as the run progresses — a client that connects late still gets every event from the start (each poll re-reads the whole file), and any number of clients can watch the same run independently.

A `POST /runs` call still needs everything a CLI `run` needs to actually succeed — Appium running and the device connected/trusted. The API doesn't remove those requirements, it just lets you kick the run off and check on it over HTTP instead of watching a terminal. Device *unlocking* specifically is handled automatically now (see [Automatic unlock](ios/configuration.md#automatic-unlock)) as long as the device has no passcode/Face ID/Touch ID set — Appium can't enter a passcode or biometric on a real device, so a locked, secured device still needs a person.

## Durable dashboard sync

Every run writes `reports/{run_id}/run_manifest.json` alongside its
`dashboard_results.json`. The manifest is written atomically at the
orchestration boundary and records the run timestamp, platform, the app and
risk IDs actually attempted, `started_at`, `completed_at`, a terminal `status`
of `completed` or `failed`, and a cleaned terminal error.

The manifest exists because report-file existence is not a completion signal.
The report summary is written from a `finally` block, so a fatal device or
orchestration failure still leaves a partial `dashboard_results.json` behind. A
risk that fails is recorded as an ordinary result row and leaves the run
`completed`; only an uncaught setup, device, or orchestration failure marks the
run `failed`.

A separate CLI worker with no HTTP surface is the only writer of dashboard rows
from automation results. It syncs a run only when its manifest says `completed`.
The dashboard never writes findings, assessments, or applications from a report
feed; it starts runs and watches progress, and what it shows comes from whatever
the worker has already synced.

Because the worker owns completion, a browser that stops watching a run — the
poll window ending, or the tab closing — changes nothing about the run or its
dashboard state. Only a terminal `failed` manifest marks an assessment or retest
failed. A run correlated with a retest through
`retest_runs.external_test_run_id` is completed by the worker, which also moves
that ticket to `under_review` and posts a `retest_completed` entry into the
conversation the retest itself names — the dashboard's conversations belong to
an application risk, so a retest raised under an earlier assessment still
reports into the one thread; a failed manifest fails the retest with the run's
error, posts `retest_failed`, and imports none of its partial rows. Both entries
carry a `sync_key`, so a repeated or retried pass never posts a second one, and
a retest already in a terminal state is left alone.

```bash
SUPABASE_URL=https://dashboard.example.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=replace_with_service_role_key \
python -m mobile_playbook.dashboard_sync --reports-dir reports
```

Report folders produced before the manifest existed are skipped, because their
completion cannot be verified. Pass `--allow-legacy-report` to import them
deliberately after review. A missing reports directory is treated as an empty
queue, and missing credentials exit with code 2 and a single error line rather
than a traceback.

## SARIF export

SARIF (Static Analysis Results Interchange Format) is the OASIS standard JSON
format that security tools use to hand findings to each other — code-scanning
dashboards, CI annotations, aggregators. Emitting it lets this project's results
be consumed by tooling that has never heard of `dashboard_results.json`.

SARIF here is strictly an **export**. `dashboard_results.json` remains the
canonical normalized feed and the only thing the dashboard sync worker reads;
nothing in the pipeline consumes SARIF back. The flow is:

```text
run → normalized TestResult rows → dashboard_results.json → results.sarif → download / external tool
```

Each completed run writes `reports/<run_timestamp>/results.sarif` alongside its
feed, from the same rows. The write is best-effort at the orchestration boundary,
so a SARIF failure can never change a run's outcome. A run whose manifest is not
`completed` produces **no** SARIF at all, so a partial or failed run is never
published as a result set.

```bash
curl http://127.0.0.1:8080/reports/<RUN_TIMESTAMP>/sarif
```

Returns `application/sarif+json` with a
`Content-Disposition: attachment; filename="<run_timestamp>.sarif"` header, so a
browser link downloads it directly. `404` when the run directory does not exist,
when its manifest is missing or not `completed`, or when its results feed is
missing or unreadable. Run timestamps go through the same validation as every
other report endpoint, so `..` and path separators are rejected before anything
is read. Runs that predate this feature have their SARIF generated on the first
request and cached to disk, which is why the endpoint works for old reports
without a backfill. The same file is also reachable through the existing
report-file endpoint at `/reports/{run_timestamp}/files/results.sarif`; the
dedicated `/sarif` route is preferred because it sets the media type and the
download filename, and because it can generate the document on demand.

### Field mapping

| Project field | SARIF |
| --- | --- |
| `test_id` (the risk id) | `rule.id` and `result.ruleId` |
| Risk name | `rule.name` |
| Risk description | `rule.shortDescription.text` |
| Risk description + goal | `rule.fullDescription.text` |
| `summary` | `result.message.text` |
| `verdict` | `result.kind` / `result.level` |
| run timestamp | `automationDetails.id` as `mobile-playbook/<run_timestamp>`, and `run.properties.run_timestamp` |
| manifest `started_at`/`completed_at` | `invocations[0].startTimeUtc`/`endTimeUtc` |
| everything else | `result.properties` |

Verdicts map as:

| Verdict | `kind` | `level` |
| --- | --- | --- |
| At Risk | `fail` | from severity — see below |
| Reduced Risk | `pass` | `none` |
| Inconclusive (and any unrecognised verdict) | `review` | `none` |

SARIF 2.1.0 §3.27.9–§3.27.10 allow a `level` other than `none` **only** on a
result whose `kind` is `fail`; for every other kind the level SHALL be `none`.
The exporter follows that rule, so a passing or inconclusive result carries no
severity in its `level`. For failing results the level is derived from the
project's own severity:

| `severity` | `level` |
| --- | --- |
| `critical`, `high` | `error` |
| `medium` | `warning` |
| `low`, `info` | `note` |
| missing or unrecognised | `error` |

Because SARIF has nowhere to put severity on a non-failing result, **the
project's verdict and severity are always preserved in custom properties**,
whatever the kind. `result.properties.dashboard_verdict` and
`result.properties.dashboard_severity` carry the original dashboard values
verbatim for every result, including the passing and inconclusive ones whose
`level` is forced to `none`. A consumer that wants this project's own severity
ordering should read those properties rather than `level`.

The original product values are preserved verbatim in `result.properties`:
`app_id`, `app_name`, `platform`, `package_or_bundle_id`, `test_id`,
`test_name`, `test_case_id`, `category`, `verdict`, `severity`,
`dashboard_verdict`, `dashboard_severity`, `status`, `run_timestamp`,
`report_path`, `started_at`, `completed_at`, `duration_seconds` and `evidence`.
(`verdict`/`severity` and their `dashboard_`-prefixed twins hold the same
values; the prefixed pair names them unambiguously as product data rather than
SARIF semantics.) Rules carry `category`, `platform`, `tactic`, `feature_id` and
`is_blocking` in `rule.properties`.

### Fingerprints, locations and evidence

`result.partialFingerprints["mobilePlaybook/v1"]` is a sha256 over
`platform | app_id | test_id | test_case_id` — stable project identifiers only.
No timestamp, no random value, no filesystem path goes into it, so the same
check on the same app keeps the same fingerprint across runs and a consumer can
track one finding over time. `test_case_id` is included because one risk can run
several cases against the same app, and they are distinct findings.

**No source locations are invented.** These are runtime findings about a device
and a binary, not a line in a file, so results carry no `locations` array rather
than a fabricated file/line/column. That is the main limitation compared with a
source-code analyzer: a consumer cannot annotate a diff or a pull request from
these results, and any tool that requires a location per result will need to
supply its own. Where a run recorded evidence, the file appears as a relative URI
in `result.attachments[].artifactLocation.uri` and in
`result.properties.evidence`, resolved relative to the run directory the SARIF
file sits in. Evidence stored outside the run directory is reduced to its file
name and flagged `"external": true`, so no absolute host path ever reaches the
document.

Output is deterministic: results are sorted by app, risk, test case and report
path, rules by id, JSON keys are sorted, and the document is UTF-8. Two exports
of the same run are byte-identical.

Documents validate against the official SARIF 2.1.0 JSON Schema. Note that the
schema does not encode the §3.27.10 constraint on `level` — that is prose — so
`tests/test_sarif_writer.py` asserts it directly on every generated document
rather than relying on schema validation to catch it.

### Two workflows, two meanings of "completed"

The automation run and the dashboard sync are separate workflows with separate
lifecycles, and a run is routinely `completed` while its sync is still `queued`
or `running`. They answer different questions:

| | `GET /runs/{run_id}` | `GET /runs/{run_id}/sync-status` |
| --- | --- | --- |
| Owned by | the API's run registry | the dashboard sync worker |
| `completed` means | the device finished executing every selected risk and the report is on disk | every Supabase write for that report succeeded and the ledger recorded it |
| Read by | the live progress view | the "is the dashboard current?" indicator |

Everything under `/runs/{run_id}/summary` and `/reports/...` is served straight
off disk, so the raw results and evidence for a run stay available whether or
not its dashboard sync ever succeeds. A failed sync is not a failed run.

The dashboard is eventually consistent by design: results appear in Supabase
some seconds after a run finishes, once the detached worker has processed the
queue. A client that wants to know when the dashboard caught up should poll the
sync status rather than assume the run's own `completed` implies it.

### Per-run sync status

```bash
curl http://127.0.0.1:8080/runs/<RUN_TIMESTAMP>/sync-status
```

```json
{
  "run_id": "<RUN_TIMESTAMP>",
  "run_timestamp": "<RUN_TIMESTAMP>",
  "status": "queued",
  "attempt": 1,
  "queued_at": "<TIMESTAMP>",
  "started_at": null,
  "completed_at": null,
  "last_updated_at": "<TIMESTAMP>",
  "error": null,
  "retryable": false,
  "counts": {"applications": 0, "assessments": 0, "findings": 0, "history": 0, "activity": 0}
}
```

`status` is one of:

| Status | Meaning |
| --- | --- |
| `queued` | The run completed and a worker has been asked to publish it; nothing written yet. |
| `running` | A worker is writing this run's rows to Supabase right now. |
| `completed` | Every expected write for this report succeeded and the ledger recorded it. |
| `failed` | A write failed. `error` carries a short, redacted reason and `retryable` says whether repeating the pass could help. |
| `not_required` | No dashboard sync is expected — the automation run did not complete, or the folder predates the manifest. |

`attempt` counts how many times a worker has been asked to publish this run;
it increments only when a new pass is queued after a terminal state, so a
duplicate trigger for a pass already `queued` or `running` does not inflate it.
`counts` describes the rows the *most recent attempt* reconciled, so a repeat
pass that the ledger short-circuits reports zeros — accurately, because it wrote
nothing. `error` is passed through the same one-line cleaner the run reports
use, and anything shaped like a bearer token is replaced with `[redacted]`
before it is stored, so a Supabase rejection quoting a request cannot leak the
service-role key to a browser.

### Worker health

```bash
curl http://127.0.0.1:8080/sync/status
```

```json
{
  "enabled": true,
  "worker_state": "idle",
  "queue_depth": 0,
  "last_success_at": "<TIMESTAMP>",
  "last_failure_at": null,
  "last_error": null,
  "recovery_sweep_enabled": true
}
```

`enabled` reflects `DASHBOARD_SYNC_AUTO_TRIGGER` as the API process sees it.
`worker_state` is `running` when something currently holds the host-wide sync
lock and `idle` otherwise — the same lock that serializes passes, so this is an
observation rather than a second source of truth. `queue_depth` counts runs
whose recorded status is `queued` or `running`. `recovery_sweep_enabled`
reports whether the launchd agent is installed for the current user.

This endpoint reports operational state only. It never returns credentials,
report contents, or filesystem paths.

**Diagnosing a worker that is not keeping up.** Read `/sync/status` first:

- `enabled: false` — automatic triggering is off for the API process. Check
  `DASHBOARD_SYNC_AUTO_TRIGGER` in the environment and in `.env`.
- `queue_depth` above zero with `worker_state: "idle"` and a stale
  `last_success_at` — nothing is draining the queue. Confirm the recovery sweep
  with `launchctl print gui/$(id -u)/com.mobile-playbook.dashboard-sync`, and
  read `work/dashboard-sync.log` for the last pass's output.
- `last_error` set — the last pass ran and something failed. The per-run
  `sync-status` for each affected run names the specific failure.
- Credentials are the usual cause of a pass that starts and immediately exits 2;
  that shows up in `work/dashboard-sync.log`, not in this endpoint.

### Retrying one run

```bash
curl -X POST http://127.0.0.1:8080/runs/<RUN_TIMESTAMP>/sync
```

Returns `202` with the same body as `sync-status`. It queues another worker pass
and returns immediately; the status moves `queued` → `running` → `completed` on
its own.

The retry deliberately does **not** pass `--force`, so the processed ledger still
short-circuits any report that already landed. Retrying is therefore idempotent:
a run that was fully synced writes nothing on the retry, and a run that failed
partway is reconciled by the same upsert-and-`sync_key` path that makes an
ordinary repeat pass safe. A run already `queued` or `running` returns its
current status without starting a second worker, and a `not_required` run is
refused with `409`.

Like every other endpoint here this has no authentication of its own and relies
on the localhost/trusted-LAN boundary described under [Security model](#security-model);
it is reachable only on the automation host's API, never from Supabase.

### Scheduling

The primary trigger is tied to run completion rather than a timer. After an API
or CLI run writes its terminal manifest, it starts a detached one-shot worker
that scans the report queue. The child loads credentials from the repository
`.env`; the API does not import the service-role key merely to trigger it. Set
`DASHBOARD_SYNC_AUTO_TRIGGER=false` to disable this behavior on an installation
that does not use the dashboard.

Post-run workers pass `--lock-wait-seconds 60`. This serializes simultaneous
iOS and Android completions instead of allowing the second invocation to exit
as busy before its report is seen. Manual invocations remain non-blocking by
default.

A launchd calendar job is retained as a recovery sweep for a trigger that could
not start or a host interruption. It invokes the same worker in one-shot mode
every five minutes. The template is at
`tools/dashboard_sync/com.mobile-playbook.dashboard-sync.plist`; copy it into
`~/Library/LaunchAgents`, substitute the absolute repository path, and load it
with `launchctl bootstrap gui/$(id -u)`. Credentials come from `.env` and must
not be placed in the plist, which is normally world-readable.

`--interval-seconds` is retained for development only and must be greater than
zero.

The recovery plist deliberately uses `StartCalendarInterval` and omits
`RunAtLoad`. On one verified macOS host launchd held every speculative launch
request indefinitely — `StartInterval`, `RunAtLoad`, and `KeepAlive` alike — at
`state = not running` with `pended nondemand spawn = speculative`, while
`launchctl kickstart` ran the same job immediately. Power state, low power mode,
session type, and plist ownership were all ruled out; the calendar activation
class fires on that same host where the others never did. Check the recovery job
with `launchctl print gui/$(id -u)/com.mobile-playbook.dashboard-sync` and look
at `runs`. The single-instance lock makes the recovery sweep safe to combine with
post-run and manual invocations.

### Repeat and overlap safety

The worker takes a host-wide lock (`reports/.dashboard_sync.lock`) for each
pass, so a manual invocation and a scheduled one cannot overlap; the loser
reports `skipped, another dashboard sync is already running` and exits 0.

Processed runs are recorded in `reports/.dashboard_sync_ledger.json`, keyed by
run timestamp against a digest of the manifest and the report feed. A pass over
unchanged reports performs no database writes at all. Editing a report's feed
changes its digest and makes it eligible again; `--force` re-syncs regardless,
for reconciliation or a database rebuild.

Rows the worker appends to `finding_history` and `activity_log` carry a
`sync_key` covered by a partial unique index, so a retry or a concurrent writer
cannot duplicate them. Rows created by people in the dashboard leave `sync_key`
null and are unaffected. A finding's status and `latest_test_run_id` only move
forward: replaying an older run imports its assessment row without regressing
the current finding.

Within one report, the finding's status and `latest_test_run_id` are written
last, after the history and activity rows. A pass that fails midway therefore
leaves the run un-applied, and the next pass re-attempts the append-only rows
rather than seeing "no change" and skipping them permanently.

Use `--run-timestamp 2026-01-01_00-00-00` to sync one completed run, or
`--interval-seconds 60` to run it as a development reconciler loop. The worker
uses stable dashboard keys (`applications.external_id = app_id`,
`assessments.external_id = "<run_timestamp>::<app_id>"`, and
`findings.external_id = "<app_id>::<test_id>"`) so re-running it updates the
same rows. If it adopts a dashboard-created `manual::...` assessment
placeholder, the update is conditional so a concurrent browser sync cannot
cause it to re-key the wrong row.

Keep `SUPABASE_SERVICE_ROLE_KEY` only in this worker's server-side
environment. Do not put it in frontend `.env`, any `VITE_*` variable,
checked-in examples, or the FastAPI process environment unless that process is
separately redesigned and authenticated. The FastAPI API remains a localhost
or trusted-lab service and does not need database-wide credentials.

## One run per platform at a time

Each platform's config identifies one physical device, and a run drives real Appium sessions against it. `POST /runs` rejects a second request for a platform that already has a run in progress:

```bash
curl -w "\n%{http_code}\n" -X POST http://127.0.0.1:8080/runs \
  -H "Content-Type: application/json" \
  -d '{"platform": "ios", "config_path": "configs/ios.yaml"}'
# {"detail": "A ios run is already in progress"}
# 409
```

This is per-platform, not global — an iOS run and an Android run are always free to run at the same time, since they target separate devices (the same assumption the CLI's `run-all` already makes). The lock releases as soon as the in-progress run finishes, whether it succeeds or fails.

## Uploading an IPA or APK

`POST /artifacts/{platform}` accepts a multipart file upload and drops it straight into this repo's existing intake drop-zone (`intake/ios/ipas/` or `intake/android/apks/`), then inspects it for metadata to help fill in an app's config:

```bash
curl -X POST http://127.0.0.1:8080/artifacts/ios -F "file=@example_app.ipa"
```

```json
{
  "path": "intake/ios/ipas/<IPA_PATH>",
  "metadata": {
    "bundle_id": "com.example.placeholder",
    "display_name": "Example App",
    "artifact_id": "<ARTIFACT_ID>",
    "sha256": "<SHA256>",
    "icon": {"available": true, "storage_ref": "icons/<ARTIFACT_ID>.png", "mime_type": "image/png"}
  }
}
```

The `artifact_id`/`sha256` and `icon` fields come from the same inspection pass — see [Application icons](#application-icons).

The file must match the platform's expected extension (`.ipa` for `ios`, `.apk` for `android`) or the request is rejected with `400`. Uploads are streamed to disk, capped at 2 GiB by default, and can be adjusted with `MAX_ARTIFACT_UPLOAD_BYTES`. Metadata comes from `inspect_ipa_metadata()` or `inspect_apk_metadata()`: iOS reads bundle ID, display name, version and `Info.plist`; Android reads package name, display name and version through `aapt`, `aapt2` or `apkanalyzer`. A file with the same name overwrites whatever was already in the intake folder after the upload completes, matching how that folder already works as a plain drop-zone.

## Application icons

An app's icon is extracted from the same build the automation would test, stored
on the backend, and served as a PNG. The dashboard database holds a logical
reference to it and nothing more: no image bytes, no base64, no filesystem path.

### Endpoint

```bash
curl -o icon.png http://127.0.0.1:8080/config/ios/apps/<APP_ID>/icon
```

`GET /config/{platform}/apps/{app_id}/icon` returns `200` with `image/png`, an
`ETag` of the source build's SHA-256 and `Cache-Control: private, max-age=300`.
A matching `If-None-Match` gets `304`. An unknown app and an app whose build has
no readable icon both get `404`, with a detail that names no path or filename.

Read-only is the whole surface. Re-extraction is not exposed over HTTP: it reads
backend-owned build files, so it stays an operator task on the host that owns
them — see [Backfilling existing apps](#backfilling-existing-apps).

### Storage layout

```text
intake/ios/ipas/<IPA_PATH>          original builds, unchanged
intake/android/apks/<APK_PATH>
derived/artifacts/<ARTIFACT_ID>.json   extracted metadata
derived/icons/<ARTIFACT_ID>.png        normalized icon
```

`<ARTIFACT_ID>` is the build's SHA-256, so two uploads of the same file share one
entry and re-extraction is free. `derived/` is set by `ARTIFACT_STORE_DIR` and
defaults to `<repository root>/derived` — see
[configuration.md](configuration.md#artifact_store_dir) for persistence and
retention.

### Which build an icon belongs to

A run records the SHA-256 of the build it actually executed against, per app, in
`run_manifest.json`:

```json
{"artifacts": {"<APP_ID>": "<ARTIFACT_SHA256>"}}
```

The checksum is taken when the app is first attempted, and the icon is derived
at the same moment, so both are pinned to the build under test. The sync worker
then links the icon for *that* checksum. Uploading a new build between execution
and synchronisation therefore cannot change what the dashboard shows for a
finished run.

If the recorded build has no derived icon in the store, the worker writes
nothing for that app and the previous reference stands. It never falls forward
to a different build's icon.

Runs made before this field existed have no `artifacts` map. Those fall back to
resolving whatever build the app configuration currently points at — the legacy
path, kept only so old reports still sync.

### What gets extracted

Extraction reads the build as an untrusted archive: entry names are validated
before anything is read, nothing is written to disk from the archive, members
over 4 MiB are skipped, and images over 2048x2048 are rejected. Only PNG is
accepted, and only after its header is parsed.

**iOS** reads `Info.plist` for `CFBundleIcons` -> `CFBundlePrimaryIcon` ->
`CFBundleIconFiles`/`CFBundleIconName`, the `~ipad` variant, and the legacy
`CFBundleIconFiles`/`CFBundleIconFile` keys, then matches those names against the
loose PNGs at the `.app` root and picks the largest that decodes. Asset-catalog
apps are covered by this path because `actool` writes the primary app icon to the
bundle root as well as into `Assets.car`. Apple ships those PNGs through its own
pngcrush variant — a `CgBI` chunk, raw deflate, BGRA order, premultiplied alpha —
which no browser can decode, so they are rebuilt into standard PNGs before being
stored.

**Android** asks `aapt`/`aapt2`/`apkanalyzer` for the manifest's declared icon
resources and reads the densest PNG among them. Without those tools on `PATH` it
falls back to scanning the archive for conventional `res/mipmap-*`/`res/drawable-*`
launcher icons, so extraction still works, just less precisely.

### iOS asset catalogs

When a bundle has no loose icon PNG, the catalog is inspected with `assetutil`
(`/usr/bin/assetutil`, run as an argument list, never through a shell). The
catalog is written to a temporary directory that is removed immediately after,
capped at 64 MiB in and 16 MiB out, with a 20-second timeout. `CFBundleIconName`
selects the icon by name, the `marketing` idiom (the 1024px store artwork) is
excluded, and the largest remaining rendition is identified.

**Raster export from `Assets.car` is not implemented.** `assetutil` can thin a
catalog or describe it as JSON; it has no export mode, and no other tool shipped
with macOS or Xcode exposes the pixel data. Rather than claim support that does
not exist, the catalog path identifies the icon and reports a precise reason:

| Reason | Meaning |
| --- | --- |
| `asset_catalog_tool_unavailable` | `assetutil` is not on this host (not macOS). Retried on the next pass. |
| `asset_catalog_unreadable` | The catalog could not be read or parsed. |
| `asset_catalog_no_icon` | The catalog holds no rendition matching the app's icon name. |
| `asset_catalog_no_extractor` | The primary icon was identified but cannot be exported. |

`asset_catalog_no_extractor` is logged with the rendition's dimensions so an
operator knows exactly what the catalog contains. In practice this is rare:
`actool` writes the primary app icon to the bundle root as well as into the
catalog, and that loose copy is what the normal path reads.

### Limitations

- `Assets.car` raster export is not available (above). A build whose icon exists
  *only* in the catalog reports unavailable.
- An Android adaptive icon falls back to its raster foreground
  (`res/**/ic_launcher_foreground.png` and friends). One that is vector-only
  reports `adaptive_icon_vector_only`; binary XML and vector drawables are not
  rendered, and the foreground is used as-is rather than composited over its
  background layer.
- A CgBI image larger than 512x512 is rejected; the pure-Python decoder is not
  worth running at that size. Standard PNGs are unaffected up to 2048x2048.
- Android apps configured by package name alone have no icon until the workflow
  has an APK on disk. No device operation is added to fetch one.

Extraction never fails a surrounding operation. An upload, an app registration
and an automation run all succeed with an icon reported as unavailable.

### Caching

Results are keyed by artifact checksum, so the cache invalidates itself: a
rebuilt artifact has a different digest, a different store entry and a different
icon. A successful extraction is stored once and reused. An absence is recorded
too, so a large artifact with no icon is not rescanned on every pass — except
for absences caused by the host rather than the build
(`asset_catalog_tool_unavailable`), which are always retried so installing the
tooling is enough to fix them.

### Backfilling existing apps

Apps that predate icon support keep `icon_ref = null` until something fills it
in. Two things do:

- a dashboard sync pass, which records the reference alongside the rest of the
  application row it already writes;
- a one-time sweep for every configured app that already exists in the dashboard:

```bash
python -m mobile_playbook.icon_backfill                    # both platforms
python -m mobile_playbook.icon_backfill --platform ios
python -m mobile_playbook.icon_backfill --platform android
python -m mobile_playbook.icon_backfill --app <APP_ID>
python -m mobile_playbook.icon_backfill --dry-run
```

The sweep matches an application by backend id **and** platform first. If no row
is linked, it falls back to the project's existing adoption rule — a single
unlinked row with the same name and platform. More than one match is counted as
`ambiguous` and left alone rather than guessed at. Each platform reports
`scanned`, `linked`, `unavailable`, `skipped`, `ambiguous` and `failed`;
`--dry-run` reports those counts without writing.

`--force` re-extracts even when an icon is already stored, which is what to use
after fixing a build whose icon could not be read the first time. The sweep only
updates applications the dashboard already has; it never creates rows, and it
skips any app whose config or build cannot be read. It needs the same Supabase
credentials as the sync worker and is not reachable from the API.


## Editing config

`/config/{platform}/apps`, `/config/{platform}/risk-settings/{risk_id}`, and `/config/{platform}/device` / `/config/{platform}/runner` read and write the same YAML files under `configs/` that the CLI reads — there's no separate copy of the config for the API. Every write re-runs the real config loader/validator against what's now on disk and reverts the file if the edit **introduced** a problem, so an edit can never make the config worse than it found it. Problems that were already there — another app still waiting for its build, say — don't block an unrelated edit, which would otherwise make one broken entry freeze the whole config:

```bash
curl -X POST http://127.0.0.1:8080/config/ios/apps \
  -H "Content-Type: application/json" \
  -d '{"name": "REPLACE_WITH_APP_NAME", "bundle_id": "com.example.app", "test_bundle_id": "com.example.app", "artifact": {"source": "local_ipa", "ipa": "intake/ios/ipas/example_app.ipa"}, "risks": {"ios-feature-01-risk-01": {"enabled": true}}}'
# {"id": "replace_with_app_name"}

curl -X PUT http://127.0.0.1:8080/config/ios/apps/replace_with_app_name \
  -H "Content-Type: application/json" \
  -d '{"risks": {"ios-feature-04-risk-01": {"enabled": true}}}'

curl -X DELETE http://127.0.0.1:8080/config/ios/apps/replace_with_app_name

curl -X PUT http://127.0.0.1:8080/config/ios/risk-settings/ios-feature-01-risk-01 \
  -H "Content-Type: application/json" \
  -d '{"sensitive_scan": {"reveal_values": false}}'

curl -X PUT http://127.0.0.1:8080/config/ios/device -H "Content-Type: application/json" -d '{"platform_version": "18.0"}'
```

`PUT` merges the given fields onto the current value rather than replacing it wholesale — a request that only sets one nested field leaves everything else in that app/risk/section untouched. Reads and writes go through `ruamel.yaml` in round-trip mode, so hand-written comments and formatting elsewhere in the file survive an edit intact.

The API uses explicit request models for the stable outer shapes: risk metadata, demonstration blocks, app create/update, device settings and runner settings. A few nested config fields remain open-ended JSON objects by design: `artifact` varies by artifact provider, `risks` holds per-risk app overrides, and `risk-settings` bodies are owned by each risk's YAML schema. Those flexible fields are still required to be JSON objects rather than arbitrary JSON values.

`configs/split/ios/apps.yaml` is the one exception to full comment/anchor preservation on the entries themselves: its app entries use `<<: *anchor` references to templates defined in the sibling `templates.yaml`, which can only be parsed together with that file, not on its own. Editing or adding an app there writes that one entry with fully explicit values instead of the anchor shorthand — every other untouched app entry, and all of the file's comments, are left byte-for-byte as they were. `configs/split/android/apps.yaml` has no such anchors, so Android app edits round-trip in full.

`GET /config/{platform}/risk-settings/{risk_id}` only covers risks that have global settings shared across apps (`ios-feature-01-risk-01`, `ios-feature-02-risk-01`, `ios-feature-04-risk-01`, `android-feature-01-risk-02`, `android-feature-06-risk-01`) — a per-app override still goes through that app's own `risks.<risk_id>` entry via the apps endpoints above.

Every app entry also carries `sector`, `agency`, `version`, and `cisos` (a list of `{"name", "email"}`) alongside its identity/artifact fields — organizational metadata a dashboard displays, not read by the automation run itself. All four are optional and default to blank/empty; `PUT /config/{platform}/apps/{app_id}` accepts any of them like any other field, including clearing `cisos` back to `[]` (unlike most other list-valued fields on this endpoint, an empty `cisos` in the request body does clear it rather than being treated as "no change").

## Playbook images

A risk's `demonstration` comes from the `### Demonstration` section of its Markdown risk document — see [developer-playbook.md](developer-playbook.md). The `demonstration:` block in `configs/split/{platform}/risks.yaml` is a fallback, served only when the Markdown document supplies no demonstration. A step parsed from a numbered heading also carries an optional `title`; steps parsed from an ordered list have none, and clients fall back to `Step N`.

A demonstration step can cite screenshots from the platform's playbook. They're stored as `{path, caption}` entries under a step's `images`, where `path` is relative to `playbook_dir` at the top of `configs/split/{platform}/risks.yaml`:

```yaml
playbook_dir: /path/to/ios-playbook-format/playbooks

ios-feature-01-risk-01:
  demonstration:
    - id: steps
      type: steps
      items:
        - id: step_2
          text: Upload the .ipa file to MobSF and review the static analysis report.
          images:
            - path: attachments/ios-feature-01-risk-01-step-2.png
              caption: Screenshot shows possible exposed credentials and api key found by MobSF
```

Paths are deliberately never absolute: moving the playbook means changing `playbook_dir` alone, and every image keeps resolving. `playbook_dir` may be absolute or relative to the repository root, and `~` expands. That file is gitignored, so the path stays machine-local.

`GET /platforms/{platform}/risks` adds two derived fields to each image — `url`, the endpoint to fetch it from, and `exists`, whether the file is actually on disk right now. `exists` is what turns a stale `playbook_dir` into something a dashboard can show plainly rather than a broken image. Both are recomputed per request and stripped on `PUT`, so they never end up in the YAML.

The image itself comes from `GET /platforms/{platform}/playbook/images/{image_path}`. Since a playbook is typically a whole vault — notes, source, editor state — and this API has no authentication, that endpoint serves **only** files under `playbook_dir` whose suffix is one of `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`. Anything else, and anything resolving outside `playbook_dir` (`../`), is a `404`.

## Developer remediation controls

`GET /platforms/{platform}/risks` says what a risk *is* and how security
demonstrates it. The endpoints below say what a developer should *change* to
reduce it, read from the external playbook directory described in
[developer-playbook.md](developer-playbook.md).

The two are deliberately separate. A risk's `demonstration` is security's
reproduction procedure; a control's `steps` are developer remediation
instructions. Neither is ever served as the other.

### Control summaries on the risk list

Each risk in `GET /platforms/{platform}/risks` gains three fields:

```json
{
  "risk_id": "example-feature-01-risk-01",
  "name": "Example risk",
  "description": "Example risk description",
  "goal": "Example security goal",
  "controls_available": true,
  "controls_error": null,
  "controls": [
    {
      "control_id": "example-feature-01-risk-01-control-01",
      "risk_id": "example-feature-01-risk-01",
      "title": "Example control",
      "status": "active",
      "required": true,
      "step_count": 4,
      "playbook_revision": "sha256:example",
      "has_source_archive": true
    }
  ]
}
```

If the playbook directory is missing or unreadable, `controls` is empty,
`controls_available` is `false`, and `controls_error` carries the reason. The
risk list itself keeps working — a broken playbook never takes the catalogue
down, and it is never reported as "this risk has no controls".

### Control detail

`GET /platforms/{platform}/controls/{control_id}` returns the whole control as
normalised JSON, so no client has to parse Markdown:

```json
{
  "control_id": "example-feature-01-risk-01-control-01",
  "risk_id": "example-feature-01-risk-01",
  "platform": "ios",
  "title": "Example control",
  "status": "active",
  "status_source": "default",
  "required": true,
  "playbook_revision": "sha256:example",
  "source_file": "example-feature-01-risk-01-control-01.md",
  "summary": "Your app can reduce this risk by taking the following steps:",
  "step_count": 2,
  "intro": [],
  "steps": [
    {
      "step_key": "rotate-example-key",
      "step_id_source": "declared",
      "content_hash": "sha256:…",
      "step_index": 0,
      "number": 1,
      "step_title": "The first thing the developer changes",
      "text": "The first thing the developer changes. Some more detail.",
      "content": [
        {
          "type": "image",
          "path": "attachments/example_control_ss1.png",
          "alt": "Alt text",
          "width": "400",
          "caption": "What the screenshot shows",
          "url": "/platforms/ios/controls/example-feature-01-risk-01-control-01/assets/attachments/example_control_ss1.png",
          "exists": true
        }
      ]
    }
  ],
  "references": [{ "label": "Example reference", "url": "https://example.test/reference" }],
  "source_archives": [],
  "source_download_url": null
}
```

`content` blocks are one of `paragraph`, `caption`, `heading`, `code`, `list`,
`table`, `image`. A client should render only the kinds it knows and drop the
rest rather than passing anything through as raw HTML.

`step_key` is the step's stable identifier and the only safe thing to record
progress against. `step_id_source` says where it came from: `declared` when the
playbook author wrote a `<!-- playbook-step-id: … -->` directive above the step,
`auto` when it was derived because none was declared — from the step title for
a heading-based step, from the instruction text for an ordered-list step.
A declared id survives rewording and reordering; a derived one survives
reordering, renumbering and body edits but deliberately changes when the step is
retitled, so a tick is never carried across to a different instruction.

`content_hash` covers the step's title, its text and everything rendered under it. It is
for spotting that a step changed, never for storing or re-rendering an older
version — the API serves the current playbook and has no endpoint for any other.

`playbook_revision` is the SHA-256 of the control document. The catalogue-wide
`revision` on `/playbook/status` additionally covers screenshots and archives,
so a client can poll one value to learn that anything changed. Image URLs carry
a `?v=` content digest so a replaced screenshot is refetched rather than served
from cache.

### Endpoints

| Method & Path | Purpose |
| --- | --- |
| `GET /platforms/{platform}/risks/{risk_id}/controls` | Every control addressing one risk, in full detail. Accepts either the platform-prefixed id or the generic playbook id. `404` for an unknown risk, `503` if the playbook directory is unreadable. |
| `GET /platforms/{platform}/controls/{control_id}` | One control's remediation steps, assets, references and archive metadata. |
| `GET /platforms/{platform}/controls/{control_id}/assets/{asset_path}` | One screenshot from that control, resolved under the playbook root. Only approved image extensions; anything escaping the root is a `404`. |
| `GET /platforms/{platform}/controls/{control_id}/source` | Metadata for the implemented-control archive: `exists`, `file_name`, `size_bytes`, `sha256`, `download_enabled`. Never the bytes. |
| `GET /platforms/{platform}/controls/{control_id}/source/download` | The archive itself, as `application/zip`. `403` when `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED=false`; `404` when the control has no archive. |
| `GET /platforms/{platform}/playbook/status` | Diagnostics: the configured path, whether it is readable, risk/control counts, the catalogue revision, and every warning. Always answers `200`, even when the directory is missing. |
| `POST /platforms/{platform}/playbook/reload` | Rebuilds the catalogue for a platform and returns the same shape as `status`. `503` if the directory is unreadable. |

### Caching

The catalogue is built on first request and cached per platform. It rebuilds by
itself when any `*.md` file's path, modification time or size changes, or when
the contents of `attachments/`/`implemented_controls/` change — an edit shows up
on the next request with no restart. `POST …/playbook/reload` forces it.

## Is an app ready to test?

Adding an app to config is only half of making it testable. `GET /config/{platform}/apps/{app_id}/provisioning` answers whether it can actually be run, as three stages that complete independently of one another:

| Stage id | Means |
| --- | --- |
| `app_registered` | The app is in this platform's roster. `in_progress` until it is. |
| `service_online` | This API answered, so the assessment service is up. |
| `configuration_applied` | The app's entry is complete enough to run — identity resolvable, a build available (iOS) or the app installed on the device (Android), and at least one risk enabled. |

```bash
curl http://127.0.0.1:8080/config/ios/apps/example_app/provisioning
# {"app_id": "example_app", "platform": "ios", "bundle_id": "com.example.app", "status": "ready",
#  "stages": [
#    {"id": "app_registered",        "label": "Server environment prepared",  "state": "done", "detail": "The app has been set up for testing."},
#    {"id": "service_online",        "label": "Assessment service is running","state": "done", "detail": "The testing service is online."},
#    {"id": "configuration_applied", "label": "Configuration applied",        "state": "done", "detail": "This app is configured and ready to test."}
#  ],
#  "error": null}
```

Stage states are `done`, `in_progress`, `pending`, `failed`, and `unknown` (couldn't be checked cheaply — verify by hand). Overall `status` is `failed` if any stage failed, `ready` if every stage is `done` or `unknown`, else `pending`. **`unknown` deliberately does not block `ready`**, so an unverifiable check can't strand an app in setup forever.

**Stage text is written for end users and deliberately discloses nothing about this repo's internals** — no paths, filenames, config field names, risk ids or bundle ids. A dashboard renders `label`/`detail` directly. Specifics (which build was ambiguous, which validator rule failed, which artifact path is missing) go to `mobile_playbook.api.provisioning`'s logger instead, so operators still get them from the server log. `bundle_id` is returned as its own field for callers to store, not as display text.

An app that isn't registered, or whose config no longer validates, returns `200` with a generic `failed`/`pending` report rather than an error code — the caller is polling to find out *why* it isn't ready. `404` is reserved for "this build has no provisioning support at all", so a client can tell the two apart.

**An app is only held back by its own problems.** Config errors are attributed to the app they name, so one broken entry reports `failed` on its own while every other app still reports its real status. Only a problem outside any app (a malformed file, a missing `device` field) fails them all.

**A build that hasn't been provided yet is not a config error.** An app can be registered before its IPA/APK exists — `configuration_applied` sits at `in_progress` with "Waiting for the app build to be provided", and flips to `done` on the next poll once the build appears, with no config change. That holds for a fixed `artifact.ipa` path as much as for `intake_ipa`. A path that is simply wrong therefore isn't caught by `validate`; a run surfaces it as `ARTIFACT_NOT_FOUND`.

It is designed to be polled: config read + filesystem check + at most one `adb` call, never an Appium session. iOS apps using `intake_ipa` resolve their build through the same `resolve_intake_ipa` the run itself uses, so readiness cannot disagree with what a run would find.

### Configuration ready is not the same as runnable

The stages above answer "is this app configured?". Whether it can run *now* is a
separate question — the device may be unplugged — so the same response also
carries structured execution readiness. A caller should branch on these rather
than parse `detail`:

| Field | Means |
| --- | --- |
| `configuration_ready` | The app's entry is complete and at least one risk is enabled. |
| `device_required` | Any enabled risk needs a real device. |
| `device_ready` | A device is attached and reachable. |
| `platform_available` | No run currently holds this platform's device. |
| `runnable` | All of the above; a run can be started right now. |
| `blocker_code` | Machine-readable reason it is not runnable, or `null`. |
| `retryable` | Whether waiting could clear the blocker on its own. |
| `detail` | One user-facing sentence. Never a traceback or a path. |

```bash
curl http://127.0.0.1:8080/config/android/apps/example_app/provisioning
# {"status": "ready", ...,
#  "configuration_ready": true, "device_required": true, "device_ready": false,
#  "platform_available": true, "runnable": false,
#  "blocker_code": "no_device", "retryable": true,
#  "detail": "Waiting for a compatible test device to become available."}
```

Blocker codes: `configuration_incomplete` and `no_tests_enabled` are not
retryable (someone has to change something); `app_build_missing`,
`app_not_installed`, `no_device`, `device_unreachable` and `platform_busy` are.

Note the example above: `status` is `ready` while `runnable` is `false`. That
combination is the point of the split — the configuration is finished, the
device simply is not there yet. A caller that treats `status == "ready"` as
"start the run" will start runs that cannot work.

An iOS device probe that cannot be performed at all (`xcrun xctrace`
unavailable) reports `device_ready: true` rather than blocking, matching
preflight: the run itself stays the authority and fails fast with a real device
error.

## Endpoints

| Method & Path | Purpose |
| --- | --- |
| `GET /health` | Liveness check. |
| `GET /platforms/{platform}/risks` | Every risk's full metadata — `risk_id`, `name`, `description`, `goal`, `tactic`, `is_blocking`, `automation_available`, `demonstration`, and platform-specific requirement fields. The displayed text comes from `configs/split/{platform}/risks.yaml`, not from the risk's Python class. `platform` is `ios` or `android`. See [Risk Metadata](ios/risks.md#risk-metadata). |
| `PUT /platforms/{platform}/risks/{risk_id}` | Replace a risk's displayed metadata in `configs/split/{platform}/risks.yaml`. Body may contain any of `name`, `description`, `goal`, `tactic`; any other field is a 422. Returns the stored values. It never affects what the automation run itself does. |
| `PUT /platforms/{platform}/risks/{risk_id}/demonstration` | Replace a risk's `demonstration` content — the "how to demonstrate this" setup/steps a dashboard shows, stored in `configs/split/{platform}/risks.yaml`. Body is the full `demonstration` array; it never affects what the automation run itself does. Each image's derived `url`/`exists` are stripped before writing, so a GET's response can be PUT straight back. |
| `GET /platforms/{platform}/playbook/images/{image_path}` | One screenshot referenced by a demonstration step, resolved under that platform's `playbook_dir`. See [Playbook images](#playbook-images). |
| `GET /platforms/{platform}/features` | Every feature_id referenced by that platform's risks, with its `name`/`description` from `configs/split/{platform}/features.yaml`. `feature_id` here has no platform prefix (e.g. `"feature-01"`). |
| `PUT /platforms/{platform}/features/{feature_id}` | Partially update a feature's `name`/`description`. |
| `GET /platforms/{platform}/traffic-interception/proxy.pac` | A PAC (Proxy Auto-Configuration) file, generated from that platform's traffic-interception risk's `burp.proxy_url`, that routes Apple's own certificate/app-verification domains direct and everything else through Burp — point a device's Wi-Fi "Automatic" proxy config at this URL instead of a manual `host:port` so WebDriverAgent can still verify its certificate with the proxy on. `ios` only. Optional `?proxy_host=` overrides the auto-detected host (used when `burp.proxy_url` is a loopback address, since that's written from this server's point of view, not the phone's). See [Traffic interception](ios/configuration.md#traffic-interception). |
| `POST /config/validate` | Same check as `validate`. Body: `{"platform", "config_path"}`. Returns `422` with the config's error list if invalid. |
| `POST /runs` | Starts a run in a background thread and returns immediately (`202`) with a `run_id`. Body: `{"platform", "config_path", "apps"?, "risks"?, "out_dir"?}`. `out_dir` is a compatibility assertion and must equal the configured API report root; otherwise `422`. `409` if that platform is busy. |
| `GET /runs` | Lists runs started through this API (this process's history only — see below). Each record carries the `apps`/`risks` it was started with, so a client can find an in-progress run without knowing its `run_id`. |
| `GET /runs/{run_id}` | One run's status: `running`, `completed`, or `failed`, plus its `run_timestamp`/`run_dir` once known and the `apps`/`risks` it covers. |
| `GET /runs/{run_id}/events` | Server-Sent Events stream of this run's progress — `risk_started`/`risk_completed`/`appium_recovery` events as they happen, ending with `done`. See [Watching a run's progress live](#watching-a-runs-progress-live). |
| `GET /runs/{run_id}/summary` | The completed run's `dashboard_results.json`, by `run_id`. `409` while still running, `500` with the error if it failed. |
| `GET /runs/{run_id}/sync-status` | Where this run's *dashboard sync* stands: `queued`, `running`, `completed`, `failed`, or `not_required`, with attempt, timings, a redacted error and the row counts written. Separate from the run's own status — see [Two workflows](#two-workflows-two-meanings-of-completed). |
| `POST /runs/{run_id}/sync` | Queues another dashboard sync pass for one run (`202`). Idempotent — the processed ledger still short-circuits a report that already landed. `409` if the run needs no sync or a pass is already pending. |
| `GET /sync/status` | Dashboard sync worker health: whether automatic triggering is enabled, whether a pass is running, the queue depth, and the last success/failure. Operational state only, no credentials or report contents. |
| `GET /reports` | Lists every `<configured-report-root>/<run_timestamp>/` directory on disk, most recent first — including CLI runs written to that root. |
| `GET /reports/{run_timestamp}/summary` | The same `dashboard_results.json`, looked up directly by `run_timestamp` instead of `run_id`. Works for any run on disk regardless of how it was started. |
| `GET /reports/{run_timestamp}/sarif` | The run's results as SARIF 2.1.0 (`application/sarif+json`, sent as a download). Generated on demand for runs made before the export existed. `404` unless the run has a `completed` manifest and a results feed. See [SARIF export](#sarif-export). |
| `GET /reports/{run_timestamp}/files/{file_path}` | Serves any file inside that run's report directory — screenshots, recordings, `report.json`, `logs.txt`, `critical_findings.md`, etc. |
| `GET /reports/{run_timestamp}/evidence-file?ref=...` | Serves evidence named by an API-issued opaque ref. Missing query parameter: `422`; malformed ref: `400`; missing, escaped or wrong-run artifact: `404`. See [Report root and evidence contract](#report-root-and-evidence-contract). |
| `GET /apps/{app_id}/risks/{risk_id}/history?limit=20` | Returns the latest matching summary rows for one app/risk without forcing clients to scan all report folders. |
| `GET /artifacts/{platform}` | Builds sitting in `intake/{ios,android}/{ipas,apks}/`, newest first. iOS identity fields are read from each IPA; Android APK identity fields are read with Android SDK tooling when available. Unreadable files are skipped. |
| `POST /artifacts/{platform}` | Multipart file upload (`file`) into `intake/{ios,android}/{ipas,apks}/`. Returns `{"path", "metadata"}`; `400` if the file extension doesn't match the platform. |
| `GET/POST /config/{platform}/apps` | List every configured app, or add a new one. |
| `GET/PUT/DELETE /config/{platform}/apps/{app_id}` | Read, partially update, or remove one app. |
| `GET /config/{platform}/apps/{app_id}/provisioning` | Whether this app is ready to be tested yet, stage by stage. See [Is an app ready to test?](#is-an-app-ready-to-test) |
| `GET /config/{platform}/apps/{app_id}/icon` | The app's icon as `image/png`, with an `ETag` and `Cache-Control`. `404` for an unknown app and for one with no readable icon alike. Read-only; re-extraction is not exposed over HTTP. See [Application icons](#application-icons). |
| `GET/PUT /config/{platform}/risk-settings/{risk_id}` | Read or partially update a risk's global settings (only risks with shared cross-app settings — see below). |
| `GET/PUT /config/{platform}/device` | Read or partially update the `device:` block. |
| `GET/PUT /config/{platform}/runner` | Read or partially update the `runner:` block. |

A run is asynchronous because it isn't a quick request/response: it drives real Appium sessions against a physical device and can take several minutes, and some risks (for example the custom-keyboard keystroke-collection risk) may need a person to grant permissions mid-run that `runner.permission_alerts` doesn't already cover. Use `GET /runs/{run_id}/events` for live progress, or poll `GET /runs/{run_id}` for just the coarse status, instead of expecting `POST /runs` to block until finished.

## What's tracked where

`GET /runs`/`GET /runs/{run_id}` come from a registry persisted as
`.job_registry.json` inside the configured report root, written on every status
change and reloaded on startup. This history survives an API restart. A run still
`"running"` when the server stops is restored as `"failed"` with an "Interrupted by
API server restart" error. The `/reports/...` endpoints read the same root directly,
so existing CLI or API reports there remain readable whether or not the registry
contains them.

Dashboard sync state lives on disk beside the report it describes, so it
survives an API restart and is readable whether the run came from the CLI or the
API. The **processed ledger** (`reports/.dashboard_sync_ledger.json`) remains the
authority on whether a report was published: it maps a run timestamp to the
digest of the manifest and feed that were synced. The per-run
`reports/<run_timestamp>/sync_status.json` sidecar adds the lifecycle around
that fact — attempt, timings, error, counts — and is written atomically under a
per-run lock, so a reader never sees a half-written file and two writers cannot
interleave.

The sidecar is a projection, never a competing source of truth. If it is missing
or unreadable the API derives the status from the manifest and the ledger
instead: a `failed` manifest or a folder with no manifest reads as
`not_required`, a completed manifest whose digest is in the ledger reads as
`completed`, and a completed manifest that is not reads as `queued`. That is why
runs that predate this feature report a sensible status without a backfill, and
why deleting a sidecar cannot make a published run look unpublished.
`reports/.dashboard_sync_worker.json` holds the last pass's outcome for
`/sync/status`.

Summary, history, run and sync-status routes have explicit response models.
Summary/history models preserve unknown result fields so additions to the
on-disk feed remain visible; their `evidence` items describe the API-enriched
`kind`, display `path`, opaque `ref`, `label` and `size_bytes` contract. Download
routes return file responses and retain their service-specific root and run
policies.

## CORS

Browser callers are limited to `http://localhost:5173` and `http://127.0.0.1:5173` by default. Set `CORS_ALLOWED_ORIGINS` to a comma-separated list of exact origins for any other dashboard host, for example a LAN-hosted frontend. Do not use `*`; the server rejects wildcard origins at startup. Origins are compared exactly — scheme, host and port all have to match, and nothing is normalized or wildcard-expanded.

The value can come from either the shell environment or the repository `.env`:

```env
CORS_ALLOWED_ORIGINS="http://localhost:5173,https://dashboard.example.com"
```

Precedence is **exported environment variable → repository `.env` → built-in localhost defaults**. An explicitly exported value always wins, including an exported empty string, which falls back to the defaults rather than reaching into `.env`. An unset, empty or absent value in every source leaves the defaults in place. Values may be quoted, and surrounding whitespace around each origin is trimmed.

**Only allowlisted keys are read from `.env` by the API** — currently `CORS_ALLOWED_ORIGINS`, `ARTIFACT_STORE_DIR`, `IOS_PLAYBOOK_DIR`, `ANDROID_PLAYBOOK_DIR` and `REPORTS_DIR`, all non-secret. `mobile_playbook/api/settings.py` holds an explicit allowlist and refuses any other key, and it parses one key at a time rather than importing the file — so `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_URL` and `MOBSF_API_KEY` never enter the API process's environment even though they sit in the same file. The service-role key stays worker-only: the dashboard sync worker is a separate process that loads its own credentials (see [Durable dashboard sync](#durable-dashboard-sync)). Nothing read here is logged or returned by any endpoint.

Resolution happens when `cors_allowed_origins()` is called rather than at module import of a launcher, so it behaves the same however the app is started — `python -m mobile_playbook.api`, the same command with `--reload` (whose worker subprocess re-imports the app), or `uvicorn mobile_playbook.api.app:app` directly.

## Current limitations and follow-up work

1. **No built-in user authentication on the automation API.** No auth, no
   authorisation, no rate limiting. Acceptable only for localhost or
   trusted-network use. Internet-accessible deployments need authentication and
   TLS in front of this service, preferably at a reverse proxy or VPN boundary.
2. **Evidence and report access is filesystem based.** The path helpers
   constrain reads to `reports/`, `work/`, or configured playbook image
   directories, but there is no per-user authorisation and a protected
   deployment should still treat these as sensitive assessment artifacts.
3. **Single-host worker assumptions.** The sync lock, processed ledger and
   per-run status sidecars are local files. Two automation hosts writing the
   same dashboard would need shared state.
4. **Eventual consistency between reports and the dashboard.** There is no push
   notification when a run lands in Supabase; clients poll
   `GET /runs/{run_id}/sync-status`.
5. **No published contract for a replacement implementation.** FastAPI serves a
   generated schema at `/openapi.json` and a browser at `/docs`, but there is no
   versioned, reviewed OpenAPI or JSON Schema file that an alternative server
   could be validated against — so "implements the same endpoints" cannot be
   checked mechanically.
6. **No API versioning.** Paths are unversioned, so a breaking change would
   break clients silently.
7. **No retention or pruning** of `reports/` and `work/`. Both grow without
   bound and hold sensitive data.
8. **One run per platform, one host.** No queue, no horizontal scaling; the
   attached device is the hard constraint.
9. **SARIF results carry no source locations**, because these are runtime
   findings about a device and a binary. Consumers that require a location per
   result cannot annotate a diff from them. See
   [SARIF export](#sarif-export).
10. **Implemented-control archives have no per-user authorisation.** They are
    protected only by the API's network posture and the
    `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED` switch, which is all-or-nothing for the
    host. A deployment that needs per-application access control on those
    archives has to add it at a reverse proxy. See
    [developer-playbook.md](developer-playbook.md#assets-and-archives).
11. **The control catalogue is invalidated by file fingerprint, not by watching
    the filesystem.** A change is picked up on the next request, but a change
    that leaves path, modification time and size identical is not seen until a
    `POST …/playbook/reload` or a restart.
12. **Only iOS has a playbook today.** `ANDROID_PLAYBOOK_DIR` is read and the
    catalogue is platform-agnostic, but no Android control documents exist yet,
    so Android risks report no controls.
13. **Control status inferred from a filename is a compatibility measure.** A
    `(deprioritise)` marker in a filename is honoured because the existing
    corpus uses it, but it is fragile. Prefer front matter or
    `configs/split/<platform>/controls.yaml`; a control resolved from a filename
    reports `status_source: "naming"` so the difference stays visible.
