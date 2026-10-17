# Operations

Running the dashboard sync worker: what it does, how it is triggered, how it
stays safe against duplicate and partial passes, and how to diagnose it when
the dashboard falls behind. For the endpoints referenced here see
[api.md](api.md); for the design behind them see
[architecture.md](architecture.md#dashboard-sync-layer).

The stable command remains `python -m mobile_playbook.dashboard_sync`. The
package's `__main__.py` only calls the worker; argument parsing and the worker
loop live in `mobile_playbook/dashboard_sync/worker.py`, and only the
worker-owned Supabase adapter reads service-role credentials.

Skip this page entirely if you use the backend standalone. Reports and evidence
land on disk and the API serves them whether or not any dashboard exists.

## Ownership

```text
Backend automation   owns execution, raw reports, evidence, run status, SARIF
Sync worker          translates completed reports into Supabase
Supabase             owns users, roles, teams, applications, assessments,
                     findings, finding history, tickets, retests, risk conversations,
                     activity
Frontend             reads backend automation state and Supabase dashboard
                     state; performs no authoritative synchronisation
```

The worker is the **only** writer of dashboard rows from automation results.
Nothing in a browser writes them, and nothing reads SARIF back.

The sync records the build version each run tested on its assessment
(`assessments.app_version`), from the run manifest's `artifact_versions`: the
intake build's version on iOS, and on Android the version installed on the
device when the run started
([Recording the tested version](android/README.md#recording-the-tested-version)). A run
that no run request started, such as a risk rerun from a test page or a CLI run,
updates the assessment that was current when it started instead of adding one,
and leaves that assessment's `app_version` as it is.
Each assessment keeps the newest run applied to it in `latest_run_id`, and an
older run, such as a replayed report, leaves it unchanged. These columns come
from fixes `0041_assessment_build_versions.sql` and
`0042_assessment_latest_run.sql`; apply both before deploying this sync worker,
because against an older schema every assessment write is refused and the
report stays unsynced.

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
python -m mobile_playbook.dashboard_sync --reports-dir artifacts/reports
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

## Running the assessment execution worker

The sync worker imports finished reports. A second, separate worker turns queued
assessments into runs, so execution never depends on a browser tab staying open.

```bash
SUPABASE_URL=https://dashboard.example.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=<SERVICE_ROLE_KEY> \
AUTOMATION_API_URL=http://127.0.0.1:8080 \
python -m mobile_playbook.workers.assessment_worker
```

The installed `mobile-playbook-assessment-worker` command is equivalent. The
module moved from `mobile_playbook.assessment_worker`; a launch agent or service
that still runs the old module path fails with `No module named` and must be
updated.

It requires the dashboard schema through fix
`0036_assessment_run_request_run_id.sql`. Without the `assessment_run_requests`
table every pass logs a Supabase error and nothing runs. When a run starts, the
worker marks the request `completed` and records the run's `run_id` on it in the
same write, so the dashboard can follow that run and its dashboard sync. Apply
`0036` before deploying this worker: against an older schema that write is
refused after the run has started, the request keeps its lease until it expires,
and the request is then claimed and started again.

A request that names a `build_version`, set by the dashboard's "Assess a new
version", passes it to `POST /runs`, so the run tests that build. When the
automation host has no build with that version, the request waits with the
retryable blocker `app_build_missing` and is retried once the build is in
intake. A request without one lets the host test the app's highest version.

When readiness reports an Android app as `app_not_installed`, the worker asks the
automation API to install it with the configured install method, then checks
readiness again and starts the run. That request waits up to eight minutes. A
paid or unavailable app, a store account prompt, or a store screen the
installer does not recognise fails the request with its own `blocker_code`, so
someone can act on the device and press **Retry now**. A slow download or a
device error waits and is retried. iOS readiness never reports
`app_not_installed`. See
[Installing missing apps](android/README.md#installing-missing-apps).

| Variable / flag | Default | Effect |
| --- | --- | --- |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | — | required; the queue lives in the dashboard database |
| `AUTOMATION_API_URL` / `--api-url` | `http://127.0.0.1:8080`, the API default | where readiness is asked and runs are started |
| `REPORTS_DIR` / `--reports-dir` | `artifacts/reports` | report root passed as the API `out_dir` compatibility assertion; it must resolve to the API's configured `REPORTS_DIR` |
| `ASSESSMENT_WORKER_ID` / `--worker-id` | host name plus a random suffix | identifies the lease holder |
| `ASSESSMENT_WORKER_POLL_SECONDS` / `--poll-seconds` | `15` | how often an empty queue is checked |
| `ASSESSMENT_WORKER_LEASE_SECONDS` / `--lease-seconds` | `900` | how long a claim is held before another worker may recover it |
| `--once` | off | process at most one request and exit, for cron or a smoke test |

Retry policy is fixed in code: `30s` doubling to a `900s` ceiling, giving up
after 20 attempts. A non-retryable blocker is not retried at all.

### Keeping it running with launchd

Dashboard assessments only run while this worker is running. Install the
tracked agent once:

```bash
sed "s#REPLACE_WITH_REPO_PATH#$(pwd)#g" \
   tools/deploy/launchd/com.mobile-playbook.assessment-worker.plist \
   > ~/Library/LaunchAgents/com.mobile-playbook.assessment-worker.plist
launchctl bootstrap gui/$(id -u) \
   ~/Library/LaunchAgents/com.mobile-playbook.assessment-worker.plist
launchctl kickstart gui/$(id -u)/com.mobile-playbook.assessment-worker
launchctl print gui/$(id -u)/com.mobile-playbook.assessment-worker
```

Run it from the repository root, and change `--api-url` in the copy if the API
listens on a port other than `8080`. The agent uses `StartCalendarInterval`
every five minutes for the same reason as the
[dashboard sync agent](#how-a-pass-is-triggered): on this host `RunAtLoad` and
`KeepAlive` never fire. launchd skips a tick while the worker is still running,
so the schedule only restarts it after it exits; `kickstart` starts it
immediately instead of at the next tick. Output goes to
`artifacts/work/assessment-worker.log`. Like the sync agent, the plist carries
no credentials; the worker reads `.env` itself.

Once the agent's plist is in `~/Library/LaunchAgents/`, `python -m mobile_playbook.api`
manages it. When the API is listening it loads the agent if needed and runs
`launchctl kickstart -k`, which replaces a worker left running from before, so
the worker runs the same code as the API. When the API exits, including after
`Ctrl+C` or a startup failure, it runs `launchctl bootout`. Unloading the agent
rather than only stopping the process matters, because the five-minute schedule
would otherwise start the worker again while the API is down. Start the API with
`--no-worker` to leave the agent alone. If the API is killed outright, the worker
keeps running and simply waits with `automation_unavailable` until an API is back.

To stop it by hand, `launchctl bootout gui/$(id -u)/com.mobile-playbook.assessment-worker`.

### Is the worker healthy?

It logs one line per action. A healthy idle worker is quiet; a working one logs
`started run <RUN_TIMESTAMP> for assessment <ID>` or
`assessment <ID> waiting on <BLOCKER>, next attempt in <N>s`.

The queue itself is the better check, because it does not depend on log
retention:

```sql
select status, count(*), min(next_attempt_at)
from assessment_run_requests
group by status;
```

Rows stuck in `claimed` or `running` with `lease_expires_at` in the past mean no
worker is alive — any worker recovers them on its next pass, so a growing count
there is the signal that none is running.

### How a waiting assessment resumes

Nothing has to be done. A blocked attempt is recorded as `waiting` with a
`blocker_code` and a `next_attempt_at`, and the worker picks it up again when
that time passes. Connect the device and the next attempt starts the run against
the original assessment — no new assessment record is created.

### Retrying by hand

Use **Retry now** on the assessment page. It brings `next_attempt_at` forward and
clears the blocker on the request that already exists; it never opens a second
one, so clicking twice cannot start two runs. Equivalently, as the service role:

```sql
select request_assessment_run('<ASSESSMENT_ID>');
```

### Diagnosing "waiting for a device"

```bash
curl http://127.0.0.1:8080/config/<PLATFORM>/apps/<APP_ID>/provisioning
```

Read `blocker_code`. `no_device` means nothing is attached; `device_unreachable`
means the device tooling could not be reached at all; `platform_busy` means
another run holds the device — check `GET /runs`. `app_not_installed` on Android
is installed by the worker; the install outcomes are listed in
[Installing missing apps](android/README.md#installing-missing-apps). `configuration_incomplete` and
`no_tests_enabled` will not clear on their own and need a config change.

With Corellium baseline role devices (`device.corellium.roles`), `no_device`
names the rooted or non-rooted VM that has no eligible instance with a
compatible baseline, `platform_busy` also covers a VM still claimed by a run or
awaiting shutdown cleanup, and a missing intake APK reads as
`app_build_missing`. See
[Runs, installs and readiness](corellium-lifecycle.md#runs-installs-and-readiness).

### Corellium VMs awaiting cleanup

A run, install or crashed process that could not confirm a Corellium VM off
leaves its claim pending, which blocks reuse of that VM. The API server retries
the shutdown once at startup and the assessment worker every five minutes, so
nothing has to be done once Corellium is reachable again. Read the log for
`Recovered Corellium instance` or `still awaits cleanup`. A claim made on another
machine, or held by a process that is still running, is left alone. Never delete
the claim store to clear a pending VM; see
[Exclusive durable ownership](corellium-lifecycle.md#exclusive-durable-ownership).

### Shutting it down safely

Send `SIGINT`/`SIGTERM`. The worker only ever holds a lease while deciding
whether to start a run — it does not wait for the run itself — so stopping it
cannot interrupt a test in progress. Any lease it was holding expires after
`--lease-seconds` and the next worker recovers that request, so a hard kill is
safe too and costs at most one lease interval.

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
cp tools/deploy/launchd/com.mobile-playbook.dashboard-sync.plist \
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

**Single instance.** `artifacts/reports/.dashboard_sync.lock` is a host-wide `flock` held
for the duration of a pass. A second pass exits as busy, or waits when
`--lock-wait-seconds` is set (post-run triggers use 60s so two platforms
finishing together serialise rather than one vanishing).

**Processed ledger.** `artifacts/reports/.dashboard_sync_ledger.json` maps a run timestamp
to the sha256 digest of that run's manifest and results feed. A matching digest
short-circuits the report as unchanged. A re-run that rewrites the same
timestamp changes the digest and is resynced.

**Idempotency, independent of both.** Applications are upserted on
`external_id` and `platform`; assessments on `external_id`. `finding_history` and `activity_log` rows carry a
deterministic `sync_key` with a unique index behind it, so a duplicate insert
conflicts and is swallowed. A finding's `status` and `latest_test_run_id` are
written **last**, so a pass that dies partway leaves the pre-run state for the
retry to find. Replaying an older run never regresses a finding a newer run
already moved.

**Failed runs.** A run whose manifest is not `completed` is never imported. Its
correlated retest, if any, is failed with the run's error, a `retest_failed`
entry is posted into that retest's risk conversation, and none of its partial
rows are read.

## Sync status per run

`GET /runs/{run_id}/sync-status` returns one of:

| Status | Meaning |
| --- | --- |
| `queued` | the run completed; a worker has been asked to publish it |
| `running` | a worker is writing this run's rows now |
| `completed` | every expected write succeeded and the ledger recorded it |
| `failed` | a write failed; `error` is short and redacted, `retryable` says whether repeating could help |
| `not_required` | no sync is expected — the run did not complete, or the folder predates the manifest |

State lives in `artifacts/reports/<RUN_TIMESTAMP>/sync_status.json`, written atomically
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
| `queue_depth` > 0, `worker_state: "idle"`, stale `last_success_at` | nothing is draining the queue | check the launchd agent's activation class; read `artifacts/work/dashboard-sync.log` |
| `last_error` set | the last pass ran and something failed | read the per-run `sync-status` for each affected run |
| pass starts and exits 2 | missing credentials | `artifacts/work/dashboard-sync.log`, not this endpoint |
| a run stays `not_required` | its manifest is not `completed`, or it predates the manifest | expected; use `--allow-legacy-report` deliberately for historical folders |

`artifacts/work/dashboard-sync.log` accumulates every detached worker's output and is the
first place to look for anything the endpoints do not explain. When a post-run
worker starts and the log is over 10 MB, the trigger first moves it aside to
`dashboard-sync.log.1`, replacing the previous one, so the two files together stay
around 20 MB. The launchd sweep appends without rotating. A worker already
running at the moment of rotation finishes its pass in the `.1` file.

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

## Log files

The API writes its logs to the terminal. To also keep them on disk, start it
with `--log-file`; the file rotates at 10 MB and keeps five older copies:

```bash
python -m mobile_playbook.api --port 8080 --log-file artifacts/work/logs/api.log
```

A relative path resolves against the repository root. Prefer this to redirecting
the API's output into a file, which grows without limit.

## Pruning old run work folders

Every run writes working files under `artifacts/work/<platform>/<run_id>/` and
places the IPA it tests at `artifacts/work/ios/acquired/<run_id>/<app_id>/original.ipa`.
That file is a hard link into `acquired/.by-sha256/`, which holds one read-only
copy of each distinct IPA, so repeated runs of the same build take no extra space.
Nothing removes run folders, so the work folder still grows with every run. `prune-work` lists the
per-run folders older than the retention period, 30 days by default, and deletes
them only with `--apply`:

```bash
python -m mobile_playbook prune-work                   # list what would be deleted
python -m mobile_playbook prune-work --older-than 60   # a longer retention period
python -m mobile_playbook prune-work --apply           # delete the listed folders
```

A folder is deleted only when all of these hold:

- its name is a run id: a run timestamp, or the 12-character ids older runs used;
- it is older than the cutoff, by the timestamp in its name or else its newest file;
- no report newer than the cutoff references it; and
- its run is not in progress, according to the API run registry or the run's manifest.

A stored IPA in `acquired/.by-sha256/` is deleted once no remaining run folder
links to it, except one stored within the last hour, which a starting run may be
about to link. Folder sizes count only files the folder does not share, so
reclaimable space is not counted twice.

State that is not per-run is never a candidate: `ios/traffic_interception/` (the
Burp capture file and its health record), `android/repackaging/` (the repackaging
keystore and APKs pulled from devices), log files, and any folder whose name is
not a run id.

A report and its evidence age out together. Reports themselves are never
deleted, but once a report is past the cutoff, its evidence files under the work
folder can be, and the API then drops those items from the report's evidence
list. Deletion cannot be undone, so review the list before adding `--apply`.

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

**Storage.** `ARTIFACT_STORE_DIR` (default `<repository root>/artifacts/derived`) must be
on a volume that survives a redeploy wherever the checkout is disposable —
[configuration.md](configuration.md#artifact_store_dir) covers persistence,
cleanup and retention. Losing the store is not a data-loss event: icons report
as unavailable until something re-derives them.

**Filling in existing applications.**

```bash
python -m mobile_playbook.workers.icon_backfill --dry-run
python -m mobile_playbook.workers.icon_backfill
```

Run it once after applying `0016_application_icon_refs.sql`. It only updates
applications the dashboard already has, never creates rows, and skips anything
ambiguous. Ordinary sync passes keep the references current afterwards.

**Migration requirement.** `0016_application_icon_refs.sql` must be applied
before a sync pass can record icon references. Without it the worker's
application writes fail with `column ... does not exist`.
