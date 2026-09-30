# Setup

This page gets a working backend from a clean checkout: Python environment,
dependencies, configuration, a safe dry run, and the HTTP API. Platform-specific
device preparation lives in [ios/configuration.md](ios/configuration.md) and
[android/configuration.md](android/configuration.md); every setting is described
in [configuration.md](configuration.md).

All examples use placeholder identifiers (`Example App`, `example-app`,
`com.example.placeholder`, `<RUN_ID>`). Substitute your own.

## 1. Python environment

Python 3.11 or newer is required.

```bash
cd /path/to/mobile_playbook_automation
python3 -m venv .venv
source .venv/bin/activate
```

## 2. Install dependencies

```bash
python -m pip install -e .
```

This installs the runtime dependencies (Appium client, Selenium, PyYAML,
FastAPI, uvicorn, python-multipart, ruamel.yaml). To run the tests, install the
`dev` extra instead: `python -m pip install -e ".[dev]"`. Either form also
installs the `mobile-playbook`, `mobile-playbook-api`,
`mobile-playbook-dashboard-sync`, `mobile-playbook-assessment-worker` and
`mobile-playbook-icon-backfill` commands into the environment's `bin/`; each is
equivalent to the matching `python -m` form used throughout these docs.

## 3. Configure `.env`

```bash
cp .env.example .env
```

`.env` holds local secrets and is not committed. The API reads only
allowlisted settings from it; the CLI and database workers load the whole file:

| Key | Used by | Notes |
| --- | --- | --- |
| `MOBSF_API_KEY` | the run, when the iOS static-analysis risk uses the MobSF provider | |
| `SUPABASE_URL` | dashboard sync, assessment, and icon backfill workers | dashboard project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | dashboard sync, assessment, and icon backfill workers | bypasses row-level security; never expose it to a browser |
| `DASHBOARD_SYNC_AUTO_TRIGGER` | the API and CLI, to decide whether to launch a post-run worker | |
| `CORS_ALLOWED_ORIGINS` | API | exact browser origins |
| `ARTIFACTS_DIR` | API, CLI, workers | base directory for runtime data; default `<repo>/artifacts` |
| `INTAKE_DIR` | API, CLI, workers | override for IPA/APK intake |
| `REPORTS_DIR` | API, CLI, workers | override for reports; empty uses `<repo>/artifacts/reports` |
| `WORK_DIR` | API, CLI, workers | override for work files |
| `ARTIFACT_STORE_DIR` | the API, worker and backfill | where derived icons and artifact metadata live; empty means `<repo>/artifacts/derived` |
| `IOS_PLAYBOOK_DIR` | the API process | where the iOS developer remediation playbook lives; empty falls back to `playbook_dir` in `configs/split/ios/risks.yaml` |
| `ANDROID_PLAYBOOK_DIR` | the API process | the same for Android |

