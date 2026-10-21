# Android Automation

Android support uses the same CLI, run orchestration and report structure as
iOS, driving a connected device over ADB and Appium.

Examples use placeholders: `Example Android App`, `example-app`,
`com.example.android.placeholder`, `<APK_PATH>`, `<PACKAGE_NAME>`,
`<DEVICE_ID>`, `<RUN_TIMESTAMP>`, `<APP_ID>`, `<RISK_ID>`.

## What is shared, what is Android-specific

| Shared across platforms (documented centrally) | Android-specific (documented here) |
| --- | --- |
| CLI commands, flags and `run-all` — [setup.md](../setup.md) | `adb`, UiAutomator2, device authorisation |
| YAML entry-point + `include:` mechanism — [configuration.md](../configuration.md) | `device` fields, package-name app shape, tool paths |
| Run lifecycle, run IDs, manifests — [architecture.md](../architecture.md) | APK pull, repackaging, resigning, reinstall |
| HTTP API and SSE — [api.md](../api.md) | Which Android risks need extra tooling |
| Report layout, `dashboard_results.json`, SARIF — [api.md](../api.md) | Android per-test report fields and statuses |
| Dashboard sync worker — [operations.md](../operations.md) | — |
| Cross-cutting failures — [troubleshooting.md](../troubleshooting.md) | ADB, signing-tool and capture failures |

This page links to the central documents rather than repeating them.

## Quickstart

