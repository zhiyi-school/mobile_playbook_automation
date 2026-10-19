# Android Risks

## android-feature-01-risk-01

`android-feature-01-risk-01` demonstrates that the app's APK can be obtained and analysed statically, following the playbook: the APK is uploaded to MobSF, and the report shows what anyone with the APK can learn from its manifest, code and bundled files. As on iOS, completing the analysis is itself the finding, so a run that analyses the APK is **At Risk**; the critical findings rank what the analysis exposed.

Requires: the app's installable APK in `artifacts/intake/android/apks/`, found by its package name (see [APK requirements](README.md#apk-requirements)), and `aapt` or `aapt2` on `PATH` to read that package name. It needs no device, so a run of only this risk connects to none; pulling the installed APKs from the phone instead is planned. MobSF is needed only when `analyzer.provider` is `mobsf`.

Stages:

1. Take the intake APK whose package (read with `aapt dump badging`) is the app's, the highest `versionCode` when there are several, and record its name, size and SHA-256 under `metadata.apks` and `metadata.apk_sha256`; `metadata.base_apk` is its path. With no such APK the run stops as `APK_NOT_FOUND`.
2. With `analyzer.provider: mobsf`, upload the APK to MobSF, scan it and fetch the JSON report (the same client as iOS: `POST /api/v1/upload`, `/api/v1/scan`, `/api/v1/report_json`), then normalize it: package identity and SDK levels, permissions and which are dangerous, exported component counts, the file list, and every titled finding MobSF rated above info.
   For split intake builds, MobSF scans only the unchanged base APK; its summary records `analysis_scope: base_apk`. Use the built-in analyzer to inventory and scan code, libraries and resources across every split. Neither analyzer merges or re-signs the inputs. `metadata.apks` records every input file and hash; `metadata.apk_sha256` remains the base hash.
3. Otherwise, or when MobSF fails and `fallback_to_builtin` is true, run the built-in scanner over the base and all splits in the selected intake build: parse its `AndroidManifest.xml` from `aapt dump xmltree` (falling back to `aapt2`), list every file in it, and scan for secrets. Text files outside compiled `res/` XML (for example `assets/*.json`) are checked for key-value secrets and known secret formats; `classes*.dex` and `resources.arsc`, where Firebase and Google API keys are compiled, are checked for the known formats only. Values are masked unless `sensitive_scan.reveal_values` is true.
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
4. Evaluate the recording and app state:
   - First check the `before_launch` frame. If it was already blank, the recording was not working, so the result is an `ERROR` regardless of redirects or warning text.
   - Verify that the target package is still in the foreground. Every exit or redirect, including a browser, launcher, store or unknown app, is inconclusive: the cause could be a crash, login flow or device incompatibility, so it does not establish capture protection. Other apps are left open. A system permission prompt covering the app is also inconclusive; set `runner.auto_grant_permissions: true` to grant the app's permissions before the test. A failed foreground query is an automation error because the recorded frame cannot be attributed to the target app.
   - Record developer/USB-debugging and screen-recording/sharing keyword matches under `metadata.ui_observations`. These are supporting observations, not proof that recording was blocked; a UI-query error is recorded there too. A warning with visible recorded content still means capture was allowed.
   - Decide from the `app_open` frame. The helper measures the middle 80% of each frame, leaving out the status and navigation bars. A frame counts as blank when at least 98% of it is black and it has at most 16 distinct colours. A blank frame is `BLOCKED`; a frame with content is `ALLOWED`, regardless of warning keywords or the reported `FLAG_SECURE` value.
5. Copy both frames into the report as `capture/before_launch.png` and `capture/app_open.png` (`screen_capture_frame` evidence), record each frame's `dark_ratio`, `distinct_colors` and `blank` under `metadata.capture`, stop the recording, close the app, and write the report.

Alongside the verdict, `metadata.flag_secure` records whether any of the target app's own windows in `adb shell dumpsys window windows` has `FLAG_SECURE` set (`null` if that could not be read; absent when the app had left the screen). Android lists window flags by name without the `FLAG_` prefix (`fl=LAYOUT_IN_SCREEN SECURE …`), or as a hex value on some builds, so the check reads the `fl=` line of each window whose `package=` is the target app. It explains a blank recording but does not decide the verdict: the recording does, because content can also be protected in other ways, such as a secure video view inside an ordinary window.

Verdict-to-status mapping:

