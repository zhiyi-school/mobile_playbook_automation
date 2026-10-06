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
  --risks android-feature-06-risk-01

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-01-risk-02
```

Outputs land under:

```text
artifacts/reports/<RUN_TIMESTAMP>/android/<APP_ID>/<RISK_ID>/<CASE_ID>/
```

## Requirements

| | Needed for |
| --- | --- |
| `adb` on `PATH` (Android platform-tools) | everything |
| A connected device with USB debugging **authorised** | everything |
| Appium with the UiAutomator2 driver | both risks |
| `apktool`, `apksigner`, `keytool` on `PATH` | `android-feature-01-risk-02` only |
| The Play Store app, signed in to a Google account, with the device language set to English | installing missing apps |

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
[Configuration](configuration.md). Where a local file is supplied instead, it
goes under `artifacts/intake/android/apks/` and is referenced as `<APK_PATH>`.

## Installing apps from the Play Store

An app that is not installed on the test device is installed from its Play
Store listing by package name. The assessment worker does this before starting
a run: readiness reports `app_not_installed`, the worker calls
`POST /config/android/apps/<APP_ID>/install`, and the run starts once the app is
there. The API holds the Android device for the whole install, so it never
overlaps a run. To install by hand:

```bash
python -m mobile_playbook install-app --config configs/android.yaml --apps example-app
```

Omit `--apps` to install every configured app that is missing. The command
exits `1` when any app was not installed.

The installer opens the listing in the Play Store app, reads the screen, and
taps **Install** only. It never taps a price, Buy, Update, a sign-in or
account prompt, or anything on a screen it does not recognise. Only a free app
is ever installed. An app already installed is left as it is, including an
older version.

| Outcome | `blocker_code` | Retries on its own | What to do |
| --- | --- | --- | --- |
| Installed, or already installed | — | — | nothing; the run starts |
| A price or Buy button | `app_paid` | no | verify the purchase, install it on the device by hand, then **Retry now** |
| Not found, not released, region-locked or incompatible | `app_unavailable` | no | install it another way, or remove the app |
| Signed out, or account setup or verification asked for | `store_account_action_required` | no | sign in or finish the prompt on the device, then **Retry now** |
| A screen the rules do not recognise | `store_screen_unrecognised` | no | see below |
| The download did not finish within five minutes | `app_install_timeout` | yes | usually a slow network |
| Appium or adb failed | `app_install_failed` | yes | check the device and Appium |

The rules match the English labels and the Play Store's own layout, so set the
test device's language to English. When the Play Store shows something
unexpected, nothing is tapped. A screenshot and the screen's layout
(`screenshot.png`, `page_source.xml`) are saved under
`artifacts/work/android/play_store/diagnostics/<TIMESTAMP>_<PACKAGE_NAME>/`, and
the rules in `mobile_playbook/platforms/android/acquisition/play_store.py` can be
updated from them. The Appium server log for installs is
`artifacts/work/android/play_store/appium.log`. After an install, the installer
should be `com.android.vending`; any other installer is reported in the outcome.

## Risk execution

| Risk | Needs | Notes |
| --- | --- | --- |
| `android-feature-06-risk-01` — screen recording / `FLAG_SECURE` | ADB, Appium, a connected device | verifies whether capture is blocked |
| `android-feature-01-risk-02` — APK repackaging | ADB, Appium, plus `apktool`, `apksigner`, `keytool` | pulls, decodes, rebuilds, resigns, reinstalls and launches |

The repackaging risk modifies device state. Working files are left under
`artifacts/work/android/repackaging/` for inspection, and the original APK is reinstalled
afterwards when `restore_original_after_test` is true. Use a test device.

Stages, statuses and how to add a new Android risk: [Risks](risks.md).

## Appium capabilities

Capabilities are derived from the `device` and `runner` config rather than
written by hand — the UiAutomator2 driver, the resolved `adb_serial`, and the
app's `package_name`. `runner.auto_grant_permissions` and
`runner.permission_alerts` control how runtime permission prompts are handled.
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
- [configs/split/android/apps.example.yaml](../../configs/split/android/apps.example.yaml) — a copyable app entry

Central: [setup.md](../setup.md) · [configuration.md](../configuration.md) ·
[architecture.md](../architecture.md) · [api.md](../api.md) ·
[operations.md](../operations.md) · [troubleshooting.md](../troubleshooting.md)
