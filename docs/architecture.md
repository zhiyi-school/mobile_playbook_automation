# Architecture And Technology

This document explains how a run is actually executed under the hood, and what libraries/tools each part of the framework depends on. It is platform-agnostic and mechanism-focused; for what each risk tests and how to configure it, see [docs/ios/](ios/README.md) and [docs/android/](android/README.md).

## Technology Stack

| Concern | Technology |
| --- | --- |
| Language / runtime | Python 3.11+ (stdlib-heavy; almost nothing is reimplemented in a framework) |
| CLI | `argparse` (`mobile_playbook/cli.py`) |
| Config format | YAML via `PyYAML` (`yaml.safe_load`) |
| Device automation | `Appium-Python-Client` + `selenium` (Appium is the only automation engine used for both platforms) |
| iOS driver | Appium `XCUITest` driver, real device only |
| Android driver | Appium `UiAutomator2` driver |
| Android bridge | `adb` (Android platform-tools), shelled out to directly |
| Android repackaging tools | `apktool`, `apksigner`, `keytool`, all shelled out to directly |
| iOS binary inspection | `otool` (Xcode command line tools), shelled out to directly |
| iOS static analysis | MobSF REST API (primary) with a built-in Python fallback scanner; optional Docker auto-start |
| Custom-keyboard collection server | Python stdlib `http.server.ThreadingHTTPServer` — no web framework |
| IPA handling | stdlib `zipfile` and `plistlib` — no third-party archive/plist library |
| Hashing | stdlib `hashlib.sha256`, streamed in 1MB chunks |
| Concurrency | stdlib `threading` (used for both-platforms-at-once `run-all`, and for the control server) |
| Testing | `pytest`, with device/Appium/subprocess boundaries mocked |
| Packaging | `setuptools` (`pyproject.toml`) |

Notably, there is no HTTP framework (Flask/FastAPI/etc.), no ORM/database, and no third-party MobSF client library — the two places that need an HTTP server or client (the LocalKeyboard control server and the MobSF integration) are both hand-rolled on top of `http.server` and `urllib.request` respectively, keeping the dependency surface deliberately small.

## Execution Pipeline

A `run`/`run-all` invocation flows through the same stages regardless of platform:

```text
CLI (argparse)
  -> load .env (project root, then the config file's directory)
  -> load YAML config (PyYAML, resolving `include:` splits)
  -> validate config (required fields, known risk IDs, per-risk requirements)
  -> resolve app/risk selection (--apps/--risks CSV filters)
  -> [dry-run? print planned work and stop]
  -> connect device (only if at least one selected, enabled risk requires one)
  -> for each (app, risk) pair:
       -> per-risk preflight check (Android only; checks adb/appium/apktool/etc.)
       -> risk.run(app, config, device_client, report_writer)
       -> risk writes its own report.json/evidence into the report writer's tree
  -> write run summary.md, dashboard_results.json
  -> close device session
```

This is implemented generically once in [`mobile_playbook/orchestration/scan_runner.py`](../mobile_playbook/orchestration/scan_runner.py) via a `PlatformRunner` protocol, and each platform (`IosPlatformRunner`, `AndroidPlatformRunner`) implements `requires_device`, `connect_device`, `close_device`, `iter_enabled_tests`, and `run_test`. The orchestration function itself doesn't know or care which platform it's driving.

### CLI layer

