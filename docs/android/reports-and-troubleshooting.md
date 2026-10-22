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

`android-feature-04-risk-01`:

- `report.json`
- `logs.txt`
- `overlay/permission_list.png` and `overlay/permission_toggle.png`, the Display over other apps list and the attacker's permission page
- `overlay/target_screen.png`, the app's screen when the overlay was checked
- `overlay/dumpsys_overlay_window.txt`, the attacker's overlay windows from `adb shell dumpsys window windows`
- `overlay/attacker_captured.png`, the attacker app showing the values it recorded, when the overlay was shown

A screenshot that could not be saved is listed under `metadata.evidence_errors` instead.

`android-feature-05-risk-01`:

- `report.json`
- `logs.txt`
- `accessibility/accessibility_settings.png`, `accessibility/accessibility_service_page.png` and `accessibility/accessibility_consent.png`, Accessibility settings, the attacker's service page and its consent prompt as the service was turned on
- `accessibility/target_before.png` and `accessibility/target_after.png`, the app's screen before and after the attack
- `accessibility/attacker_events.json`, the attacker service's event log: each scan, search, click and match
- `accessibility/accessibility_list_after.png` and `accessibility/accessibility_service_on.png`, the Accessibility list and the service's switch after the attack
- `accessibility/permission_prompt_<n>.png`, each system permission dialog before it was denied

A screenshot that could not be saved is listed under `metadata.evidence_errors` instead.

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

`android-feature-02-risk-02`:

- `report.json`
- `logs.txt`
- `logcat_<package>.txt`, the app's own log lines, with the sensitive values found masked unless `reveal_values` is true
- `runtime_log_findings.json`, each value found with its first line and number of occurrences
- `critical_findings.json` and `critical_findings.md`, the ranked flags
- the unfiltered capture under `artifacts/work/android/runtime_logs/<package>/`, only when `keep_raw` is true

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
- `OVERLAY_ALLOWED`: the attacker's overlay window was shown over the app while it was in front, and the attacker recorded the synthetic values typed into it. Reported as At Risk.
- `OVERLAY_BLOCKED`: the attacker added its overlay window, but it was hidden while the app was in front. Reported as Reduced Risk.
- `OVERLAY_NOT_TESTED`: the app was not in front, the attacker never added its window, or the window or its visibility could not be read from `dumpsys window`. Reported as Inconclusive; `metadata.verdict` says which.
- `A11Y_ACTION_PERFORMED`: the attacker's accessibility service clicked the app's logout control, and the app showed its signed-out screen or, with no `logged_out` locator, opened a new window, with no authentication prompt. Reported as At Risk.
- `A11Y_ACTION_BLOCKED`: the service's click opened a biometric or device-credential prompt and the app stayed signed in, or Appium saw a logout control the service could not find. Reported as Reduced Risk.
- `A11Y_NOT_TESTED`: the app was not in front, the service was not bound or did not act on the app, its click changed nothing, or the screen had no logout control. Reported as Inconclusive; `metadata.verdict` and `metadata.verdict_rule` say which.
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
- `SENSITIVE_DATA_LOGGED`: a canary, secret or personal-data value appeared in the app's runtime log. Reported as At Risk.
- `USB_DEBUGGING_BLOCKED`: the app showed a warning that USB debugging is on and logged nothing sensitive before it. Reported as Reduced Risk.
- `SCRIPTED_STEPS_INCOMPLETE`: a scripted step failed and nothing sensitive was logged, so the flow the steps lead to was not tested. Reported as Inconclusive.
- `NO_SENSITIVE_DATA_LOGGED`: the app logged lines and none held a sensitive value. Reported as Reduced Risk; `metadata.coverage` says whether scripted steps ran or the app was only launched.
- `NO_APP_LOGS`: the app ran but wrote nothing to the log. Reported as Reduced Risk.
- `FAILED`: unexpected Android automation failure.

