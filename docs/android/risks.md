# Android Risks

## android-feature-06-risk-01

`android-feature-06-risk-01` demonstrates whether a sensitive screen can be screen-recorded or screen-shared, either because the app does not set `FLAG_SECURE` or because its own tamper/debugging checks fail to react to a recording session.

Requires: `adb`, `appium`.

Stages:

1. Connect an Appium driver and start a screen recording (best-effort; the test continues if recording cannot start, and records why in `report.json` as `metadata.recording`, for example on devices without Android's `screenrecord` utility such as some Huawei phones).
2. Wait `record_lead_in` seconds, then activate (launch) the target app.
3. Wait `launch_wait` seconds, then evaluate the app's response, checking in order:
   - Did the foreground app switch away from the target package? If so, classify as redirected to a known browser, exited to the home-screen launcher (Pixel, Samsung or Huawei), or switched to an unknown app. A system permission prompt (`com.android.permissioncontroller`) covering the app is inconclusive: it is Android asking for a permission, not the app protecting itself, so screen capture was not tested. Set `runner.auto_grant_permissions: true` to grant the app's permissions before the test so no prompt appears. A switch to a store app (the Play Store, `com.android.vending`, or Aurora Store, `com.aurora.store`) is inconclusive instead: the app refused to open, typically because it was not installed from that store, so its screen was never shown and screen capture was not tested.
   - Does the visible UI contain developer/USB-debugging keywords (for example "usb debugging")?
   - Does the visible UI contain screen-recording/sharing warning keywords?
   - Does any of the target app's own windows in `adb shell dumpsys window windows` have `FLAG_SECURE` set? Android lists window flags by name without the `FLAG_` prefix (`fl=LAYOUT_IN_SCREEN SECURE …`), or as a hex value on some builds, so the check reads the `fl=` line of each window whose `package=` is the target app; other apps' windows are ignored. If the app has no window in the dump, the result is an `ERROR` rather than `ALLOWED`.
   - If none of the above trip, the screen is considered visible and unprotected.
4. Wait `record_tail` seconds, stop the recording, and save it as `recordings/<package>.mp4`.
5. Close the app and write the report.

Verdict-to-status mapping:

- Any `ALLOWED` verdict (UI visible, no security signal detected) → `SCREEN_CAPTURE_ALLOWED`.
- Any `BLOCKED` verdict (browser/launcher redirect, debugging/capture warning text, or `FLAG_SECURE`) → `SCREEN_CAPTURE_BLOCKED`.
- Any `INCONCLUSIVE` verdict (the app redirected to the app store instead of opening, or a system permission prompt covered it) → `SCREEN_CAPTURE_NOT_TESTED`, with the Inconclusive security verdict.
- Any `ERROR` verdict (driver, launch, or ADB failure, or no window for the app in the window dump) → `FAILED`.

Timing knobs (`record_lead_in`, `launch_wait`, `record_tail`) are documented in [Configuration](configuration.md).

## android-feature-01-risk-02

`android-feature-01-risk-02` demonstrates whether a repackaged (decompiled, patched, and re-signed) build of the app can still install and launch, which indicates the app lacks effective tamper/signature detection.

Requires: `adb`, `apktool`, `apksigner`, `keytool`, `appium`. Apps installed as split APKs also need [APKEditor](https://github.com/REAndroid/APKEditor) and `java`; preflight does not check these because apps without splits don't use them.

Stages (each must succeed before the next runs; the first failure stops the pipeline):

1. **Backup APKs** — `adb shell pm path <package>` to enumerate installed APK paths (handles split APKs), then `adb pull` each into `artifacts/work/android/repackaging/<package>/original/`.
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

`PASS` → `REPACKAGING_SURVIVED` (the repackaged build kept working — the app did not detect the tampering). Any `FAIL` → `REPACKAGING_BLOCKED` (the app detected the repackaging and reacted, for example by redirecting to the app store or exiting).

When `restore_original_after_test` is true (the default) and the install stage ran, the rebuilt app is uninstalled after the test and the original is reinstalled with the install method that `device.app_store` selects. If that does not succeed (for example, a paid app), the backed-up APKs are reinstalled with `adb install-multiple` instead, and `metadata.restore` records `method: adb`, the app store outcome, and a note that later tests ran against an adb install. The restore also runs when validation raises an error; a restore that fails adds a `Restore failed: …` entry to `errors`. If the pipeline stopped before the install stage, the original was never replaced and nothing is reinstalled.

Generated work files (backed-up, decoded, and rebuilt APKs) are left under `artifacts/work/android/repackaging/` for inspection.

## Risk Metadata

**`configs/split/android/risks.yaml` is the source of truth for everything a dashboard displays about a risk** — its `name`, `description`, `tactic` and `demonstration`. The `AndroidRisk` class holds none of that text: it carries only `risk_id`, `feature_id`, the test-case identifiers and the flags that change what a run does (`is_blocking`, `requires`, `requires_device`). `list_risks()` returns the class's empty defaults and `GET /platforms/{platform}/risks` overlays the YAML entry on top. See [iOS risk metadata](../ios/risks.md#risk-metadata) for the reasoning — the same arrangement applies here.

There is no Android playbook yet, so the current entries carry over what the risk classes used to say; once one exists its `### Title` and `### Description` take over automatically and these entries become the fallback. `tactic` is `null` for both Android risks until they're mapped.

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
