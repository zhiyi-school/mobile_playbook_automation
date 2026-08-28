# HTTP API

`mobile_playbook/api/` is a thin HTTP wrapper around the same functions the CLI (`python -m mobile_playbook ...`) calls — config loading/validation, risk listing, `run_platform()`, and the `reports/<run_timestamp>/` files each run already writes. It exists so a separate dashboard (or `curl`, or the interactive docs) can trigger runs and read results without shelling out to the CLI.

Nothing about the CLI changes because of this — `python -m mobile_playbook ...` still works exactly as before, and both entry points share the same underlying code.

## Running the server

```bash
python -m mobile_playbook.api --port 8080
```

Add `--reload` during development to restart on code changes, and `--host 0.0.0.0` to accept connections from other machines on the network (leave it on the default `127.0.0.1` for local-only use).

Run this from the repository root, the same way you'd run `python -m mobile_playbook`, since config paths and `reports/` are resolved relative to the process's working directory.

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

`POST /runs` takes the same inputs as the CLI's `run` command (`--config`/`--platform`/`--apps`/`--risks`/`--out`) and starts it in a background thread, returning right away with a `run_id`:

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
{"run_id": "2026-08-20_09-28-42", "platform": "ios", "status": "running"}
```

`apps`/`risks` are optional comma-separated strings, same as the CLI flags — omit either to run every enabled app/risk in the config. `out_dir` defaults to `reports` if left out.

The `run_id` *is* the run's timestamp and its `reports/<run_id>/` directory name — reserved atomically the moment the request comes in, so it's already known before the run itself finishes, and two requests in the same second never collide (each gets its own `-2`/`-3`/... suffix, same scheme the CLI already uses for same-second collisions). There's no separate ID scheme to translate between.

Poll it for status:

```bash
curl http://127.0.0.1:8080/runs/2026-08-20_09-28-42
```

```json
{"run_id": "2026-08-20_09-28-42", "platform": "ios", "config_path": "configs/ios.yaml", "status": "completed", "run_timestamp": "2026-08-20_09-28-42", "run_dir": "reports/2026-08-20_09-28-42", "error": null, "started_at": "...", "completed_at": "..."}
```

Once `status` is `"completed"`, fetch the results:

```bash
curl http://127.0.0.1:8080/runs/2026-08-20_09-28-42/summary
```

This returns the same `dashboard_results.json` content the run wrote to disk. The identical value also works under `/reports` (useful since that path works for CLI-started runs too, not just ones started via `/runs`):

```bash
curl http://127.0.0.1:8080/reports/2026-08-20_09-28-42/summary
```

## Watching a run's progress live

`GET /runs/{run_id}` only ever reports one of three coarse states (`running`/`completed`/`failed`) — enough to know when a run is done, but nothing about what it's doing while it runs, which can be minutes for a config with several apps and risks. `GET /runs/{run_id}/events` streams that in real time over Server-Sent Events instead of needing to poll:

```bash
curl -N http://127.0.0.1:8080/runs/2026-08-20_09-28-42/events
```

```text
data: {"type": "risk_started", "timestamp": "...", "app_id": "app_one", "risk_id": "ios-feature-01-risk-01"}

data: {"type": "risk_completed", "timestamp": "...", "app_id": "app_one", "risk_id": "ios-feature-01-risk-01", "verdict": "At Risk", "final_status": "IPA_ANALYSIS_COMPLETE"}

data: {"type": "appium_recovery", "timestamp": "...", "message": "ios: Appium server at http://127.0.0.1:4723 is no longer reachable mid-run — attempting to recover and resume."}

data: {"type": "done", "status": "completed", "error": null}
```

Every `risk_started`/`risk_completed` event comes from the same run loop that writes each test's `report.json`, and `appium_recovery` fires from the same mid-run health check that restarts Appium after a crash (see [Appium auto-start](ios/configuration.md#appium-auto-start)) — so this is the same information already available in `summary.md`/`appium.log` after the fact, just pushed live instead of read after the run finishes. The stream ends with a `"done"` event once `GET /runs/{run_id}` would report anything other than `"running"`, then closes; a browser can consume it directly with `new EventSource(url)`.

These events are read from `reports/{run_id}/events.jsonl`, appended to as the run progresses — a client that connects late still gets every event from the start (each poll re-reads the whole file), and any number of clients can watch the same run independently.

A `POST /runs` call still needs everything a CLI `run` needs to actually succeed — Appium running and the device connected/trusted. The API doesn't remove those requirements, it just lets you kick the run off and check on it over HTTP instead of watching a terminal. Device *unlocking* specifically is handled automatically now (see [Automatic unlock](ios/configuration.md#automatic-unlock)) as long as the device has no passcode/Face ID/Touch ID set — Appium can't enter a passcode or biometric on a real device, so a locked, secured device still needs a person.

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
curl -X POST http://127.0.0.1:8080/artifacts/ios -F "file=@app.ipa"
```

