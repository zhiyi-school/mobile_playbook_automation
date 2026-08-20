# Android Configuration

The Android config lives at `configs/android.yaml`. `device` and `runner` are written inline in that entry-point file — there's only ever one device and one runner profile per project — while the app roster and per-risk settings, which don't fit comfortably in one file, live in their own files under `configs/split/android/` and are pulled in via `include:`.

## Quickstart

Set up the split config from the tracked examples:

```bash
cp configs/android.example.yaml configs/android.yaml
cp configs/split/android/apps.example.yaml configs/split/android/apps.yaml
for f in tools repackaging screen_capture; do cp configs/split/android/risk_settings.example.yaml "configs/split/android/$f.yaml"; done
```

`risk_settings.example.yaml` shows `tools`, `repackaging`, and `screen_capture` together in one file for easier reading; trim each copy above down to just its own top-level key.

The config contains `device`, `runner`, `tools`, per-risk timing blocks, and `apps` — the first two inline, the rest via `include:`.

## Device

```yaml
device:
  appium_server_url: "http://127.0.0.1:4723"
  adb_path: "adb"
  adb_serial: null
```

`adb_serial` is optional. Set it when more than one Android device is attached.

### Appium auto-start

If `appium_server_url` isn't reachable, the framework normally fails preflight with a one-line error asking you to run `appium` yourself. Set `device.appium_auto_start` to have it launch Appium instead and wait for it to come up:

```yaml
device:
  appium_auto_start:
    enabled: true
    command: ["appium"]
    wait_seconds: 60
    poll_interval_seconds: 1
```

This is checked once when a run connects to the device, and again before every single test — if Appium was running fine but crashes partway through a run, the next test's check notices it's unreachable, restarts it, reconnects, and the run continues with the remaining apps/risks rather than every subsequent test failing the same way. Appium is started once and left running for the rest of the run (and afterward) — it is not stopped between tests. Every launch attempt (including restarts after a crash) is appended to `appium.log` in that run's report directory, so what happened and why is inspectable after the fact.

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

These are reserved settings for future Android static-analysis and proxy integrations; the current Android risks (`android-feature-06-risk-01`, `android-feature-01-risk-02`) do not read them.

## Apps

```yaml
apps:
  - id: "example_app"
    name: "Example App"
    package_name: "com.example.app"
    risks:
      android-feature-06-risk-01:
        enabled: true
      android-feature-01-risk-02:
        enabled: true
```

The legacy shape is also accepted:

```yaml
apps:
  - com.example.app
  - com.example.other_app
```

When using the legacy shape, both Android risks are enabled for each package.

See the one app entry under `apps:` in [configs/split/android/apps.example.yaml](../../configs/split/android/apps.example.yaml) for a full, copyable app block including per-app risk overrides.

## Risk Blocks

Android risk IDs are prefixed `android-feature...`. To configure a risk for an app:

1. Add it under the app's `risks` mapping with `enabled: true` (or leave it `false`/omitted to skip it):

   ```yaml
   risks:
     android-feature-01-risk-02:
       enabled: true
   ```

2. Its actual settings come from that risk's global settings file, shared by every app that enables it — here's the start of `repackaging.yaml`, taken directly from `configs/split/android/risk_settings.example.yaml`:

   ```yaml
   repackaging:
     work_dir: "work/android/repackaging"
     # If unset, a keystore is generated at <work_dir>/<app>/../release.keystore on first use.
     keystore_path: null
     keystore_alias: "mobileplaybook"
     keystore_pass: "password"
   ```

   See `configs/split/android/risk_settings.example.yaml` for the rest of this and `screen_capture`'s fields, and the field references below for what each controls.
3. Only add more fields under the app's own `risks.<risk_id>` entry when this one app needs to differ from those shared defaults — nest just the field being changed. Anything left unset there falls back to the global file.

Risks run in the order they're listed under an app's `risks` mapping, not sorted by ID — keep that in mind if the order of your own `risks:` blocks matters to you.

### Repackaging timing and signing (`android-feature-01-risk-02`)

Global defaults live under a top-level `repackaging` block and can be overridden per app under `risks.android-feature-01-risk-02`:

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

### Screen capture timing (`android-feature-06-risk-01`)

Global defaults live under a top-level `screen_capture` block and can be overridden per app under `risks.android-feature-06-risk-01`:

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

`configs/android.yaml` has `device` and `runner` written inline, plus an `include:` mapping (section name → file path) for the sections that live under `configs/split/android/`:

```yaml
device:
  appium_server_url: "http://127.0.0.1:4723"
  # ...

runner:
  work_dir: "work/android"
  # ...

include:
  tools: split/android/tools.yaml
  repackaging: split/android/repackaging.yaml
  screen_capture: split/android/screen_capture.yaml
  apps: split/android/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/android/`. Inline values and included sections can coexist in the same file — nothing requires every section to go through `include:`, which is how `device`/`runner` stay inline while the rest are pulled in. See `configs/android.example.yaml`, the tracked example of this entry-point file.

The real tools/repackaging/screen_capture/app-roster content lives in `configs/split/android/*.yaml` (all git-ignored, since they contain real app config — only the `*.example.yaml` files under `configs/split/android/` are tracked). `configs/android.yaml` itself is also git-ignored, since it holds the real device config.

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file, the same as for iOS. Existing shell environment variables are not overwritten. Android risks do not currently require any environment variables.
