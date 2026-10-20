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

`android-feature-02-risk-01`:

- `report.json`
- `logs.txt`
- `private_data_listing.txt`, the run-as `ls -laR` of the private data directories, and `private_data_findings.json`, as for `android-feature-07-risk-01`, when run-as could read them
- `critical_findings.json` and `critical_findings.md`, the ranked flags
- the streamed archive under `artifacts/work/android/private_data/<package>/`, only when `keep_archive` is true

`android-feature-07-risk-01`:

- `report.json`
- `logs.txt`
- `private_data_listing.txt`, the root `ls -laR` of the private data directories
- `private_data_findings.json`, each file read with its category, the masked findings, and the masked findings in known third-party SDK files, which do not decide the verdict
- `critical_findings.json` and `critical_findings.md`, the ranked flags
- the pulled archive under `artifacts/work/android/private_data/<package>/`, only when `keep_archive` is true

## Statuses

Each test's `report.json` (and `logs.txt`) carries one of these precise statuses in its `final_status` field:

- `APK_ANALYSIS_COMPLETE`: the APK was analysed; reported as At Risk, since the analysis itself shows the APK's contents are exposed.
- `APK_NOT_FOUND`: no APK in `artifacts/intake/android/apks/` has the app's package name, or `aapt`/`aapt2` is not on `PATH` to read it. Reported as Inconclusive.
- `SCREEN_CAPTURE_ALLOWED`: the recording captured content while the target app remained in the foreground; UI warnings and `FLAG_SECURE` do not override the recording.
- `SCREEN_CAPTURE_BLOCKED`: the target app remained in the foreground and its recorded frame was blank, with a working pre-launch recording.
- `SCREEN_CAPTURE_NOT_TESTED`: the app exited, redirected to another app, or a system permission prompt covered it. Reported as Inconclusive because these observations do not prove capture protection. Check that the app works on this device and inspect the captured frame. For a store redirect, check its installation requirements; for a permission prompt, set `runner.auto_grant_permissions: true` (or grant the permission on the device) and run it again.
- `REPACKAGING_SURVIVED`: repackaged app installed and passed basic launch validation.
- `REPACKAGING_BLOCKED`: repackaged app installed but failed validation.
- `REPACKAGING_FAILED`: backup, decode, patch, rebuild, sign, or install failed, or the launch validation could not reach a verdict (for example a system permission prompt that kept returning); `errors` says which.
- `PRIVATE_DATA_EXPOSED`: a secret or personal-data value was readable in plain text in the app's private data directory, outside the files known third-party SDKs write.
- `PRIVATE_DATA_PROTECTED`: nothing sensitive was readable in plain text and the app keeps encrypted stores there. Reported as Reduced Risk.
- `PRIVATE_DATA_NO_FINDINGS`: nothing sensitive was found in plain text, but no encrypted store was found either. Reported as Inconclusive.
- `PRIVATE_DATA_NOT_ACCESSIBLE`: run-as refused to read the app's data because it is not debuggable, and `adb backup` would not return its data either. Reported as Reduced Risk.
- `PRIVATE_DATA_BACKUP_POSSIBLE`: run-as was refused, but the app allows backups and `adb backup` would return its data; the backup was not tested. Reported as Inconclusive.
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

Confirm `apktool`, `zipalign`, `apksigner`, and `keytool` are available on `PATH`. If it stops at `merge_split_apks`, the app is installed as split APKs: make sure `java` is on `PATH` and an APKEditor jar is under `tools/vendor/apkeditor/` (or `repackaging.apkeditor_jar` points at one). Repackaging work files are left under `artifacts/work/android/repackaging/` for inspection.

Android app redirects to Play Store after repackaging:

This is treated as repackaging blocked. The app likely detected signature or package tampering and routed to Play Store recovery.

Test fails with `the device is still locked after waking it`:

The runner wakes and unlocks the device over adb before each test, which only works when the device has no PIN, password or pattern. Remove it in the device's security settings, or unlock the device by hand before the run. Set `runner.keep_screen_on: true` so the screen does not time out during a run while the device charges.

Screen capture test fails before launching the app:

The test needs the screen-capture helper. `screen-capture helper APK not found` means it has not been built: run `python tools/companion_apps/android_screen_capture/build.py` (needs the Android SDK and a JDK). `the screen-capture consent prompt did not appear` usually means the device's screen was off or locked when the prompt opened; the runner wakes and unlocks it before each test, so check that the device has no PIN, password or pattern. `screen capture did not start (denied: …)` means the prompt was cancelled instead of allowed. `the entire-screen option was not found` means the prompt asked whether to share one app or the entire screen, and no option containing "entire screen" was found; set the device language to English.

Private data test fails before reading any files:

`The device does not grant root` means neither adbd nor `su` runs commands as uid 0; run the test on a rooted device, or on the rooted VM with Corellium role devices. `does not exist; is <package> installed` means the app has no `/data/data/<package>` directory. `more than the … byte limit (max_archive_mb)` means the app holds more data than `private_data.max_archive_mb` allows; raise it for that app. `Pulling the private data archive failed` usually means the host is out of space or the connection dropped; the copy on the device is removed either way.

USB debugging private data test fails:

`is it installed on the device` means `dumpsys package` reports nothing for the app. `The app is debuggable, but the device refused run-as` means the manifest is debuggable but the device's `run-as` still refuses it, which some vendor builds do; `metadata.run_as.detail` holds the refusal. `Streaming the private data directories with run-as failed` means tar or the adb connection failed partway, or the device returned something other than a tar archive; the partial copy is deleted. The size limit message is the same as for the rooted test.

Keystore or signing failure:

Confirm `keytool`/`apksigner` are on `PATH` and that `repackaging.keystore_path` (if set) points to a valid, readable keystore with the configured alias and password.
