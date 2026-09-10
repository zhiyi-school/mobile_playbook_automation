# Architecture And Technology

This document explains how a run is actually executed under the hood, and what libraries/tools each part of the framework depends on. It is platform-agnostic and mechanism-focused; for what each risk tests and how to configure it, see [docs/ios/](ios/README.md) and [docs/android/](android/README.md). For settings see [configuration.md](configuration.md), for endpoints [api.md](api.md), and for running the sync worker [operations.md](operations.md).

## Ownership

Four components, four responsibilities. Nothing crosses these lines.

```text
Backend automation   owns execution, IPA/APK files, extracted artifact
                     metadata, application icons, raw reports, evidence,
                     run status, SARIF
Developer playbook   an external, read-only directory owning the remediation
                     control text, screenshots and reference archives; the
                     backend serves it, never copies or edits it
Sync worker          translates completed reports into Supabase
Supabase             owns users, roles, teams, applications, assessments,
                     findings, finding history, tickets, retests, risk conversations,
                     activity, developer control progress, and small
                     references (checksums, icon refs, control ids) back to
                     backend-owned files — never the files themselves
Frontend             reads backend automation state and Supabase dashboard
                     state; performs no authoritative synchronisation
```

The consequences that matter in practice:

- The **sync worker is the only writer** of dashboard rows from automation
  results. No browser writes them; the frontend starts runs, watches progress,
  and reads what the worker has already published. The worker holds the
  service-role key, which bypasses row-level security, and it is the only
  process that ever does.
- **Everything under `/runs` and `/reports` comes from disk.** The API never
  touches the dashboard database. Raw results and evidence stay available even
  if synchronisation never succeeds.
