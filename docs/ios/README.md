# iOS Automation

iOS support drives a physical iPhone over Appium/XCUITest and analyzes IPA artifacts on the workstation.

Create a working config: see [Configuration](configuration.md#quickstart) for the split-file setup steps.

List iOS risks:

```bash
python -m mobile_playbook list-risks --platform ios
```

Run all enabled iOS risks:

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml --out reports
```

Outputs are written under:

```text
reports/<run_timestamp>/ios/<app_id>/<risk_id>/
```

## Documentation

- [Configuration](configuration.md): device, runner, and app config shape; split configs; environment files.
- [Risks](risks.md): what each iOS risk tests, artifact sources, binary mutability inspection, and how to add new iOS risks.
- [Manual LocalKeyboard Server](manual-local-keyboard-server.md): run the `ios-feature5-risk1` collection server by itself for phone-side manual testing.
- [Reports And Troubleshooting](reports-and-troubleshooting.md): iOS report files, statuses, and common failure causes.
- [Example app block](examples/app-block.yaml): a focused, copyable app entry for `configs/ios.yaml`.

## Requirements

- Xcode
- Appium server with the XCUITest driver installed
- A physical iPhone connected to the Mac
- MobSF if you want `ios-feature1-risk1` to use the primary analyzer path

## Quick Notes

- The framework no longer automates IPA retrieval from the App Store. Obtain each IPA yourself and point the config at the local file.
- `ios-feature1-risk1` does not require Appium or a connected phone.
- `ios-feature5-risk1` requires a working Appium/XCUITest real-device setup, and the user must add the custom keyboard in iOS Settings with Full Access enabled.
- The runner is sequential by default and can uninstall test bundles after each app test to reduce device state drift.
