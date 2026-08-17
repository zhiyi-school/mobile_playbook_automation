# iOS Configuration

The iOS config lives at `configs/ios.yaml`. Create your local working copy from the example:

```bash
cp configs/ios.example.yaml configs/ios.yaml
```

It contains `device`, `runner`, and `apps` sections.

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

For large iOS configs, `include` can split sections into smaller YAML files:

```yaml
include:
  device: device.yaml
  runner: runner.yaml
  apps: apps.yaml
```

Included paths are resolved relative to the entry-point file. Each included file may contain either the raw section value or a mapping wrapped under the section name. Inline values in the entry point override included values. See `configs/split/ios/ios.example.yaml`.

### Splitting Out Shared Risk Templates

A section's include value can also be a **list** of paths instead of one path:

```yaml
include:
  apps:
    - templates.yaml
    - apps.yaml
```

Listed files are read and concatenated as raw text, in that order, then parsed as a single YAML document — not loaded and merged separately. This matters because YAML anchors (`&name`/`*name`) only resolve within one parsed document: if `templates.yaml` and `apps.yaml` were parsed independently, `apps.yaml`'s `<<: *ipa_static_analysis` aliases would fail with an undefined-anchor error. Concatenating the raw text first is what lets `templates.yaml` define reusable `x-*` blocks (analyzer defaults, keystroke-collection settings, and so on) that `apps.yaml`'s app entries reference, while still keeping the two concerns — reusable templates vs. the actual app roster — in separate files.

`configs/ios.yaml` in this project uses exactly this: it is just an `include:` map, with `device`/`runner` each split into one file, and `apps` split across two — `configs/split/ios/templates.yaml` (the `x-*` anchors) and `configs/split/ios/apps.yaml` (the 11 app entries, referencing those anchors). All of `configs/split/ios/device.yaml`, `runner.yaml`, `templates.yaml`, and `apps.yaml` are git-ignored, since they contain a real device UDID and app roster — only the `*.example.yaml` files under `configs/split/ios/` are tracked. Android's equivalent split lives in `configs/split/android/`; see [Android Configuration](../android/configuration.md#split-android-configs).

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