The CLI also loads the full `.env` into its process, although it does not use
the service-role key for a run. The API reads one allowlisted key at a time, so
it does not load that key from `.env`. To disable source archive downloads,
export `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED=false` before starting the API; a
value in `.env` is ignored. See
[configuration.md](configuration.md#environment-variables-and-the-secret-boundary)
for precedence rules and the allowlist that enforces this.

> If you add a key by hand, make sure the previous line ends with a newline.
> A key appended to the end of an existing line is silently ignored.

## 4. Configure YAML

Each platform has one entry-point config that carries `device` and `runner`
inline and pulls the app roster and per-risk settings in through `include:`.
Copy from the tracked examples:

```bash
for example in configs/*.example.yaml configs/split/*/*.example.yaml; do
  target="${example%.example.yaml}.yaml"
  [ -e "$target" ] || cp "$example" "$target"
done
```

Every file an entry config includes has its own `*.example.yaml` next to it, so
the copies load as they are. The loop skips any real config you already have.

A minimal app entry looks like this — replace every value:

```yaml
apps:
  - id: example-app
    name: Example App
    bundle_id: com.example.placeholder
    test_bundle_id: com.example.placeholder.bundle
    artifact:
      source: local_ipa
      ipa: artifacts/intake/ios/ipas/example-app.ipa
      expected_bundle_id: com.example.placeholder
    risks:
      ios-feature-01-risk-01:
        enabled: true
```

Include behaviour, section overrides and the anchor-sharing list form are
described in [configuration.md](configuration.md#yaml-includes).

## 5. Configure the dashboard sync worker (optional)

Only needed if you want results published to a Supabase dashboard. Set
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`, and apply the
dashboard's SQL migrations from the frontend repository. To run standalone,
set `DASHBOARD_SYNC_AUTO_TRIGGER=false` in `.env`; the copied example enables
automatic sync, which otherwise starts a worker after each run even without
usable credentials. Reports and evidence still land on disk and the API still
serves them. See [operations.md](operations.md).

## 6. Device and Appium prerequisites

Automation drives a real device. Nothing below is optional for a live run,
though `validate` and `--dry-run` work without any of it.

- **Appium server** running (`appium` in a separate terminal), or
  `device.appium_auto_start.enabled: true` to let a run start it.
- **iOS**: Xcode with command-line tools, the XCUITest driver, a physical
  iPhone that is connected and trusted, and an Apple Team ID for signing.
  The device must have no passcode, Face ID or Touch ID — Appium cannot enter
  one, so a locked secured device needs a person.
- **Android**: Android platform-tools on `PATH` (`adb`), the UiAutomator2
  driver, and a device with USB debugging authorized.
- **Android repackaging risk only**: `apktool`, `apksigner`, `keytool`.
- **MobSF** (optional): only for the iOS static-analysis risk when its
  `analyzer.provider` is `mobsf`. Set `MOBSF_API_KEY`. MobSF defaults to port
  8000, which is why this API defaults to 8080.

## 7. Validate without touching a device

```bash
python -m mobile_playbook validate --platform ios --config configs/ios.yaml
python -m mobile_playbook validate --platform android --config configs/android.yaml
```

Then a dry run, which resolves apps and risks and prints the plan without
starting Appium or a device session:

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml \
  --risks ios-feature-01-risk-01 --dry-run
```

List what is available:

```bash
python -m mobile_playbook list-risks --platform ios
python -m mobile_playbook list-risks --platform android
```

## 8. Run for real

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml \
  --apps example-app --risks ios-feature-01-risk-01
```

Both platforms concurrently in one process:

```bash
python -m mobile_playbook run-all --ios-config configs/ios.yaml \
  --android-config configs/android.yaml
```

Other commands: `acquire` (fetch iOS artifacts only) and `inspect-ipa` (report
an IPA's mutability). `python -m mobile_playbook <command> --help` lists flags.

## 9. Start the API and check health

```bash
python -m mobile_playbook.api --port 8080
```

```bash
curl http://127.0.0.1:8080/health
# The response includes status, code_revision, and started_at.
```

Interactive endpoint browser: <http://127.0.0.1:8080/docs>. Flags are `--host`
(default `127.0.0.1`), `--port` (default `8080`) and `--reload`.

The API binds to loopback by default and has **no authentication of its own**.
Read [api.md](api.md#security-model) before binding it to anything else.

## 10. Where output lands

| Path | Contents |
| --- | --- |
| `artifacts/reports/<RUN_TIMESTAMP>/` | one run: `summary.md`, `dashboard_results.json`, `run_manifest.json`, `results.sarif`, `events.jsonl` |
| `artifacts/reports/<RUN_TIMESTAMP>/<PLATFORM>/<APP_ID>/<RISK_ID>/<CASE_ID>/` | per-test `report.json`, `logs.txt`, evidence |
| `artifacts/reports/<RUN_TIMESTAMP>/evidence/` | run-level evidence |
| `artifacts/work/ios/`, `artifacts/work/android/` | intermediate artifacts, unpacked bundles, Appium logs |
| `artifacts/work/dashboard-sync.log` | output of every detached sync worker |
| `artifacts/intake/ios/ipas/`, `artifacts/intake/android/apks/` | drop-zone for local artifacts |

`artifacts/reports/` and `artifacts/work/` are gitignored. Report contents are real assessment
data — treat them as sensitive.

With the copied example `.env` and no storage overrides, both CLI and API runs use `artifacts/reports/`. For API runs, `REPORTS_DIR` selects one root for creation, lookup, registry
state, history and synchronization; relative values are repository-relative.
CLI `--out` remains custom and working-directory-relative. See the
[report-root contract](api.md#report-root-and-evidence-contract).

## 11. Verify the install

```bash
python scripts/check_requirements.py
python scripts/check_docs.py
python -m ruff check .
python -m compileall -q mobile_playbook/
python -m pytest -q
```

These checks need no `.env`, device, Appium, Supabase project, external
playbook, or network after dependencies are installed. See
[testing.md](testing.md) for focused contracts, dependency ownership, CI, and
the checks that live in the frontend repository. Live runs remain separate.

## Next

- [configuration.md](configuration.md) — every setting and environment variable
- [api.md](api.md) — endpoints, run lifecycle, SARIF, sync status
- [operations.md](operations.md) — sync worker, scheduling, locking, retries
- [troubleshooting.md](troubleshooting.md) — symptom-to-cause table
- [backend-integration.md](backend-integration.md) — using this from another project