- **Binaries and images never enter the database.** IPA/APK files stay in
  `artifacts/intake/`, derived icons in the artifact store; Supabase holds a checksum and
  a logical `icons/<ARTIFACT_ID>.png` reference, and the frontend resolves that
  through the backend. See [api.md](api.md#application-icons).
- **A risk, a control and a step are three different things.** A risk is the
  problem an assessment found; its `demonstration` is how security reproduces
  it. A control is the remediation approach, and its steps are what a developer
  implements. The demonstration is never served as remediation instructions, and
  developer progress is recorded against control steps only. See
  [developer-playbook.md](developer-playbook.md).
- **Playbook Markdown, screenshots and archives never enter the database.** The
  backend normalizes them to JSON per request; Supabase holds only a
  `control_id`, a `step_key`, a status and a `playbook_revision`.
- **An icon belongs to a build, not to an app.** A run records the SHA-256 of
  the artifact it executed against, and the sync worker links the icon derived
  from that exact checksum. A build uploaded after the run cannot change what a
  finished assessment displays.
- **Two statuses answer different questions.** Automation status
  (`GET /runs/{run_id}`) says whether the device finished executing; dashboard
  sync status (`GET /runs/{run_id}/sync-status`) says whether the completed
  result reached Supabase. A run is routinely `completed` with its sync still
  `queued`, and the dashboard is eventually consistent as a result.

## Technology Stack

| Concern | Technology |
| --- | --- |
| Language / runtime | Python 3.11+ (stdlib-heavy; almost nothing is reimplemented in a framework) |
| CLI | `argparse` (`mobile_playbook/cli.py`) |
| Config format | YAML via `PyYAML` (`yaml.safe_load`) |
| API | `FastAPI` (`mobile_playbook/api/app.py`) for dashboard/run/config endpoints |
| Dashboard sync worker | stdlib `urllib.request` (`mobile_playbook/dashboard_syncing/supabase.py`) for Supabase REST writes |
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

The dashboard-facing API is a small FastAPI app intended for localhost or a trusted lab network. Durable dashboard database sync is a separate CLI worker with no HTTP surface, so the unauthenticated API server does not hold Supabase service-role credentials. Outside that API layer there is still no ORM/database and no third-party MobSF client library: the LocalKeyboard control server stays on stdlib `http.server`, Supabase sync uses PostgREST through `urllib.request`, and the MobSF integration is hand-rolled on top of `urllib.request`.

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
       -> an exception here is recorded as one FAILED result for that pair, not a run-aborting error
  -> write run summary.md, dashboard_results.json
  -> close device session
```

This is implemented generically once in [`mobile_playbook/orchestration/scan_runner.py`](../mobile_playbook/orchestration/scan_runner.py) via a `PlatformRunner` protocol, and each platform (`IosPlatformRunner`, `AndroidPlatformRunner`) implements `requires_device`, `connect_device`, `close_device`, `iter_enabled_tests`, and `run_test`. The orchestration function itself doesn't know or care which platform it's driving.

### CLI layer

`mobile_playbook/cli.py` builds an `argparse` parser with subcommands `validate`, `list-risks`, `run`, `run-all`, `acquire`, `inspect-ipa`. `--platform` is required with no default (there is no implicit platform). `run-all` is additive: it loads both platform configs and runs `_run()`/`_run_android()` — the same functions `run` uses — concurrently on two `threading.Thread`s, so the single-platform code path is never duplicated or changed. See [docs/README.md](README.md#running-both-platforms-together) for that command.

### Config layer

Config loading lives in `mobile_playbook/orchestration/preflight.py` (`load_yaml_config`, `resolve_config_includes`); `mobile_playbook/core/config_files.py` is just a re-export for a friendlier import path. YAML is parsed with `yaml.safe_load`. A config file may declare an `include:` mapping (section name → file path) to split `device`/`runner`/`apps` into separate files (see `configs/split/`); included values are deep-merged with the entry-point file's inline values always taking precedence over the included file's values when both are set.

App/risk selection (`--apps`, `--risks`) is implemented in `mobile_playbook/orchestration/artifact_intake.py`. Selectors are normalized (lowercased, alphanumeric-only) and matched against an app's `id`, `name`, `package_name`, or `bundle_id`; an unmatched `--apps` value raises with the list of available app IDs, and an unmatched `--risks` value raises with the list of known risk IDs, rather than either silently running nothing.

A risk's actual settings are resolved from two layers, not stored per app: a global, risk-scoped section on `GlobalConfig` (iOS: `ipa_static_analysis`, `keystroke_collection`; Android: `screen_capture`, `repackaging`) holds the shared defaults used by every app that enables that risk, and an app's own `risks.<risk_id>` entry only needs `enabled: true` plus whatever it wants to override for itself. iOS merges the two with `mobile_playbook.orchestration.preflight.merge_dicts` (re-exported from `core/config_files.py`), which recurses into nested dicts so an app can override one nested field (say, `collection.auto_navigation.accessibility_ids`) without repeating everything else; this is exposed through `effective_risk_config()` in `platforms/ios/config.py`, keyed by `RISK_GLOBAL_SETTINGS_FIELD`. Android's settings are flat, so its risk implementations merge inline with a one-level `{**global_config.screen_capture, **app_override}` spread instead.

### Registries auto-discover their plugins, they don't list them

Risks (`mobile_playbook/platforms/ios/risks/registry.py`, `mobile_playbook/platforms/android/risks/registry.py`) and iOS artifact providers (`mobile_playbook/platforms/ios/artifacts/registry.py`) are all built the same way, through the shared `discover_plugins()` helper in `mobile_playbook/core/discovery.py`: it scans a package's folder on disk (`pkgutil.iter_modules`), imports each module independently inside its own `try`/`except`, and registers a class when it's a concrete subclass of the relevant base (`Risk`, `AndroidRisk`, `ArtifactProvider`) with its own `risk_id`/`source` set — which is what naturally excludes shared/abstract helper classes like `Feature04KeyboardRiskBase` without any filename-based exclusion list.

A module that fails to import is skipped with a warning and the rest of the registry is unaffected; a module that simply isn't present on disk is invisible to the scan with no error at all. `known_risks()`/`known_sources()` reflect exactly what's importable at the moment they're called, so `validate`/`list-risks` never depend on every risk or artifact-provider file existing.

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

Used by `ios-feature-04-risk-01` (see `docs/ios/manual-local-keyboard-server.md`). Implemented in `platforms/ios/control_server.py` on pure stdlib `http.server.ThreadingHTTPServer`, run on a background daemon thread — separate from the dashboard FastAPI app and with no external web dependency. State is an in-memory `ControlServerState`: a bearer token (`secrets.token_urlsafe(24)` by default), a FIFO queue of pending keystrokes, a delivered list, an events log, and a rolling audit log of the last 500 requests.

Endpoints: `GET /health`, `GET /next` (token-gated; the phone-side keyboard extension polls this for the next keystroke to "type"), `GET /events` / `GET /queue` / `GET /snapshot` (introspection), `POST /pair` (keyboard app registers and receives the token), `POST /enqueue` (test harness queues a probe string/keystroke), `POST /events` (token-gated; keyboard extension reports what it captured). The whole protocol is deliberately simple polling over plain HTTP on the LAN/USB-tethered network — there is no push channel or websocket.

### MobSF integration

`platforms/ios/risks/mobsf_client.py` contains the small stdlib MobSF REST client used by `Feature01Risk01` in `platforms/ios/risks/feature_01_risk_01.py` (no `requests` dependency). It calls, in order: `POST /api/v1/upload`, `POST /api/v1/scan`, then `POST /api/v1/report_json`, authenticating with an `Authorization: <api_key>` header. If `analyzer.auto_start.enabled` is true, the risk launches the configured command (typically a `docker run ...mobsf...` command, but the command itself is entirely config-driven, not hardcoded) via `subprocess.Popen`, polls the MobSF base URL until it's reachable or a timeout is hit, and can optionally terminate the process afterward. If MobSF is unreachable and `fallback_to_builtin` is true, the risk falls back to its built-in package-inventory analyzer instead of failing the run. Android's `tools.mobsf_url`/`tools.burp_proxy` settings are currently only used for a preflight TCP-reachability probe (`platforms/android/preflight.py`) — no Android risk calls MobSF or a proxy yet.

### Behavior validation layer (iOS)

`platforms/ios/behavior/generic_checks.py` (`run_expected_behavior_checks`) implements the `expected_behavior` block from an app's config: optionally asserts the app's Appium app-state code is in `{3, 4}` (foreground/running), always captures a screenshot and the page-source XML into the report directory, then asserts every string in `source_contains` appears in the page source and every string in `source_not_contains` does not. It also delegates to a small, intentionally minimal plugin point, `platforms/ios/behavior/app_specific.py` (`run_app_specific_check`), which looks up a named function by `globals()` — currently only a stub (`check_app_one`) exists, as an extension point for real per-app checks rather than a finished feature.

### Reporting / serialization layer

API runs use one `REPORTS_DIR` root (default `<repository>/artifacts/reports`) for report
creation, lookup, registry state, history and synchronization, independent of the
server working directory. CLI runs retain their existing custom `--out` behavior.
See [api.md](api.md#report-root-and-evidence-contract) for the full contract.

All result objects are `SerializableDataclass` subclasses (`mobile_playbook/reporting/serialization.py`; `mobile_playbook/core/serialization.py` re-exports it), whose `to_dict()` recursively converts `Path → str` and nested dataclasses/lists/dicts into plain JSON-safe structures. The platform-agnostic result schema — `TestResult` and `Evidence` — lives in `mobile_playbook/reporting/status_mapper.py`; iOS and Android each normalize their own richer result objects (`RiskRunResult`, `AndroidRiskRunResult`) into this common shape for the dashboard feed.

`ReportWriter` (`mobile_playbook/reporting/report_writer.py`) owns a single run's directory: it creates `<output-root>/<run_timestamp>/`, an `evidence/` folder, and a `<platform>/` folder; `test_report_dir(app_id, risk_id, case_id)` creates and returns the per-test folder each risk writes `report.json`/`logs.txt`/evidence into; `write_summary()` writes `summary.md` and (via `dashboard_export.write_dashboard_results`) `dashboard_results.json`. `run_timestamp` itself comes from `orchestration/scheduler.py`'s `new_run_timestamp`, a local, second-resolution, sortable timestamp string that appends a numeric suffix if a run folder with that name already exists.

Both outputs are deliberately kept human/dashboard-facing rather than a raw dump of every field: `mobile_playbook/reporting/messages.py`'s `clean_message()` reduces a raw error (which for a failed Appium/Selenium call is a multi-line "Message: ...\nStacktrace:\n..." block) down to its first meaningful line before it goes into `summary.md`'s Notes column or a `TestResult.summary`. The full untouched error text is never lost — it still lives in each per-test `logs.txt` and `report.json`, which `TestResult.report_path` points back to.

### SARIF export layer

`mobile_playbook/reporting/sarif_writer.py` converts a completed run's
`dashboard_results.json` into `reports/<run_timestamp>/results.sarif`, a SARIF
2.1.0 document. SARIF is the OASIS interchange format security tools use to pass
findings between each other, so this exists purely for interoperability with
tooling outside this project.

The direction of the arrow matters: `dashboard_results.json` stays the canonical
normalized feed, and SARIF is derived from it. Nothing in this codebase reads
SARIF back — not the dashboard sync worker, not the API's report views, not the
frontend. Removing the exporter would change no behaviour other than the export
itself. `scan_runner.run_platform` writes it through the same `_best_effort`
wrapper as the manifest, so a serialization failure can never alter a run's
outcome, and `build_from_run_dir` returns `None` unless the manifest says
`completed`, so an incomplete run is never rendered as a result set.

Rule text comes from the authored risk metadata in
`configs/split/<platform>/risks.yaml` merged over the Risk classes' own
attributes, read here with plain `yaml.safe_load` rather than through
`api/config_editor.py` — the reporting layer must not depend on the API layer.
Fingerprints are a sha256 over `platform | app_id | test_id | test_case_id`, and
results carry no `locations`, because a runtime finding about a device has no
source line to point at. The exporter is strictly SARIF 2.1.0 conformant,
including §3.27.10's rule that only a `fail` result may carry a `level` other
than `none`; because that leaves passing and inconclusive results with nowhere
to express severity, the project's verdict and severity are always preserved in
`result.properties`. All of this is documented in
[api.md](api.md#sarif-export).

### Developer playbook catalogue layer

`mobile_playbook/playbook/` reads the external developer remediation playbook
and turns it into structured JSON. It has four parts: `source.py` resolves the
configured directory and does every safe path join, `markdown.py` tokenizes a
document into typed blocks, `controls.py` turns those blocks into one risk or
control record, and `catalogue.py` indexes them into a cached
platform → risk → control tree with a warning list.

The direction of the arrow matters here too. The playbook directory is the
source of truth, the backend only reads it, and the in-memory catalogue is a
cache keyed on a fingerprint of the source files. Nothing is imported into a
database and there is no second control catalogue — Supabase stores only the
developer's progress against a `control_id` and `step_key` this layer reports.

The package deliberately does not import FastAPI or `api/config_editor.py`; it
reads `risks.yaml` with plain `yaml.safe_load` for the same reason the SARIF
exporter does. `api/services/playbook.py` is the only adapter between it and
HTTP, so the catalogue can be exercised without an app instance.

Identity comes from a document's level-2 heading rather than its filename, so a
renamed or draft-suffixed file is still catalogued as the control it declares
itself to be, with the disagreement reported as a warning. Deriving a control's
owning risk from its own id, rather than from the links in the risk document,
means a wrong link surfaces as a warning instead of silently attaching a control
to the wrong risk. See [developer-playbook.md](developer-playbook.md).

Maintenance entry points live beside the parser: `validator.py` performs
read-only structural/reference checks, `identity_preview.py` compares exact
effective identities across two roots, and `contract_fixture.py` exports the
single sanitized transport fixture consumed by frontend rendering tests. All
three call `catalogue.build`; none reimplements Markdown interpretation.

### API configuration and the secret boundary

The repository `.env` holds both non-secret settings and real credentials, so
the two processes that read it read it very differently.

The **dashboard sync worker** loads the whole file through
`mobile_playbook/env_file.py`'s `load_env_file()`, because it genuinely needs
`SUPABASE_SERVICE_ROLE_KEY` — a key that bypasses row-level security — plus
`SUPABASE_URL`. That is the only process that ever holds it.

The **API** must not. `mobile_playbook/api/settings.py` reads one allowlisted
key at a time instead: `env_setting()` checks `ALLOWED_ENV_KEYS` (currently just
`CORS_ALLOWED_ORIGINS`) and raises `DisallowedSettingError` for anything else,
and its parser returns a single requested key rather than importing the file
into `os.environ`. It deliberately does not reuse `load_env_file()` and does not
import that module at all, so there is no code path from the API package to
whole-file loading — the same trade-off `dashboard_sync_trigger.py` already made
for reading `DASHBOARD_SYNC_AUTO_TRIGGER`. The small duplicated parser is the
price of that guarantee.

Precedence is shell environment → `.env` → application default, so an operator's
explicit export always wins. Resolution is lazy, inside `cors_allowed_origins()`,
rather than a module-import side effect on a launcher: `app.py` evaluates
`allow_origins=cors_allowed_origins()` when it is imported, and under
`uvicorn --reload` the app is imported afresh in a worker subprocess, so
anything that mutated the parent's environment in `__main__.main()` would be
fragile. Resolving on call is correct for every entrypoint and leaves
`os.environ` untouched.

### Dashboard sync layer

Publishing results to the dashboard is a second workflow that runs after — and
independently of — the automation run. `mobile_playbook/dashboard_sync.py` is
the compatibility entry point; ownership lives under `dashboard_syncing/`:
`contracts.py` defines the injected store and result types, `identity.py` owns
pure identities/status mapping, `mapping.py` maps report rows to ordered store
mutations, `orchestrator.py` coordinates manifests, status and the processed
ledger, `supabase.py` is the credential-bearing PostgREST adapter, and
`worker.py` owns argument parsing and the loop. The worker has no HTTP surface
and is the only writer of dashboard rows from automation results. It is started
by `dashboard_sync_trigger.py` after a terminal manifest and by the recovery
sweep.

The two workflows have separate lifecycles and separate meanings of "completed".
A run is `completed` when the device finished executing its risks and the report
is on disk; its sync is `completed` only once every Supabase write for that
report succeeded. A run is routinely `completed` with its sync still `queued` or
`running`, and the dashboard is eventually consistent as a result. Because the
raw report endpoints read straight off disk, a sync that fails leaves the run's
results and evidence fully available.

Three files carry the state, each with one job:

- `reports/.dashboard_sync_ledger.json` — the authority on *whether* a report was
  published, mapping a run timestamp to the sha256 digest of the manifest and
  feed that were synced. A digest mismatch is what makes a re-run of the same
  timestamp resync rather than being skipped.
- `reports/<run_timestamp>/sync_status.json` — the lifecycle *around* that fact
  for one run: status, attempt, timings, a redacted error, and the row counts the
  last attempt reconciled. `mobile_playbook/sync_status.py` writes it atomically
  under a per-run `flock`, and rebuilds it from the manifest and ledger when it is
  missing or unreadable, so it is a projection rather than a competing truth.
- `reports/.dashboard_sync.lock` — the host-wide `flock` that serializes passes.
  The API reads it (without holding it) to answer whether a worker is running.

Idempotency does not depend on any of that state being correct. Applications and
assessments are upserted on `external_id`; `finding_history` and `activity_log`
rows carry a deterministic `sync_key` with a unique index behind it, so a
duplicate insert conflicts and is swallowed; and a finding's status and
`latest_test_run_id` are written last, so a pass that dies partway leaves the
pre-run state for the retry to find. Replaying an older run never regresses a
finding that a newer run already moved.

The API package does not import `dashboard_syncing.supabase` or `worker`; it
only launches the module entry point in a child process. This keeps service-role
credentials outside the API process while mapping and orchestration remain
testable with an injected store.

### API configuration editing

`mobile_playbook/api/config_editor.py` is a compatibility facade. Shared
round-trip YAML, validation, per-file locks and rollback live in
`api/config_editing/shared.py`; `sections.py` owns device, runner and global
risk settings; `android_apps.py` and `ios_apps.py` own their roster formats;
and `metadata.py` owns feature, risk metadata and demonstration edits. The iOS
editor remains text-block based because its roster references anchors from a
separately included templates file.

HTTP response models live in `api/models.py`; report rows allow additional
fields because `dashboard_results.json` is extensible. Filename, media-type and
containment primitives live in `api/downloads.py`, while each service still
decides its allowed root, run binding and HTTP error.

### Concurrency

Two independent uses of `threading` exist: the control server runs its HTTP server on a background daemon thread so the test harness can keep driving Appium while it listens for phone-side events, and `run-all` (`mobile_playbook/cli.py`, `_run_all`) runs the iOS and Android `run` flows on two threads so both platforms execute at once in one process. Both are appropriate uses of threads over processes because the actual work is I/O-bound (subprocess calls to `adb`/`apktool`/`otool`/Docker, and network calls to Appium/MobSF) rather than CPU-bound, so the GIL is not a bottleneck.

## Data Model Summary

- **iOS** (`platforms/ios/models.py`): `DeviceConfig`, `RunnerConfig`, `ExpectedBehaviorConfig`, `AppConfig`, `GlobalConfig` for configuration; `ArtifactAcquisitionResult`, `BinaryInspectionResult`, `InstallResult`, `BehaviorResult`, `CleanupResult` for per-stage outcomes, all rolled up into one `RiskRunResult` per (app, risk) run.
- **Android** (`platforms/android/models.py`): `AndroidDeviceConfig`, `AndroidRunnerConfig`, `AndroidAppConfig`, `AndroidGlobalConfig` for configuration; a single `AndroidRiskRunResult` per (app, risk) run (Android has no IPA-equivalent artifact-acquisition stage yet — APKs are pulled live from the device inside the repackaging risk itself, not acquired up front).

## Maintenance and extension points

Keep dependencies directed toward domain owners rather than CLI or HTTP
facades:

Human-facing prose uses the spelling “artifact”. Established compatibility
identifiers retain `artifact`, including API and YAML fields, routes, database
columns, status values, environment variables, module paths, manifest keys and
the SARIF `artifactLocation` property. Changing those identifiers requires a
versioned, coordinated migration rather than a spelling-only edit.

| Change | Owner and required verification |
| --- | --- |
| API request/response contract | `mobile_playbook/api/models.py`, route and service modules; extend `tests/test_api_*.py` and the focused contract command in [testing.md](testing.md) |
| Report/evidence behavior | `mobile_playbook/api/services/reports.py`, `mobile_playbook/reporting/`; preserve the configured report-root and opaque-ref rules in [api.md](api.md#report-root-and-evidence-contract) |
| Dashboard synchronization | `mobile_playbook/dashboard_syncing/`; keep `mobile_playbook/dashboard_sync.py` as the supported `python -m` entry point |
| Configuration editing | `mobile_playbook/api/config_editing/`; keep `api/config_editor.py` as the public compatibility facade |
| Risk implementation | `mobile_playbook/platforms/<platform>/risks/`, discovered dynamically; follow the platform guide linked from [testing.md](testing.md#adding-a-risk) |
| Playbook parsing/rendering contract | `mobile_playbook/playbook/`, sanitized `tests/fixtures/playbook_contract/`, and the versioned frontend transport fixture described in [developer-playbook.md](developer-playbook.md#parser-to-frontend-contract-fixture) |
| Regression fixture | Prefer a local test factory or `tmp_path`; add shared files under `tests/fixtures/` only when several tests model the same stable contract |

The supported executable entry points remain `python -m mobile_playbook`,
`python -m mobile_playbook.api`, `python -m mobile_playbook.dashboard_sync`,
`python -m mobile_playbook.assessment_worker`, and the read-only playbook
validator/identity/contract modules. Only the sync and assessment workers load
service-role credentials; API credential loading stays allowlisted as described
above.

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
