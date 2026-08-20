# iOS Reports And Troubleshooting

Each run creates a timestamped report directory:

```text
reports/<run_timestamp>/
```

The timestamp format is `YYYY-MM-DD_HH-MM-SS` in the workstation's local timezone. If a timestamp already exists, the next run gets a suffix such as `-2`.

iOS per-app risk outputs are written under:

```text
reports/<run_timestamp>/ios/<app_id>/<risk_id>/<test_case_id>/
```

The top-level `reports/<run_timestamp>/summary.md` and `dashboard_results.json` cover all platforms in a single run; see [docs/android/reports-and-troubleshooting.md](../android/reports-and-troubleshooting.md) for the Android side.

`summary.md`'s table and each `dashboard_results.json` record carry a short, cleaned one-line message rather than a raw error dump (a failed Appium call's full "Message: ...\nStacktrace:\n..." text is reduced to just its first line) — the complete untouched error still lives in that test's `logs.txt`/`report.json`. For iOS, each `dashboard_results.json` record also carries a `report_path` field (e.g. `ios/parking/ios-feature-04-risk-01/collection_server`) pointing at that per-test folder, and `summary.md` links to it directly from a `Report` column.

## Common Risk Files

`ios-feature1-risk1`:

- `report.json`
- `ipa_analysis.json`
- `package_inventory.json`
- `critical_findings.json`
- `critical_findings.md`
- `mobsf_report.json` when MobSF analysis succeeds

`ios-feature-04-risk-01`:

- `report.json`
- `logs.txt`
- `collection_events.json`
- `keyboard_local_log_page_source.xml`
- `keyboard_local_log.png`
- `target_page_source*.xml`
- `target_text_field_candidates*.json`

## Statuses

Each test's `report.json` (and `logs.txt`) carries one of these precise statuses in its `final_status` field:

- `IPA_ANALYSIS_COMPLETE`: IPA was acquired, inspected, and analyzed.
- `PROTECTED_OR_ENCRYPTED_BINARY`: executable appears protected or encrypted.
- `RISK_EXISTS`: custom-keyboard collection evidence was observed.
- `KEYSTROKE_COLLECTION_NOT_OBSERVED`: probe text was typed but not observed in evidence.
- `CUSTOM_KEYBOARD_NOT_AVAILABLE`: the target field did not allow the custom keyboard.
- `PAIRING_TIMEOUT`: keyboard app did not call `/pair`.
- `INSTALL_FAILED`, `LAUNCH_FAILED`, `BEHAVIOR_FAILED`, `FAILED`: setup, launch, behavior, or unexpected failure.

`summary.md`'s `Status` column doesn't show these directly — it shows a 3-way security verdict (`RiskRunResult.verdict`) that each risk sets itself, alongside `final_status`, at the point it decides the outcome: **At Risk** (the risk was demonstrated, e.g. `IPA_ANALYSIS_COMPLETE`/`RISK_EXISTS`), **Reduced Risk** (the app mitigated it, e.g. `KEYSTROKE_COLLECTION_NOT_OBSERVED`/`CUSTOM_KEYBOARD_NOT_AVAILABLE`), or **Inconclusive** — the field's default, and what any status not listed above leaves it as (install/launch/pairing/behavior failures included). The precise underlying status is always still in that test's `report.json`.

## Troubleshooting

Local IPA not found:

Check `artifact.ipa` and make sure the file exists under `intake/ios/ipas/` or the configured absolute path.

Bundle ID mismatch:

Check `bundle_id`, `test_bundle_id`, `artifact.expected_bundle_id`, and the IPA's `CFBundleIdentifier`.

Appium connection failure:

Confirm Appium is running and the XCUITest driver is installed, and that the iPhone is trusted and unlocked.

`device.udid '...' is not a connected iOS device`:

Before opening an Appium session, the framework checks `xcrun xctrace list devices` and fails fast with this one-line message if the configured `device.udid` isn't currently connected — for example if a different iPhone is plugged in, or the configured one shows under "Devices Offline" (unplugged, locked, or not yet trusted). Run `xcrun xctrace list devices` yourself, then either reconnect/unlock/trust the configured device or update `device.udid` to the one that's actually attached. Without this check, the same situation used to surface as a raw multi-line Appium/XCUITest stack trace (`Unknown device or simulator UDID: '...'`).

WebDriverAgent signing failure:

Check `device.team_id`, `device.xcode_signing_id`, `device.updated_wda_bundle_id`, Xcode Accounts, and device registration.

IPA install failure:

Check provisioning, entitlements, device compatibility, and whether the IPA is installable outside the framework.

Protected or encrypted iOS executable:

App Store IPAs may be encrypted as acquired. The framework reports this and does not bypass it.

iOS permission prompt blocks testing:

Use `runner.permission_alerts.action: "dismiss"` for automated dismissal, or `alert_only` when the tester should decide manually.

iOS text field not found:

Inspect `target_text_field_candidates*.json` and `target_page_source*.xml`. If the app uses pseudo-fields, add labels to `auto_navigation.button_label_contains`.

Custom keyboard unavailable:

Secure text fields and some numeric/password fields block third-party keyboards by iOS design.
