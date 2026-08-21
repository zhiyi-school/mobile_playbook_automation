# Android Reports And Troubleshooting

Each run creates a timestamped report directory:

```text
reports/<run_timestamp>/
```

The timestamp format is `YYYY-MM-DD_HH-MM-SS` in the workstation's local timezone. If a timestamp already exists, the next run gets a suffix such as `-2`.

Android per-app risk outputs are written under:

```text
reports/<run_timestamp>/android/<app_id>/<risk_id>/<test_case_id>/
```

The top-level `reports/<run_timestamp>/summary.md` and `dashboard_results.json` cover all platforms in a single run; see [docs/ios/reports-and-troubleshooting.md](../ios/reports-and-troubleshooting.md) for the iOS side. If `device.appium_auto_start` is enabled, that same top-level directory also gets an `appium.log` with every Appium launch attempt for the run, including any restarts after a mid-run crash. That directory also gets an `events.jsonl`, one JSON line per `risk_started`/`risk_completed`/`appium_recovery` event as the run progresses — used by the API's `GET /runs/{run_id}/events` stream (see [docs/api.md](../api.md#watching-a-runs-progress-live)) but readable directly too.

`summary.md`'s Notes column carries a cleaned, single-line message rather than a raw error dump, and links to each test's report folder from a `Report` column — the complete untouched error still lives in that test's `logs.txt`/`report.json`. This cleanup currently only extends to `dashboard_results.json` for iOS; Android's `dashboard_results.json` records still carry the raw, uncleaned message and no `report_path` field pending a similar pass for `android/results.py`.

## Common Risk Files

`android-feature-06-risk-01`:

- `report.json`
- `logs.txt`
- `recordings/<package>.mp4` when Appium recording succeeds

`android-feature-01-risk-02`:

- `report.json`
- `logs.txt`
- `recordings/<package>.mp4` when Appium validation recording succeeds
- generated APK work files under `work/android/repackaging/`

## Statuses

Each test's `report.json` (and `logs.txt`) carries one of these precise statuses in its `final_status` field:

- `SCREEN_CAPTURE_ALLOWED`: UI was visible and no secure-window signal was detected.
- `SCREEN_CAPTURE_BLOCKED`: app redirected, warned, exited, or set `FLAG_SECURE`.
- `REPACKAGING_SURVIVED`: repackaged app installed and passed basic launch validation.
- `REPACKAGING_BLOCKED`: repackaged app installed but failed validation.
- `REPACKAGING_FAILED`: backup, decode, patch, rebuild, sign, or install failed.
- `FAILED`: unexpected Android automation failure.

`summary.md`'s `Status` column doesn't show these directly — it shows a 3-way security verdict (`AndroidRiskRunResult.verdict`) that each risk sets itself, alongside `final_status`, at the point it decides the outcome: **At Risk** (the risk was demonstrated, e.g. `SCREEN_CAPTURE_ALLOWED`/`REPACKAGING_SURVIVED`), **Reduced Risk** (the app mitigated it, e.g. `SCREEN_CAPTURE_BLOCKED`/`REPACKAGING_BLOCKED`), or **Inconclusive** — the field's default, and what `REPACKAGING_FAILED`/`FAILED` leave it as. The precise underlying status is always still in that test's `report.json`.

## Troubleshooting

Android ADB failure:

Run `adb devices`, unlock the device, approve debugging, and set `device.adb_serial` if multiple devices are attached.

Appium connection failure:

Confirm Appium is running and the UiAutomator2 driver is installed. Set `device.appium_auto_start` (see [Configuration](configuration.md#appium-auto-start)) to have the framework start and monitor Appium itself instead — including recovering automatically if it crashes mid-run. Every launch attempt lands in that run's `appium.log`.

Android repackaging tool failure:

Confirm `apktool`, `apksigner`, and `keytool` are available on `PATH`. Repackaging work files are left under `work/android/repackaging/` for inspection.

Android app redirects to Play Store after repackaging:

This is treated as repackaging blocked. The app likely detected signature or package tampering and routed to Play Store recovery.

Screen recording missing from a report:

Screen recording is best-effort; if the Appium driver cannot start or stop recording (for example, unsupported device or driver version), the risk still runs and reports a verdict, just without a `recordings/<package>.mp4` file.

Keystore or signing failure:

Confirm `keytool`/`apksigner` are on `PATH` and that `repackaging.keystore_path` (if set) points to a valid, readable keystore with the configured alias and password.
