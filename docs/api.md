# HTTP API

`mobile_playbook/api/` is a thin HTTP wrapper around the same functions the CLI (`python -m mobile_playbook ...`) calls — config loading/validation, risk listing, `run_platform()`, and the `reports/<run_timestamp>/` files each run already writes. It exists so a separate dashboard (or `curl`, or the interactive docs) can trigger runs and read results without shelling out to the CLI.

Nothing about the CLI changes because of this — `python -m mobile_playbook ...` still works exactly as before, and both entry points share the same underlying code.

## Running the server

```bash
python -m mobile_playbook.api --port 8000
```

Add `--reload` during development to restart on code changes, and `--host 0.0.0.0` to accept connections from other machines on the network (leave it on the default `127.0.0.1` for local-only use).

Run this from the repository root, the same way you'd run `python -m mobile_playbook`, since config paths and `reports/` are resolved relative to the process's working directory.

## Exploring it without a dashboard

FastAPI serves interactive, browsable docs at **http://127.0.0.1:8000/docs** — every endpoint below can be called from there with a form, no client code required. `curl` also works, for example:

```bash
curl http://127.0.0.1:8000/reports
curl http://127.0.0.1:8000/platforms/ios/risks
curl -X POST http://127.0.0.1:8000/config/validate \
  -H "Content-Type: application/json" \
  -d '{"platform": "ios", "config_path": "configs/ios.yaml"}'
```

## Triggering a run

`POST /runs` takes the same inputs as the CLI's `run` command (`--config`/`--platform`/`--apps`/`--risks`/`--out`) and starts it in a background thread, returning right away with a `run_id`:

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "ios",
    "config_path": "configs/ios.yaml",
    "apps": "sp",
    "risks": "ios-feature1-risk1"
  }'
```

```json
{"run_id": "2026-08-20_09-28-42", "platform": "ios", "status": "running"}
```

`apps`/`risks` are optional comma-separated strings, same as the CLI flags — omit either to run every enabled app/risk in the config. `out_dir` defaults to `reports` if left out.

The `run_id` *is* the run's timestamp and its `reports/<run_id>/` directory name — reserved atomically the moment the request comes in, so it's already known before the run itself finishes, and two requests in the same second never collide (each gets its own `-2`/`-3`/... suffix, same scheme the CLI already uses for same-second collisions). There's no separate ID scheme to translate between.

Poll it for status:

```bash
curl http://127.0.0.1:8000/runs/2026-08-20_09-28-42
```

```json
{"run_id": "2026-08-20_09-28-42", "platform": "ios", "config_path": "configs/ios.yaml", "status": "completed", "run_timestamp": "2026-08-20_09-28-42", "run_dir": "reports/2026-08-20_09-28-42", "error": null, "started_at": "...", "completed_at": "..."}
```

Once `status` is `"completed"`, fetch the results:

```bash
curl http://127.0.0.1:8000/runs/2026-08-20_09-28-42/summary
```

This returns the same `dashboard_results.json` content the run wrote to disk. The identical value also works under `/reports` (useful since that path works for CLI-started runs too, and survives an API server restart):

```bash
curl http://127.0.0.1:8000/reports/2026-08-20_09-28-42/summary
```

A `POST /runs` call still needs everything a CLI `run` needs to actually succeed — Appium running, the device connected/unlocked/trusted, and for risks like keystroke collection, someone available to interact with the phone mid-run. The API doesn't remove those requirements, it just lets you kick the run off and check on it over HTTP instead of watching a terminal.

## Endpoints

| Method & Path | Purpose |
| --- | --- |
| `GET /health` | Liveness check. |
| `GET /platforms/{platform}/risks` | Same data as `list-risks`. `platform` is `ios` or `android`. |
| `POST /config/validate` | Same check as `validate`. Body: `{"platform", "config_path"}`. Returns `422` with the config's error list if invalid. |
| `POST /runs` | Starts a run in a background thread and returns immediately (`202`) with a `run_id` — it does not wait for the run to finish. Body: `{"platform", "config_path", "apps"?, "risks"?, "out_dir"?}`, mirroring `run`'s `--apps`/`--risks`/`--out` flags. |
| `GET /runs` | Lists runs started through this API (this process's history only — see below). |
| `GET /runs/{run_id}` | One run's status: `running`, `completed`, or `failed`, plus its `run_timestamp`/`run_dir` once known. |
| `GET /runs/{run_id}/summary` | The completed run's `dashboard_results.json`, by `run_id`. `409` while still running, `500` with the error if it failed. |
| `GET /reports` | Lists every `reports/<run_timestamp>/` directory on disk, most recent first — including runs started from the CLI, not just from this API. |
| `GET /reports/{run_timestamp}/summary` | The same `dashboard_results.json`, looked up directly by `run_timestamp` instead of `run_id`. Works for any run on disk regardless of how it was started. |
| `GET /reports/{run_timestamp}/files/{file_path}` | Serves any file inside that run's report directory — screenshots, recordings, `report.json`, `logs.txt`, `critical_findings.md`, etc. |

A run is asynchronous because it isn't a quick request/response: it drives real Appium sessions against a physical device and can take several minutes, and some risks (for example the custom-keyboard keystroke-collection risk) need a person to unlock the phone and grant permissions mid-run. Poll `GET /runs/{run_id}` for status instead of expecting `POST /runs` to block until finished.

## What's tracked where

`GET /runs`/`GET /runs/{run_id}` come from an in-memory registry scoped to the running server process — restarting the server loses that history. The `reports/{run_timestamp}/...` endpoints read straight off disk instead, so they see every run that ever wrote a `reports/<run_timestamp>/` folder, from the CLI or the API, past or present, regardless of whether the server was restarted since.

## What this doesn't do yet

This is a read/trigger API only: it runs existing configs and reports on existing results. Editing config files (apps, risk settings, device/runner) or uploading new IPAs/APKs through the API is a separate, not-yet-built layer — today those still need direct edits to the YAML files under `configs/` and files dropped into `intake/`.