```json
{"path": "intake/ios/ipas/app.ipa", "metadata": {"bundle_id": "com.example.app", "display_name": "...", "...": "..."}}
```

The file must match the platform's expected extension (`.ipa` for `ios`, `.apk` for `android`) or the request is rejected with `400`. Metadata comes from `inspect_ipa_metadata()` or `inspect_apk_metadata()`: iOS reads bundle ID, display name, version and `Info.plist`; Android reads package name, display name and version through `aapt`, `aapt2` or `apkanalyzer`. A file with the same name overwrites whatever was already in the intake folder, matching how that folder already works as a plain drop-zone.

## Editing config

`/config/{platform}/apps`, `/config/{platform}/risk-settings/{risk_id}`, and `/config/{platform}/device` / `/config/{platform}/runner` read and write the same YAML files under `configs/` that the CLI reads — there's no separate copy of the config for the API. Every write re-runs the real config loader/validator against what's now on disk and reverts the file if the edit **introduced** a problem, so an edit can never make the config worse than it found it. Problems that were already there — another app still waiting for its build, say — don't block an unrelated edit, which would otherwise make one broken entry freeze the whole config:

```bash
curl -X POST http://127.0.0.1:8080/config/ios/apps \
  -H "Content-Type: application/json" \
  -d '{"name": "REPLACE_WITH_APP_NAME", "bundle_id": "com.example.app", "test_bundle_id": "com.example.app", "artifact": {"source": "local_ipa", "ipa": "intake/ios/ipas/app.ipa"}, "risks": {"ios-feature-01-risk-01": {"enabled": true}}}'
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

`configs/split/ios/apps.yaml` is the one exception to full comment/anchor preservation on the entries themselves: its app entries use `<<: *anchor` references to templates defined in the sibling `templates.yaml`, which can only be parsed together with that file, not on its own. Editing or adding an app there writes that one entry with fully explicit values instead of the anchor shorthand — every other untouched app entry, and all of the file's comments, are left byte-for-byte as they were. `configs/split/android/apps.yaml` has no such anchors, so Android app edits round-trip in full.

`GET /config/{platform}/risk-settings/{risk_id}` only covers risks that have global settings shared across apps (`ios-feature-01-risk-01`, `ios-feature-02-risk-01`, `ios-feature-04-risk-01`, `android-feature-01-risk-02`, `android-feature-06-risk-01`) — a per-app override still goes through that app's own `risks.<risk_id>` entry via the apps endpoints above.

Every app entry also carries `sector`, `agency`, `version`, and `cisos` (a list of `{"name", "email"}`) alongside its identity/artifact fields — organizational metadata a dashboard displays, not read by the automation run itself. All four are optional and default to blank/empty; `PUT /config/{platform}/apps/{app_id}` accepts any of them like any other field, including clearing `cisos` back to `[]` (unlike most other list-valued fields on this endpoint, an empty `cisos` in the request body does clear it rather than being treated as "no change").

## Playbook images

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
            - path: attachments/feature1_risk1_ss1.png
              caption: Screenshot shows possible exposed credentials and api key found by MobSF
```

Paths are deliberately never absolute: moving the playbook means changing `playbook_dir` alone, and every image keeps resolving. `playbook_dir` may be absolute or relative to the repository root, and `~` expands. That file is gitignored, so the path stays machine-local.

`GET /platforms/{platform}/risks` adds two derived fields to each image — `url`, the endpoint to fetch it from, and `exists`, whether the file is actually on disk right now. `exists` is what turns a stale `playbook_dir` into something a dashboard can show plainly rather than a broken image. Both are recomputed per request and stripped on `PUT`, so they never end up in the YAML.

