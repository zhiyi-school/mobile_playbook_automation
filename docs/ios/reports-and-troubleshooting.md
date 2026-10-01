# iOS Reports And Troubleshooting

Each run creates a timestamped report directory:

```text
artifacts/reports/<run_timestamp>/
```

The timestamp format is `YYYY-MM-DD_HH-MM-SS` in the workstation's local timezone. If a timestamp already exists, the next run gets a suffix such as `-2`.

iOS per-app risk outputs are written under:

```text
artifacts/reports/<run_timestamp>/ios/<app_id>/<risk_id>/<test_case_id>/
```

The top-level `artifacts/reports/<run_timestamp>/summary.md` and `dashboard_results.json` cover the app results in this run. `run-all` creates a separate run directory
for each platform. See [docs/android/reports-and-troubleshooting.md](../android/reports-and-troubleshooting.md) for the Android side. If `device.appium_auto_start` is enabled, that same top-level directory also gets an `appium.log` with every Appium launch attempt for the run, including any restarts after a mid-run crash. That directory also gets an `events.jsonl`, one JSON line per `risk_started`/`risk_completed`/`appium_recovery` event as the run progresses — used by the API's `GET /runs/{run_id}/events` stream (see [docs/api.md](../api.md#watching-a-runs-progress-live)) but readable directly too.

`summary.md`'s table and each `dashboard_results.json` record carry a short, cleaned one-line message rather than a raw error dump (a failed Appium call's full "Message: ...\nStacktrace:\n..." text is reduced to just its first line) — the complete untouched error still lives in that test's `logs.txt`/`report.json`. For iOS, each `dashboard_results.json` record also carries a `report_path` field (e.g. `ios/<app_id>/ios-feature-04-risk-01/collection_server`) pointing at that per-test folder, and `summary.md` links to it directly from a `Report` column.

## Common Risk Files

`ios-feature-01-risk-01`:

- `report.json`
- `ipa_analysis.json`
- `package_inventory.json`
- `critical_findings.json`
- `critical_findings.md`
- `mobsf_report.json` when MobSF analysis succeeds

`ios-feature-03-risk-01`:

