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

The config contains `device`, `runner`, `tools`, per-risk settings blocks, and `apps` — the first two inline, the rest via `include:`.

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

The phone approves adb over Wi-Fi separately from USB: a host approved on USB without **Always allow from this computer** can be asked again when it connects over Wi-Fi, and `adb devices` lists the Wi-Fi connection as `unauthorized` while the phone shows **Allow USB debugging?**. Up to 10 seconds are allowed for someone to approve the prompt; after that the connection is kept and reported as waiting for the phone to allow debugging, so adb commands fail as unauthorized rather than as no device. Unlock the phone, tap **Allow** and tick **Always allow from this computer** so later runs connect without a prompt. Nothing on this host approves the prompt for a phone.

#### One user of the phone at a time

With `provider: physical`, every run whose risks use the device, `install-app`, and `POST /config/android/apps/{app_id}/install` hold a host-wide lock on the phone until they finish, whether they come from the CLI, the API or the assessment worker. A second one fails at once with "The physical test phone is in use by …", naming the run or install that holds it and its process. The API install answers `409` with `platform_busy`. Readiness reports `platform_busy` while another process holds the phone, so the worker waits rather than failing. Static-only runs never take the lock. The lock is released when the run or install ends, including on failure or Ctrl-C, and the operating system drops it if the process dies. The lock lives under `~/.local/state/mobile-playbook/device-locks/`, or the absolute `DEVICE_LOCK_DIR` from the process environment (see [Runtime storage](../storage.md#layout)).

`app_store` chooses how missing apps are installed: `apk` (adb, from the app's
APK in `artifacts/intake/android/apks/`), `play_store` (the
default when unset) or `aurora_store`. Any other value fails config validation. It can also be set with
`PUT /config/android/device`. Restart the API and the assessment worker after
changing it in the file. See
[Installing missing apps](README.md#installing-missing-apps).

### Corellium virtual device

`provider` chooses what is tested: `physical` (the default when unset) uses the phone `adb_serial` selects, and `corellium` uses an Android virtual device on Corellium instead. Any other value fails config validation.

`device.corellium` takes one of two forms. Without `roles`, it selects one instance that runs every risk and is used as it is, as described in this section. With `roles`, each run restores a non-rooted and, when a risk needs root, a rooted VM from known baselines and installs the build under test on them; see [Baseline role devices](#baseline-role-devices).

```yaml
device:
  provider: "corellium"
  corellium:
    project: "testing"
    rooted: false
    os_version: null
    instance: null
    auto_start: true
    auto_stop: true
    adb_port: 5001
    # adb_address: "10.11.1.1:5001"
    boot_timeout_seconds: 600
```

- `project` (required): the Corellium project's name, or its id when several projects share a name.
- `rooted` (required): `true` picks a rooted instance, `false` a stock one, whatever the instance is named. For Android this comes from the build variant Corellium reports (`osbuild`, for example `r2 user`): `userdebug` and `eng` builds count as rooted and `user` builds as stock. Corellium lists a `jailbroken` patch on every Android instance, so that patch is not used for Android; for iOS it marks a jailbroken instance.
- `os_version`: only instances whose Android version is this one or starts with it, so `"16"` matches `16.0.0`; unset accepts any version.
- `instance`: an instance name or id, to use only that instance. It must still be an Android instance in `project` that matches `rooted`.
- `auto_start` (default `true`): power the instance on, or resume it, when testing needs it. With `false`, a run fails when the instance is off or paused.
- `auto_stop` (default `false`): power the instance off once a run's tests are done.
- `adb_port`: the port adb listens on at the instance's services address, used when Corellium does not report one (it reports `port-adb` while the instance is on); default `5001`.
- `adb_address`: an explicit `host:port`, as the instance's **Connect** tab shows it. Set, it replaces the address built from the instance and `adb_port`.
- `boot_timeout_seconds`: how long to wait for the instance to power on and for Android to finish booting; default `600`.

`adb_serial` is ignored while `provider` is `corellium`; the instance's adb address takes its place for adb and as the Appium session's `udid`.

#### Which instance is used

Each time the framework connects, it lists the project's instances and keeps the Android ones that match `rooted`, `os_version` and `instance` and are in a usable state. It prefers one that is already on, then one that is starting, then a paused one, then one that is off, so a running instance is used before another is powered on; equal candidates are taken in name order. When none matches, the error lists every instance in the project with its platform, version, rooted state and power state.

#### Starting and stopping

- A run, `install-app` and `POST /config/android/apps/{app_id}/install` start the instance when `auto_start` allows, then connect adb and wait until Android reports `sys.boot_completed`.
- With `auto_stop`, the instance is powered off when the run's last queued test finishes, whether the tests passed, failed or the run was aborted, and whoever started the instance. Reconnecting after an Appium failure mid-run does not stop it, and reconnects adb to the same instance instead of choosing one again. Installs do not stop it, because a run normally follows.
- When the instance was started for a connection that then fails, for example because adb never reaches it, it is powered off again if `auto_stop` is set.
- Each run starts and stops the instance on its own, so several dashboard assessments in a row each wait for it to boot. Set `auto_stop: false` while you use the instance by hand or run many assessments back to back.

The readiness check (`GET /config/android/apps/{app_id}/provisioning`) is polled, so it only reads the instance's state and never starts it. An instance that is off or paused counts as ready when `auto_start` is set, because the run starts it, and the app's install state then reads as unknown until the instance is on. Without `auto_start`, such an instance reads as no device connected.

#### Requirements

- `CORELLIUM_API_URL` (for example `https://app.corellium.com/api`; the site URL without `/api` also works) and `CORELLIUM_API_TOKEN` in the repository-root `.env`. The Corellium client reads these keys directly for CLI runs, dashboard assessments, readiness checks and installs. No shell exports or startup wrapper are needed, and reading them does not copy file values into `os.environ`. Existing process values remain optional overrides.
- This host connected to the VPN of the project in `project`. Corellium's VPN is per project, and projects can reuse the same addresses, so another project's VPN can reach the wrong instance. The VPN carries traffic only while at least one VM in the project is on; with baseline role devices, every VM a run restores starts off, so keep another VM in the project on for adb to reach a restored VM promptly.
- `app_store: "apk"` in most cases, because Corellium Android images do not include Google Play.

The Corellium client in `mobile_playbook/platforms/corellium/` lists projects and instances, chooses one and powers it on and off without depending on Android, so iOS can reuse it.

#### Baseline role devices

With `roles`, a run claims each VM its selected risks need, restores the VM's baseline snapshot once, starts it, connects adb, installs the app's intake APK, runs the risks on their own VM and confirms every VM off at the end. A risk that lists `root` in its requirements runs on the `rooted` VM and every other device risk on the `non_rooted` VM; a run whose risks need no device touches no VM. The lifecycle is described in [shared Corellium lifecycle](../corellium-lifecycle.md#runs-installs-and-readiness).

```yaml
device:
  provider: "corellium"
  app_store: "apk"
  corellium:
    auto_start: true
    auto_stop: true
    boot_timeout_seconds: 600
    restore_timeout_seconds: 600
    roles:
      non_rooted:
        project: "android"
        instance: "<NON_ROOTED_INSTANCE_ID>"
        # os_version: "16"
        # adb_address: "10.11.1.1:5001"
      rooted:
        project: "android"
        instance: "<ROOTED_INSTANCE_ID>"
    baselines:
      "<NON_ROOTED_INSTANCE_ID>":
        rooted: false
        os_version: "16.0.0"
        osbuild: "r2 user"
        snapshot_id: "<SNAPSHOT_ID>"
      "<ROOTED_INSTANCE_ID>":
        rooted: true
        os_version: "16.0.0"
        osbuild: "r2 userdebug"
        snapshot_id: "<SNAPSHOT_ID>"
```

- `roles.<role>`: `non_rooted` and/or `rooted`, each with a required `project` (name or id), an optional `instance` (exact id preferred; a name must be unique), an optional `os_version` prefix, and optional `adb_address` or `adb_port` overrides for that VM. Two roles never pin the same instance. A role that is not listed blocks only the runs whose risks need it.
- `baselines`: the automation inventory, keyed by **exact instance id**. Only instances listed here can be selected, pinned or not. Each entry declares `rooted`, the instance's exact `os_version` and `osbuild` as Corellium reports them, and exactly one of `snapshot_id` or `snapshot_name`. `rooted` must match the build variant in `osbuild` (`userdebug` and `eng` are rooted), and every listed role needs at least one baseline of its rooted state. A name must match one snapshot of that instance; identical names on two instances are fine.
- `auto_start` and `auto_stop` must both be written out as `true`: every run starts what it restores and confirms it off.
- `boot_timeout_seconds` bounds each power-on and shutdown and `restore_timeout_seconds` each restore; both default to `600`.
- `app_store` must be `apk`. Target apps stay outside the baselines, so each run installs the app's highest-version APK from `artifacts/intake/android/apks/` on every VM that tests it.
- `project`, `rooted`, `instance` and `os_version` at the top of `device.corellium` belong to the single-instance form and are rejected next to `roles`.

A role's VM is chosen only from the `baselines` entries whose snapshot exists, belongs to that instance and matches it. When the role's `instance` is unset, an idle running instance is preferred, then a paused one, then one that is off; a pinned `instance` never falls back to another VM.

**Moving from a single instance.** Prepare and record a baseline for the instance (below), move its `project`, `instance` and `os_version` under `roles.non_rooted` (or `roles.rooted` when `rooted: true`), drop the top-level `rooted`, add the instance to `baselines`, set `auto_stop: true` and `app_store: "apk"`, and put each app's APK in the intake folder. Add a `rooted` role with its own VM and baseline when risks that need root should run. `device.provider: physical` ignores `device.corellium` entirely, so physical runs are unchanged by either form.

**A physical phone with a rooted VM.** A physical phone can serve the `non_rooted` role while a Corellium VM serves `rooted`, so every risk that does not need root runs on the phone, for example because the app needs Google Play services, which stock non-rooted VMs lack. List the role in `device.physical_roles`, name the phone in `adb_serial`, and keep only the VM's role under `corellium.roles`:

```yaml
device:
  provider: "corellium"
  app_store: "apk"
  adb_serial: "wifi"              # or the phone's USB serial, or host:port
  physical_roles: ["non_rooted"]
  corellium:
    auto_start: true
    auto_stop: true
    roles:
      rooted:
        project: "android"
        instance: "<ROOTED_INSTANCE_ID>"
    baselines:
      "<ROOTED_INSTANCE_ID>":
        rooted: true
        os_version: "16.0.0"
        osbuild: "r2 userdebug"
        snapshot_id: "<SNAPSHOT_ID>"
```

`physical_roles` accepts only `non_rooted`, needs `provider: corellium` with `corellium.roles`, and a role is served by the phone or a VM, never both. `adb_serial` is required and must not be a VM's `adb_address`. With `wifi`, the phone's Wi-Fi connection is found as described under [Device](#device), and a Corellium VM's adb address is never taken for it.

For each run, in order:

1. The run takes the [phone lock](#one-user-of-the-phone-at-a-time) and connects the phone. It must be authorized, booted and on a `user` build. A missing, unapproved, rooted or busy phone fails the run before any VM is claimed or restored. A phone that has not approved this host, over USB or Wi-Fi, fails within seconds with the steps to approve it; the run never waits for or taps the prompt.
2. The run records the apps already on the phone. Only the rooted VM is claimed, restored and started; a run whose risks all run on the phone never contacts Corellium.
3. A copy of the app already on the phone is uninstalled, which deletes its data. The intake APK is then installed on the phone and the VM, and each must report the APK's version.
4. Risks run on their own device. Each result's `metadata.device` names `device_type` (`physical_phone` or `corellium_vm`) and the adb `serial`, plus the phone's model or the VM's instance and baseline.
5. At the end, even after a failure or Ctrl-C, every app that was not on the phone before the run is uninstalled: the test build, a replaced copy, the screen-capture helper and Appium's helper apps. Apps that were already there stay. The phone is never disconnected. The VM is confirmed off independently.

`install-app` and `POST /config/android/apps/{app_id}/install` install on the phone by default and keep the app there, after uninstalling any earlier copy; `--role rooted` or `?role=rooted` uses the VM. Readiness checks the phone with read-only adb calls and never connects it, switches it to Wi-Fi or installs on it. When the phone's connection is `unauthorized`, readiness says the phone is waiting to allow debugging from this host, and the dashboard asks for the prompt to be approved with **Always allow**. Use a dedicated test phone: risks drive its screen, record it and can grant permissions, and the screen recordings and evidence stay on this host.

**Preparing a baseline.** A baseline is a snapshot of one specific VM, taken after:

- Android setup is complete, with a consistent language and orientation and no screen-lock credential;
- USB debugging is enabled and this host's adb key (`$ANDROID_USER_HOME/adbkey.pub`, else `~/.android/adbkey.pub`) is authorized, so no prompt appears;
- required companion apps, stable test certificates and supporting configuration are installed, including Google Play services when the apps under test need them;
- recordings, collected input and earlier test data are cleared;
- Google Play services has been updated and then frozen at that version, with its base APK compiled, so a restored VM does not make the install wait behind an update or a compilation (see below);
- no Appium, capture or interception session is active, and the target apps are not installed.

On a Google apps VM, `artd` stops any `dex2oat` run after 570 s. Play services' shared base APK has no usable profile, so ART compiles it with the `speed` filter, which takes longer than that and fails, both after a Play services update and in the daily background compilation job. Freeze and compile Play services before taking the snapshot:

1. Let Play services update through the Play Store, and wait until `adb shell dumpsys package com.google.android.gms` reports the new `versionCode`.
2. Disable the Play Store so Play services stays at that version: `adb shell pm disable-user --user 0 com.android.vending`. Play services then falls behind over time; enabling the Play Store again brings back the update and its failing compile.
3. Compile the Play services base with the `verify` filter, which finishes in a few minutes: `adb shell cmd package compile -m verify --primary-dex com.google.android.gms`. It prints `Success`, and `adb shell dumpsys package dexopt` then shows the base at `[status=verify]`.
4. Reboot (`adb reboot`) and check for about ten minutes that nothing compiles: `adb shell ps -A -o ARGS` lists no `dex2oat` or `dexoptanalyzer`, and the base is still at `verify`. Then stop the VM and take the snapshot.

The daily background job still targets `speed` for the base, so each run disables it on every VM it connects (see [runs, installs and readiness](../corellium-lifecycle.md#runs-installs-and-readiness)); the setting lasts until the next boot and is not part of the snapshot.

An existing snapshot such as `automation-ready-v1` can serve its own instance once it has been checked against this list; the second VM needs its own snapshot. Creating or replacing a baseline is maintenance work done by hand in Corellium: keep the original snapshot, the active prepared baseline and the previous prepared baseline for each instance, and update `baselines` only after the replacement has passed [qualification](corellium-qualification.md). Runs restore snapshots and never create or delete them.

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

UiAutomator2 sessions need `ANDROID_HOME` or `ANDROID_SDK_ROOT` in Appium's environment. When an Android run, `install-app` or the API's Android install starts Appium and neither variable is set, Appium gets both set to the SDK that holds `device.adb_path`: the path (or `adb` on `PATH`) is resolved through symlinks, and the SDK root is the parent of its `platform-tools/` directory. A variable that is already set is passed on unchanged. If neither is set and adb is missing or not inside a `platform-tools/` directory, Appium is not started and the run fails with a message saying so; export `ANDROID_HOME` or point `device.adb_path` at `<sdk>/platform-tools/adb`. An Appium that is already running keeps the environment it was started with, including one the API server starts from the iOS config.

## Runner

```yaml
runner:
  auto_grant_permissions: false
  keep_screen_on: false
```

Before every Android test that uses the device, the runner wakes the device's screen and dismisses its lock screen over adb (`input keyevent KEYCODE_WAKEUP`, then `wm dismiss-keyguard`), since those risks drive the device's UI. `android-feature-01-risk-01` reads APKs from the intake folder and does not use the device, so it skips this and the permission grant, and a run of only that risk connects to no device. This only works without a PIN, password or pattern; if the lock screen is still showing afterwards, the test fails with `the device is still locked after waking it` and does not run.

`runner.keep_screen_on: true` also sets the device to keep its screen on whenever it is charging, from any power source (`adb shell svc power stayon true`), when a run connects to it, so the screen does not time out between tests. It changes a device setting that stays after the run; `adb shell svc power stayon false` turns it off again.

`runner.work_dir` is optional. Unset, it follows `WORK_DIR` to `<work>/android`; a relative value resolves against the repository root, never the working directory the run started from.

## Tools

```yaml
tools:
  mobsf_url: "http://localhost:8000"
  mobsf_api_key: ""
  burp_proxy: "http://127.0.0.1:8080"
```

`mobsf_url` and `mobsf_api_key` are the defaults for `android-feature-01-risk-01` when its `apk_static_analysis.analyzer` sets no `mobsf_url` or `api_key`, so MobSF is configured in one place. `burp_proxy` is reserved for a future proxy integration; no Android risk reads it.

## Apps

```yaml
apps:
  - id: "example_app"
    name: "Example App"
    package_name: "com.example.app"
    risks:
      android-feature-01-risk-01:
        enabled: true
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

When using the legacy shape, every Android risk is enabled for each package.

Configure each package once. The app's `id` is its identity on the dashboard,
and its backed-up APKs live in a work folder named after the package, so two
entries for one package would share that folder and appear as two apps. Adding
an app through `POST /config/android/apps` refuses a package that is already
configured and names the existing app. The `id` may match an iOS app's `id`; the
dashboard keeps one application per `id` and platform.

Each app can also carry `sector`, `agency`, `version`, and `cisos` (a list of `{name, email}`) — organizational metadata a dashboard displays alongside an app's findings. None of this is read by the automation run itself; all four are optional and default to blank/empty, and can be set here directly or through `GET`/`PUT /config/android/apps/{app_id}` (see [HTTP API](../api.md#editing-config)).

`requires_play_services` (`true` or `false`, optional) says whether the app needs Google Play services. When it is unset, the app's intake APK manifest decides. A device risk for an app that needs them is not run on a device without them; see [Google Play services](risks.md#google-play-services).

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

   See `configs/split/android/repackaging.example.yaml`, `screen_capture.example.yaml`, `apk_static_analysis.example.yaml` and `private_data.example.yaml` for the rest of these fields, and the field references below for what each controls.
3. Only add more fields under the app's own `risks.<risk_id>` entry when this one app needs to differ from those shared defaults — nest just the field being changed. Anything left unset there falls back to the global file.

Risks run in the order they're listed under an app's `risks` mapping, not sorted by ID — keep that in mind if the order of your own `risks:` blocks matters to you.

### APK static analysis (`android-feature-01-risk-01`)

Global defaults live under a top-level `apk_static_analysis` block and can be overridden per app under `risks.android-feature-01-risk-01`. The block has the same shape as iOS's `ipa_static_analysis` and is merged recursively, so an app can override one nested field such as `analyzer.provider`:

```yaml
apk_static_analysis:
  analyzer:
    provider: "mobsf"
    mobsf_url: null
    api_key_env: "MOBSF_API_KEY"
    timeout_seconds: 120
    auto_start:
      enabled: false
      command: []
      wait_seconds: 90
      stop_after_scan: false
      generate_api_key: true
    fallback_to_builtin: true
  sensitive_scan:
    reveal_values: false
  api_key_reuse_test:
    enabled: true
    provider: "google_geocode"
    timeout_seconds: 5
    max_keys: 5
    test_address: "Singapore"
```

- `analyzer.provider`: `mobsf` to scan the base APK with MobSF, or `builtin` for the built-in scanner. Unset, the built-in scanner runs.
- `analyzer.mobsf_url` / `analyzer.api_key`: the MobSF server and key. Unset, they come from `tools.mobsf_url` and `tools.mobsf_api_key`, and the key then from the environment variable `analyzer.api_key_env` names (`MOBSF_API_KEY`).
- `analyzer.auto_start`, `analyzer.timeout_seconds`: start MobSF with `command` when it is not reachable, and how long each MobSF request may take, as for [iOS](../ios/configuration.md).
- `analyzer.fallback_to_builtin`: when MobSF fails, run the built-in scanner instead of failing the test.
- `sensitive_scan.reveal_values`: report secrets in full instead of masked. Leave it false unless the reports stay on this workstation.
- `api_key_reuse_test`: test Google API keys found in the APK against the Geocoding API from the workstation (an outbound request per key, up to `max_keys`).

The APK is always the one in `artifacts/intake/android/apks/` with the app's package name; see [Risks](risks.md#android-feature-01-risk-01).

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
  permission_prompt_wait: 2
  max_permission_prompts: 3
```

- `work_dir` (optional): root directory for backed-up, decoded, and rebuilt APKs (per app, under `<work_dir>/<package_name_with_underscores>/`). Unset, it is `<work>/android/repackaging`; a relative value resolves against the repository root.
- `keystore_path`: path to a debug/release keystore used to re-sign the repackaged APK; a relative value resolves against the repository root. If unset, a keystore is generated at `<work_dir>/<app>/../release.keystore` via `keytool` on first use.
- `keystore_alias` / `keystore_pass`: alias and password used when generating the keystore and signing with `apksigner`.
- `restore_original_after_test`: whether to replace the repackaged build with the original after validation, from the APKs backed up before the test, so later tests run against the same build, and with the install method that `device.app_store` selects only if that fails.
- `apkeditor_jar` (optional): path to the APKEditor jar used to merge split APKs before decoding; a relative value resolves against the repository root. Unset, the newest `APKEditor*.jar` under `tools/vendor/apkeditor/` is used.
- `launch_wait` / `post_click_wait` / `record_lead_in` / `record_tail`: pacing for the post-repackaging Appium launch validation and its screen recording.
- `permission_prompt_wait` / `max_permission_prompts`: seconds to wait after denying a system permission prompt that covers the app, and how many prompts to deny at each foreground check before the validation is inconclusive.

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

### Private data (`android-feature-07-risk-01`)

Global defaults live under a top-level `private_data` block in `private_data.yaml` under `configs/split/android/` (see `private_data.example.yaml`), and can be overridden per app under `risks.android-feature-07-risk-01`:

```yaml
private_data:
  launch_wait: 5
  max_archive_mb: 200
  reveal_values: false
  keep_archive: false
```

- `launch_wait`: seconds the app stays open after launch so it writes its data, before it is stopped and its directories are copied (default 5).
- `max_archive_mb`: the largest private data size, in MiB, that is copied off the device; a larger app fails the run (default 200).
- `reveal_values`: report sensitive values in full instead of masked (default false).
- `keep_archive`: keep the pulled archive under `artifacts/work/android/private_data/<package>/` instead of deleting it after the scan (default false). It holds the app's real data and is never copied into the report. Kept archives are not pruned with old run folders, so remove them by hand.

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
  apk_static_analysis: split/android/apk_static_analysis.yaml
  repackaging: split/android/repackaging.yaml
  screen_capture: split/android/screen_capture.yaml
  private_data: split/android/private_data.yaml
  apps: split/android/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/android/`. Inline values and included sections can coexist in the same file — nothing requires every section to go through `include:`, which is how `device`/`runner` stay inline while the rest are pulled in. See `configs/android.example.yaml`, the tracked example of this entry-point file.

The real tools/apk_static_analysis/repackaging/screen_capture/private_data/app-roster content lives in `configs/split/android/*.yaml` (all git-ignored, since they contain real app config — only the `*.example.yaml` files under `configs/split/android/` are tracked). `configs/android.yaml` itself is also git-ignored, since it holds the real device config.

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file, the same as for iOS. Existing shell environment variables are not overwritten. `android-feature-01-risk-01` reads the MobSF API key from `MOBSF_API_KEY` (or the variable `analyzer.api_key_env` names) from the repository `.env` when neither `analyzer.api_key` nor `tools.mobsf_api_key` sets one. The Corellium client also reads its two settings directly from that file without exporting them. `device.provider: corellium` needs `CORELLIUM_API_URL` and `CORELLIUM_API_TOKEN`; see [Corellium virtual device](#corellium-virtual-device).
