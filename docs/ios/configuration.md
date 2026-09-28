# iOS Configuration

The iOS config lives at `configs/ios.yaml`. `device` and `runner` are written inline in that entry-point file — there's only ever one device and one runner profile per project — while the app roster and per-risk settings, which don't fit comfortably in one file, live in their own files under `configs/split/ios/` and are pulled in via `include:`.

## Quickstart

Set up the split config from the tracked examples:

```bash
cp configs/ios.example.yaml configs/ios.yaml
cp configs/split/ios/apps.example.yaml configs/split/ios/apps.yaml
for f in ipa_static_analysis traffic_interception keystroke_collection; do cp configs/split/ios/risk_settings.example.yaml "configs/split/ios/$f.yaml"; done
```

`risk_settings.example.yaml` shows all three risks' settings together in one file for easier reading; trim each copy above down to just its own top-level key (`ipa_static_analysis:` in one, `traffic_interception:` in another, `keystroke_collection:` in the third) — see [Global Risk Settings](#global-risk-settings).

The config contains `device`, `runner`, `ipa_static_analysis`, `traffic_interception`, `keystroke_collection`, and `apps` sections — the first two inline, the rest via `include:`.

## Device

Minimum device shape:

```yaml
device:
  udid: "REPLACE_WITH_DEVICE_UDID"
  team_id: "REPLACE_WITH_APPLE_TEAM_ID"
  appium_server_url: "http://127.0.0.1:4723"
  xcode_signing_id: "Apple Development"
  keep_wda: true
```

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

`python -m mobile_playbook.api` also checks this setting before starting the API server. If that backend process starts Appium, it stops its Appium process group during graceful shutdown; an Appium instance that was already running remains untouched. Cleanup cannot be guaranteed after `SIGKILL`, power loss, or a system crash.

### Automatic unlock

Same two points — right after connecting, and again before every test — also check whether the device's screen is locked and unlock it if so, logged as a `device_unlocked` event in that run's `events.jsonl` when it actually had to do anything. This is a no-op (and reported as `was_locked: false`) if the screen was already unlocked.

This only actually unlocks a device with **no passcode/Face ID/Touch ID set** — Appium can't enter a passcode or biometric on a real device, so a secured device still needs a person to unlock it before/during a run. If the device also has Auto-Lock disabled (Settings > Display & Brightness > Auto-Lock > Never), the screen won't lock from idling during a run's long waits in the first place, which avoids the problem rather than just recovering from it.

If a test still fails outright (an exception escapes a risk's own error handling — a genuine bug or device hiccup, not that risk's normal failure path), `run_test` checks whether the device was actually locked at that moment before giving up: if so, it unlocks and retries that one test exactly once, and only records a failure if the retry fails too. This only ever catches a failure that already escaped a risk's own try/except entirely; risks like `ios-feature-02-risk-01`/`ios-feature-04-risk-01` that catch their own errors and report `Inconclusive`/`FAILED` themselves won't trigger this retry, since nothing propagates up to see.

## Runner

The `runner` section controls install timing, run order, workspace location, and permission prompts:

```yaml
runner:
  sequential: true
  uninstall_after_each_test: true
  app_install_timeout_ms: 480000
  launch_wait_seconds: 5
  work_dir: "work/ios"
  permission_alerts:
    enabled: true
    action: "dismiss"
    wait_seconds: 2
    max_alerts: 3
```

## Apps

Each iOS app entry contains identity, artifact source, launch expectations, and enabled risks:

```yaml
apps:
  - id: "example_app"
    name: "Example App"
    bundle_id: ""
    test_bundle_id: ""
    artifact:
      source: "local_ipa"
      ipa: "intake/ios/ipas/example_app.ipa"
      workspace_dir: "work/ios/acquired"
      expected_bundle_id: ""
    expected_behavior:
      app_state_must_be_foreground: true
      source_contains: []
      source_not_contains: []
    risks:
      ios-feature-01-risk-01:
        enabled: true
      ios-feature-04-risk-01:
        enabled: false
```

For `local_ipa`, `bundle_id`, `test_bundle_id`, and `artifact.expected_bundle_id` can be left blank. The framework reads `Payload/*.app/Info.plist` from the IPA and fills them in at runtime when possible.

The framework no longer retrieves IPAs from the App Store. Obtain each IPA yourself and point `artifact.ipa` at the local file.

