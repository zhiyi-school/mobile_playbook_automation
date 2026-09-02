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
FastAPI, uvicorn, python-multipart, ruamel.yaml) and `pytest`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

`.env` holds local secrets and is not committed. Two processes read it very
differently, and the difference is deliberate:

| Key | Read by | Notes |
| --- | --- | --- |
| `MOBSF_API_KEY` | the run, when the iOS static-analysis risk uses the MobSF provider | |
| `SUPABASE_URL` | the dashboard sync worker only | |
| `SUPABASE_SERVICE_ROLE_KEY` | the dashboard sync worker only | bypasses row-level security; never expose it to a browser |
| `DASHBOARD_SYNC_AUTO_TRIGGER` | the API and CLI, to decide whether to launch a post-run worker | |
| `CORS_ALLOWED_ORIGINS` | the API process | one of the four keys the API reads from `.env` |
| `ARTIFACT_STORE_DIR` | the API, worker and backfill | where derived icons and artifact metadata live; empty means `<repo>/derived` |
| `IOS_PLAYBOOK_DIR` | the API process | where the iOS developer remediation playbook lives; empty falls back to `playbook_dir` in `configs/split/ios/risks.yaml` |
| `ANDROID_PLAYBOOK_DIR` | the API process | the same for Android |
| `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED` | the API process | `false` refuses implemented-control archive downloads |

The API process never loads the whole file — it reads one allowlisted key at a
time, so `SUPABASE_SERVICE_ROLE_KEY` never enters it. See
[configuration.md](configuration.md#environment-variables-and-the-secret-boundary)
for precedence rules and the allowlist that enforces this.

> If you add a key by hand, make sure the previous line ends with a newline.
> A key appended to the end of an existing line is silently ignored.

## 4. Configure YAML

Each platform has one entry-point config that carries `device` and `runner`
inline and pulls the app roster and per-risk settings in through `include:`.
Copy from the tracked examples:

```bash
cp configs/ios.example.yaml configs/ios.yaml
cp configs/split/ios/apps.example.yaml configs/split/ios/apps.yaml
for f in ipa_static_analysis traffic_interception keystroke_collection; do
  cp configs/split/ios/risk_settings.example.yaml "configs/split/ios/$f.yaml"
done

cp configs/android.example.yaml configs/android.yaml
cp configs/split/android/apps.example.yaml configs/split/android/apps.yaml
for f in tools repackaging screen_capture; do
  cp configs/split/android/risk_settings.example.yaml "configs/split/android/$f.yaml"
done
```

`risk_settings.example.yaml` shows every risk's settings together for reading;
trim each real copy down to its own top-level key.

A minimal app entry looks like this — replace every value:

```yaml
apps:
  - id: example-app
    name: Example App
    bundle_id: com.example.placeholder
    test_bundle_id: com.example.placeholder.bundle
    artifact:
      source: local_ipa
      ipa: intake/ios/ipas/example-app.ipa
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
dashboard's SQL migrations from the frontend repository. Skip this entirely to
run the backend standalone — reports and evidence still land on disk and the
API still serves them. See [operations.md](operations.md).

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
  --risks ios-feature-01-risk-01 --dry-run --out reports
```

List what is available:

```bash
python -m mobile_playbook list-risks --platform ios
python -m mobile_playbook list-risks --platform android
```

## 8. Run for real

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml \
  --apps example-app --risks ios-feature-01-risk-01 --out reports
```

Both platforms concurrently in one process:

```bash
python -m mobile_playbook run-all --ios-config configs/ios.yaml \
  --android-config configs/android.yaml --out reports
```

Other commands: `acquire` (fetch iOS artifacts only) and `inspect-ipa` (report
an IPA's mutability). `python -m mobile_playbook <command> --help` lists flags.

## 9. Start the API and check health

```bash
python -m mobile_playbook.api --port 8080
```

```bash
curl http://127.0.0.1:8080/health
# {"status": "ok"}
```

Interactive endpoint browser: <http://127.0.0.1:8080/docs>. Flags are `--host`
(default `127.0.0.1`), `--port` (default `8080`) and `--reload`.

The API binds to loopback by default and has **no authentication of its own**.
Read [api.md](api.md#security-model) before binding it to anything else.

## 10. Where output lands

| Path | Contents |
| --- | --- |
| `reports/<RUN_TIMESTAMP>/` | one run: `summary.md`, `dashboard_results.json`, `run_manifest.json`, `results.sarif`, `events.jsonl` |
| `reports/<RUN_TIMESTAMP>/<PLATFORM>/<APP_ID>/<RISK_ID>/<CASE_ID>/` | per-test `report.json`, `logs.txt`, evidence |
| `reports/<RUN_TIMESTAMP>/evidence/` | run-level evidence |
| `work/ios/`, `work/android/` | intermediate artifacts, unpacked bundles, Appium logs |
| `work/dashboard-sync.log` | output of every detached sync worker |
| `intake/ios/ipas/`, `intake/android/apks/` | drop-zone for local artifacts |

`reports/` and `work/` are gitignored. Report contents are real assessment
data — treat them as sensitive.

## 11. Verify the install

```bash
python -m pytest -q
```

See [testing.md](testing.md) for what the suite covers and how to run subsets.

## Next

- [configuration.md](configuration.md) — every setting and environment variable
- [api.md](api.md) — endpoints, run lifecycle, SARIF, sync status
- [operations.md](operations.md) — sync worker, scheduling, locking, retries
- [troubleshooting.md](troubleshooting.md) — symptom-to-cause table
- [backend-integration.md](backend-integration.md) — using this from another project
