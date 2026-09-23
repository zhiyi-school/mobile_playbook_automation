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

The top-level `reports/<run_timestamp>/summary.md` and `dashboard_results.json` cover all platforms in a single run; see [docs/android/reports-and-troubleshooting.md](../android/reports-and-troubleshooting.md) for the Android side. If `device.appium_auto_start` is enabled, that same top-level directory also gets an `appium.log` with every Appium launch attempt for the run, including any restarts after a mid-run crash. That directory also gets an `events.jsonl`, one JSON line per `risk_started`/`risk_completed`/`appium_recovery` event as the run progresses — used by the API's `GET /runs/{run_id}/events` stream (see [docs/api.md](../api.md#watching-a-runs-progress-live)) but readable directly too.

`summary.md`'s table and each `dashboard_results.json` record carry a short, cleaned one-line message rather than a raw error dump (a failed Appium call's full "Message: ...\nStacktrace:\n..." text is reduced to just its first line) — the complete untouched error still lives in that test's `logs.txt`/`report.json`. For iOS, each `dashboard_results.json` record also carries a `report_path` field (e.g. `ios/<app_id>/ios-feature-04-risk-01/collection_server`) pointing at that per-test folder, and `summary.md` links to it directly from a `Report` column.

## Common Risk Files

`ios-feature-01-risk-01`:

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

`ios-feature-02-risk-01`:

- `report.json`
- `logs.txt`
- `burp_capture.json` — matched decrypted HTTPS metadata only; raw headers, cookies, authorization values, request bodies, response bodies, and unmatched request contents are not persisted

Capture counters and source-state diagnostics are stored under `launch_result.capture_summary` in `report.json`, including valid, malformed, HTTPS, non-HTTPS, matched-HTTPS and unmatched counts, whether the file was created during the window, and whether it changed, became unavailable, or ended with a partial line.

Run-level Burp checks appear once under `## Preflight warnings` in `summary.md` and as `preflight_warning` entries in `events.jsonl`. They are advisory and do not abort the run or get copied into an individual risk's `logs.txt`.

## Statuses

Each test's `report.json` (and `logs.txt`) carries one of these precise statuses in its `final_status` field:

- `IPA_ANALYSIS_COMPLETE` (**info**): IPA was acquired, inspected, and analyzed.
- `PROTECTED_OR_ENCRYPTED_BINARY` (**info**): executable appears protected or encrypted.
- `RISK_EXISTS` (**high**): custom-keyboard collection evidence was observed, or (`ios-feature-02-risk-01`) matching HTTPS and its completed response were decrypted through a locally trusted interception CA, showing missing or ineffective certificate/public-key pinning for that exchange. Installing the test CA is only a prerequisite, and this finding does not mean HTTPS itself is broken.
- `KEYSTROKE_COLLECTION_NOT_OBSERVED` (**low**): probe text was typed but not observed in evidence.
- `CUSTOM_KEYBOARD_NOT_AVAILABLE` (**low**): the target field did not allow the custom keyboard.
- `PAIRING_TIMEOUT` (**medium**): keyboard app did not call `/pair`.
- `PROXY_NOT_CONFIGURED`, `PROXY_UNREACHABLE` (**medium**): (`ios-feature-02-risk-01`) Burp's proxy isn't configured or isn't reachable.
- `DEVICE_PROXY_SETUP_FAILED` (**info**): (`ios-feature-02-risk-01`) Appium could not configure the selected Wi-Fi proxy.
- `CA_INSTALLATION_FAILED` (**info**): (`ios-feature-02-risk-01`) the configured Burp CA could not be installed.
- `CA_TRUST_FAILED` (**info**): (`ios-feature-02-risk-01`) the configured Burp CA could not be confirmed as fully trusted.
- `DEVICE_PROXY_RESTORE_FAILED` (**info**): (`ios-feature-02-risk-01`) the original Wi-Fi proxy settings could not be restored.
- `CAPTURE_PIPELINE_SILENT` (**info**): (`ios-feature-02-risk-01`) no capture activity was observed; the pipeline, app request, and TLS handshake remain indistinguishable.
- `CAPTURE_DATA_INVALID` (**info**): (`ios-feature-02-risk-01`) new capture data existed but could not be parsed.
- `CAPTURE_SOURCE_CHANGED` (**info**): (`ios-feature-02-risk-01`) the capture file was replaced or truncated during the test.
- `CAPTURE_SOURCE_UNAVAILABLE` (**info**): (`ios-feature-02-risk-01`) the capture file could not be read.
- `TRAFFIC_INTERCEPTION_NOT_OBSERVED` (**low**): (`ios-feature-02-risk-01`) no valid HTTPS capture matched the configured hosts; plain HTTP, outdated entries without `scheme`, unrelated hosts, and failed handshakes do not establish the risk.
- `INSTALL_FAILED`, `LAUNCH_FAILED`, `BEHAVIOR_FAILED`, `FAILED` (**medium**): setup, launch, behavior, or unexpected failure.

