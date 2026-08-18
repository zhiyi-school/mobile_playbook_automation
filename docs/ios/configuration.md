# iOS Configuration

The iOS config lives at `configs/ios.yaml`, using the split layout — a small entry-point file plus per-section files under `configs/split/ios/` — since a real app roster and per-risk settings don't fit comfortably in one file.

## Quickstart

Set up the split config from the tracked examples:

```bash
cp configs/ios.example.yaml configs/ios.yaml
for f in device runner apps; do cp "configs/split/ios/$f.example.yaml" "configs/split/ios/$f.yaml"; done
for f in ipa_static_analysis keystroke_collection; do cp configs/split/ios/risk_settings.example.yaml "configs/split/ios/$f.yaml"; done
```

`risk_settings.example.yaml` shows both risks' settings together in one file for easier reading; trim each copy above down to just its own top-level key (`ipa_static_analysis:` in one, `keystroke_collection:` in the other) — see [Global Risk Settings](#global-risk-settings).

The config contains `device`, `runner`, `ipa_static_analysis`, `keystroke_collection`, and `apps` sections.

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
      ios-feature1-risk1:
        enabled: true
      ios-feature5-risk1:
        enabled: false
```

For `local_ipa`, `bundle_id`, `test_bundle_id`, and `artifact.expected_bundle_id` can be left blank. The framework reads `Payload/*.app/Info.plist` from the IPA and fills them in at runtime when possible.

The framework no longer retrieves IPAs from the App Store. Obtain each IPA yourself and point `artifact.ipa` at the local file.

See [docs/ios/examples/app-block.yaml](examples/app-block.yaml) for a full, copyable app entry including both risk blocks.

## Risk Blocks

iOS risk IDs are prefixed `ios-feature...`. Enable a risk by adding it under an app's `risks` mapping:

```yaml
risks:
  ios-feature1-risk1:
    enabled: true
```

Risk-specific options (analyzer provider, keyboard collection settings, and so on) are documented in [Risks](risks.md).

## Split iOS Configs

`configs/ios.yaml` is just an `include:` mapping (section name → file path), with the entry-point file at `configs/ios.yaml` and its sections living under `configs/split/ios/`:

```yaml
include:
  device: split/ios/device.yaml
  runner: split/ios/runner.yaml
  ipa_static_analysis: split/ios/ipa_static_analysis.yaml
  keystroke_collection: split/ios/keystroke_collection.yaml
  apps: split/ios/apps.yaml
```

Included paths are resolved relative to the entry-point file — here, that's `configs/`, so each path is prefixed `split/ios/`. Each included file may contain either the raw section value or a mapping wrapped under the section name. Inline values in the entry point override included values. See `configs/ios.example.yaml`, the tracked example of this entry-point file.

### Global Risk Settings

`ipa_static_analysis` and `keystroke_collection` each hold one risk's shared default settings — the analyzer config for `ios-feature1-risk1`, the keyboard-collection config for `ios-feature5-risk1` — used by every app that enables that risk. An app's own `risks.<risk_id>` entry in `apps.yaml` only needs `enabled: true`; any field nested under it there overrides the shared default for that app alone, merged recursively (so, for example, an app can override just `collection.auto_navigation.accessibility_ids` without repeating the rest of `collection`). See [Risks](risks.md) for what each field controls.

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

`configs/ios.yaml` in this project uses exactly this: it is just an `include:` map, with `device`/`runner`/`ipa_static_analysis`/`keystroke_collection` each split into one file, and `apps` split across two — `configs/split/ios/templates.yaml` (the `x-*` anchors) and `configs/split/ios/apps.yaml` (the 11 app entries, referencing those anchors). All of `configs/split/ios/device.yaml`, `runner.yaml`, `ipa_static_analysis.yaml`, `keystroke_collection.yaml`, `templates.yaml`, and `apps.yaml` are git-ignored, since they contain a real device UDID and app roster — only the `*.example.yaml` files under `configs/split/ios/` are tracked.

## Environment Files

The CLI loads `.env` from the project root and from the directory containing the selected config file. Existing shell environment variables are not overwritten.

Example:

```bash
cp .env.example .env
```

```bash
MOBSF_API_KEY="REPLACE_WITH_MOBSF_API_KEY"
```

`MOBSF_API_KEY` is used by `ios-feature1-risk1` when `analyzer.provider: mobsf` and MobSF auto-start is not generating its own temporary key.
