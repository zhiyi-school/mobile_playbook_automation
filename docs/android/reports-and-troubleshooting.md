# Android Reports And Troubleshooting

Each run creates a timestamped report directory:

```text
artifacts/reports/<run_timestamp>/
```

The timestamp format is `YYYY-MM-DD_HH-MM-SS` in the workstation's local timezone. If a timestamp already exists, the next run gets a suffix such as `-2`.

Android per-app risk outputs are written under:

```text
artifacts/reports/<run_timestamp>/android/<app_id>/<risk_id>/<test_case_id>/
```

The top-level `artifacts/reports/<run_timestamp>/summary.md` and `dashboard_results.json` cover the app results in this run. `run-all` creates a separate run directory
for each platform. See [docs/ios/reports-and-troubleshooting.md](../ios/reports-and-troubleshooting.md) for the iOS side. If `device.appium_auto_start` is enabled, that same top-level directory also gets an `appium.log` with every Appium launch attempt for the run, including any restarts after a mid-run crash. That directory also gets an `events.jsonl`, one JSON line per `app_version`/`risk_started`/`risk_completed`/`appium_recovery` event as the run progresses (`app_version` records each app's installed version and build number before its first risk; `app_version_changed` flags a different version found after its last risk) — used by the API's `GET /runs/{run_id}/events` stream (see [docs/api.md](../api.md#watching-a-runs-progress-live)) but readable directly too.

`summary.md`'s Notes column carries a cleaned, single-line message rather than a raw error dump, and links to each test's report folder from a `Report` column — the complete untouched error still lives in that test's `logs.txt`/`report.json`. Android `dashboard_results.json` rows use the same cleaned summary text and include `report_path` so the dashboard can link back to each test report.

## Common Risk Files

`android-feature-01-risk-01`:

- `report.json`
- `logs.txt`
- `apk_analysis.json`, the analysis summary: APK identity and hashes, manifest summary, file counts, findings, and masked secrets
- `package_inventory.json`, every file in the APKs with DEX files, native libraries and ABIs
- `critical_findings.json` and `critical_findings.md`, the ranked flags
- `mobsf_report.json`, MobSF's raw report, when MobSF ran

These are listed in `evidence` with kind `report`.

`android-feature-06-risk-01`:

- `report.json`
- `logs.txt`
- `capture/before_launch.png` and `capture/app_open.png`, frames the screen-capture helper recorded before and after launching the app

`android-feature-01-risk-02`:

- `report.json`
- `logs.txt`
- `recordings/<package>.mp4` when Appium validation recording succeeds
- `screenshots/after_launch.png`, and `screenshots/after_tap.png` when something was tapped
- `run_as_ls.txt` when `run-as` could list the repackaged app's private files
- generated APK work files under `artifacts/work/android/repackaging/`

## Statuses

Each test's `report.json` (and `logs.txt`) carries one of these precise statuses in its `final_status` field:

- `APK_ANALYSIS_COMPLETE`: the APK was analysed; reported as At Risk, since the analysis itself shows the APK's contents are exposed.
- `APK_NOT_FOUND`: no APK in `artifacts/intake/android/apks/` has the app's package name, or `aapt`/`aapt2` is not on `PATH` to read it. Reported as Inconclusive.
- `SCREEN_CAPTURE_ALLOWED`: UI was visible and no secure-window signal was detected.
- `SCREEN_CAPTURE_BLOCKED`: app redirected to a browser or another app, warned, exited, or set `FLAG_SECURE`.
- `SCREEN_CAPTURE_NOT_TESTED`: app redirected to the app store instead of opening, or a system permission prompt covered it, so screen capture was not tested. Reported as Inconclusive. For a store redirect, reinstall the app from that store and run the test again; for a permission prompt, set `runner.auto_grant_permissions: true` (or grant the permission on the device) and run it again.
- `REPACKAGING_SURVIVED`: repackaged app installed and passed basic launch validation.
- `REPACKAGING_BLOCKED`: repackaged app installed but failed validation.
- `REPACKAGING_FAILED`: backup, decode, patch, rebuild, sign, or install failed.
- `FAILED`: unexpected Android automation failure.

`summary.md`'s `Status` column doesn't show these directly — it shows a 3-way security verdict (`AndroidRiskRunResult.verdict`) that each risk sets itself, alongside `final_status`, at the point it decides the outcome: **At Risk** (the risk was demonstrated, e.g. `SCREEN_CAPTURE_ALLOWED`/`REPACKAGING_SURVIVED`), **Reduced Risk** (the app mitigated it, e.g. `SCREEN_CAPTURE_BLOCKED`/`REPACKAGING_BLOCKED`), or **Inconclusive** — the field's default, and what `REPACKAGING_FAILED`/`FAILED` leave it as. The precise underlying status is always still in that test's `report.json`.

## Which build was tested

`android-feature-01-risk-01` analyses the intake APK rather than the installed build, so its `report.json` records `metadata.apk_sha256`, the SHA-256 of the intake APK, and `metadata.apks` with its name, size and hash. Each Android test that uses the device records `metadata.installed_base_apk_sha256`: the SHA-256 of the installed `base.apk`, hashed on the device before the test changes anything (`null` if the device could not hash it). Two results for the same app can only be compared when this fingerprint matches; a different fingerprint means a different build was tested, even when the version number is the same. The repackaging test also records `metadata.installer`, the app that installed the tested build (`null` for an adb or sideloaded install), and `metadata.restore.matches_original`, whether the build put back afterwards is the one tested.

## Troubleshooting

Android ADB failure:

Run `adb devices`, unlock the device, approve debugging, and set `device.adb_serial` if multiple devices are attached.

If the dashboard says more than one test device is connected, `adb devices` lists several entries and `device.adb_serial` is unset; one phone attached over both USB and Wi-Fi counts as two. Set `device.adb_serial` to the entry to test, for example `10.0.0.5:5555` for the Wi-Fi connection, or `wifi` to follow the phone's Wi-Fi address automatically. The API log names the setting when this happens.

Appium connection failure:

Confirm Appium is running and the UiAutomator2 driver is installed. Set `device.appium_auto_start` (see [Configuration](configuration.md#appium-auto-start)) to have the framework start and monitor Appium itself instead — including recovering automatically if it crashes mid-run. Every launch attempt lands in that run's `appium.log`.

Android repackaging tool failure:

Confirm `apktool`, `apksigner`, and `keytool` are available on `PATH`. If it stops at `merge_split_apks`, the app is installed as split APKs: make sure `java` is on `PATH` and an APKEditor jar is under `tools/vendor/apkeditor/` (or `repackaging.apkeditor_jar` points at one). Repackaging work files are left under `artifacts/work/android/repackaging/` for inspection.

Android app redirects to Play Store after repackaging:

This is treated as repackaging blocked. The app likely detected signature or package tampering and routed to Play Store recovery.

Test fails with `the device is still locked after waking it`:

The runner wakes and unlocks the device over adb before each test, which only works when the device has no PIN, password or pattern. Remove it in the device's security settings, or unlock the device by hand before the run. Set `runner.keep_screen_on: true` so the screen does not time out during a run while the device charges.

Screen capture test fails before launching the app:

The test needs the screen-capture helper. `screen-capture helper APK not found` means it has not been built: run `python tools/companion_apps/android_screen_capture/build.py` (needs the Android SDK and a JDK). `the screen-capture consent prompt did not appear` usually means the device's screen was off or locked when the prompt opened; the runner wakes and unlocks it before each test, so check that the device has no PIN, password or pattern. `screen capture did not start (denied: …)` means the prompt was cancelled instead of allowed. `the entire-screen option was not found` means the prompt asked whether to share one app or the entire screen, and no option containing "entire screen" was found; set the device language to English.

Keystore or signing failure:

Confirm `keytool`/`apksigner` are on `PATH` and that `repackaging.keystore_path` (if set) points to a valid, readable keystore with the configured alias and password.