`summary.md`'s `Status` column doesn't show these directly — it shows a 3-way security verdict (`RiskRunResult.verdict`) that each risk sets itself, alongside `final_status`, at the point it decides the outcome: **At Risk** (the risk was demonstrated, e.g. `IPA_ANALYSIS_COMPLETE`/`RISK_EXISTS`), **Reduced Risk** (the app mitigated it, e.g. `KEYSTROKE_COLLECTION_NOT_OBSERVED`/`CUSTOM_KEYBOARD_NOT_AVAILABLE`), or **Inconclusive** — the field's default, and what any status not listed above leaves it as (install/launch/pairing/behavior failures included). The precise underlying status is always still in that test's `report.json`.

## Troubleshooting

Appium-driven traffic-interception setup requires an English iPhone UI unless localized selectors are added, the configured `device_setup.wifi_ssid`, no device passcode, and an already trusted WebDriverAgent. Burp must be running with its listener bound to all interfaces. Immediately before the risk, Appium sets the selected Wi-Fi network to a manual proxy using the detected Mac LAN address; afterward it restores the exact original proxy mode and values, including on risk failures. A missing configured CA is downloaded, installed, and fully trusted only when `configure_ca_if_missing` is enabled. Settings UI changes between iOS versions can require selector updates; inspect the saved setup screenshot and page source when navigation fails.

Preflight warning codes and actions:

- `BURP_PROXY_UNREACHABLE`: start Burp and verify the configured listener.
- `BURP_CAPTURE_PATH_IS_DIRECTORY`: configure a JSONL file rather than a directory.
- `BURP_CAPTURE_UNREADABLE`: grant the automation process read access to the capture file.
- `BURP_CAPTURE_PARENT_MISSING`: create the configured capture file's parent directory or correct the path.
- `BURP_CAPTURE_PARENT_UNWRITABLE`: grant write access to the capture parent directory.
- `BURP_HEALTH_MISSING`: run `tools/check_burp_interception.py` successfully to create the canary health record.
- `BURP_HEALTH_STALE`: rerun the canary check; adjust `health_max_age_seconds` only when a longer verification window is intentional.
- `BURP_HEALTH_MISMATCH`: rerun the canary check with the configured proxy, capture path, and device.
- `BURP_CAPTURE_STALE`: verify the Burp extension is loaded and writing to the configured capture file.
- `BURP_EXPECTED_HOSTS_EMPTY`: configure the application's exact API hosts to avoid attributing unrelated device traffic to it.

`CAPTURE_PIPELINE_SILENT`: confirm the Burp extension is loaded and writing to the configured capture path, then generate a known request and check the raw Burp history.

`CAPTURE_DATA_INVALID`: inspect the newly appended capture lines for truncated or non-JSON records and confirm the extension writes one JSON object per line.

`CAPTURE_SOURCE_CHANGED`: ensure the capture file is not being rotated, replaced, or truncated while a test is running.

`CAPTURE_SOURCE_UNAVAILABLE`: verify `burp.capture_path`, file permissions, and that the process running the automation can read the file.

Local IPA not found:

Check `artifact.ipa` and make sure the file exists under `intake/ios/ipas/` or the configured absolute path.

Bundle ID mismatch:

Check `bundle_id`, `test_bundle_id`, `artifact.expected_bundle_id`, and the IPA's `CFBundleIdentifier`.

Appium connection failure:

