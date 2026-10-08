# Android Configuration

The Android config lives at `configs/android.yaml`. `device` and `runner` are written inline in that entry-point file — there's only ever one device and one runner profile per project — while the app roster and per-risk settings, which don't fit comfortably in one file, live in their own files under `configs/split/android/` and are pulled in via `include:`.

## Quickstart

Set up the split config from the tracked examples:

```bash
for example in configs/*.example.yaml configs/split/*/*.example.yaml; do
  target="${example%.example.yaml}.yaml"
  [ -e "$target" ] || cp "$example" "$target"
done
```

Every file an entry config includes has its own `*.example.yaml` next to it, so
the copies load as they are. The loop skips any real config you already have.

The config contains `device`, `runner`, `tools`, per-risk timing blocks, and `apps` — the first two inline, the rest via `include:`.

## Device

```yaml
device:
  appium_server_url: "http://127.0.0.1:4723"
  adb_path: "adb"
  adb_serial: null
  app_store: "play_store"
```

`adb_serial` is optional. Set it when more than one Android device is attached, or when one phone is connected over both USB and Wi-Fi (for example `10.0.0.5:5555` to use only the Wi-Fi connection). It selects the device for adb commands and is passed to Appium as the session's `udid`.

Set `adb_serial: "wifi"` to use the phone's Wi-Fi connection without fixing its address. Each adb client finds it the first time it targets the device. An existing Wi-Fi connection is used as is. Otherwise, with exactly one phone attached by USB, its `wlan0` address is read, adb on the phone is switched to Wi-Fi on port 5555 if it is not listening already (`adb tcpip`, which lasts until the phone reboots), and the host connects to it. The phone must be on a network this host can reach. Without a Wi-Fi connection or a single USB phone to find one through, the device counts as not connected and the API log says why. Use this when USB transfers are unreliable, as on some Huawei phones, while keeping the cable attached for charging.

