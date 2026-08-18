# Android Configuration

The Android config lives at `configs/android.yaml`, using the split layout — a small entry-point file plus per-section files under `configs/split/android/` — since a real app roster and per-risk settings don't fit comfortably in one file.

## Quickstart

Set up the split config from the tracked examples:

```bash
cp configs/android.example.yaml configs/android.yaml
for f in device runner apps; do cp "configs/split/android/$f.example.yaml" "configs/split/android/$f.yaml"; done
for f in tools repackaging screen_capture; do cp configs/split/android/risk_settings.example.yaml "configs/split/android/$f.yaml"; done
```

`risk_settings.example.yaml` shows `tools`, `repackaging`, and `screen_capture` together in one file for easier reading; trim each copy above down to just its own top-level key.

The config contains `device`, `runner`, `tools`, per-risk timing blocks, and `apps`.

## Device

```yaml
device:
  appium_server_url: "http://127.0.0.1:4723"
  adb_path: "adb"
  adb_serial: null
```

`adb_serial` is optional. Set it when more than one Android device is attached.

## Runner

```yaml
runner:
  work_dir: "work/android"
  auto_grant_permissions: false
```

## Tools

```yaml
tools:
  mobsf_url: "http://localhost:8000"
  mobsf_api_key: ""
  burp_proxy: "http://127.0.0.1:8080"
```

These are reserved settings for future Android static-analysis and proxy integrations; the current Android risks (`android-feature6-risk1`, `android-feature1-risk2`) do not read them.

## Apps

```yaml
apps:
  - id: "parking"
    name: "Parking"
    package_name: "sg.parking.streetsmart"
    risks:
      android-feature6-risk1:
        enabled: true
      android-feature1-risk2:
        enabled: true
```

The legacy shape is also accepted:

```yaml
apps:
  - sg.parking.streetsmart
  - sg.gov.app.mol
```

When using the legacy shape, both Android risks are enabled for each package.

See [docs/android/examples/app-block.yaml](examples/app-block.yaml) for a full, copyable app entry including per-app risk overrides.

## Risk Blocks

Android risk IDs are prefixed `android-feature...`. Enable a risk by adding it under an app's `risks` mapping:

```yaml
risks:
  android-feature1-risk2:
    enabled: true
```

Risks run in the order they're listed under an app's `risks` mapping, not sorted by ID — keep that in mind if the order of your own `risks:` blocks matters to you.

### Repackaging timing and signing (`android-feature1-risk2`)

Global defaults live under a top-level `repackaging` block and can be overridden per app under `risks.android-feature1-risk2`:

```yaml
repackaging:
  work_dir: "work/android/repackaging"
  keystore_path: null
  keystore_alias: "mobileplaybook"
  keystore_pass: "password"
  restore_original_after_test: true
  record_lead_in: 2
  launch_wait: 8
  post_click_wait: 5
  record_tail: 3
```

- `work_dir`: root directory for backed-up, decoded, and rebuilt APKs (per app, under `<work_dir>/<package_name_with_underscores>/`).
- `keystore_path`: path to a debug/release keystore used to re-sign the repackaged APK. If unset, a keystore is generated at `<work_dir>/<app>/../release.keystore` via `keytool` on first use.
- `keystore_alias` / `keystore_pass`: alias and password used when generating the keystore and signing with `apksigner`.
- `restore_original_after_test`: whether to reinstall the original, backed-up APK(s) after the repackaged build has been validated.
- `launch_wait` / `post_click_wait` / `record_lead_in` / `record_tail`: pacing for the post-repackaging Appium launch validation and its screen recording.

### Screen capture timing (`android-feature6-risk1`)

Global defaults live under a top-level `screen_capture` block and can be overridden per app under `risks.android-feature6-risk1`:

```yaml
screen_capture:
  record_lead_in: 2
  launch_wait: 4
  record_tail: 5
```

- `record_lead_in`: seconds to wait after starting the screen recording before launching the app.
- `launch_wait`: seconds to wait after launching the app before checking for capture-blocking signals.
- `record_tail`: seconds to keep recording after the verdict is determined, before stopping.

Full risk behavior and status meanings are documented in [Risks](risks.md).

## Split Android Configs

`configs/android.yaml` is just an `include:` mapping (section name → file path), with the entry-point file at `configs/android.yaml` and its sections living under `configs/split/android/`:

```yaml
include:
  device: split/android/device.yaml
  runner: split/android/runner.yaml
  tools: split/android/tools.yaml
  repackaging: split/android/repackaging.yaml
  screen_capture: split/android/screen_capture.yaml
  apps: split/android/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/android/`. See `configs/android.example.yaml`, the tracked example of this entry-point file.

The real device/runner/tools/repackaging/screen_capture/app-roster content lives in `configs/split/android/*.yaml` (all git-ignored, since they contain real device and app config — only the `*.example.yaml` files under `configs/split/android/` are tracked).

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file, the same as for iOS. Existing shell environment variables are not overwritten. Android risks do not currently require any environment variables.
