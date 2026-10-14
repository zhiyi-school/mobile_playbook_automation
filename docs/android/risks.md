# Android Risks

## android-feature-01-risk-01

`android-feature-01-risk-01` demonstrates that the app's APK can be obtained and analysed statically, following the playbook: the APK is uploaded to MobSF, and the report shows what anyone with the APK can learn from its manifest, code and bundled files. As on iOS, completing the analysis is itself the finding, so a run that analyses the APK is **At Risk**; the critical findings rank what the analysis exposed.

Requires: the app's installable APK in `artifacts/intake/android/apks/`, found by its package name (see [APK requirements](README.md#apk-requirements)), and `aapt` or `aapt2` on `PATH` to read that package name. It needs no device, so a run of only this risk connects to none; pulling the installed APKs from the phone instead is planned. MobSF is needed only when `analyzer.provider` is `mobsf`.

Stages:

1. Take the intake APK whose package (read with `aapt dump badging`) is the app's, the highest `versionCode` when there are several, and record its name, size and SHA-256 under `metadata.apks` and `metadata.apk_sha256`; `metadata.base_apk` is its path. With no such APK the run stops as `APK_NOT_FOUND`.
2. With `analyzer.provider: mobsf`, upload the APK to MobSF, scan it and fetch the JSON report (the same client as iOS: `POST /api/v1/upload`, `/api/v1/scan`, `/api/v1/report_json`), then normalize it: package identity and SDK levels, permissions and which are dangerous, exported component counts, the file list, and every titled finding MobSF rated above info.
3. Otherwise, or when MobSF fails and `fallback_to_builtin` is true, run the built-in scanner over the APK: parse its `AndroidManifest.xml` from `aapt dump xmltree` (falling back to `aapt2`), list every file in it, and scan for secrets. Text files outside compiled `res/` XML (for example `assets/*.json`) are checked for key-value secrets and known secret formats; `classes*.dex` and `resources.arsc`, where Firebase and Google API keys are compiled, are checked for the known formats only. Values are masked unless `sensitive_scan.reveal_values` is true.
4. When `api_key_reuse_test.enabled` is true, test up to `max_keys` Google API keys found against the Geocoding API from the workstation, as iOS does.
5. Write the reports and record the verdict.

Critical findings (`critical_findings.json`, ranked HIGH to LOW):

- `APK_PACKAGE_ANALYZABLE` (HIGH, always).
- `SENSITIVE_INFORMATION_EXPOSURE`, with the highest severity of the secrets found, and `GOOGLE_API_KEY_REUSE_TEST` when keys were tested.
- `MOBSF_STATIC_ANALYSIS_FINDINGS`: MobSF's high and medium findings (MobSF only).
- From the manifest: `DEBUGGABLE_BUILD` (HIGH), `DANGEROUS_PERMISSIONS_REQUESTED` (MEDIUM), `EXPORTED_COMPONENTS` (MEDIUM: components other apps can start without a permission, leaving out the launcher activity), `CLEARTEXT_TRAFFIC_ALLOWED` (MEDIUM: `usesCleartextTraffic="true"`, or unset with `targetSdkVersion` below 28) and `APP_DATA_BACKUP_ALLOWED` (LOW: `allowBackup` true or unset). With MobSF, only permissions and exported components come from the manifest summary; MobSF's own manifest findings cover the rest.
- `NATIVE_LIBRARIES_PRESENT` (LOW), `CANDIDATE_CONFIG_RESOURCES` (MEDIUM, built-in only), and `MOBSF_FALLBACK_USED` (LOW) when the built-in scanner stood in for MobSF.

Statuses: `APK_ANALYSIS_COMPLETE` (At Risk), `APK_NOT_FOUND` (Inconclusive, nothing analysed), and `FAILED` (Inconclusive), for example when MobSF fails and `fallback_to_builtin` is false.