`summary.md`'s `Status` column doesn't show these directly — it shows a 3-way security verdict (`AndroidRiskRunResult.verdict`) that each risk sets itself, alongside `final_status`, at the point it decides the outcome: **At Risk** (the risk was demonstrated, e.g. `SCREEN_CAPTURE_ALLOWED`/`REPACKAGING_SURVIVED`), **Reduced Risk** (the app mitigated it, e.g. `SCREEN_CAPTURE_BLOCKED`/`REPACKAGING_BLOCKED`), or **Inconclusive** — the field's default, and what `REPACKAGING_FAILED`/`FAILED` leave it as. The precise underlying status is always still in that test's `report.json`.

## Which build was tested

`android-feature-01-risk-01` analyses the intake APK rather than the installed build, so its `report.json` records `metadata.apk_sha256`, the SHA-256 of the intake APK, and `metadata.apks` with its name, size and hash. Each Android test that uses the device records `metadata.installed_base_apk_sha256`: the SHA-256 of the installed `base.apk`, hashed on the device before the test changes anything (`null` if the device could not hash it). Two results for the same app can only be compared when this fingerprint matches; a different fingerprint means a different build was tested, even when the version number is the same. The repackaging test also records `metadata.installer`, the app that installed the tested build (`null` for an adb or sideloaded install), and `metadata.restore.matches_original`, whether the build put back afterwards is the one tested.

## Troubleshooting

Android ADB failure:

Run `adb devices`, unlock the device, approve debugging, and set `device.adb_serial` if multiple devices are attached.

If the dashboard says more than one test device is connected, `adb devices` lists several entries and `device.adb_serial` is unset; one phone attached over both USB and Wi-Fi counts as two. Set `device.adb_serial` to the entry to test, for example `10.0.0.5:5555` for the Wi-Fi connection, or `wifi` to follow the phone's Wi-Fi address automatically. The API log names the setting when this happens.

If a run fails or reports `cleanup_incomplete` after the phone's USB cable or Wi-Fi dropped, check the run log for "has no backup adb connection" and `events.jsonl` for `backup_serials`. Without a backup, the run had only one connection to the phone; keep the phone on a Wi-Fi network this host can reach and leave `device.adb_backup` on (see [Backup adb connection](configuration.md#backup-adb-connection)). Packages listed in `left` are still on the phone; remove them with `adb uninstall <package>` once it is reachable.

Appium connection failure:

