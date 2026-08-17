# Android Configuration

The Android config lives at `configs/android.yaml`. Create your local working copy from the example:

```bash
cp configs/android.example.yaml configs/android.yaml
```

It contains `device`, `runner`, `tools`, per-risk timing blocks, and `apps`.

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

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file, the same as for iOS. Existing shell environment variables are not overwritten. Android risks do not currently require any environment variables.
