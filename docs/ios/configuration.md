# iOS Configuration

The iOS config lives at `configs/ios.yaml`. `device` and `runner` are written inline in that entry-point file — there's only ever one device and one runner profile per project — while the app roster and per-risk settings, which don't fit comfortably in one file, live in their own files under `configs/split/ios/` and are pulled in via `include:`.

## Quickstart

Set up the split config from the tracked examples:

```bash
cp configs/ios.example.yaml configs/ios.yaml
cp configs/split/ios/apps.example.yaml configs/split/ios/apps.yaml
for f in ipa_static_analysis keystroke_collection; do cp configs/split/ios/risk_settings.example.yaml "configs/split/ios/$f.yaml"; done
```

`risk_settings.example.yaml` shows both risks' settings together in one file for easier reading; trim each copy above down to just its own top-level key (`ipa_static_analysis:` in one, `keystroke_collection:` in the other) — see [Global Risk Settings](#global-risk-settings).

The config contains `device`, `runner`, `ipa_static_analysis`, `keystroke_collection`, and `apps` sections — the first two inline, the rest via `include:`.

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
      ipa: "intake/ios/ipas/Example_App.ipa"
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

   See `configs/split/ios/risk_settings.example.yaml` for the rest of this and `keystroke_collection`'s fields, and [Risks](risks.md) for what each field controls.
3. Only add more fields under the app's own `risks.<risk_id>` entry when this one app needs to differ from those shared defaults — nest just the field being changed. Anything left unset there falls back to the global file; see [Global Risk Settings](#global-risk-settings) for how the two are merged.

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
  keystroke_collection: split/ios/keystroke_collection.yaml
  apps: split/ios/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/ios/`. Each included file may contain either the raw section value or a mapping wrapped under the section name. Inline values in the entry point override included values (this is also how `device`/`runner` can stay inline while other sections are included — nothing requires every section to go through `include:`). See `configs/ios.example.yaml`, the tracked example of this entry-point file.

### Global Risk Settings

`ipa_static_analysis` and `keystroke_collection` each hold one risk's shared default settings — the analyzer config for `ios-feature-01-risk-01`, the keyboard-collection config for `ios-feature-04-risk-01` — used by every app that enables that risk. An app's own `risks.<risk_id>` entry in `apps.yaml` only needs `enabled: true`; any field nested under it there overrides the shared default for that app alone, merged recursively (so, for example, an app can override just `collection.auto_navigation.accessibility_ids` without repeating the rest of `collection`). See [Risks](risks.md) for what each field controls.

`configs/split/ios/risk_settings.example.yaml` shows both risks' settings together in one file for easier reading, but the real (git-ignored) config keeps them as separate files, one per risk, matching the `include:` map above.

### Splitting Out Shared Templates

A section's include value can also be a **list** of paths instead of one path:

```yaml
include:
  apps:
    - split/ios/templates.yaml
    - split/ios/apps.yaml
```

Listed files are read and concatenated as raw text, in that order, then parsed as a single YAML document — not loaded and merged separately. This matters because YAML anchors (`&name`/`*name`) only resolve within one parsed document: if `templates.yaml` and `apps.yaml` were parsed independently, `apps.yaml`'s `<<: *local_ipa_artifact` aliases would fail with an undefined-anchor error. Concatenating the raw text first is what lets `templates.yaml` define reusable `x-*` blocks (artifact source, expected-behavior checks) that `apps.yaml`'s app entries reference, while still keeping the two concerns — reusable templates vs. the actual app roster — in separate files.

`configs/ios.yaml` in this project uses exactly this: `device`/`runner` are inline, `ipa_static_analysis`/`keystroke_collection` are each split into one file, and `apps` is split across two — `configs/split/ios/templates.yaml` (the `x-*` anchors) and `configs/split/ios/apps.yaml` (the 11 app entries, referencing those anchors). All of `configs/split/ios/ipa_static_analysis.yaml`, `keystroke_collection.yaml`, `templates.yaml`, and `apps.yaml` are git-ignored, since they contain a real app roster — only the `*.example.yaml` files under `configs/split/ios/` are tracked (`configs/ios.yaml` itself is also git-ignored, since it holds the real device UDID).

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