The image itself comes from `GET /platforms/{platform}/playbook/images/{image_path}`. Since a playbook is typically a whole vault — notes, source, editor state — and this API has no authentication, that endpoint serves **only** files under `playbook_dir` whose suffix is one of `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`. Anything else, and anything resolving outside `playbook_dir` (`../`), is a `404`.

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
| `POST /runs` | Starts a run in a background thread and returns immediately (`202`) with a `run_id` — it does not wait for the run to finish. Body: `{"platform", "config_path", "apps"?, "risks"?, "out_dir"?}`, mirroring `run`'s `--apps`/`--risks`/`--out` flags. `409` if that platform already has a run in progress. |
| `GET /runs` | Lists runs started through this API (this process's history only — see below). |
| `GET /runs/{run_id}` | One run's status: `running`, `completed`, or `failed`, plus its `run_timestamp`/`run_dir` once known. |
| `GET /runs/{run_id}/events` | Server-Sent Events stream of this run's progress — `risk_started`/`risk_completed`/`appium_recovery` events as they happen, ending with `done`. See [Watching a run's progress live](#watching-a-runs-progress-live). |
| `GET /runs/{run_id}/summary` | The completed run's `dashboard_results.json`, by `run_id`. `409` while still running, `500` with the error if it failed. |
| `GET /reports` | Lists every `reports/<run_timestamp>/` directory on disk, most recent first — including runs started from the CLI, not just from this API. |
| `GET /reports/{run_timestamp}/summary` | The same `dashboard_results.json`, looked up directly by `run_timestamp` instead of `run_id`. Works for any run on disk regardless of how it was started. |
| `GET /reports/{run_timestamp}/files/{file_path}` | Serves any file inside that run's report directory — screenshots, recordings, `report.json`, `logs.txt`, `critical_findings.md`, etc. |
| `GET /reports/{run_timestamp}/evidence-file?path=...` | Serves report evidence stored under either `reports/` or `work/`, while rejecting paths outside those roots. |
| `GET /apps/{app_id}/risks/{risk_id}/history?limit=20` | Returns the latest matching summary rows for one app/risk without forcing clients to scan all report folders. |
| `GET /artifacts/{platform}` | Builds sitting in `intake/{ios,android}/{ipas,apks}/`, newest first. iOS identity fields are read from each IPA; Android APK identity fields are read with Android SDK tooling when available. Unreadable files are skipped. |
| `POST /artifacts/{platform}` | Multipart file upload (`file`) into `intake/{ios,android}/{ipas,apks}/`. Returns `{"path", "metadata"}`; `400` if the file extension doesn't match the platform. |
| `GET/POST /config/{platform}/apps` | List every configured app, or add a new one. |
| `GET/PUT/DELETE /config/{platform}/apps/{app_id}` | Read, partially update, or remove one app. |
| `GET /config/{platform}/apps/{app_id}/provisioning` | Whether this app is ready to be tested yet, stage by stage. See [Is an app ready to test?](#is-an-app-ready-to-test) |
| `GET/PUT /config/{platform}/risk-settings/{risk_id}` | Read or partially update a risk's global settings (only risks with shared cross-app settings — see below). |
| `GET/PUT /config/{platform}/device` | Read or partially update the `device:` block. |
| `GET/PUT /config/{platform}/runner` | Read or partially update the `runner:` block. |

A run is asynchronous because it isn't a quick request/response: it drives real Appium sessions against a physical device and can take several minutes, and some risks (for example the custom-keyboard keystroke-collection risk) may need a person to grant permissions mid-run that `runner.permission_alerts` doesn't already cover. Use `GET /runs/{run_id}/events` for live progress, or poll `GET /runs/{run_id}` for just the coarse status, instead of expecting `POST /runs` to block until finished.

## What's tracked where

`GET /runs`/`GET /runs/{run_id}` come from a registry that's persisted to `reports/.job_registry.json`, written on every status change and reloaded on startup — this history survives an API server restart. A run still `"running"` at the moment the server stops can never actually finish (the restart kills the thread driving it), so on reload it's rewritten to `"failed"` with an "Interrupted by API server restart" error instead of hanging a poller forever. The `reports/{run_timestamp}/...` endpoints read straight off disk instead, so they see every run that ever wrote a `reports/<run_timestamp>/` folder, from the CLI or the API, past or present, regardless of whether the server was restarted since.


## Known gaps / recommended backend additions

1. **No authentication on the automation API.** Fine for local/trusted-network use, but anything beyond that needs a reverse proxy or backend change.