Create a working config — see [Configuration](configuration.md#quickstart) for
the split-file setup steps.

```bash
python -m mobile_playbook list-risks --platform android

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-01-risk-01

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-06-risk-01

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-01-risk-02

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-07-risk-01

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-02-risk-01
```

Outputs land under:

```text
artifacts/reports/<RUN_TIMESTAMP>/android/<APP_ID>/<RISK_ID>/<CASE_ID>/
```

## Requirements

| | Needed for |
| --- | --- |
| `adb` on `PATH` (Android platform-tools) | every risk that uses the device |
| A connected device with USB debugging **authorised** | every risk except `android-feature-01-risk-01` |
| No PIN, password or pattern on the device, so the runner can unlock it over adb before each test | every risk except `android-feature-01-risk-01` |
| Appium with the UiAutomator2 driver | `android-feature-04-risk-01`, `android-feature-06-risk-01` and `android-feature-01-risk-02` |
| The app's APK in `artifacts/intake/android/apks/` and `aapt` or `aapt2` on `PATH` to find it; MobSF when `analyzer.provider` is `mobsf` | `android-feature-01-risk-01` only |
| The screen-capture helper APK, built with `python tools/companion_apps/android_screen_capture/build.py` (Android SDK and a JDK), and the device language set to English for Android versions that ask to share one app or the entire screen | `android-feature-06-risk-01` only |
| The overlay attacker APK, `F4_Overlay_Attacker.apk`, in `artifacts/intake/android/apks/` | `android-feature-04-risk-01` only |
| `apktool`, `zipalign`, `apksigner`, `keytool` on `PATH` | `android-feature-01-risk-02` only |
| A rooted device, whose adbd or `su` runs commands as root; with Corellium role devices, the rooted VM | `android-feature-07-risk-01` only |
| For `play_store` or `aurora_store`, that store app signed in to a Google account, with the device language set to English | installing missing apps |

Confirm the device before running anything:

```bash
adb devices          # the device must show as "device", not "unauthorized"
```

Set `device.adb_serial` when more than one device is attached; leave it unset
for a single device. `<DEVICE_ID>` in examples is that serial.

Emulators work for risks that do not depend on real hardware behaviour, but
screen-capture blocking (`FLAG_SECURE`) and repackaging results can differ from
a physical device. Treat emulator results as indicative.

## APK requirements

Android does not have an IPA-equivalent up-front acquisition stage. The
repackaging risk pulls the installed APK from the device itself, so the usual
input is a **package name**, not a file:

```yaml
apps:
  - id: example-app
    name: Example Android App
    package_name: com.example.android.placeholder
    risks:
      android-feature-01-risk-02:
        enabled: true
```

An older flat package-list shape is still accepted; see
[Configuration](configuration.md). A standalone build goes in
`artifacts/intake/android/apks/` as `<App_Name>_<version>.apk`.
A split build goes in its own immediate child directory, for example
`artifacts/intake/android/apks/com.example.android.placeholder/`, containing
`base.apk`, every split delivered together by the store, and `apk-set.json`:

```json
{
  "schema_version": 1,
  "package_name": "com.example.android.placeholder",
  "version_code": "7",
  "files": [
    {"name": "base.apk", "sha256": "<base SHA-256>"},
    {"name": "split_config.arm64_v8a.apk", "sha256": "<split SHA-256>"}
  ]
}
```

Export all paths returned by `pm path <package>` after the store installation
finishes. Verify each APK's vendor signature with `apksigner verify
--print-certs`, and retain the acquisition provenance separately. Keep those
bytes unchanged. Do not merge or re-sign intake APKs. Stage complete sets
outside the active intake directory, then move them into place together.

The scanner requires every declared file and hash to match, rejects extra
APKs and symlinks, and checks that all APK manifests identify the same package
and version code. An unmarked split directory is ignored. The SDK tools
`aapt` or `aapt2` must be on `PATH` to read package metadata. Discovery uses
the manifest package name, selects the highest `versionCode`, and lists a
split set as one artifact identified by its base APK. Icon extraction reads
resources across the validated set, including density splits. Current intake
takes precedence over older repackaging backups for dashboard artifacts.
These integrity checks do not establish that an arbitrary input came from a
trusted publisher; verify signatures when acquiring it.

## Installing missing apps

An app that is not installed on the test device is installed by package name.
The assessment worker does this before starting a run: readiness reports
`app_not_installed`, the worker calls
`POST /config/android/apps/<APP_ID>/install`, and the run starts once the app is
there. The API holds the Android device for the whole install, so it never
overlaps a run. To install by hand:

```bash
python -m mobile_playbook install-app --config configs/android.yaml --apps example-app
```

Omit `--apps` to install every configured app that is missing. The command
exits `1` when any app was not installed. An app already installed is left as
it is, including an older version.

With Corellium baseline role devices (`device.corellium.roles`), the worker
never installs before a run: each run restores its VMs and installs the app's
intake APK on them. `install-app` then restores the non-rooted VM, or the
rooted one with `--role rooted`, installs each selected app's intake APK, checks
its version and shuts the VM down again; see
[Runs, installs and readiness](../corellium-lifecycle.md#runs-installs-and-readiness).

`device.app_store` in `configs/android.yaml` chooses the install method.
Restart the API and the assessment worker after changing it.

| `app_store` | Installs from | Module |
| --- | --- | --- |
| `apk` | the app's APK in `artifacts/intake/android/apks/`, over adb | `apk.py` |
| `play_store` (default) | the Google Play Store app | `play_store.py` |
| `aurora_store` | the Aurora Store app, which downloads Google Play builds | `aurora_store.py` |

### APKs over adb

The `apk` method copies every APK in the selected intake build with `adb
push`, checks each copied size, then installs the complete set together with
`pm install -r <base> <splits...>`. It grants no runtime permissions and needs
no Appium session or store account. Baseline role preparation uses the same
installer and complete set. Neither path merges or signs the inputs.

A set exported from Google Play retains its original signatures, but contains
the ABI, density, language and feature selections delivered to the source
device. Acquire a suitable set for a different target device. Sideloading the
files does not transfer the source account's Play licence or Play Integrity
status; an app may still require installation through Play on the target.
The repackaging risk deliberately modifies separate working copies as part of
its attack test; it never changes the intake set.

Some devices, such as Huawei phones with **Monitor apps installed via ADB**
turned on in Developer options (`settings get secure adb_install_need_confirm`
is `1`), hold every adb install until someone confirms it on screen. The
installer checks this first and stops without installing; turn the option off.
Appium's own helper apps are installed over adb too, so Appium cannot start on
such a device either.

### Play Store and Aurora Store

The store installers open the app's listing in the store app, read the screen,
and tap **Install** only; Aurora Store's "Continue to Aurora Store?" prompt is
also answered with **Open**. They never tap a price, Buy, Update, Manual
download, a sign-in or account prompt, the system's install confirmation, a
device security notice, or anything on a screen they do not recognise. Only a
free app is ever installed. Aurora Store keeps an **Open** button on the
listing while it installs, so the installer waits for the package to appear on
the device rather than reading Open as installed.

Aurora Store hands the download to Android to install. A device that asks for
confirmation at that point, or a Huawei phone whose security check shows a risk
notice and CAPTCHA for apps from outside AppGallery, stops the install for a
person to finish on the device. The Aurora Store rules come from its listing,
link prompt and in-progress screens; a complete install through it has not been
observed yet.

Some Huawei phones ship `com.android.vending` and `com.google.android.gms` as
Huawei stub packages (versions ending in `-hw`); these are not the Google Play
Store, and the Play Store installer cannot install apps on them.

### Outcomes

| Outcome | `blocker_code` | Retries on its own | What to do |
| --- | --- | --- | --- |
| Installed, or already installed | — | — | nothing; the run starts |
| A price or Buy button | `app_paid` | no | verify the purchase, install it on the device by hand, then **Retry now** |
| Not found, not released, region-locked or incompatible; for `apk`, no APK of the app in the intake folder | `app_unavailable` | no | add the APK or install it another way, or remove the app |
| Signed out, account setup or verification, the system's install confirmation, or a device security check | `store_account_action_required` | no | finish the prompt on the device, then **Retry now** |
| A screen the rules do not recognise | `store_screen_unrecognised` | no | see below |
| The download did not finish within five minutes | `app_install_timeout` | yes | usually a slow network |
| The device confirms every adb install, refused the APKs, or the APKs are another app's | `app_install_failed` | no | follow the detail, then **Retry now** |
| Appium or adb failed | `app_install_failed` | yes | check the device and Appium |

The store rules match English labels, so set the test device's language to
English. When a store shows something unexpected, nothing is tapped. A
screenshot and the screen's layout (`screenshot.png`, `page_source.xml`) are
saved under
`artifacts/work/android/<APP_STORE>/diagnostics/<TIMESTAMP>_<PACKAGE_NAME>/`,
and the rules in the store's module can be updated from them. The Appium server
log for installs is `artifacts/work/android/<APP_STORE>/appium.log`. After a
store install, the installer should be the store's own package
(`com.android.vending` or `com.aurora.store`); any other installer is reported
in the outcome.

## Recording the tested version

Android tests the app installed on the device, so its version is read from the
device with `adb shell dumpsys package <PACKAGE_NAME>`: the `versionName` is the
version and the `versionCode` is the build number. It is read three times:

- **After an install**, whether the app was just installed or was already
  there, by any install method. The install outcome carries `version_name` and
  `version_code`, `install-app` prints them, and the assessment worker logs
  them.
- **Before an app's first risk in a run.** The run records the version in its
  manifest's `artifact_versions`, which the dashboard sync stores on the
  assessment, and adds an `app_version` event with `version` and
  `build_number` to `events.jsonl`. This is the version the run tested, and it
  is read for every run, including CLI runs and reruns that skip the install.
- **After an app's last risk in a run.** When the version or build number
  differs from the one read at the start, the run adds an
  `app_version_changed` warning event with both versions. The repackaging risk
  restores the build it backed up, so it causes this only when that backup
  cannot be reinstalled and it falls back to the configured install method,
  which installs the store's latest version. The version recorded for the run
  stays the one read at the start.

A version the device does not report is left out with a warning; the run still
goes ahead. The build number is kept in the run's events only, as the dashboard
has no column for it.

## Risk execution

| Risk | Needs | Notes |
| --- | --- | --- |
| `android-feature-01-risk-01` — APK static analysis | the app's intake APK; MobSF optional | no device; scans the APK with MobSF or the built-in scanner and ranks what it exposes |
| `android-feature-04-risk-01` — overlay | ADB, Appium, the overlay attacker APK, a device without a PIN, password or pattern | allows an attacker app to display over other apps through Settings, as the playbook does, then opens the app and checks whether the attacker's overlay window is shown over it |
| `android-feature-06-risk-01` — screen recording / `FLAG_SECURE` | ADB, Appium, the screen-capture helper, a device without a PIN, password or pattern | records the screen with a helper app, as the playbook does, and checks whether the app's content was captured |
| `android-feature-01-risk-02` — APK repackaging | ADB, Appium, plus `apktool`, `zipalign`, `apksigner`, `keytool` | pulls, decodes, rebuilds, aligns, resigns, reinstalls and launches |
| `android-feature-07-risk-01` — private data directory | ADB, a rooted device | copies the app's private data directories off the device as root and flags secrets and personal data stored in plain text |
| `android-feature-02-risk-01` — private data over USB debugging | ADB, no root | copies a debuggable app's private data directories with `run-as` and scans them as the rooted test does; for other apps, records whether `adb backup` could return their data |
| `android-feature-02-risk-02` — runtime logs over USB debugging | ADB, Appium, no root | captures the device log with `adb logcat` (the playbook uses Android Studio's Logcat window) while the app is opened and taken through its scripted steps or left running, keeps the app's lines and scans them for canaries, secrets and personal data |

The repackaging risk modifies device state. Working files are left under
`artifacts/work/android/repackaging/` for inspection, and the original APK is reinstalled
afterwards when `restore_original_after_test` is true. Use a test device.

Stages, statuses and how to add a new Android risk: [Risks](risks.md).

## Appium capabilities

Capabilities are derived from the `device` and `runner` config rather than
written by hand — the UiAutomator2 driver, the resolved `adb_serial`, and the
app's `package_name`. `runner.auto_grant_permissions` grants the app's declared permissions over adb
before each test, so most runtime permission prompts don't appear; Android has no
prompt-handling setting like iOS's `runner.permission_alerts`. The repackaging
risk denies any prompt that still covers the app; see [Risks](risks.md#android-feature-01-risk-02).
Field-by-field detail: [Configuration](configuration.md).

## Reports and evidence

Android output follows the shared report layout described in
[api.md](../api.md#durable-dashboard-sync). Android-specific statuses, evidence
kinds and per-test fields are in
[Reports And Troubleshooting](reports-and-troubleshooting.md).

## Documentation

- [Configuration](configuration.md) — device, runner, tool and per-risk config shape, including the legacy package-list app shape
- [Risks](risks.md) — what each Android risk tests, stages, statuses, adding new risks
- [Reports And Troubleshooting](reports-and-troubleshooting.md) — Android report files, statuses and common failures
- [Corellium Image Qualification](corellium-qualification.md) — checking baseline role VMs against real Corellium images
- [configs/split/android/apps.example.yaml](../../configs/split/android/apps.example.yaml) — a copyable app entry

Central: [setup.md](../setup.md) · [configuration.md](../configuration.md) ·
[architecture.md](../architecture.md) · [api.md](../api.md) ·
[operations.md](../operations.md) · [troubleshooting.md](../troubleshooting.md)