Settings (`analyzer`, `sensitive_scan`, `api_key_reuse_test`) are documented in [Configuration](configuration.md#apk-static-analysis-android-feature-01-risk-01).

## android-feature-06-risk-01

`android-feature-06-risk-01` demonstrates whether another app on the device can record the target app's screen, following the playbook: a helper app records the screen through Android's screen-recording API (`MediaProjection`) after the user approves the system prompt, the target app is opened while it records, and the recording shows whether the app's content was captured or came out blank, as a `FLAG_SECURE` window does.

Requires: `adb`, `appium`, `screen_capture_helper` (the helper APK built with `python tools/companion_apps/android_screen_capture/build.py`, or the current helper already installed on the device; see [the helper's README](../../tools/companion_apps/android_screen_capture/README.md)). The consent prompt has to be tapped, so the device must not have a PIN, password or pattern; the runner wakes and unlocks it before each test (see [Configuration](configuration.md#runner)).

Stages:

1. Install the helper (`com.mobileplaybook.screencapture`) unless the device already has its current version, go to the home screen, and open the helper, which shows the system's screen-recording consent prompt. Newer Android versions first ask whether to share one app or the entire screen and may default to one app, which would record only the helper; Appium then opens that choice (`com.android.systemui:id/screen_share_mode_options`) and picks the option containing "entire screen", so the device language must be English. Appium taps the allow button (`android:id/button1`: "Start now" on Android 14, "ALLOW" on Huawei's Android 12, "Share screen" on Android 17 once the entire screen is chosen), and the helper starts recording as a foreground service.
2. Wait `record_lead_in` seconds, then save a frame of the home screen (`before_launch`). It shows ordinary content when the recording works.
3. Activate (launch) the target app, wait `launch_wait` seconds, and save a frame (`app_open`).
4. Evaluate the app's response, checking in order:
   - Did the foreground app switch away from the target package? If so, classify as redirected to a known browser, exited to the home-screen launcher (Pixel, Samsung or Huawei), or switched to an unknown app. A system permission prompt (`com.android.permissioncontroller`) covering the app is inconclusive: it is Android asking for a permission, not the app protecting itself, so screen capture was not tested. Set `runner.auto_grant_permissions: true` to grant the app's permissions before the test so no prompt appears. A switch to a store app (the Play Store, `com.android.vending`, or Aurora Store, `com.aurora.store`) is inconclusive instead: the app refused to open, typically because it was not installed from that store, so its screen was never shown and screen capture was not tested.
   - Does the visible UI contain developer/USB-debugging keywords (for example "usb debugging")?
   - Does the visible UI contain screen-recording/sharing warning keywords? The recording is running at this point, so an app that detects it and warns is caught here.
   - Is the `app_open` frame blank? The helper measures the middle 80% of each frame, leaving out the status and navigation bars, which stay visible over a protected window. A frame counts as blank when at least 98% of it is black and it has at most 16 distinct colours: a protected window records as one flat black, while even a dark-themed screen has text and icons. A blank frame is `BLOCKED`; a frame with content is `ALLOWED`. If the `before_launch` frame was already blank, the recording was not working, and the result is an `ERROR` instead.
5. Copy both frames into the report as `capture/before_launch.png` and `capture/app_open.png` (`screen_capture_frame` evidence), record each frame's `dark_ratio`, `distinct_colors` and `blank` under `metadata.capture`, stop the recording, close the app, and write the report.

Alongside the verdict, `metadata.flag_secure` records whether any of the target app's own windows in `adb shell dumpsys window windows` has `FLAG_SECURE` set (`null` if that could not be read; absent when the app had left the screen). Android lists window flags by name without the `FLAG_` prefix (`fl=LAYOUT_IN_SCREEN SECURE …`), or as a hex value on some builds, so the check reads the `fl=` line of each window whose `package=` is the target app. It explains a blank recording but does not decide the verdict: the recording does, because content can also be protected in other ways, such as a secure video view inside an ordinary window.

Verdict-to-status mapping:

- Any `ALLOWED` verdict (the recording captured the app's screen) → `SCREEN_CAPTURE_ALLOWED`.
- Any `BLOCKED` verdict (browser/launcher redirect, debugging/capture warning text, or a blank recording of the app) → `SCREEN_CAPTURE_BLOCKED`.
- Any `INCONCLUSIVE` verdict (the app redirected to the app store instead of opening, or a system permission prompt covered it) → `SCREEN_CAPTURE_NOT_TESTED`, with the Inconclusive security verdict.
- Any `ERROR` verdict (driver, launch, or ADB failure, or a recording that was already blank before launch) → `FAILED`. A helper that cannot be installed, is refused consent, or does not answer also ends the run as `FAILED`, with the reason in `errors`.

Settings (`record_lead_in`, `launch_wait`, `consent_timeout`, `helper_apk`) are documented in [Configuration](configuration.md).

## android-feature-01-risk-02

`android-feature-01-risk-02` demonstrates whether a repackaged (decompiled, patched, and re-signed) build of the app can still install and launch, which indicates the app lacks effective tamper/signature detection.

Requires: `adb`, `apktool`, `apksigner`, `keytool`, `appium`. Apps installed as split APKs also need [APKEditor](https://github.com/REAndroid/APKEditor) and `java`; preflight does not check these because apps without splits don't use them.

Stages (each must succeed before the next runs; the first failure stops the pipeline):

1. **Backup APKs** — `adb shell pm path <package>` to enumerate installed APK paths (handles split APKs), then `adb pull` each (`platforms/android/device/apks.py`) into `artifacts/work/android/repackaging/<package>/original/`.
2. **Ensure keystore** — generate a local signing keystore with `keytool -genkeypair` if one does not already exist at `keystore_path`.
3. **Merge split APKs** — when the backup holds split APKs, `java -jar APKEditor.jar m` merges `base.apk` and the splits into `merged.apk`, so resources that live in a split are not decoded as `@null`. Apps without splits skip this. If the app has splits and APKEditor or `java` is missing, the stage fails with a message saying so.
4. **apktool decode** — `apktool d` `merged.apk` (or `base.apk` when there are no splits) into `repackaged/base/`.
5. **Patch manifest** — add `android:debuggable="true"` to the `<application>` element if not already present.
6. **Change app name** — prefix the `app_name` string resource with `RPK ` so the repackaged build is visually distinguishable (best-effort; does not fail the pipeline if the string is missing).
7. **Rebuild APK** — `apktool b` the patched sources into `repackaged.apk`.
8. **Sign APKs** — `apksigner sign` the rebuilt APK with the local keystore.
9. **Install repackaged APKs** — uninstall the existing app, then `adb install` the signed, repackaged APK.

If any stage fails, the run stops there and reports `REPACKAGING_FAILED` with the Inconclusive verdict. `errors` names the failing stage and, where the tool gave one, its reason, for example `Failed at stage: install_repackaged_apks - Failure [INSTALL_FAILED_...]`.

If all stages succeed, an Appium-driven validation launches the repackaged app and checks:

- Does it redirect to a store app (the Play Store or Aurora Store), such as the Play Store's "get this app from Play" prompt (a common tamper-detection response)?
- Does it exit immediately after launch?
- After tapping the first clickable element, does either of the above happen?

A screenshot is saved after launch (`screenshots/after_launch.png`) and after the tap (`screenshots/after_tap.png`) and added to `evidence`. An app that detects the tampering but shows its warning inside its own screen, rather than exiting, stays in the foreground and is reported as `PASS`; the screenshots are where that warning can be seen.

`PASS` → `REPACKAGING_SURVIVED` (the repackaged build kept working — the app did not detect the tampering). Any `FAIL` → `REPACKAGING_BLOCKED` (the app detected the repackaging and reacted, for example by redirecting to the app store or exiting).

After the validation, while the repackaged build is still installed, `adb shell run-as <package> ls -R` lists its private files, as the playbook's last step does to confirm the app is now debuggable. `metadata.debuggable` records `run_as: true` with `files_listed` and saves the listing as `run_as_ls.txt` (added to `evidence`), or `run_as: false` with the device's refusal in `detail`. It is recorded alongside the verdict and does not change it.

When `restore_original_after_test` is true (the default) and the install stage ran, the rebuilt app is uninstalled after the test and the APKs backed up at the start are reinstalled with `adb install-multiple`, so later tests run against the same build this test tested. The app that originally installed it (`metadata.installer`, for example `com.android.vending`) is recorded as its installer again with `-i`; if the device refuses that, the backup is reinstalled without an installer and `metadata.restore.note` says so. `metadata.restore` records `method: backup`. Only if the backup cannot be reinstalled is the app reinstalled with the install method that `device.app_store` selects, recorded as `method: app_store` with `backup_failure` and a note that it may be a different build, since a store installs its latest version. After any restore, `metadata.restore.base_apk_sha256` is the restored build's fingerprint and `matches_original` says whether it is the build that was tested (`null` when either fingerprint could not be read). The restore also runs when validation raises an error; a restore that fails adds a `Restore failed: …` entry to `errors`. If the pipeline stopped before the install stage, the original was never replaced and nothing is reinstalled.

Generated work files (backed-up, decoded, and rebuilt APKs) are left under `artifacts/work/android/repackaging/` for inspection.

## Risk Metadata

**`configs/split/android/risks.yaml` is the source of truth for everything a dashboard displays about a risk** — its `name`, `description`, `tactic` and `demonstration`. The `AndroidRisk` class holds none of that text: it carries only `risk_id`, `feature_id`, the test-case identifiers and the flags that change what a run does (`is_blocking`, `requires`, `requires_device`). `list_risks()` returns the class's empty defaults and `GET /platforms/{platform}/risks` overlays the YAML entry on top. See [iOS risk metadata](../ios/risks.md#risk-metadata) for the reasoning — the same arrangement applies here.

There is no Android playbook yet, so the current entries carry over what the risk classes used to say; once one exists its `### Title` and `### Description` take over automatically and these entries become the fallback. `tactic` is `null` for the screen-capture and repackaging risks until they're mapped; the APK static-analysis entry carries `Discovery`, the tactic its playbook entry names.

Both halves of an entry are editable over HTTP without a code change: `PUT /platforms/android/risks/{risk_id}` replaces the metadata fields, and `PUT /platforms/android/risks/{risk_id}/demonstration` replaces the demonstration. Neither touches the run.

Similarly, a feature_id's own `name`/`description` (there's no `Feature` class — `feature_id` is just a string tag on each risk) live in `configs/split/android/features.yaml`, exposed at `GET /platforms/android/features` and editable at `PUT /platforms/android/features/{feature_id}`.

## Adding An Android Risk

1. Add a new class under `mobile_playbook/platforms/android/risks/`.
2. Subclass `AndroidRisk` and set a unique `risk_id`; the displayed text comes from the YAML entry, never from the class.
3. Reuse the ADB/Appium device client and report writing where possible.
4. Add mocked pytest coverage for device and external-tool behavior.

Risks are discovered automatically: `mobile_playbook/platforms/android/risks/registry.py` scans the folder for concrete `AndroidRisk` subclasses and picks up any file that defines one, keyed by its `risk_id`. Adding the file is enough — nothing else needs editing.

## Adding Android Apps

Add another object under `apps` in `configs/android.yaml`, or a bare package-name string for the legacy shape. App identity, package name, and enabled risks all live in config, not in code.