Each app can also carry `sector`, `agency`, `version`, and `cisos` (a list of `{name, email}`) — organizational metadata a dashboard displays alongside an app's findings. None of this is read by the automation run itself; all four are optional and default to blank/empty, and can be set here directly or through `GET`/`PUT /config/ios/apps/{app_id}` (see [HTTP API](../api.md#editing-config)).

See the one app entry under `apps:` in [configs/split/ios/apps.example.yaml](../../configs/split/ios/apps.example.yaml) for a full, copyable app block including both risk blocks.

## Risk Blocks

iOS risk IDs are prefixed `ios-feature...`. To configure a risk for an app:

1. Add it under the app's `risks` mapping with `enabled: true` (or leave it `false`/omitted to skip it):

   ```yaml
   risks:
     ios-feature-01-risk-01:
       enabled: true
   ```

2. Its actual settings come from that risk's global settings file, shared by every app that enables it — here's the start of `ipa_static_analysis.yaml`, taken directly from `configs/split/ios/risk_settings.example.yaml`:

   ```yaml
   ipa_static_analysis:
     analyzer:
       provider: "mobsf"
       mobsf_url: "http://127.0.0.1:8000"
       api_key_env: "MOBSF_API_KEY"
       timeout_seconds: 120
       auto_start:
         enabled: false
         command: []
         wait_seconds: 90
         stop_after_scan: false
         generate_api_key: true
       fallback_to_builtin: true
   ```

   See `configs/split/ios/risk_settings.example.yaml` for the rest of this and `repackaging`/`traffic_interception`/`screen_capture`/`keystroke_collection`'s fields, and [Risks](risks.md) for what each field controls.
3. Only add more fields under the app's own `risks.<risk_id>` entry when this one app needs to differ from those shared defaults — nest just the field being changed. Anything left unset there falls back to the global file; see [Global Risk Settings](#global-risk-settings) for how the two are merged.

### Repackaging

`ios-feature-01-risk-02` reads its shared defaults from the `repackaging` global settings block:

```yaml
repackaging:
  frida:
    dylib_path: "tools/Frida/FridaGadget.dylib"
    config_path: "tools/Frida/FridaGadget.config"
    script: null
    attach_timeout_seconds: 15
  insert_dylib_path: null
  resign:
    identity: null
    provisioning_profile: null
  tamper_markers:
    - "Tamper detected"
    - "Integrity check failed"
    - "Jailbreak detected"
  exercise:
    sample_window_seconds: 15
    sample_interval_seconds: 1
    tap_text_field: false
    button_label_contains: []
  restore_original_after_test: true
```

`frida.dylib_path`/`frida.config_path` are the gadget injected into the bundle, resolved relative to the repository root (override to point elsewhere). `frida.script` is an optional Frida JS file loaded once the repackaged build is running: attaching to the gadget and running it is the dynamic proof that the injected code loaded, and is required before an `At Risk` verdict — a failed attach leaves the run `GADGET_ATTACH_FAILED` (Inconclusive), so `frida` must be installed for a real device run. `insert_dylib_path` is optional: leave it `null` and the tool is used from its vendored location `tools/insert_dylib/`, resolved by repository-relative path with no PATH entry required (set an absolute path only to override).