Confirm Appium is running and the UiAutomator2 driver is installed. Set `device.appium_auto_start` (see [Configuration](configuration.md#appium-auto-start)) to have the framework start and monitor Appium itself instead — including recovering automatically if it crashes mid-run. Every launch attempt lands in that run's `appium.log`.

Android repackaging tool failure:

Confirm `apktool`, `zipalign`, `apksigner`, and `keytool` are available on `PATH`. If it stops at `merge_split_apks`, the app is installed as split APKs: make sure `java` is on `PATH` and an APKEditor jar is under `tools/vendor/apkeditor/` (or `repackaging.apkeditor_jar` points at one). Repackaging work files are left under `artifacts/work/android/repackaging/` for inspection.

Android app redirects to Play Store after repackaging:

This is treated as repackaging blocked. The app likely detected signature or package tampering and routed to Play Store recovery.

Test fails with `the device is still locked after waking it`:

The runner wakes and unlocks the device over adb before each test, which only works when the device has no PIN, password or pattern. Remove it in the device's security settings, or unlock the device by hand before the run. Set `runner.keep_screen_on: true` so the screen does not time out during a run while the device charges.

Overlay test fails before launching the app:

`overlay attacker APK not found` means `F4_Overlay_Attacker.apk` is not in `artifacts/intake/android/apks/` (or at `overlay.attacker_apk`) and the device does not have its current version. `INSTALL_FAILED_UPDATE_INCOMPATIBLE` means the device has the attacker app signed with another key, for example one rebuilt from its source archive; uninstall `com.mobileplaybook.overlay` and run the test again. `Overlay permission was not allowed` means the Settings switch was tapped but `appops get` never reported `allow`; check `overlay/permission_toggle.png`. `metadata.permission_granted_via: appops_fallback` means no switch was found on that device's Settings page, so the permission was set with `appops` instead.

Overlay test is not tested although the overlay appears:

`The overlay window visibility fields were not recognised` means the device's `dumpsys window` output has none of the fields the test reads; send `overlay/dumpsys_overlay_window.txt` with the report so the fields can be added. `The overlay held focus and the app behind it could not be read` means `dumpsys activity activities` had no resumed-activity line the test recognises.

Accessibility test fails or is not tested:

`accessibility attacker APK not found` means `F5R1_Accessibility_Attacker.apk` is not in `artifacts/intake/android/apks/` (or at `accessibility.attacker_apk`) and the device does not have its current version. `INSTALL_FAILED_UPDATE_INCOMPATIBLE` means the device has the attacker app signed with another key; uninstall `com.mobileplaybook.accessibility` and run the test again. `metadata.service_enable.method` other than `settings_ui` means Settings could not be driven; `ui_failures` says why, and `settings_labels` can supply the device's labels. `Android did not bind it` with the service enabled usually means Appium suppressed accessibility services; check that the Appium UiAutomator2 driver honours `disableSuppressAccessibilityService`. `could not be confirmed off` fails the run because the attacker's service may still be on: check with `adb shell settings get secure enabled_accessibility_services`, remove `com.mobileplaybook.accessibility/.AttackAccessibilityService` from it, and run `adb shell am force-stop com.mobileplaybook.accessibility`. Avoid `uiautomator dump` while the test runs, since it suspends other accessibility services.

Screen capture test fails before launching the app:

The test needs the screen-capture helper. `screen-capture helper APK not found` means it has not been built: run `python tools/companion_apps/android_screen_capture/build.py` (needs the Android SDK and a JDK). `the screen-capture consent prompt did not appear` usually means the device's screen was off or locked when the prompt opened; the runner wakes and unlocks it before each test, so check that the device has no PIN, password or pattern. `screen capture did not start (denied: …)` means the prompt was cancelled instead of allowed. `the entire-screen option was not found` means the prompt asked whether to share one app or the entire screen, and no option containing "entire screen" was found; set the device language to English.

Private data test fails before reading any files:

`The device does not grant root` means neither adbd nor `su` runs commands as uid 0; run the test on a rooted device, or on the rooted VM with Corellium role devices. `does not exist; is <package> installed` means the app has no `/data/data/<package>` directory. `more than the … byte limit (max_archive_mb)` means the app holds more data than `private_data.max_archive_mb` allows; raise it for that app. `Pulling the private data archive failed` usually means the host is out of space or the connection dropped; the copy on the device is removed either way.

USB debugging private data test fails:

`is it installed on the device` means `dumpsys package` reports nothing for the app. `The app is debuggable, but the device refused run-as` means the manifest is debuggable but the device's `run-as` still refuses it, which some vendor builds do; `metadata.run_as.detail` holds the refusal. `Streaming the private data directories with run-as failed` means tar or the adb connection failed partway, or the device returned something other than a tar archive; the partial copy is deleted. The size limit message is the same as for the rooted test.

Runtime log test fails or is inconclusive:

`dumpsys package reports no user ID` means the app is not installed. `logcat exited` means adb could not start the capture; both attempts, with and without the `uid` format, are described. `logcat stopped before the app was closed` usually means the device disconnected during the test. `The app did not start` means no process for the app was seen and it wrote no lines; check that it launches on this device. For `SCRIPTED_STEPS_INCOMPLETE`, `metadata.steps.failure` names the step and why it failed. `no element matched …` means the element was not on screen within `step_timeout_seconds`: the app may have opened on a different screen, for example already signed in, or the ID or text differs on this build. Dump the screen with `adb shell uiautomator dump` to compare (see [Scripted steps](configuration.md#scripted-steps)).

Keystore or signing failure:

Confirm `keytool`/`apksigner` are on `PATH` and that `repackaging.keystore_path` (if set) points to a valid, readable keystore with the configured alias and password.
