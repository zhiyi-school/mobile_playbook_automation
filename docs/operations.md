# Operations

Running the dashboard sync worker: what it does, how it is triggered, how it
stays safe against duplicate and partial passes, and how to diagnose it when
the dashboard falls behind. For the endpoints referenced here see
[api.md](api.md); for the design behind them see
[architecture.md](architecture.md#dashboard-sync-layer).

Skip this page entirely if you use the backend standalone. Reports and evidence
land on disk and the API serves them whether or not any dashboard exists.

## Ownership

```text
Backend automation   owns execution, raw reports, evidence, run status, SARIF
Sync worker          translates completed reports into Supabase
Supabase             owns users, roles, teams, applications, assessments,
                     findings, finding history, tickets, retests, messages,
                     activity
Frontend             reads backend automation state and Supabase dashboard
                     state; performs no authoritative synchronisation
```

The worker is the **only** writer of dashboard rows from automation results.
Nothing in a browser writes them, and nothing reads SARIF back.

## Two statuses, two questions

| | Automation status | Dashboard sync status |
| --- | --- | --- |
| Endpoint | `GET /runs/{run_id}` | `GET /runs/{run_id}/sync-status` |
| Owner | the API's run registry | the sync worker |
| `completed` means | the device finished every selected risk and the report is on disk | every Supabase write for that report succeeded and the ledger recorded it |

A run is routinely `completed` while its sync is still `queued` or `running`.
The dashboard is eventually consistent by design.

## Running the worker

```bash
SUPABASE_URL=https://dashboard.example.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=<SERVICE_ROLE_KEY> \
python -m mobile_playbook.dashboard_sync --reports-dir reports
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--run-timestamp <RUN_TIMESTAMP>` | limit the pass to one run; repeatable |
| `--allow-legacy-report` | import report folders that predate `run_manifest.json`; their completion is unverified |
| `--force` | re-sync reports already in the processed ledger |
| `--lock-wait-seconds <n>` | wait this long for the host lock instead of exiting busy |
| `--interval-seconds <n>` | development-only in-process loop |
| `--triggered-by <id>` | attribute history rows to a user |

Exit codes: `0` success, `1` at least one report failed to sync, `2` missing
credentials (one error line, no traceback).

## How a pass is triggered

**Post-run trigger (primary).** After a run writes its terminal manifest, the
API or CLI launches a detached one-shot worker. The child loads credentials
from `.env` itself, so the API never imports the service-role key. Controlled by
`DASHBOARD_SYNC_AUTO_TRIGGER`. Triggering is best-effort and can never change a
run's outcome.

**Recovery sweep (backstop).** A launchd agent runs the same command on a
schedule so a trigger that never fired cannot strand a report. Install from the
tracked template:

```bash
cp tools/dashboard_sync/com.mobile-playbook.dashboard-sync.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) \
   ~/Library/LaunchAgents/com.mobile-playbook.dashboard-sync.plist
launchctl print gui/$(id -u)/com.mobile-playbook.dashboard-sync
```

The template uses `StartCalendarInterval`. That matters: on this host
`StartInterval`, `RunAtLoad` and `KeepAlive` are all held as
`pended nondemand spawn = speculative` and never fire, while a calendar
interval does. If a sweep appears not to run, check the activation class before
anything else.

The plist carries **no credentials** — it is world-readable in
`~/Library/LaunchAgents`. The worker reads `.env` itself.

**Manual retry.** `POST /runs/{run_id}/sync` queues another pass for one run.

## Safety properties

**Single instance.** `reports/.dashboard_sync.lock` is a host-wide `flock` held
for the duration of a pass. A second pass exits as busy, or waits when
`--lock-wait-seconds` is set (post-run triggers use 60s so two platforms
finishing together serialise rather than one vanishing).

**Processed ledger.** `reports/.dashboard_sync_ledger.json` maps a run timestamp
to the sha256 digest of that run's manifest and results feed. A matching digest
short-circuits the report as unchanged. A re-run that rewrites the same
timestamp changes the digest and is resynced.

**Idempotency, independent of both.** Applications and assessments are upserted
on `external_id`. `finding_history` and `activity_log` rows carry a
deterministic `sync_key` with a unique index behind it, so a duplicate insert
conflicts and is swallowed. A finding's `status` and `latest_test_run_id` are
written **last**, so a pass that dies partway leaves the pre-run state for the
retry to find. Replaying an older run never regresses a finding a newer run
already moved.

**Failed runs.** A run whose manifest is not `completed` is never imported. Its
correlated retest, if any, is failed with the run's error and none of its
partial rows are read.

## Sync status per run

`GET /runs/{run_id}/sync-status` returns one of:

| Status | Meaning |
| --- | --- |
| `queued` | the run completed; a worker has been asked to publish it |
| `running` | a worker is writing this run's rows now |
| `completed` | every expected write succeeded and the ledger recorded it |
| `failed` | a write failed; `error` is short and redacted, `retryable` says whether repeating could help |
| `not_required` | no sync is expected — the run did not complete, or the folder predates the manifest |

State lives in `reports/<RUN_TIMESTAMP>/sync_status.json`, written atomically
under a per-run lock. It is a *projection*: if it is missing or unreadable the
API derives the status from the manifest and the ledger instead, so runs made
before this existed report correctly and deleting a sidecar cannot make a
published run look unpublished.

`counts` describes what the **most recent attempt** reconciled, so a repeat pass
the ledger short-circuits reports zeros — accurately, because it wrote nothing.

## Worker health

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

`worker_state` is `running` when something holds the host lock. `queue_depth`
counts runs whose recorded status is `queued` or `running`.
`recovery_sweep_enabled` reports whether the launchd agent is installed for the
current user. The endpoint returns operational state only — no credentials, no
report contents, no filesystem paths.

### Diagnosing a worker that is not keeping up

Read `/sync/status` first, then:

| Symptom | Cause | Action |
| --- | --- | --- |
| `enabled: false` | automatic triggering is off for the API process | check `DASHBOARD_SYNC_AUTO_TRIGGER` in the environment and `.env` |
| `queue_depth` > 0, `worker_state: "idle"`, stale `last_success_at` | nothing is draining the queue | check the launchd agent's activation class; read `work/dashboard-sync.log` |
| `last_error` set | the last pass ran and something failed | read the per-run `sync-status` for each affected run |
| pass starts and exits 2 | missing credentials | `work/dashboard-sync.log`, not this endpoint |
| a run stays `not_required` | its manifest is not `completed`, or it predates the manifest | expected; use `--allow-legacy-report` deliberately for historical folders |

`work/dashboard-sync.log` accumulates every detached worker's output and is the
first place to look for anything the endpoints do not explain.

## Retrying one run

```bash
curl -X POST http://127.0.0.1:8080/runs/<RUN_ID>/sync
```

Returns `202` with the same body as `sync-status`. The retry deliberately does
**not** pass `--force`, so the ledger still short-circuits anything that already
landed — retrying a fully-synced run writes nothing, and a run that failed
partway is reconciled by the same upsert-and-`sync_key` path that makes an
ordinary repeat pass safe. A run already `queued` or `running` returns its
current status without starting a second worker; a `not_required` run is
refused with `409`.

## Limitations

- **Single host.** The lock, the ledger and the status sidecars are all local
  files. Two automation hosts writing the same dashboard would need shared
  state.
- **No worker authentication boundary.** Anything that can run a process on the
  host can read `.env` and therefore the service-role key. Host access is the
  security boundary.
- **Eventual consistency.** There is no push notification; the dashboard learns
  a run landed by polling `sync-status`.


## Application icons

The backend owns IPA/APK files and every icon derived from them; the dashboard
database holds only a checksum and a logical `icons/<ARTIFACT_ID>.png`
reference. See [api.md](api.md#application-icons) for the endpoint, the
extraction rules and their limits.

**Storage.** `ARTIFACT_STORE_DIR` (default `<repository root>/derived`) must be
on a volume that survives a redeploy wherever the checkout is disposable —
[configuration.md](configuration.md#artifact_store_dir) covers persistence,
cleanup and retention. Losing the store is not a data-loss event: icons report
as unavailable until something re-derives them.

**Filling in existing applications.**

```bash
python -m mobile_playbook.icon_backfill --dry-run
python -m mobile_playbook.icon_backfill
```

Run it once after applying `0016_application_icon_refs.sql`. It only updates
applications the dashboard already has, never creates rows, and skips anything
ambiguous. Ordinary sync passes keep the references current afterwards.

**Migration requirement.** `0016_application_icon_refs.sql` must be applied
before a sync pass can record icon references. Without it the worker's
application writes fail with `column ... does not exist`.
