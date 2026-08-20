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

This returns the same `dashboard_results.json` content the run wrote to disk. The identical value also works under `/reports` (useful since that path works for CLI-started runs too, and survives an API server restart):

```bash
curl http://127.0.0.1:8080/reports/2026-08-20_09-28-42/summary
```

A `POST /runs` call still needs everything a CLI `run` needs to actually succeed — Appium running, the device connected/unlocked/trusted, and for risks like keystroke collection, someone available to interact with the phone mid-run. The API doesn't remove those requirements, it just lets you kick the run off and check on it over HTTP instead of watching a terminal.

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

The file must match the platform's expected extension (`.ipa` for `ios`, `.apk` for `android`) or the request is rejected with `400`. Metadata comes from this repo's existing `inspect_ipa_metadata()`/`inspect_apk_metadata()` — for iOS that's the real bundle ID, display name, and full `Info.plist`; Android's APK inspector isn't implemented yet (`mobile_playbook/platforms/android/apk_tools.py`), so an Android upload still saves the file but its `metadata` comes back as `{"error": "..."}` instead of real fields. A file with the same name overwrites whatever was already in the intake folder, matching how that folder already works as a plain drop-zone.

## Editing config

`/config/{platform}/apps`, `/config/{platform}/risk-settings/{risk_id}`, and `/config/{platform}/device` / `/config/{platform}/runner` read and write the same YAML files under `configs/` that the CLI reads — there's no separate copy of the config for the API. Every write re-runs the real config loader/validator against what's now on disk and reverts the file if that fails, so an edit can never leave the config in a state `python -m mobile_playbook validate` would reject:

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

`GET /config/{platform}/risk-settings/{risk_id}` only covers risks that have global settings shared across apps (`ios-feature-01-risk-01`, `ios-feature-04-risk-01`, `android-feature-01-risk-02`, `android-feature-06-risk-01`) — a per-app override still goes through that app's own `risks.<risk_id>` entry via the apps endpoints above.

## Endpoints

| Method & Path | Purpose |
| --- | --- |
| `GET /health` | Liveness check. |
| `GET /platforms/{platform}/risks` | Every risk's full metadata — `risk_id`, `name`, `description`, `goal`, `is_blocking`, `mitre_attack_mobile_technique_id`, and platform-specific requirement fields. `platform` is `ios` or `android`. See [Risk Metadata](ios/risks.md#risk-metadata). |
| `POST /config/validate` | Same check as `validate`. Body: `{"platform", "config_path"}`. Returns `422` with the config's error list if invalid. |
| `POST /runs` | Starts a run in a background thread and returns immediately (`202`) with a `run_id` — it does not wait for the run to finish. Body: `{"platform", "config_path", "apps"?, "risks"?, "out_dir"?}`, mirroring `run`'s `--apps`/`--risks`/`--out` flags. `409` if that platform already has a run in progress. |
| `GET /runs` | Lists runs started through this API (this process's history only — see below). |
| `GET /runs/{run_id}` | One run's status: `running`, `completed`, or `failed`, plus its `run_timestamp`/`run_dir` once known. |
| `GET /runs/{run_id}/summary` | The completed run's `dashboard_results.json`, by `run_id`. `409` while still running, `500` with the error if it failed. |
| `GET /reports` | Lists every `reports/<run_timestamp>/` directory on disk, most recent first — including runs started from the CLI, not just from this API. |
| `GET /reports/{run_timestamp}/summary` | The same `dashboard_results.json`, looked up directly by `run_timestamp` instead of `run_id`. Works for any run on disk regardless of how it was started. |
| `GET /reports/{run_timestamp}/files/{file_path}` | Serves any file inside that run's report directory — screenshots, recordings, `report.json`, `logs.txt`, `critical_findings.md`, etc. |
| `POST /artifacts/{platform}` | Multipart file upload (`file`) into `intake/{ios,android}/{ipas,apks}/`. Returns `{"path", "metadata"}`; `400` if the file extension doesn't match the platform. |
| `GET/POST /config/{platform}/apps` | List every configured app, or add a new one. |
| `GET/PUT/DELETE /config/{platform}/apps/{app_id}` | Read, partially update, or remove one app. |
| `GET/PUT /config/{platform}/risk-settings/{risk_id}` | Read or partially update a risk's global settings (only risks with shared cross-app settings — see below). |
| `GET/PUT /config/{platform}/device` | Read or partially update the `device:` block. |
| `GET/PUT /config/{platform}/runner` | Read or partially update the `runner:` block. |

A run is asynchronous because it isn't a quick request/response: it drives real Appium sessions against a physical device and can take several minutes, and some risks (for example the custom-keyboard keystroke-collection risk) need a person to unlock the phone and grant permissions mid-run. Poll `GET /runs/{run_id}` for status instead of expecting `POST /runs` to block until finished.

## What's tracked where

`GET /runs`/`GET /runs/{run_id}` come from an in-memory registry scoped to the running server process — restarting the server loses that history. The `reports/{run_timestamp}/...` endpoints read straight off disk instead, so they see every run that ever wrote a `reports/<run_timestamp>/` folder, from the CLI or the API, past or present, regardless of whether the server was restarted since.