- `report.json`
- `logs.txt`
- `recording.mp4`: the recorder's screen recording, attached as `screen_recording` evidence whenever it was retrieved
- `capture_controls.json`: target visibility, foreground/timing observations, recorder controls and decoded video coverage results
- `control_target_start_reference.png`: the target reference before the canary hold
- `control_before_reference.png` / `control_after_reference.png`: Appium references for the recorder controls
- `control_frames/`: ReplayKit marker frames and `video-before.png` / `video-after.png` from coverage validation
- `ocr_matches.json`: attempted frame count (`frames_scanned`), distinct matching frame count (`matched_frame_count`), frames judged blank, canary matches, per-frame processing failures (`failed_frames`), OCR provider and settings (`provider`, `provider_settings`), where the frames came from (`screenshots` or `video`) and `secure_field_exposed`
- `report.json` also records password probe status (`TYPED`, `DISABLED`, or `FOCUS_FAILED`) under `launch_result.probe.typed.secure`. The summary does not infer masking from an absent password canary; older reports lacking probe metadata show an unknown outcome. See [screen-capture result interpretation](risks.md#ios-feature-03-risk-01).
- `reference_screen.png`: Appium's own screenshot of the target app during the capture window
- `frames/*.png`: the recorded frames in which a canary was readable
- `pulled/`: what was pulled from the recorder's Documents folder, including `done.json` and the broadcast screenshots
- `video_frames/`: still frames taken from the video when no screenshot fell inside the window
- `broadcast-<stage>.png` / `.xml`: diagnostics when the broadcast could not be started

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
- `SCREEN_CAPTURE_OBSCURED` (**low**): historical (`ios-feature-03-risk-01`) reports classified blank or covered frames as Reduced Risk. Current runs require independent visibility and decoded video coverage for `SCREEN_CAPTURE_OBSCURED_VERIFIED`; blank samples alone remain `CANARY_NOT_OBSERVED`. Newly generated historical summaries explicitly state that protection was unverified; stored report files are not rewritten.
- `SCREEN_CAPTURE_OBSCURED_VERIFIED` (**low**): (`ios-feature-03-risk-01`) the target canary was independently visible, foreground and timing checks passed, every decoded frame spanning the verified interval was uniform, and recorder markers were readable before and after it in the same video. Reduced Risk applies to that exercised window. See [coverage requirements](risks.md#ios-feature-03-risk-01).
- `TEXT_FIELD_UNREACHABLE` (**info**): (`ios-feature-03-risk-01`) the configured probe could not reach an editable field, including after configured auto-navigation. The verdict is Inconclusive and capture protection was not assessed. Inspect `launch_result.probe.error` and the target UI diagnostics; use a different probe when no field is reachable.
- `TARGET_NOT_FOREGROUND` (**info**): (`ios-feature-03-risk-01`) the target app left the foreground before the probe finished, so taps stopped and capture protection was not assessed. Inspect `launch_result.app_permission_alerts`, `launch_result.probe.error` and the saved target screen.
- `CANARY_NOT_OBSERVED` (**info**): (`ios-feature-03-risk-01`) recorded frames were blank or uniform, or readable without a recognised canary. The result is Inconclusive; review the attached recording and `capture_controls.json` to distinguish unverified target visibility from failed recorder controls.
- `OCR_UNAVAILABLE` (**info**): (`ios-feature-03-risk-01`) required OCR or video-processing dependencies are missing, or `ocr_provider` is `none`; the recording is attached for manual review.
- `OCR_FAILED` (**info**): (`ios-feature-03-risk-01`) video extraction, image processing or Tesseract recognition failed, and no successfully processed frame contained a recognised canary. The result is Inconclusive; the recording and per-frame errors are retained for manual review.
- `BROADCAST_NOT_STARTED` (**info**): (`ios-feature-03-risk-01`) the broadcast could not be started, or no recording or frames came back.
- `RECORDING_RETRIEVAL_FAILED` (**info**): (`ios-feature-03-risk-01`) the export, the pull, or the `max_pull_bytes` limit failed.
- `DIRTY_STARTING_STATE` (**info**): (`ios-feature-03-risk-01`, `ios-feature-04-risk-01`) a companion app left by an earlier run could not be removed.
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
- `BURP_HEALTH_MISSING`: run `scripts/check_burp_interception.py` successfully to create the canary health record.
- `BURP_HEALTH_STALE`: rerun the canary check; adjust `health_max_age_seconds` only when a longer verification window is intentional.
- `BURP_HEALTH_MISMATCH`: rerun the canary check with the configured proxy, capture path, and device.
- `BURP_CAPTURE_STALE`: verify the Burp extension is loaded and writing to the configured capture file.
- `BURP_EXPECTED_HOSTS_EMPTY`: configure the application's exact API hosts to avoid attributing unrelated device traffic to it.

Screen capture (`ios-feature-03-risk-01`) preflight warnings are advisory too:

- `RECORDER_IPA_MISSING`: build ReplayConsentRecorder and place the IPA at `screen_capture.recorder_app.ipa` (by default `artifacts/companion/ios/ipas/ReplayConsentRecorder.ipa`). Outside a dry run the same condition is also a configuration error.
- `OCR_UNAVAILABLE`: install Tesseract 5 (`brew install tesseract` on macOS) and verify that `tesseract --list-langs` includes every configured language. Set `capture.tesseract.executable` or `tessdata_dir` when discovery differs in the backend process. The Python dependencies also supply the macOS Quartz/AVFoundation image/video bindings. Missing dependencies retain the recording for manual review.

`OCR_FAILED`: decoded coverage failures are recorded under `launch_result.capture_controls.coverage` and `capture_controls.json`, including missing/edited video segments, decode errors and failed same-video controls. Control OCR errors are recorded under `launch_result.capture_controls` and `capture_controls.json`. For target processing errors, inspect `launch_result.ocr.failed_frames`, `launch_result.ocr.video_errors` or `ocr_matches.json` for the affected frame or video sample and error. Video errors distinguish decoder setup, sample decoding and PNG writing; a recording with no decodable frames is a processing failure, even if its file header resembles MP4. An unreadable image, a nonzero Tesseract exit or a recognition timeout is a processing failure, including when the process supplies no error detail. Check the exported images, executable permissions, configured language data and timeout, then review the attached recording. A canary recognised in another frame still establishes At Risk, with the processing errors retained.

`BROADCAST_NOT_STARTED`: the iOS broadcast sheet's button labels depend on the iOS version and language. Check `broadcast-start_broadcast.png`/`.xml` in the report directory and add the visible label to `recorder_app.start_broadcast_labels` (or `stop_broadcast_labels` for stopping). Raise `sheet_timeout_seconds` when the sheet is slow to appear.

`RECORDING_RETRIEVAL_FAILED` with "never wrote done.json": the recorder did not finish exporting within `export_timeout_seconds`. Long recordings take longer to copy, so raise the timeout. Also check that the installed recorder build has file sharing enabled (`UIFileSharingEnabled`), which Appium needs to read its Documents folder.

`RECORDING_RETRIEVAL_FAILED` naming `max_pull_bytes`: the evidence folder was larger than the limit. Shorten `capture_window_seconds` or raise `capture.max_pull_bytes`.

`CAPTURE_PIPELINE_SILENT`: confirm the Burp extension is loaded and writing to the configured capture path, then generate a known request and check the raw Burp history.

`CAPTURE_DATA_INVALID`: inspect the newly appended capture lines for truncated or non-JSON records and confirm the extension writes one JSON object per line.

`CAPTURE_SOURCE_CHANGED`: ensure the capture file is not being rotated, replaced, or truncated while a test is running.

`CAPTURE_SOURCE_UNAVAILABLE`: verify `burp.capture_path`, file permissions, and that the process running the automation can read the file.

Local IPA not found:

Check `artifact.ipa` and make sure the file exists under `artifacts/intake/ios/ipas/` or the configured absolute path.

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

`Cannot install the com.example.LocalKeyboard.4228qcqtj9 application ... ApplicationVerificationFailed ... Failed to verify code signature` (or the same for `com.example.ReplayConsentRecorder`):

`ios-feature-04-risk-01`'s keyboard host app (`artifacts/companion/ios/ipas/LocalKeyboard.ipa`, configured via `keystroke_collection.keyboard_app.ipa`) is a prebuilt binary with no Xcode project source in this repo, so nothing rebuilds it automatically the way WDA gets rebuilt on every session. Its provisioning profile is free-tier (7-day validity) same as WDA's, but with no automatic renewal, it will eventually expire and every install attempt fails with this error until it's resigned.

`ios-feature-03-risk-01`'s ReplayConsentRecorder IPA (`artifacts/companion/ios/ipas/ReplayConsentRecorder.ipa`) is a development export with the same limited profile lifetime.

Fix either with `tools/companion_apps/localkeyboard_resign/resign.py`, which re-signs any companion IPA without its source: it reads the app's and each extension's bundle IDs and App Groups from the IPA, generates a placeholder Xcode project with those identifiers in a temporary directory (via `xcodegen`), and builds it with the device connected and Automatic Signing on — Apple ties profile issuance to (team + bundle ID + device + capabilities), not to specific source code, so this mints fresh profiles without the real project. It then applies those profiles and a matching signing identity to the existing IPA via `codesign`, extensions first:

```bash
python tools/companion_apps/localkeyboard_resign/resign.py --udid <device.udid> --team-id <device.team_id>
python tools/companion_apps/localkeyboard_resign/resign.py --ipa artifacts/companion/ios/ipas/ReplayConsentRecorder.ipa --udid <device.udid> --team-id <device.team_id>
```

Both values come from `configs/ios.yaml`'s `device` section. This overwrites `--ipa` (defaults to `artifacts/companion/ios/ipas/LocalKeyboard.ipa`) with the resigned version; pass `--out` to write elsewhere instead. With `resign.enabled`, both risks run it automatically when the companion's profile has expired or the install is rejected for its signature. Requires `xcodegen` (`brew install xcodegen`) and a signing identity in the keychain whose certificate's team (its X.509 `OU` field — not necessarily what its display name suggests) matches `--team-id`.

Protected or encrypted iOS executable:

App Store IPAs may be encrypted as acquired. The framework reports this and does not bypass it.

iOS permission prompt blocks testing:

Use `runner.permission_alerts.action: "dismiss"` for automated dismissal, or `alert_only` when the tester should decide manually. A result with status `ALERT_UNHANDLED` lists the prompt's button labels; none of them was safe to tap for the configured action, so the prompt was left on screen. Prompts that only offer an action such as **Update Now** need a current build of the app or manual handling. See [Configuration](configuration.md#runner) for how prompts are recognised and matched.

iOS text field not found:

Inspect `target_text_field_candidates*.json` and `target_page_source*.xml`. If the app uses pseudo-fields, add labels to `auto_navigation.button_label_contains`.

Custom keyboard unavailable:

Secure text fields and some numeric/password fields block third-party keyboards by iOS design.


For `CANARY_NOT_OBSERVED` with successful recorder controls, inspect `capture_controls.json`'s `coverage.reason`. A short canary hold, foreground-state gaps, unavailable device clock, changed clock, missing first screenshot, absent video, frame gaps or visible target pixels prevents verified Reduced Risk. The video coverage decoder has a 120-second processing deadline. The three-second edge exclusions and recorded clock/origin bounds prevent transition frames or timestamp uncertainty from qualifying as target protection. Coverage validation uses the existing companion app and macOS dependencies; no IPA rebuild or live configuration edit is required.