`resign.identity` is the `codesign` identity; leave it `null` and it is resolved from `device.team_id` by keychain lookup (the SHA-1 of the team's Apple Development certificate). `resign.provisioning_profile` is the `.mobileprovision` used to embed entitlements and re-sign for the device; leave it `null` and a matching profile is auto-discovered from `~/Library/MobileDevice/Provisioning Profiles` (the first whose team, `application-identifier`, provisioned devices, expiry, and signing certificate match this app, device, and identity). A valid profile for the target bundle ID is an operational prerequisite; when none is configured or discovered the run is `RESIGN_FAILED` (Inconclusive). `tamper_markers` are merged with the app's `expected_behavior.source_not_contains` for the UI scan. `exercise` controls the sampling window and the sensitive path driven on both passes, and `restore_original_after_test` reinstalls the clean build afterward. See [Risks](risks.md#ios-feature-01-risk-02) for the evidence channels and the three-way verdict.

`resign.rewrite_bundle_id` (optional): install the repackaged build under a single bundle id you own a development profile for, instead of the app's real id. This is the free-account path — one registered App ID is reused for every app: each app is renamed to it, tested, then uninstalled so the id is free for the next. It avoids the free-account App-ID quota, but because the app runs under a different identity, a launch failure may be the rename rather than a tamper defense — so verify any `Reduced Risk` against the baseline evidence. Leave `null` to keep each app's real id (needs a wildcard profile / paid account).

### Traffic interception

`ios-feature-02-risk-01` configures the selected Wi-Fi network's proxy through the iOS Settings UI immediately before the risk and applies the configured proxy cleanup afterward. Enabling the risk means resistance to the locally trusted interception CA is expected; installing the CA is a test prerequisite, not itself a vulnerability (see [Risks](risks.md#ios-feature-02-risk-01)).

```yaml
traffic_interception:
  burp:
    proxy_url: "http://127.0.0.1:8081"
    capture_path: "artifacts/work/ios/traffic_interception/capture.jsonl"
    health_max_age_seconds: 300
  device_setup:
    mode: "appium_ui"
    wifi_ssid: "REPLACE_WITH_TEST_WIFI_NAME"
    proxy_host: "auto"
    proxy_port: 8081
    ca_download_url: "http://burp/cert"
    ca_display_name: "PortSwigger CA"
    configure_ca_if_missing: true
    proxy_cleanup_mode: "off"
    navigation_timeout_seconds: 20
  expected_hosts:
    - "api.example.com"
  capture_timeout_seconds: 30
  exercise:
    exercise_wait_seconds: 20
    accessibility_ids: []
```

`burp.proxy_url` is checked from the Mac before the risk runs, so a loopback URL is appropriate when Burp runs locally. `device_setup.proxy_host` controls the address written to the iPhone: `auto` detects the Mac's current routable LAN address for every run, while a loopback, unspecified, or link-local address is rejected. `collection.advertised_host` under `keystroke_collection` accepts the same values. `wifi_ssid` must name the test network, `proxy_port` must match a Burp listener bound to all interfaces, `ca_download_url` and `ca_display_name` identify Burp's CA, `configure_ca_if_missing` controls whether a missing CA is downloaded and installed, `proxy_cleanup_mode` selects `off` to disable the proxy after the risk or `restore` to reinstate the original proxy settings, and `navigation_timeout_seconds` bounds each Settings UI navigation step.

The iPhone must use the English UI unless localized selectors are added, must have no passcode, and must already trust WebDriverAgent. Burp must be running with its listener bound to all interfaces. Certificate installation is attempted only when the configured CA is not already fully trusted. Settings accessibility labels can change between iOS releases, so selector updates may be required after an iOS upgrade. Navigation failures save a screenshot and page source in the risk report directory.

`burp.capture_path` is optional: unset, it follows `WORK_DIR` to `artifacts/work/ios/traffic_interception/capture.jsonl`, and a value set here resolves against the repository root rather than the working directory the run started from. `burp.health_max_age_seconds` defaults to 300 when omitted and rejects negative values. A successful `tools/check_burp_interception.py` canary writes a sibling health record by replacing the capture suffix with `.health.json` (for example, `capture.jsonl` becomes `capture.health.json`). Before a selected traffic-interception risk runs, preflight compares that record's proxy, path, device, and age. These warnings do not abort the run.

`expected_hosts` filters `capture_path`'s entries down to this app's own traffic. Each value is normalized from a hostname or URL, then matches only that exact hostname or its subdomains; `api.example.com.evil.test` and `notapi.example.com` do not match `api.example.com`. An empty list accepts any newly captured valid HTTPS exchange and can therefore attribute unrelated device traffic to the app, so configure the application's exact hosts for a meaningful result. Only entries explicitly marked `https` can produce `RISK_EXISTS`; plain HTTP and entries from an outdated extension that omit `scheme` cannot. The capture extension stores exchange metadata and body lengths, not raw headers, cookies, authorization values, request bodies, or response bodies.

A device-wide proxy can also intercept iOS's own traffic to Apple's certificate/app-verification servers, which WebDriverAgent needs to confirm its Developer App certificate. The existing PAC endpoint remains available as an optional manual alternative that routes Apple service domains directly while proxying other traffic through Burp.

The API server generates this PAC file for you from the current `traffic_interception.burp.proxy_url` — no separate file to host or keep in sync:

```
GET /platforms/ios/traffic-interception/proxy.pac
```

For manual PAC setup, use Settings > Wi-Fi > Configure Proxy > Automatic and enter `http://<this-machine's-IP>:8080/platforms/ios/traffic-interception/proxy.pac`. If `burp.proxy_url` is loopback, the endpoint substitutes the detected LAN IP; use `?proxy_host=<ip>` when that detection is unsuitable.

### Screen capture

`ios-feature-03-risk-01` reads its shared defaults from the `screen_capture` block in `configs/split/ios/screen_capture.yaml`. `recorder_app` describes the ReplayConsentRecorder companion app and `capture` describes the capture window (see [Risks](risks.md#ios-feature-03-risk-01)):

```yaml
screen_capture:
  recorder_app:
    bundle_id: "com.example.ReplayConsentRecorder"
    ipa: "artifacts/companion/ios/ipas/ReplayConsentRecorder.ipa"
    install: true
    uninstall_after_test: true
    require_clean_state: true
    resign:
      enabled: true
      timeout_seconds: 900
    sign_in:
      username_accessibility_id: "recorder-username"
      password_accessibility_id: "recorder-password"
      username: "tester"
      password: "tester"
    start_button_accessibility_id: "start-phone-recording"
    export_button_accessibility_id: "export-evidence"
    start_broadcast_labels: ["Start Broadcast", "Start Recording"]
    stop_broadcast_labels: ["Stop Broadcast", "Stop Recording", "Stop"]
    sheet_timeout_seconds: 10
    countdown_seconds: 4
    evidence_documents_path: "Evidence"
    export_timeout_seconds: 30
  capture:
    capture_window_seconds: 12
    canary_prefix: "SCR"
    type_secure_canary: true
    ocr_provider: "vision"
    video_frame_fallback: true
    video_frame_interval_seconds: 1
    max_pull_bytes: 209715200
    input:
      method: "send_keys"
    text_field:
      accessibility_id: null
    auto_navigation:
      enabled: true
      max_steps: 4
      settle_seconds: 1
      allow_any_button: false
      accessibility_ids: []
      button_label_contains: ["Log in", "Login", "Sign in", "Continue", "Next", "Get started"]
      exclude_button_label_contains: ["Delete", "Remove", "Cancel", "Log out", "Sign out", "Pay", "Purchase"]
```

`recorder_app`:

- `bundle_id` / `ipa`: at least one is required. A missing `bundle_id` is read from the IPA. Outside a dry run, a configured `ipa` must exist. Companion IPAs live under `artifacts/companion/ios/ipas/`.
- `install`: install the IPA at the start of the risk. With `false`, the recorder must already be installed under `bundle_id`.
- `uninstall_after_test`: remove the recorder afterwards, even when an earlier crashed run left it behind.
- `require_clean_state`: remove a recorder that is already installed before the run. Uninstalling also wipes its App Group, so old recordings cannot match this run's canary. When removal fails, the result is `DIRTY_STARTING_STATE`.
- `resign.enabled` / `resign.timeout_seconds`: re-sign the IPA with `tools/localkeyboard_resign/resign.py` when its profile has expired or the install is rejected for its signature (see [Reports And Troubleshooting](reports-and-troubleshooting.md#troubleshooting)).
- `sign_in`: accessibility IDs and dummy values for the recorder's own sign-in fields. They only enable the recorder's start button and are not credentials for anything else.
- `start_button_accessibility_id`: the recorder button that opens the iOS broadcast sheet, both to start and to stop.
- `export_button_accessibility_id`: the recorder button that copies the evidence into its Documents folder.
- `start_broadcast_labels` / `stop_broadcast_labels`: labels tried on the iOS broadcast sheet. `sheet_timeout_seconds` bounds the wait for them.
- `countdown_seconds`: wait after **Start Broadcast** for the iOS countdown to finish.
- `evidence_documents_path`: the folder under the recorder's Documents that is pulled through Appium.
- `export_timeout_seconds`: how long to keep pulling until the recorder's `done.json` appears.

`capture`:

- `capture_window_seconds`: how long the canary stays on screen. Must be greater than 0.
- `canary_prefix`: the start of the plain canary. The run timestamp supplies the unique suffix.
- `type_secure_canary`: also type a `PWD…` canary into a password field when the screen has one.
- `ocr_provider`: `vision` (macOS Vision through pyobjc) or `none`. `none` skips OCR, reports `OCR_UNAVAILABLE`, and relies on the attached video.
- `video_frame_fallback` / `video_frame_interval_seconds`: when no recorder screenshot falls inside the window, pull still frames from the `.mp4` at this interval.
- `max_pull_bytes`: the largest evidence folder accepted from the device. Anything larger is `RECORDING_RETRIEVAL_FAILED`.
- `input`, `text_field` and `auto_navigation`: how the canary is typed and how a text field is found. They mean the same as the equivalent `collection` settings under `keystroke_collection`.

### Keystroke collection

`ios-feature-04-risk-01` runs a command-and-control server on the Mac that the keyboard app on the iPhone connects back to. `collection.bind_host` is what that server binds to and stays `0.0.0.0` so the iPhone can reach it; `collection.advertised_host` is only the address typed into the keyboard app's server-URL field, and the two are not interchangeable.

`collection.advertised_host` takes the same values as `device_setup.proxy_host`: `auto` detects the Mac's current routable LAN address for every run, an explicit address is used as written, and a loopback, unspecified, or link-local address is rejected. A value still left as `REPLACE_WITH_MAC_LAN_IP`, or left empty, falls back to the URL the server bound to. An unreachable value here surfaces later as a pairing timeout rather than an immediate error, which is why the rejection rules are strict.

## Split iOS Configs

`configs/ios.yaml` has `device` and `runner` written inline, plus an `include:` mapping (section name → file path) for the sections that live under `configs/split/ios/`:

```yaml
device:
  udid: "..."
  # ...

runner:
  sequential: true
  # ...

include:
  ipa_static_analysis: split/ios/ipa_static_analysis.yaml
  traffic_interception: split/ios/traffic_interception.yaml
  keystroke_collection: split/ios/keystroke_collection.yaml
  screen_capture: split/ios/screen_capture.yaml
  apps: split/ios/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/ios/`. Each included file may contain either the raw section value or a mapping wrapped under the section name. Inline values in the entry point override included values (this is also how `device`/`runner` can stay inline while other sections are included — nothing requires every section to go through `include:`). See `configs/ios.example.yaml`, the tracked example of this entry-point file.

### Global Risk Settings

`ipa_static_analysis`, `repackaging`, `traffic_interception`, `screen_capture`, and `keystroke_collection` each hold one risk's shared default settings — the analyzer config for `ios-feature-01-risk-01`, the repackaging config for `ios-feature-01-risk-02`, the Burp proxy config for `ios-feature-02-risk-01`, the screen-recorder config for `ios-feature-03-risk-01`, the keyboard-collection config for `ios-feature-04-risk-01` — used by every app that enables that risk. An app's own `risks.<risk_id>` entry in `apps.yaml` only needs `enabled: true`; any field nested under it there overrides the shared default for that app alone, merged recursively (so, for example, an app can override just `collection.auto_navigation.accessibility_ids` without repeating the rest of `collection`). See [Risks](risks.md) for what each field controls.

`configs/split/ios/risk_settings.example.yaml` shows these risks' settings together in one file for easier reading, but the real (git-ignored) config keeps them as separate files, one per risk, matching the `include:` map above.

### Splitting Out Shared Templates

A section's include value can also be a **list** of paths instead of one path:

```yaml
include:
  apps:
    - split/ios/templates.yaml
    - split/ios/apps.yaml
```

Listed files are read and concatenated as raw text, in that order, then parsed as a single YAML document — not loaded and merged separately. This matters because YAML anchors (`&name`/`*name`) only resolve within one parsed document: if `templates.yaml` and `apps.yaml` were parsed independently, `apps.yaml`'s `<<: *local_ipa_artifact` aliases would fail with an undefined-anchor error. Concatenating the raw text first is what lets `templates.yaml` define reusable `x-*` blocks (artifact source, expected-behavior checks) that `apps.yaml`'s app entries reference, while still keeping the two concerns — reusable templates vs. the actual app roster — in separate files.

`configs/ios.yaml` in this project uses exactly this: `device`/`runner` are inline, `ipa_static_analysis`/`keystroke_collection` are each split into one file, and `apps` is split across two — `configs/split/ios/templates.yaml` (the `x-*` anchors) and `configs/split/ios/apps.yaml` (the 11 app entries, referencing those anchors). All of `configs/split/ios/ipa_static_analysis.yaml`, `keystroke_collection.yaml`, `screen_capture.yaml`, `templates.yaml`, and `apps.yaml` are git-ignored, since they contain a real app roster — only the `*.example.yaml` files under `configs/split/ios/` are tracked (`configs/ios.yaml` itself is also git-ignored, since it holds the real device UDID).

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file. Existing shell environment variables are not overwritten.

Example:

```bash
cp .env.example .env
```

```bash
MOBSF_API_KEY="REPLACE_WITH_MOBSF_API_KEY"
```

`MOBSF_API_KEY` is used by `ios-feature-01-risk-01` when `analyzer.provider: mobsf` and MobSF auto-start is not generating its own temporary key.
