# Troubleshooting

Symptom-to-cause tables for the backend. Platform-specific failures have their
own pages: [ios/reports-and-troubleshooting.md](ios/reports-and-troubleshooting.md)
and [android/reports-and-troubleshooting.md](android/reports-and-troubleshooting.md).

## Where to look first

| Question | Source |
| --- | --- |
| Did the run execute? | `reports/<RUN_TIMESTAMP>/run_manifest.json` — `status` is `completed` or `failed` |
| What did each risk decide? | `reports/<RUN_TIMESTAMP>/summary.md`, or `dashboard_results.json` for the machine-readable feed |
| What happened during one test? | `reports/<RUN_TIMESTAMP>/<PLATFORM>/<APP_ID>/<RISK_ID>/<CASE_ID>/logs.txt` and `report.json` |
| What happened moment to moment? | `reports/<RUN_TIMESTAMP>/events.jsonl`, or `GET /runs/<RUN_ID>/events` live |
| Did the dashboard get it? | `GET /runs/<RUN_ID>/sync-status` |
| Is the sync worker healthy? | `GET /sync/status`, then `artifacts/work/dashboard-sync.log` |
| What did the API do? | its stdout — `mobile_playbook.*` records are formatted by the API's log config |

Report contents are real assessment data. Treat them as sensitive and do not
paste them into issue trackers unredacted.

## Configuration and startup

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `validate` reports missing sections | an `include:` path is wrong, or the included file is empty | paths are relative to the entry-point config's directory; see [configuration.md](configuration.md#yaml-includes) |
| An app in `apps.yaml` is never selected | `--apps` matched nothing | matching ignores case and non-alphanumerics; check the `id`/`name` actually present |
| A setting in `.env` has no effect on the API | only `CORS_ALLOWED_ORIGINS` is read from `.env` by the API | export it, or add it to `ALLOWED_ENV_KEYS` deliberately |
| A key added to `.env` is ignored entirely | it was appended to a line that had no trailing newline | put the key on its own line |
| API refuses to start, complains about origins | `CORS_ALLOWED_ORIGINS` contains `*` | list exact origins; wildcards are rejected by design |
| Port 8000 conflict | MobSF defaults to 8000 | the API defaults to 8080; keep them apart |

## Runs

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `POST /runs` returns `409` | that platform already has a run in progress | one run per platform at a time — one device |
| `POST /runs` returns `422` | an unknown app or risk was selected | `GET /platforms/{platform}/risks`, `GET /config/{platform}/apps` |
| A run stays `running` forever | a hung device or Appium session; the backend has no timeout | restart the API; on reload, still-`running` records are rewritten to `failed` with "Interrupted by API server restart" |
| A new run cannot start after a hang | the platform claim is still held in-process | restarting the API releases it |
| Run failed before any risk executed | device, Appium or artifact setup | `run_manifest.json`'s `error`, then `artifacts/work/<platform>/` logs |
| A risk reports `Inconclusive` | the check could not reach a verdict — not a crash | read that test's `logs.txt`; this is a normal outcome |
| Two runs in the same second | both reserved a timestamp | the second takes a `-2` suffix; folders never merge |

## Devices and tooling

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Appium not reachable | server not started | run `appium`, or set `device.appium_auto_start.enabled: true` |
| Appium dies mid-run | session crash | the run's mid-run health check restarts it and emits an `appium_recovery` event |
| iOS device never unlocks | the device has a passcode/Face ID/Touch ID | Appium cannot enter one on a real device; remove it on the test device |
| iOS install fails | signing identity or Team ID mismatch | check `device.team_id` and `device.xcode_signing_id` |
| Android device not found | USB debugging not authorised | `adb devices`, accept the prompt on the device |
| Repackaging risk fails immediately | `apktool`/`apksigner`/`keytool` missing | install them and confirm they are on `PATH` |
| MobSF analysis fails | MobSF not running, or `MOBSF_API_KEY` unset/incorrect | start MobSF; the static-analysis risk can also use the `builtin` provider |

## Dashboard sync

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Dashboard shows nothing after a completed run | the sync is still `queued`/`running` | poll `GET /runs/<RUN_ID>/sync-status`; the dashboard is eventually consistent |
| Sync status is `not_required` | the run did not complete, or the folder predates `run_manifest.json` | expected; `--allow-legacy-report` imports historical folders deliberately |
| Sync status is `failed` | a Supabase write failed | read `error`; if `retryable` is true, `POST /runs/<RUN_ID>/sync` |
| Sync status `failed`, `retryable: false` | ambiguous application match | two unlinked dashboard applications share a name and platform; link or rename one |
| `queue_depth` > 0 and nothing drains it | the recovery sweep is not firing | check the launchd activation class — see [operations.md](operations.md#how-a-pass-is-triggered) |
| Worker exits immediately, code 2 | `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` missing | set them in `.env`; they are worker-only |
| Duplicate applications appear | an older dashboard row was created without an `external_id` | the worker adopts one unlinked match; more than one is refused as ambiguous |

## API and clients

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Browser blocked by CORS | the calling origin is not listed | set `CORS_ALLOWED_ORIGINS` to the exact origin, scheme and port included |
| `GET /runs/{run_id}/summary` returns `409` | the run is still executing | wait for `completed` |
| `GET /runs/{run_id}/summary` returns `500` | the run failed | the body carries the run's error |
| SSE stream never opens | the run id is unknown to the registry | the registry only knows runs started through this API; CLI runs have reports but no registry entry |
| SSE returns `404` | same | use `GET /reports/<RUN_TIMESTAMP>/summary` for CLI-started runs |
| A report endpoint returns `404` for a run that exists | the run directory or the requested file is absent | `GET /reports` lists what is on disk |
| `GET /reports/{ts}/sarif` returns `404` | no completed manifest, or no results feed | SARIF is only generated for completed runs |
| Evidence link 404s | the file is outside `artifacts/reports/` and `artifacts/work/` | path helpers refuse anything outside those roots by design |

## Getting a clean reproduction

1. `python -m mobile_playbook validate --platform <PLATFORM> --config configs/<PLATFORM>.yaml`
2. `... run ... --dry-run` to confirm app and risk selection without a device.
3. Run a single app and a single risk: `--apps example-app --risks <RISK_ID>`.
4. Read `run_manifest.json` before anything else — it distinguishes "the run
   failed" from "the run completed and a risk found something".


## Application icons

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every app shows the dashboard placeholder | `0016_application_icon_refs.sql` not applied, or no reference written yet | apply the migration, then `python -m mobile_playbook.icon_backfill` |
| Sync fails with `column ... does not exist` | same migration missing | apply it; the worker writes icon fields on every application row |
| One app keeps its placeholder | its build has no icon this backend can read | check the reason in `derived/artifacts/<ARTIFACT_ID>.json`; `asset_catalog_no_extractor` and `adaptive_icon_vector_only` are known limits |
| Icon endpoint returns 404 for an app that has one | the API process predates the route | restart the API; `curl .../openapi.json` should list `/config/{platform}/apps/{app_id}/icon` |
| Icon is stale after a new build | the run that produced the dashboard row used the older build | expected — icons are pinned to the build under test; the next run re-pins |
| `asset_catalog_tool_unavailable` on every iOS app | not running on macOS, or `assetutil` missing | expected off macOS; loose-PNG extraction still works |
| Backfill reports `ambiguous` | several unlinked rows share the app's name and platform | link or rename them in the dashboard, then re-run |
