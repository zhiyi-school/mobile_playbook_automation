# Android Risks

## android-feature6-risk1

`android-feature6-risk1` demonstrates whether a sensitive screen can be screen-recorded or screen-shared, either because the app does not set `FLAG_SECURE` or because its own tamper/debugging checks fail to react to a recording session.

Requires: `adb`, `appium`.

Stages:

1. Connect an Appium driver and start a screen recording (best-effort; the test continues if recording cannot start).
2. Wait `record_lead_in` seconds, then activate (launch) the target app.
3. Wait `launch_wait` seconds, then evaluate the app's response, checking in order:
   - Did the foreground app switch away from the target package? If so, classify as redirected to a known browser, exited to the home-screen launcher, or switched to an unknown app.
   - Does the visible UI contain developer/USB-debugging keywords (for example "usb debugging")?
   - Does the visible UI contain screen-recording/sharing warning keywords?
   - Does `adb shell dumpsys window windows` report `FLAG_SECURE` on the current window?
   - If none of the above trip, the screen is considered visible and unprotected.
4. Wait `record_tail` seconds, stop the recording, and save it as `recordings/<package>.mp4`.
5. Close the app and write the report.

Verdict-to-status mapping:

- Any `ALLOWED` verdict (UI visible, no security signal detected) → `SCREEN_CAPTURE_ALLOWED`.
- Any `BLOCKED` verdict (browser/launcher redirect, debugging/capture warning text, or `FLAG_SECURE`) → `SCREEN_CAPTURE_BLOCKED`.
- Any `ERROR` verdict (driver, launch, or ADB failure) → `FAILED`.

Timing knobs (`record_lead_in`, `launch_wait`, `record_tail`) are documented in [Configuration](configuration.md).

## android-feature1-risk2

`android-feature1-risk2` demonstrates whether a repackaged (decompiled, patched, and re-signed) build of the app can still install and launch, which indicates the app lacks effective tamper/signature detection.

Requires: `adb`, `apktool`, `apksigner`, `keytool`, `appium`.

Stages (each must succeed before the next runs; the first failure stops the pipeline):

1. **Backup APKs** — `adb shell pm path <package>` to enumerate installed APK paths (handles split APKs), then `adb pull` each into `work/android/repackaging/<package>/original/`.
2. **Ensure keystore** — generate a local signing keystore with `keytool -genkeypair` if one does not already exist at `keystore_path`.
3. **apktool decode** — `apktool d` the base APK into `repackaged/base/`.
4. **Patch manifest** — add `android:debuggable="true"` to the `<application>` element if not already present.
5. **Change app name** — prefix the `app_name` string resource with `RPK ` so the repackaged build is visually distinguishable (best-effort; does not fail the pipeline if the string is missing).
6. **Rebuild APK** — `apktool b` the patched sources into `repackaged.apk`.
7. **Sign APKs** — `apksigner sign` the rebuilt APK (and any split APKs) with the local keystore.
8. **Install repackaged APKs** — uninstall the existing app, then `adb install` / `adb install-multiple` the signed, repackaged build(s).

If any stage fails, the run stops there and reports `REPACKAGING_FAILED` with the failing stage name in `errors`.

If all stages succeed, an Appium-driven validation launches the repackaged app and checks:

- Does it redirect to the Play Store with a "get this app from play" prompt (a common tamper-detection response)?
- Does it exit immediately after launch?
- After tapping the first clickable element, does either of the above happen?

`PASS` → `REPACKAGING_SURVIVED` (the repackaged build kept working — the app did not detect the tampering). Any `FAIL` → `REPACKAGING_BLOCKED` (the app detected the repackaging and reacted, for example by redirecting to the Play Store or exiting).

When `restore_original_after_test` is true (the default), the original, backed-up APK(s) are reinstalled after validation regardless of outcome.

Generated work files (backed-up, decoded, and rebuilt APKs) are left under `work/android/repackaging/` for inspection.

## Risk Metadata

Each risk carries descriptive metadata as class attributes alongside `risk_id`/`name`: `description` (what the risk is), `goal` (what the test is trying to show), `is_blocking` (whether a positive finding should block a release/compliance sign-off), and `mitre_attack_mobile_technique_id` (the MITRE ATT&CK for Mobile tactic or technique this risk maps to, or `None` if not yet mapped — currently the tactic name, e.g. `"Discovery"`, since not every risk has a clean single-technique match). `list_risks()` and `GET /platforms/{platform}/risks` (see [HTTP API](../api.md)) return all of these alongside the existing fields.

## Adding An Android Risk

1. Add a new class under `mobile_playbook/platforms/android/risks/`.
2. Subclass `AndroidRisk` and set a unique `risk_id`, plus `description`/`goal` describing the risk and what the test demonstrates.
3. Reuse the ADB/Appium device client and report writing where possible.
4. Add mocked pytest coverage for device and external-tool behavior.

Risks are discovered automatically: `mobile_playbook/platforms/android/risks/registry.py` scans the folder for concrete `AndroidRisk` subclasses and picks up any file that defines one, keyed by its `risk_id`. Adding the file is enough — nothing else needs editing.

## Adding Android Apps

Add another object under `apps` in `configs/android.yaml`, or a bare package-name string for the legacy shape. App identity, package name, and enabled risks all live in config, not in code.