`mobile_playbook/cli.py` builds an `argparse` parser with subcommands `validate`, `list-risks`, `run`, `run-all`, `acquire`, `inspect-ipa`. `--platform` is required with no default (there is no implicit platform). `run-all` is additive: it loads both platform configs and runs `_run()`/`_run_android()` — the same functions `run` uses — concurrently on two `threading.Thread`s, so the single-platform code path is never duplicated or changed. See [docs/README.md](README.md#running-both-platforms-together) for that command.

### Config layer

Config loading lives in `mobile_playbook/orchestration/preflight.py` (`load_yaml_config`, `resolve_config_includes`); `mobile_playbook/core/config_files.py` is just a re-export for a friendlier import path. YAML is parsed with `yaml.safe_load`. A config file may declare an `include:` mapping (section name → file path) to split `device`/`runner`/`apps` into separate files (see `configs/split/`); included values are deep-merged with the entry-point file's inline values always taking precedence over the included file's values when both are set.

App/risk selection (`--apps`, `--risks`) is implemented in `mobile_playbook/core/selection.py` (`mobile_playbook/orchestration/artifact_intake.py` is the re-export shim used by the CLI). Selectors are normalized (lowercased, alphanumeric-only) and matched against an app's `id`, `name`, `package_name`, or `bundle_id`; an unmatched `--apps` value raises with the list of available app IDs rather than silently running nothing.

### Device / Appium layer

Both platforms connect through the same Appium Python client, just with different driver options:

- **iOS** (`mobile_playbook/platforms/ios/device_client.py`, `AppiumDeviceClient`): builds `appium.options.ios.XCUITestOptions` (`platformName=iOS`, `automationName=XCUITest`, `udid`, `xcodeOrgId`/`xcodeSigningId` from `device.team_id`/`device.xcode_signing_id`, `useNewWDA`, `updatedWDABundleId`, etc.) and opens a session with `appium.webdriver.Remote(appium_server_url, options=options)`. App lifecycle calls (`install_app`, `remove_app`, `terminate_app`, `launch_app`, `query_app_state`) go through Appium's `mobile: <command>` scripting API (`driver.execute_script("mobile: installApp", args)` and similar) rather than raw XCUITest calls. Element interaction uses plain Selenium/Appium locators (`AppiumBy.ACCESSIBILITY_ID`), with a coordinate-tap fallback (`mobile: tap`) when a normal `.click()` fails.
- **Android** (`mobile_playbook/platforms/android/device_client.py` + `appium_driver.py`): builds `appium.options.android.UiAutomator2Options` (`platformName=Android`, `automationName=UiAutomator2`, `noReset=True`, optional `appPackage`/`appActivity`) and opens a session the same way.
- **ADB** (`mobile_playbook/platforms/android/adb.py`): a thin `AdbClient` wraps `adb version` and `adb devices` for availability/connection checks; every other `adb` subcommand (`shell pm list packages`, `shell dumpsys package`, `shell appops set`, `shell pm grant`, `shell dumpsys window windows`, `shell pm path`, `pull`, `install -r -g`, `install-multiple -r -g`, `uninstall`) is invoked directly by the caller (permissions, screen-capture, and repackaging risk code) via `adb.run([...])`, which shells out with `subprocess.run`.

### iOS artifact / binary layer

- **IPA unpacking** (`platforms/ios/ipa/unpacker.py`): stdlib `zipfile`, with a zip-slip guard (`safe_extract_zip` rejects absolute paths, `..`, or any entry that would resolve outside the destination directory) and `__MACOSX`/`.DS_Store` filtering. Exactly one `Payload/*.app` is expected.
- **Metadata** (`platforms/ios/ipa/plist_utils.py`): stdlib `plistlib`, reading `Payload/<App>.app/Info.plist` directly out of the zip without extracting the whole archive.
- **Binary mutability/encryption** (`platforms/ios/mutations/mutability.py`): shells out to `otool -l <executable>` and regex-scans the output for `cryptid` in `LC_ENCRYPTION_INFO`/`LC_ENCRYPTION_INFO_64` load commands. A nonzero `cryptid` is reported as `PROTECTED_OR_ENCRYPTED_BINARY`; this is a read-only inspection; the framework does not attempt to decrypt or patch around it.
- **Hashing** (`platforms/ios/mutations/hashing.py`): `hashlib.sha256`, streamed in 1MB chunks, used to fingerprint acquired/inspected IPAs for the report.
- **Signing config** (`platforms/ios/signing.py`): just a config dataclass (`team_id`, `signing_id`, `updated_wda_bundle_id`, device-registration flag) that feeds the Appium XCUITest capabilities above — there is no direct `xcodebuild`/`codesign` invocation in this repo; actual code-signing during install happens inside Appium/WebDriverAgent's own build step.

### iOS custom-keyboard control server

Used by `ios-feature5-risk1` (see `docs/ios/manual-local-keyboard-server.md`). Implemented in `platforms/ios/control_server.py` on pure stdlib `http.server.ThreadingHTTPServer`, run on a background daemon thread — no Flask/FastAPI, no external web dependency. State is an in-memory `ControlServerState`: a bearer token (`secrets.token_urlsafe(24)` by default), a FIFO queue of pending keystrokes, a delivered list, an events log, and a rolling audit log of the last 500 requests.

Endpoints: `GET /health`, `GET /next` (token-gated; the phone-side keyboard extension polls this for the next keystroke to "type"), `GET /events` / `GET /queue` / `GET /snapshot` (introspection), `POST /pair` (keyboard app registers and receives the token), `POST /enqueue` (test harness queues a probe string/keystroke), `POST /events` (token-gated; keyboard extension reports what it captured). The whole protocol is deliberately simple polling over plain HTTP on the LAN/USB-tethered network — there is no push channel or websocket.

### MobSF integration

There is no standalone MobSF client module; the REST client is embedded directly in the risk that uses it, `Feature1Risk1` in `platforms/ios/risks/feature1_risk1.py`, built entirely on stdlib `urllib.request`/`urllib.parse` (no `requests` dependency). It calls, in order: `POST /api/v1/upload` (hand-rolled multipart body), `POST /api/v1/scan`, then `POST /api/v1/report_json`, authenticating with an `Authorization: <api_key>` header. If `analyzer.auto_start.enabled` is true, `_maybe_start_mobsf()` launches the configured command (typically a `docker run ...mobsf...` command, but the command itself is entirely config-driven, not hardcoded) via `subprocess.Popen`, polls the MobSF base URL until it's reachable or a timeout is hit, and can optionally terminate the process afterward. If MobSF is unreachable and `fallback_to_builtin` is true, the risk falls back to its own built-in package-inventory analyzer instead of failing the run. Android's `tools.mobsf_url`/`tools.burp_proxy` settings are currently only used for a preflight TCP-reachability probe (`platforms/android/preflight.py`) — no Android risk calls MobSF or a proxy yet.

### Behavior validation layer (iOS)

`platforms/ios/behavior/generic_checks.py` (`run_expected_behavior_checks`) implements the `expected_behavior` block from an app's config: optionally asserts the app's Appium app-state code is in `{3, 4}` (foreground/running), always captures a screenshot and the page-source XML into the report directory, then asserts every string in `source_contains` appears in the page source and every string in `source_not_contains` does not. It also delegates to a small, intentionally minimal plugin point, `platforms/ios/behavior/app_specific.py` (`run_app_specific_check`), which looks up a named function by `globals()` — currently only a stub (`check_app_one`) exists, as an extension point for real per-app checks rather than a finished feature.

### Reporting / serialization layer

All result objects are `SerializableDataclass` subclasses (`mobile_playbook/reporting/serialization.py`; `mobile_playbook/core/serialization.py` re-exports it), whose `to_dict()` recursively converts `Path → str` and nested dataclasses/lists/dicts into plain JSON-safe structures. The platform-agnostic result schema — `TestResult` and `Evidence` — lives in `mobile_playbook/reporting/status_mapper.py`; iOS and Android each normalize their own richer result objects (`RiskRunResult`, `AndroidRiskRunResult`) into this common shape for the dashboard feed.

`ReportWriter` (`mobile_playbook/reporting/report_writer.py`) owns a single run's directory: it creates `reports/<run_timestamp>/`, an `evidence/` folder, and a `<platform>/` folder; `test_report_dir(app_id, risk_id, case_id)` creates and returns the per-test folder each risk writes `report.json`/`logs.txt`/evidence into; `write_summary()` writes `summary.md` and (via `dashboard_export.write_dashboard_results`) `dashboard_results.json`. `run_timestamp` itself comes from `orchestration/scheduler.py`'s `new_run_timestamp`, a local, second-resolution, sortable timestamp string that appends a numeric suffix if a run folder with that name already exists.

Both outputs are deliberately kept human/dashboard-facing rather than a raw dump of every field: `mobile_playbook/reporting/messages.py`'s `clean_message()` reduces a raw error (which for a failed Appium/Selenium call is a multi-line "Message: ...\nStacktrace:\n..." block) down to its first meaningful line before it goes into `summary.md`'s Notes column or a `TestResult.summary`. The full untouched error text is never lost — it still lives in each per-test `logs.txt` and `report.json`, which `TestResult.report_path` (iOS only, currently) points back to.

### Concurrency

Two independent uses of `threading` exist: the control server runs its HTTP server on a background daemon thread so the test harness can keep driving Appium while it listens for phone-side events, and `run-all` (`mobile_playbook/cli.py`, `_run_all`) runs the iOS and Android `run` flows on two threads so both platforms execute at once in one process. Both are appropriate uses of threads over processes because the actual work is I/O-bound (subprocess calls to `adb`/`apktool`/`otool`/Docker, and network calls to Appium/MobSF) rather than CPU-bound, so the GIL is not a bottleneck.

## Data Model Summary

- **iOS** (`platforms/ios/models.py`): `DeviceConfig`, `RunnerConfig`, `ExpectedBehaviorConfig`, `AppConfig`, `GlobalConfig` for configuration; `ArtifactAcquisitionResult`, `BinaryInspectionResult`, `InstallResult`, `BehaviorResult`, `CleanupResult` for per-stage outcomes, all rolled up into one `RiskRunResult` per (app, risk) run.
- **Android** (`platforms/android/models.py`): `AndroidDeviceConfig`, `AndroidRunnerConfig`, `AndroidAppConfig`, `AndroidGlobalConfig` for configuration; a single `AndroidRiskRunResult` per (app, risk) run (Android has no IPA-equivalent artifact-acquisition stage yet — APKs are pulled live from the device inside the repackaging risk itself, not acquired up front).

## External Processes And Network Calls

Since this is a security-testing tool, it's worth listing every external process and network endpoint it can invoke, for anyone reviewing what a run actually touches:

- `adb` (device bridge, package install/uninstall, shell commands, `dumpsys`)
- `apktool`, `apksigner`, `keytool` (Android repackaging only)
- `otool` (iOS binary inspection, read-only)
- Appium server, over HTTP (`device.appium_server_url`, both platforms)
- MobSF REST API, over HTTP (`analyzer.mobsf_url`, iOS static analysis only), and optionally `docker run ...`/a local MobSF command if `auto_start.enabled`
- An outbound Google Geocode API call (iOS `api_key_reuse_test`, only if enabled, only to check whether an extracted API key is externally reusable)
- The framework's own `http.server` control server, listening locally for the LocalKeyboard test harness

Nothing here is a hidden or undocumented network call — every one of them is gated behind a config flag (`analyzer.provider`, `auto_start.enabled`, `api_key_reuse_test.enabled`) and only runs for the risks that declare they need it.