- Any `ALLOWED` verdict (the recording captured the app's screen) → `SCREEN_CAPTURE_ALLOWED`.
- Any `BLOCKED` verdict (a blank recording while the target app remains in the foreground, with a working pre-launch recording) → `SCREEN_CAPTURE_BLOCKED`.
- Any `INCONCLUSIVE` verdict (the app exited, redirected to any other app, or a system permission prompt covered it) → `SCREEN_CAPTURE_NOT_TESTED`, with the Inconclusive security verdict.
- Any `ERROR` verdict (driver, launch, or ADB failure, or a recording that was already blank before launch) → `FAILED`. A helper that cannot be installed, is refused consent, or does not answer also ends the run as `FAILED`, with the reason in `errors`.

This checks only the screen shown after `launch_wait`; it does not navigate through login or establish coverage of sensitive screens. A captured splash screen demonstrates capture of that splash screen only. A blank frame is evidence of blank capture, not a diagnosis of its cause.

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
9. **Install repackaged APKs** — uninstall the existing app, then copy the signed, repackaged APK to the device with `adb push` and install it with `pm install` (see [APK installs](../architecture.md)). A copy that does not finish fails the stage with `Could not copy <apk> to the device: …`.

If any stage fails, the run stops there and reports `REPACKAGING_FAILED` with the Inconclusive verdict. `errors` names the failing stage and, where the tool gave one, its reason, for example `Failed at stage: install_repackaged_apks - Failure [INSTALL_FAILED_...]`.

If all stages succeed, an Appium-driven validation launches the repackaged app and checks:

- Does it redirect to a store app (the Play Store or Aurora Store), such as the Play Store's "get this app from Play" prompt (a common tamper-detection response)?
- Does it exit immediately after launch?
- After tapping the first clickable element, does either of the above happen?

A screenshot is saved after launch (`screenshots/after_launch.png`) and after the tap (`screenshots/after_tap.png`) and added to `evidence`. An app that detects the tampering but shows its warning inside its own screen, rather than exiting, stays in the foreground and is reported as `PASS`; the screenshots are where that warning can be seen.

`PASS` → `REPACKAGING_SURVIVED` (the repackaged build kept working — the app did not detect the tampering). Any `FAIL` → `REPACKAGING_BLOCKED` (the app detected the repackaging and reacted, for example by redirecting to the app store or exiting).

After the validation, while the repackaged build is still installed, `adb shell run-as <package> ls -R` lists its private files, as the playbook's last step does to confirm the app is now debuggable. `metadata.debuggable` records `run_as: true` with `files_listed` and saves the listing as `run_as_ls.txt` (added to `evidence`), or `run_as: false` with the device's refusal in `detail`. It is recorded alongside the verdict and does not change it.

When `restore_original_after_test` is true (the default) and the install stage ran, the rebuilt app is uninstalled after the test and the APKs backed up at the start are copied back and reinstalled together with `pm install`, so later tests run against the same build this test tested. The app that originally installed it (`metadata.installer`, for example `com.android.vending`) is recorded as its installer again with `-i`; if the device refuses that, the backup is reinstalled without an installer and `metadata.restore.note` says so. `metadata.restore` records `method: backup`. Only if the backup cannot be reinstalled is the app reinstalled with the install method that `device.app_store` selects, recorded as `method: app_store` with `backup_failure` and a note that it may be a different build, since a store installs its latest version. After any restore, `metadata.restore.base_apk_sha256` is the restored build's fingerprint and `matches_original` says whether it is the build that was tested (`null` when either fingerprint could not be read). The restore also runs when validation raises an error; a restore that fails adds a `Restore failed: …` entry to `errors`. If the pipeline stopped before the install stage, the original was never replaced and nothing is reinstalled.

Generated work files (backed-up, decoded, and rebuilt APKs) are left under `artifacts/work/android/repackaging/` for inspection.

## android-feature-07-risk-01

`android-feature-07-risk-01` demonstrates what a rooted device can collect from the app's private data directory, following the playbook: root lists `/data/data/<package>` and reads the files in it. Root can read every app's private files, so the test asks whether the app keeps sensitive data there in plain text, or protects it as the playbook's control does, by encrypting it with a key held in the Android Keystore.

Requires: `adb` and `root`, so a rooted device; with Corellium role devices it runs on the rooted VM (see [Android role connections](../corellium-lifecycle.md#android-role-connections)). Root is used through whichever the device grants: a root adbd, the `su 0` of userdebug images, or Magisk-style `su -c` (`platforms/android/device/root.py`, which the `root` preflight check also uses). It needs no Appium.

Stages:

1. Record `metadata.installed_base_apk_sha256` and `metadata.root_method` (`adbd`, `su_0` or `su_c`).
2. Launch the app with `adb shell monkey -p <package> -c android.intent.category.LAUNCHER 1` so it writes its data, wait `launch_wait` seconds, then `am force-stop` it so its files do not change while they are copied. `metadata.launch` records whether it launched; a failed launch is recorded and the test goes on to read the data already there. The app's data is never cleared, so anything an operator left on the device, such as a signed-in session, is included.
3. As root, check which of `/data/data/<package>` and `/data/user_de/0/<package>` (device-protected storage) exist, list them with `ls -laR` into `private_data_listing.txt`, and measure them with `du`. A missing `/data/data/<package>` means the app is not installed, and the run fails. Data larger than `max_archive_mb` is not copied, and the run fails.
4. As root, archive the directories with `tar` into `/data/local/tmp/`, pull the archive into `artifacts/work/android/private_data/<package>/<run timestamp>.tar`, and remove the device copy, including when the pull fails. `tar` warnings about skipped files such as sockets are kept under `metadata.tar_warnings` and do not stop the test.
5. Read every regular file in the archive in memory, without extracting it; symlinks are skipped, and a member with an absolute or `..` path is recorded as `skipped` and not read. Each file gets a category:
   - `shared_prefs`: a `shared_prefs/*.xml` file, whose entries are checked by key name and value. One holding the androidx `EncryptedSharedPreferences` keyset entries is `encrypted_prefs` instead.
   - `sqlite`: a file with the `SQLite format 3` header, an unencrypted database. Up to 1,000 rows of each table are read from a read-only temporary copy and checked as `table.column` keys and values. `sqlite_sidecar` (`-wal`, `-journal`, `-shm`) files are checked through their printable strings.
   - `json` and `text`: UTF-8 text, checked with the shared secret scanner (`platforms/static_analysis/sensitive_findings.py`); JSON key names are checked as well. A text file that is only base64 of random-looking bytes is `opaque`.
   - `code`: app code stored as data, such as a React Native or Expo JavaScript bundle (`.js`, `.mjs`, `.bundle`, `.html`, `.htm`, `.css`), checked for secrets but not for personal data, since number and email literals in code are not stored user data. Hermes bytecode (`.hbc`) is read as `binary` under the same rule.
   - `media`: content that starts with a known image, archive or code format (PNG, JPEG, GIF, RIFF, gzip, zip, DEX, ELF, VDEX, ART), not read further.
   - `opaque`: at least 256 bytes spread as evenly as random data (a byte chi-square of at most 330), as encrypted data is; a file encrypted with AES-GCM, as the playbook's control writes, falls here. About one random file in a thousand measures above the limit and is read as `binary` instead. Compressed data in an unrecognised format also looks opaque.
   - `binary`: any other content, such as a DataStore protobuf, checked through its printable strings.
   - `empty`, `too_large` (over 5 MiB, or 50 MiB for a database, and not read), or `unreadable` when parsing failed.
6. Flag what is readable in plain text:
   - Secrets, as the APK analysis finds them: values under secret-like key names (`SENSITIVE_KEY_NAME`, `SENSITIVE_KEY_VALUE`), and JWTs, Google API keys, AWS access keys, private keys, Slack tokens and URLs with credentials.
   - Personal data: email addresses (`EMAIL_ADDRESS`), Singapore NRIC/FIN numbers (`NRIC_FIN`) and Luhn-valid payment card numbers (`PAYMENT_CARD_NUMBER`) anywhere, and values under personal-data key names (`PERSONAL_DATA_KEY_NAME`) such as NRIC, passport, card, account or IBAN, date of birth, PIN, name, address and postal code. Email and phone or mobile keys count only when the value looks like an email address or a phone number.
   - Values are masked unless `reveal_values` is true.
   - A value that several rules match is one finding, kept at its highest severity with its key name where one matched; `match_types` lists every rule that matched it.
   - Files that third-party SDKs write on their own are recognised by path and their findings reported separately, under `sdk_findings`, without deciding the verdict: Firebase Installations (`files/PersistedInstallation.*.json`, which holds the installation's auth and refresh tokens), Google Data Transport (`databases/com.google.android.datatransport.events`) and Google Analytics for Firebase (`databases/google_app_measurement.db` and `google_app_measurement_local.db`), with their `-wal`, `-journal` and `-shm` files. Every app that uses these SDKs has them from its first launch, so they say nothing about how the app stores its own data.
7. Write the reports, decide the verdict, and delete the pulled archive unless `keep_archive` is true. The archive holds the app's real data, so it is never copied into the report; a kept archive's path is `metadata.archive`.

Verdicts:

- `PRIVATE_DATA_EXPOSED` (**At Risk**): at least one secret or personal-data value was readable in plain text outside the known SDK files.
- `PRIVATE_DATA_PROTECTED` (**Reduced Risk**): nothing was flagged outside the known SDK files, and at least one file is an encrypted store, `encrypted_prefs` or `opaque`, outside the `cache`, `code_cache` and `app_webview` folders, where caches and WebView data would otherwise count. `metadata.protected_files` lists those files so they can be checked.
- `PRIVATE_DATA_NO_FINDINGS` (**Inconclusive**): nothing was flagged outside the known SDK files and no encrypted store was found. Plain-text files without a match are not proof that the app stores nothing sensitive, and an app that has not yet been used may have written little.
- `FAILED` (**Inconclusive**): the device does not grant root, the app is not installed, the data is over `max_archive_mb`, or archiving or pulling failed; `errors` says which.

Report files: `private_data_listing.txt` (`private_data_listing` evidence), `private_data_findings.json` with every file's category, size, `protected` flag, `sdk` name and finding count, the masked `findings` and the masked `sdk_findings` (`private_data_findings` evidence), and `critical_findings.json` and `critical_findings.md`. The critical findings are `PRIVATE_DATA_READABLE_WITH_ROOT` (LOW, always), `SENSITIVE_INFORMATION_EXPOSURE` with the highest severity found outside the SDK files, `SDK_DATA_IN_PLAIN_TEXT` (LOW) for findings in the SDK files, and `UNENCRYPTED_DATABASES` (LOW). `metadata` also records `directories`, `archive_bytes`, `file_count`, `categories`, `finding_count`, `sdk_finding_count`, `sdk_files` and `unencrypted_databases`.

The test reads only what the app has written by the time it runs: it does not sign in or move through the app. Sign in on the rooted device beforehand to include a session's data. Baseline role devices restore the rooted VM from its snapshot at the start of each run, which removes data left on it before.

Settings (`launch_wait`, `max_archive_mb`, `reveal_values`, `keep_archive`) are documented in [Configuration](configuration.md#private-data-android-feature-07-risk-01).

## Risk Metadata

**`configs/split/android/risks.yaml` is the source of truth for everything a dashboard displays about a risk** — its `name`, `description`, `tactic` and `demonstration`. The `AndroidRisk` class holds none of that text: it carries only `risk_id`, `feature_id`, the test-case identifiers and the flags that change what a run does (`is_blocking`, `requires`, `requires_device`). `list_risks()` returns the class's empty defaults and `GET /platforms/{platform}/risks` overlays the YAML entry on top. See [iOS risk metadata](../ios/risks.md#risk-metadata) for the reasoning — the same arrangement applies here.

`requires` names the tools and services a risk checks before it runs (`adb`, `appium`, `apktool`, `apksigner`, `keytool`, `mobsf`, `burp`, `screen_capture_helper`) and `root` when it needs a rooted device. The `root` check passes when the device runs shell commands as root or its `su` grants root. With Corellium role devices, a risk that requires root runs on the rooted VM and every other device risk on the non-rooted VM; a malformed declaration fails the risk. See [Android role connections](../corellium-lifecycle.md#android-role-connections). A single physical device runs every risk.

### Google Play services

Many apps stop at a "Google Play services" error dialog on a device without Play services, so a device risk would judge the dialog rather than the app. Before each risk that uses the device, the runner checks both sides:

- **The app.** `requires_play_services` in the app's entry decides when it is set (see [Apps](configuration.md#apps)). Otherwise, the app's intake APK decides: an app needs Play services when its manifest declares the `com.google.android.gms.version` meta-data, which every Play services client library adds. An app with neither an intake APK nor the setting is not checked.
- **The device.** `adb shell pm list packages -e com.google.android.gms` must list the package, meaning it is installed and enabled.

When the app needs Play services and the device lacks them, the risk is not run. Its result is `FAILED` with verdict `Inconclusive`, and an error naming the device (its role and adb serial on Corellium role devices) and the app. Static risks are never affected. Corellium images without Google Play, such as stock `user` builds, and phones sold without Google Play (for example, recent Huawei models) produce this result for such apps. An app whose manifest declares Play services but works without them can set `requires_play_services: false`.

There is no Android playbook yet, so the current entries carry over what the risk classes used to say; once one exists its `### Title` and `### Description` take over automatically and these entries become the fallback. `tactic` is `null` for the screen-capture and repackaging risks until they're mapped; the APK static-analysis entry carries `Discovery` and the private-data entry `Collection`, the tactics their playbook entries name.

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
