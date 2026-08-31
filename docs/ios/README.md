# iOS Automation

iOS support drives a physical iPhone over Appium/XCUITest and analyzes IPA
artifacts on the workstation.

Examples use placeholders: `Example iOS App`, `example-app`,
`com.example.ios.placeholder`, `<IPA_PATH>`, `<BUNDLE_ID>`, `<DEVICE_ID>`,
`<RUN_TIMESTAMP>`, `<APP_ID>`, `<RISK_ID>`.

## What is shared, what is iOS-specific

Most of the system is platform-agnostic and documented once, centrally. Only
the right-hand column differs per platform.

| Shared across platforms (documented centrally) | iOS-specific (documented here) |
| --- | --- |
| CLI commands, flags and `run-all` — [setup.md](../setup.md) | Xcode, XCUITest driver, signing, Team ID |
| YAML entry-point + `include:` mechanism — [configuration.md](../configuration.md) | `device` fields, IPA artifact sources, WebDriverAgent |
| Run lifecycle, run IDs, manifests — [architecture.md](../architecture.md) | Automatic unlock, permission alerts, keyboard installation |
| HTTP API and SSE — [api.md](../api.md) | Which iOS risks need a device vs. only an IPA |
| Report layout, `dashboard_results.json`, SARIF — [api.md](../api.md) | iOS per-test report fields and statuses |
| Dashboard sync worker — [operations.md](../operations.md) | — |
| Cross-cutting failures — [troubleshooting.md](../troubleshooting.md) | Device, signing and Appium/XCUITest failures |

Do not look for architecture or API detail on this page; it links there instead
of repeating it.

## Quickstart

Create a working config — see [Configuration](configuration.md#quickstart) for
the split-file setup steps.

```bash
python -m mobile_playbook list-risks --platform ios

python -m mobile_playbook run --platform ios --config configs/ios.yaml \
  --risks <RISK_ID> --dry-run --out reports

python -m mobile_playbook run --platform ios --config configs/ios.yaml \
  --apps example-app --risks <RISK_ID> --out reports
```

Outputs land under:

```text
reports/<RUN_TIMESTAMP>/ios/<APP_ID>/<RISK_ID>/<CASE_ID>/
```

## Requirements

| | Needed for |
| --- | --- |
| Xcode + command-line tools | signing and the XCUITest driver |
| Appium with the XCUITest driver | every device-driven iOS risk |
| A physical iPhone, connected and trusted | every device-driven iOS risk |
| An Apple Team ID | signing WebDriverAgent and test bundles |
| MobSF (optional) | `ios-feature-01-risk-01` when `analyzer.provider` is `mobsf` |
| Burp Suite + a device already proxying through it with its CA trusted | `ios-feature-02-risk-01` |

**Device state matters.** The device must have no passcode, Face ID or Touch ID.
Appium cannot enter one on a real device, so a locked secured device stalls the
run and needs a person.

## IPA requirements and artifact sources

The framework does **not** retrieve IPAs from the App Store. Supply the file
yourself and point the config at it.

| `artifact.source` | Meaning |
| --- | --- |
| `local_ipa` | a path you provide, anywhere on disk |
| `intake_ipa` | a file dropped into `intake/ios/ipas/` |
| `installed_app_reference` | no binary — device-only checks against an already-installed app |

```yaml
apps:
  - id: example-app
    name: Example iOS App
    bundle_id: com.example.ios.placeholder
    test_bundle_id: com.example.ios.placeholder.bundle
    artifact:
      source: local_ipa
      ipa: <IPA_PATH>
      expected_bundle_id: com.example.ios.placeholder
```

`expected_bundle_id` is checked against the IPA's real `CFBundleIdentifier`, so
a mismatched file fails early rather than testing the wrong app.

### Provisioning and signing assumptions

- The run signs WebDriverAgent and test bundles with `device.team_id` and
  `device.xcode_signing_id` (default `Apple Development`).
- `device.allow_provisioning_device_registration` controls whether Xcode may
  register the device with your team.
- The repository does **not** decrypt App Store binaries, bypass FairPlay,
  require jailbreak, or redistribute packages. An encrypted main executable is
  reported as a test outcome, not worked around.

## Risk execution

Not every iOS risk needs a device:

| Risk | Needs a device | Also needs |
| --- | --- | --- |
| `ios-feature-01-risk-01` — IPA acquisition / static analysis | no | an IPA; MobSF only if `analyzer.provider: mobsf` |
| `ios-feature-02-risk-01` — TLS traffic interception | yes | Burp reachable, device proxying, CA trusted, capture extension writing to `capture_path` |
| `ios-feature-04-risk-01` — custom keyboard keystroke collection | yes | the keyboard added in iOS Settings with **Full Access** enabled |

The runner is sequential by default and can uninstall test bundles after each
app to reduce device state drift. One run per platform at a time.

What each risk actually asserts, its stages and statuses, and how to add a new
one: [Risks](risks.md).

## Reports and evidence

Per-test output for iOS follows the shared layout described in
[api.md](../api.md#durable-dashboard-sync); the iOS-specific fields, statuses
and evidence kinds are in
[Reports And Troubleshooting](reports-and-troubleshooting.md).

## Documentation

- [Configuration](configuration.md) — device, runner and app config shape; split configs; environment files
- [Risks](risks.md) — what each iOS risk tests, artifact sources, binary mutability inspection, adding new risks
- [Manual LocalKeyboard Server](manual-local-keyboard-server.md) — run the `ios-feature-04-risk-01` collection server alone for phone-side manual testing
- [Reports And Troubleshooting](reports-and-troubleshooting.md) — iOS report files, statuses and common failures
- [configs/split/ios/apps.example.yaml](../../configs/split/ios/apps.example.yaml) — a copyable app entry

Central: [setup.md](../setup.md) · [configuration.md](../configuration.md) ·
[architecture.md](../architecture.md) · [api.md](../api.md) ·
[operations.md](../operations.md) · [troubleshooting.md](../troubleshooting.md)
