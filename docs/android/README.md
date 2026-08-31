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
  --risks android-feature-06-risk-01 --out reports

python -m mobile_playbook run --platform android --config configs/android.yaml \
  --risks android-feature-01-risk-02 --out reports
```

Outputs land under:

```text
reports/<RUN_TIMESTAMP>/android/<APP_ID>/<RISK_ID>/<CASE_ID>/
```

## Requirements

| | Needed for |
| --- | --- |
| `adb` on `PATH` (Android platform-tools) | everything |
| A connected device with USB debugging **authorised** | everything |
| Appium with the UiAutomator2 driver | both risks |
| `apktool`, `apksigner`, `keytool` on `PATH` | `android-feature-01-risk-02` only |

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
goes under `intake/android/apks/` and is referenced as `<APK_PATH>`.

## Risk execution

| Risk | Needs | Notes |
| --- | --- | --- |
| `android-feature-06-risk-01` — screen recording / `FLAG_SECURE` | ADB, Appium, a connected device | verifies whether capture is blocked |
| `android-feature-01-risk-02` — APK repackaging | ADB, Appium, plus `apktool`, `apksigner`, `keytool` | pulls, decodes, rebuilds, resigns, reinstalls and launches |

The repackaging risk modifies device state. Working files are left under
`work/android/repackaging/` for inspection, and the original APK is reinstalled
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
