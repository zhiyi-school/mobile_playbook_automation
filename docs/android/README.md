# Android Automation

Android support is integrated into the same CLI and report structure as iOS, driving a connected device over ADB and Appium.

Create a working config: see [Configuration](configuration.md#quickstart) for the split-file setup steps.

List Android risks:

```bash
python -m mobile_playbook list-risks --platform android
```

Run screen-capture testing:

```bash
python -m mobile_playbook run --platform android --config configs/android.yaml --risks android-feature6-risk1 --out reports
```

Run repackaging testing:

```bash
python -m mobile_playbook run --platform android --config configs/android.yaml --risks android-feature1-risk2 --out reports
```

Outputs are written under:

```text
reports/<run_timestamp>/android/<app_id>/<risk_id>/
```

## Documentation

- [Configuration](configuration.md): device, runner, tool, and per-risk config shape, including the legacy package-list app shape.
- [Risks](risks.md): what each Android risk tests, stages, statuses, and how to add new Android risks.
- [Reports And Troubleshooting](reports-and-troubleshooting.md): Android report files, statuses, and common failure causes.
- [configs/split/android/apps.example.yaml](../../configs/split/android/apps.example.yaml): a focused, copyable app entry for `configs/android.yaml`.

## Requirements

- `adb` on `PATH`, with a connected/authorized Android device
- Appium server (used by both risks)
- `apktool`, `apksigner`, and `keytool` on `PATH` for `android-feature1-risk2`

## Quick Notes

- `android-feature6-risk1` requires ADB, Appium, and a connected Android device.
- `android-feature1-risk2` additionally requires `apktool`, `apksigner`, and `keytool`.
- Repackaging work files are left under `work/android/repackaging/` for inspection, and the original APK(s) are reinstalled afterward when `restore_original_after_test` is true.