Confirm Appium is running and the XCUITest driver is installed, and that the iPhone is trusted and unlocked. Set `device.appium_auto_start` (see [Configuration](configuration.md#appium-auto-start)) to have the framework start and monitor Appium itself instead — including recovering automatically if it crashes mid-run. Every launch attempt lands in that run's `appium.log`.

`device.udid '...' is not a connected iOS device`:

Before opening an Appium session, the framework checks `xcrun xctrace list devices` and fails fast with this one-line message if the configured `device.udid` isn't currently connected — for example if a different iPhone is plugged in, or the configured one shows under "Devices Offline" (unplugged, locked, or not yet trusted). Run `xcrun xctrace list devices` yourself, then either reconnect/unlock/trust the configured device or update `device.udid` to the one that's actually attached. Without this check, the same situation used to surface as a raw multi-line Appium/XCUITest stack trace (`Unknown device or simulator UDID: '...'`).

WebDriverAgent signing failure:

Check `device.team_id`, `device.xcode_signing_id`, `device.updated_wda_bundle_id`, Xcode Accounts, and device registration.

`failed to start Appium session ...: xcodebuild failed with code 65`:

Appium's own message here ("Consider checking the WebDriverAgent configuration guide...") is generic and doesn't say what actually went wrong — the real reason is further down in `appium.log`. The most common one: if the raw log shows `The application could not be launched because the Developer App Certificate is not trusted` or `...profile has not been explicitly trusted by the user`, WebDriverAgent built and signed fine — iOS itself just hasn't been told to trust that certificate yet. This framework detects this case and raises the concrete fix directly instead of Appium's generic text: a one-time physical action on the iPhone, **Settings > General > VPN & Device Management > select the Developer App certificate for `device.team_id` > Trust**, then confirm "Trust" in the popup. Nothing in Appium, xcodebuild, or this framework can grant this remotely. This can recur whenever the provisioning profile rotates, so it isn't a one-time fix — expect to repeat it occasionally. A proxy intercepting all device traffic (e.g. a manual Burp proxy) can also cause this by blocking iOS's own certificate-verification traffic to Apple — see [Traffic interception](configuration.md#traffic-interception) for the PAC-based fix that avoids that.

IPA install failure:

Check provisioning, entitlements, device compatibility, and whether the IPA is installable outside the framework.

`Cannot install the com.example.LocalKeyboard.4228qcqtj9 application ... ApplicationVerificationFailed ... Failed to verify code signature`:

`ios-feature-04-risk-01`'s keyboard host app (`artifacts/intake/ios/ipas/LocalKeyboard.ipa`, configured via `keystroke_collection.keyboard_app.ipa`) is a prebuilt binary with no Xcode project source in this repo, so nothing rebuilds it automatically the way WDA gets rebuilt on every session. Its provisioning profile is free-tier (7-day validity) same as WDA's, but with no automatic renewal, it will eventually expire and every install attempt fails with this error until it's resigned.

Fix it with `tools/localkeyboard_resign/resign.py`, which doesn't need the original LocalKeyboard source: it builds a placeholder Xcode project (`tools/localkeyboard_resign/project.yml`, generated via `xcodegen`) targeting the LocalKeyboard bundle IDs and App Group entitlement, with the device connected and Automatic Signing on — Apple ties profile issuance to (team + bundle ID + device), not to specific source code, so this mints a fresh profile without the real project. It then pulls that profile and a matching signing identity out of the build output and reapplies both directly to the existing `LocalKeyboard.ipa` via `codesign`, in place:

```bash
python tools/localkeyboard_resign/resign.py --udid <device.udid> --team-id <device.team_id>
```

Both values come from `configs/ios.yaml`'s `device` section. This overwrites `--ipa` (defaults to `artifacts/intake/ios/ipas/LocalKeyboard.ipa`) with the resigned version; pass `--out` to write elsewhere instead. Requires `xcodegen` (`brew install xcodegen`) and a signing identity in the keychain whose certificate's team (its X.509 `OU` field — not necessarily what its display name suggests) matches `--team-id`.

Protected or encrypted iOS executable:

App Store IPAs may be encrypted as acquired. The framework reports this and does not bypass it.

iOS permission prompt blocks testing:

Use `runner.permission_alerts.action: "dismiss"` for automated dismissal, or `alert_only` when the tester should decide manually.

iOS text field not found:

Inspect `target_text_field_candidates*.json` and `target_page_source*.xml`. If the app uses pseudo-fields, add labels to `auto_navigation.button_label_contains`.

Custom keyboard unavailable:

Secure text fields and some numeric/password fields block third-party keyboards by iOS design.
