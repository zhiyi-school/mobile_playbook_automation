# Documentation

The backend runs mobile security playbook checks against iOS and Android
devices and artifacts, writes reports and evidence to disk, serves them over
HTTP, and optionally publishes completed results to a Supabase dashboard.

## Start here

| Document | Purpose |
| --- | --- |
| [setup.md](setup.md) | From a clean checkout to a working run and API |
| [configuration.md](configuration.md) | Every YAML setting, environment variable and the secret boundary |
| [architecture.md](architecture.md) | How a run executes end to end, and what each layer owns |
| [api.md](api.md) | Every HTTP endpoint, run lifecycle, SARIF, sync status |
| [operations.md](operations.md) | The dashboard sync worker: triggers, locking, retries, health |
| [troubleshooting.md](troubleshooting.md) | Symptom-to-cause tables |
| [testing.md](testing.md) | Running and extending the test suite |
| [backend-integration.md](backend-integration.md) | Using this backend from another project |

## Platform specifics

iOS and Android differ in tooling, device connection and risks, so each has its
own pages:

- **iOS** ([ios/](ios/README.md)) — [Configuration](ios/configuration.md),
  [Risk Catalog](ios/risks.md),
  [Manual LocalKeyboard Server](ios/manual-local-keyboard-server.md),
  [Reports And Troubleshooting](ios/reports-and-troubleshooting.md)
- **Android** ([android/](android/README.md)) —
  [Configuration](android/configuration.md),
  [Risk Catalog](android/risks.md),
  [Reports And Troubleshooting](android/reports-and-troubleshooting.md)

Both platforms share the same CLI (`python -m mobile_playbook ...`), the same
run orchestration, and the same report layout under `reports/<RUN_TIMESTAMP>/`.

## Two statuses

Automation status and dashboard sync status answer different questions and can
disagree at any moment. A run is routinely `completed` while its sync is still
`queued`.

| | Automation status | Dashboard sync status |
| --- | --- | --- |
| Endpoint | `GET /runs/{run_id}` | `GET /runs/{run_id}/sync-status` |
| `completed` means | the device finished every selected risk and the report is on disk | every Supabase write for that report succeeded |

## Running both platforms together

`run` targets one platform and one config. `run-all` takes both config paths and
runs the two platforms concurrently in one process:

```bash
python -m mobile_playbook run-all \
  --ios-config configs/ios.yaml \
  --android-config configs/android.yaml \
  --out reports
```

It is additive on top of `run` — nothing about single-platform `run` changes.
Each platform reserves its own `<RUN_TIMESTAMP>` atomically and writes its own
`reports/<RUN_TIMESTAMP>/<PLATFORM>/...` tree, so results are never merged even
when both start in the same second.

## The dashboard

The companion React dashboard lives in a separate repository. It reads this
API for automation state and Supabase for dashboard state. See
[backend-integration.md](backend-integration.md) for the contract it relies on.