`app_store` chooses how missing apps are installed: `apk` (adb, from the app's
APKs in `artifacts/intake/android/apks/<PACKAGE_NAME>/`), `play_store` (the
default when unset) or `aurora_store`. Any other value fails config validation. It can also be set with
`PUT /config/android/device`. Restart the API and the assessment worker after
changing it in the file. See
[Installing missing apps](README.md#installing-missing-apps).

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
  auto_grant_permissions: false
  keep_screen_on: false
```

Before every Android test the runner wakes the device's screen and dismisses its lock screen over adb (`input keyevent KEYCODE_WAKEUP`, then `wm dismiss-keyguard`), since every Android risk drives the device's UI. This only works without a PIN, password or pattern; if the lock screen is still showing afterwards, the test fails with `the device is still locked after waking it` and does not run.

`runner.keep_screen_on: true` also sets the device to keep its screen on whenever it is charging, from any power source (`adb shell svc power stayon true`), when a run connects to it, so the screen does not time out between tests. It changes a device setting that stays after the run; `adb shell svc power stayon false` turns it off again.

`runner.work_dir` is optional. Unset, it follows `WORK_DIR` to `<work>/android`; a relative value resolves against the repository root, never the working directory the run started from.

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

Configure each package once. The app's `id` is its identity on the dashboard,
and its backed-up APKs live in a work folder named after the package, so two
entries for one package would share that folder and appear as two apps. Adding
an app through `POST /config/android/apps` refuses a package that is already
configured and names the existing app. The `id` may match an iOS app's `id`; the
dashboard keeps one application per `id` and platform.

Each app can also carry `sector`, `agency`, `version`, and `cisos` (a list of `{name, email}`) — organizational metadata a dashboard displays alongside an app's findings. None of this is read by the automation run itself; all four are optional and default to blank/empty, and can be set here directly or through `GET`/`PUT /config/android/apps/{app_id}` (see [HTTP API](../api.md#editing-config)).

See the one app entry under `apps:` in [configs/split/android/apps.example.yaml](../../configs/split/android/apps.example.yaml) for a full, copyable app block including per-app risk overrides.

## Risk Blocks

Android risk IDs are prefixed `android-feature...`. To configure a risk for an app:

1. Add it under the app's `risks` mapping with `enabled: true` (or leave it `false`/omitted to skip it):

   ```yaml
   risks:
     android-feature-01-risk-02:
       enabled: true
   ```

2. Its actual settings come from that risk's global settings file, shared by every app that enables it — here's the start of `repackaging.yaml`, taken directly from `configs/split/android/repackaging.example.yaml`:

   ```yaml
   repackaging:
     keystore_path: null
     keystore_alias: "mobileplaybook"
     keystore_pass: "REPLACE_WITH_KEYSTORE_PASSWORD"
   ```

   See `configs/split/android/repackaging.example.yaml` and `screen_capture.example.yaml` for the rest of these fields, and the field references below for what each controls.
3. Only add more fields under the app's own `risks.<risk_id>` entry when this one app needs to differ from those shared defaults — nest just the field being changed. Anything left unset there falls back to the global file.

Risks run in the order they're listed under an app's `risks` mapping, not sorted by ID — keep that in mind if the order of your own `risks:` blocks matters to you.

### Repackaging timing and signing (`android-feature-01-risk-02`)

Global defaults live under a top-level `repackaging` block and can be overridden per app under `risks.android-feature-01-risk-02`:

```yaml
repackaging:
  keystore_path: null
  keystore_alias: "mobileplaybook"
  keystore_pass: "REPLACE_WITH_KEYSTORE_PASSWORD"
  restore_original_after_test: true
  record_lead_in: 2
  launch_wait: 8
  post_click_wait: 5
  record_tail: 3
```

- `work_dir` (optional): root directory for backed-up, decoded, and rebuilt APKs (per app, under `<work_dir>/<package_name_with_underscores>/`). Unset, it is `<work>/android/repackaging`; a relative value resolves against the repository root.
- `keystore_path`: path to a debug/release keystore used to re-sign the repackaged APK; a relative value resolves against the repository root. If unset, a keystore is generated at `<work_dir>/<app>/../release.keystore` via `keytool` on first use.
- `keystore_alias` / `keystore_pass`: alias and password used when generating the keystore and signing with `apksigner`.
- `restore_original_after_test`: whether to replace the repackaged build with the original after validation, from the APKs backed up before the test, so later tests run against the same build, and with the install method that `device.app_store` selects only if that fails.
- `apkeditor_jar` (optional): path to the APKEditor jar used to merge split APKs before decoding; a relative value resolves against the repository root. Unset, the newest `APKEditor*.jar` under `tools/vendor/apkeditor/` is used.
- `launch_wait` / `post_click_wait` / `record_lead_in` / `record_tail`: pacing for the post-repackaging Appium launch validation and its screen recording.

### Screen capture timing (`android-feature-06-risk-01`)

Global defaults live under a top-level `screen_capture` block and can be overridden per app under `risks.android-feature-06-risk-01`:

```yaml
screen_capture:
  record_lead_in: 2
  launch_wait: 4
  # consent_timeout: 15
  # helper_apk: artifacts/companion/android/apks/screen-capture-helper.apk
```

- `record_lead_in`: seconds to wait after the helper starts recording before saving the home-screen frame and launching the app.
- `launch_wait`: seconds to wait after launching the app before saving its frame and checking for capture-blocking signals.
- `consent_timeout`: seconds to wait for the helper's screen-recording consent prompt to appear and for recording to start (default 15).
- `helper_apk`: the screen-capture helper APK to install when the device lacks its current version; relative paths resolve against the repository root. Defaults to `artifacts/companion/android/apks/screen-capture-helper.apk` (under `ARTIFACTS_DIR` when that is set), where `python tools/companion_apps/android_screen_capture/build.py` writes it.

`record_tail` is no longer read; the helper's frames replace the Appium recording it paced.

Full risk behavior and status meanings are documented in [Risks](risks.md).

## Split Android Configs

`configs/android.yaml` has `device` and `runner` written inline, plus an `include:` mapping (section name → file path) for the sections that live under `configs/split/android/`:

```yaml
device:
  appium_server_url: "http://127.0.0.1:4723"
  # ...

runner:
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
