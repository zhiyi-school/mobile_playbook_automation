# iOS Risks

## ios-feature-01-risk-01

`ios-feature-01-risk-01` demonstrates the risk that an acquired IPA can be analyzed on a workstation, leading to discovery of application metadata, bundled resources, frameworks, plugins, permissions, URL schemes, binary characteristics, API keys, credentials, and other embedded sensitive strings.

The preferred analyzer is a local MobSF instance. The built-in package scanner remains available as a fallback when MobSF is not configured or cannot be reached.

Stages:

1. Artifact acquisition
2. Artifact validation
3. Safe IPA unpacking for binary inspection
4. Main executable mutability/encryption inspection
5. MobSF upload and static scan when `analyzer.provider: mobsf`
6. MobSF report normalization into framework reports
7. Built-in package inventory fallback if MobSF is unavailable and `fallback_to_builtin` is true
8. Sensitive string scanning from the active analyzer output
9. Optional low-impact API key reuse checks for extracted Google API keys
10. Reporting

Reports include:

- `ipa_analysis.json`
- `package_inventory.json`
- `critical_findings.json`
- `critical_findings.md`
- `mobsf_report.json` when MobSF analysis succeeds
- `report.json`

`IPA_ANALYSIS_COMPLETE` means the IPA was acquired, unpacked, and inventoried for static-analysis exposure.

## ios-feature-01-risk-02

`ios-feature-01-risk-02` demonstrates whether a supplied IPA can be repackaged — code injected, re-signed, and installed — and still run as if untouched, which would mean no effective tamper, integrity, or anti-repackaging control is in force. It is a device-based test: it needs a physical device, the supplied IPA from the app's `artifact` config (`installed_app_reference` is rejected, since a black-box install cannot be repackaged), `codesign` on the workstation, `insert_dylib` (vendored in-repo under `tools/vendor/insert_dylib/`), `frida` (for the gadget-attach confirmation), and a valid provisioning profile for the target bundle ID (configured, or auto-discovered from the workstation's installed profiles). Resigning an arbitrary app for a real device without that profile fails, and that is reported as `RESIGN_FAILED` (Inconclusive), never as a defense.

Workflow:

1. Acquire the supplied IPA and install it unchanged as a clean baseline.
2. Exercise and observe the baseline across a sampling window. System permission prompts the app raises are answered with `runner.permission_alerts` right after launch and again after sampling, so neither pass is judged with a prompt covering it; each observation's `permission_alerts` lists them. If the baseline never reaches a drivable foreground state there is nothing to compare against, so the run is `BASELINE_FAILED`.
3. Unpack the IPA, copy the Frida Gadget from `tools/vendor/frida/` into the bundle's `Frameworks/`, and inject a load command into the main executable with `insert_dylib` (vendored in-repo under `tools/vendor/insert_dylib/`).
4. Embed the provisioning profile (configured `resign.provisioning_profile`, or one auto-discovered from `~/Library/MobileDevice/Provisioning Profiles` that matches this app, device, and identity), re-sign the modified bundle (nested code first, then the app, then `codesign --verify --deep --strict`), repackage it into an IPA, and install it.
5. Exercise and observe the repackaged build the same way, then diff it against the baseline.
6. When the diff shows equivalence, attach to the injected Frida Gadget and load the configured `frida.script` to confirm the injected code runs before returning `At Risk`; a failed attach is `GADGET_ATTACH_FAILED` (Inconclusive).

Evidence is collected on several channels for both passes and written into the report directory: app-state-over-time sampling (`query_app_state` polling), a UI tamper-marker scan of the page source against the configured `tamper_markers` merged with the app's `expected_behavior.source_not_contains`, an exercised sensitive path, a reachable-state fingerprint used for the diff, a screenshot, a screen recording (best-effort over Appium), and a best-effort device log. `baseline_comparison.json` records both observations and the divergence list.

The verdict is three-way:

- `REPACKAGING_SURVIVED` (**At Risk**): a validly re-signed build installs, launches, and is equivalent to the clean baseline across the exercised flow and sampling window, with no tamper signal on any channel — and the injected Frida Gadget was then attached to and the configured script loaded, confirming the injected code actually runs.
- `REPACKAGING_BLOCKED` (**Reduced Risk**): a validly re-signed build installs, but some channel shows a control reacting — including non-blocking behavior such as running but surfacing a tamper marker, logging out, disabling a feature, or terminating mid-window. The divergence list explains which channel diverged.
- Inconclusive: `ARTIFACT_NOT_FOUND`, `DYLIB_INJECTION_FAILED`, `REPACK_FAILED`, `RESIGN_FAILED`, `INSTALL_FAILED`, `BASELINE_FAILED`, or `GADGET_ATTACH_FAILED` — the run never reached a judgable state, or an environment/tooling prerequisite was missing (a failed signing, install, or gadget attach is here, never a defense). An Inconclusive result is never counted as a defense.

Missing or failed evidence on a single channel does not prove a defense: only a divergence from the clean baseline does. Silence is treated as equivalence only when the baseline itself was drivable and every channel agreed.

## ios-feature-02-risk-01

`ios-feature-02-risk-01` demonstrates whether matching HTTPS traffic can be decrypted through an operator-controlled MITM proxy using a locally trusted interception CA. A finding means no effective certificate or public-key pinning was observed for that exchange. It does not mean HTTPS itself is broken.

This risk assumes the device has already been configured to route its traffic through Burp and trust Burp's CA — installing and trusting that CA is a test prerequisite, not itself a vulnerability. That setup is device-level, one-time, and outside this framework's control (see [Configuration](configuration.md#traffic-interception)). The framework itself only checks that Burp's proxy port is reachable, then drives the app and reads what a companion Burp extension captured.

Workflow:

1. Check that `traffic_interception.burp.proxy_url` is configured and reachable.
2. Acquire and install the target app IPA.
3. Snapshot the capture file's byte offset immediately before launching the app. Pre-existing entries are ignored.
4. Launch the app, optionally tap through configured accessibility IDs, and wait `exercise.exercise_wait_seconds` for app requests.
5. Poll only bytes appended after the snapshot, retaining an incomplete trailing JSONL record for the next poll and filtering complete valid HTTPS records to `expected_hosts` when configured.

`RISK_EXISTS` means matching HTTPS traffic and its completed response were captured in decrypted form through Burp during the exercise window. For that exchange, resistance to a locally trusted interception CA was expected but no effective certificate or public-key pinning was observed. This is the only status for this risk that confirms an application risk and produces an `At Risk` verdict. Plain HTTP, entries without an explicit scheme, unrelated hosts, and failed TLS handshakes do not produce this finding.

The other capture outcomes remain `Inconclusive`:

- `TRAFFIC_INTERCEPTION_NOT_OBSERVED`: no valid HTTPS capture matched the configured hosts.
- `CAPTURE_PIPELINE_SILENT`: no capture activity was observed; the pipeline may be broken, the app may not have made a request, or a TLS handshake may have been blocked.
- `CAPTURE_DATA_INVALID`: new capture data existed but could not be parsed.
- `CAPTURE_SOURCE_CHANGED`: the capture file was replaced or truncated during the test.
- `CAPTURE_SOURCE_UNAVAILABLE`: the capture file could not be read.

Silence alone proves neither pipeline failure nor certificate/public-key pinning. Treat every `Inconclusive` result as a prompt to inspect the capture pipeline and raw Burp history, not as proof that the application resisted interception.

The dashboard-facing `summary`/`evidence` fields (in `dashboard_results.json`, via `GET /runs/{run_id}/summary`) go beyond the bare `final_status` for this risk specifically: `summary` reads as e.g. `"14 decrypted request(s) captured through Burp (api.example.com, ...)"` when matching HTTPS was captured, and `evidence` includes a `report` entry named "Burp capture results" pointing at that run's `burp_capture.json`. That file contains matched HTTPS metadata only. The extension records method, URL components, status, body lengths, and stated response content type; it does not store raw headers, cookies, authorization values, request bodies, or response bodies. Unmatched request contents are not persisted in either `burp_capture.json` or `report.json`.

The `launch_result.capture_summary` object in `report.json` records `new_line_count`, `valid_entry_count`, `malformed_entry_count`, `https_entry_count`, `non_https_entry_count`, `matched_count`, `matched_https_count`, `unmatched_entry_count`, `created_during_window`, `source_changed`, `source_unavailable`, `trailing_partial_line`, `expected_hosts`, `hosts`, and `evidence_path`. These aggregate diagnostics describe the capture window without retaining unmatched request details.

### Capturing Burp's traffic into `capture.jsonl`

This risk doesn't talk to Burp's own APIs — Burp has no simple built-in "give me proxy history" endpoint. Instead, a small companion Burp extension (loaded once into Burp, like any other Burp extension) appends one metadata-only JSON object per completed exchange to `burp.capture_path`:

```json
{"schema_version": 1, "scheme": "https", "host": "api.example.com", "port": 443, "method": "GET", "path": "/profile", "status_code": 200, "request_body_length": 0, "response_body_length": 1234, "response_content_type": "JSON"}
```

This mirrors how this framework already exposes evidence to itself elsewhere (`events.jsonl`, `appium.log`) — a plain append-only file that both sides agree on, rather than a live API integration.

[tools/burp/traffic_capture_extension.py](../../tools/burp/traffic_capture_extension.py) is a ready-to-load Jython Burp extension that writes this format. One-time setup:

1. Use the standalone Jython JAR vendored at `tools/vendor/jython/jython-standalone-2.7.4.jar`, or download one from `https://www.jython.org/download`.
2. In Burp Suite, open Extender > Options > Python Environment and point it at that JAR.
3. Open Extender > Extensions > Add, choose extension type `Python`, and select `tools/burp/traffic_capture_extension.py`.
4. Compare the capture file the extension prints when it loads with the one [scripts/check_burp_interception.py](#checking-the-whole-chain-before-a-batch-of-runs) prints when it runs — both halves must name the same path. The extension resolves it on its own, as `artifacts/work/ios/traffic_interception/capture.jsonl` under the repository it was loaded from, so nothing in the file needs editing. It is dependency-free and does not read the backend's storage settings, so set `MPA_BURP_CAPTURE_PATH` to an absolute path when Burp runs somewhere that path does not exist, or when `WORK_DIR` puts the capture file elsewhere. If it cannot locate the repository it records nothing and says so in the Extender output.

It hasn't been exercised against a real Burp Suite instance; treat it as a starting point to verify, not a guaranteed-working drop-in.

### Checking the whole chain before a batch of runs

Device proxy configuration, CA trust, and the capture extension are all set up once, outside this framework, and nothing re-verifies them before every run. If any one of them is wrong, every app just comes back `Inconclusive` — which looks identical to "the app actually has good pinning." Before trusting a batch of `Inconclusive` results across many apps, run [scripts/check_burp_interception.py](../../scripts/check_burp_interception.py) once:

```bash
python scripts/check_burp_interception.py --config configs/ios.yaml
```

It checks Burp's proxy is reachable, opens a known HTTPS test URL (`https://example.com` by default) on the device via `mobile: deepLink` (no need to interact with Safari's address bar UI), and confirms that a matching HTTPS exchange shows up in the capture file — i.e. the full proxy → CA trust → extension chain is genuinely decrypting traffic, not just configured-looking. On `PASS`, it atomically writes the capture file's sibling `.health.json` record with the verification time, normalized proxy, canonical capture path, canary host, valid-entry count, hashed device UDID, and tool version. A selected traffic-interception run compares that record's proxy, path, device, and age during preflight and reports any mismatch or stale state as a non-blocking warning. A fresh record proves only that the canary worked at that recorded time; it does not make a later `Inconclusive` application result proof of pinning, because the capture source, app activity, or handshake outcome may differ during the later run. `FAILED` does not create or update the record and points at which part of the chain to check.

## ios-feature-03-risk-01

`ios-feature-03-risk-01` tests whether sensitive text shown in the target app can be read from a screen recording made by another app that the user has authorised to broadcast the screen. The recorder is the tester-owned ReplayConsentRecorder companion app (`artifacts/companion/ios/ipas/ReplayConsentRecorder.ipa`), which writes a ReplayKit broadcast to an `.mp4` and saves a screenshot from the broadcast every few seconds.

Workflow:

1. Clean start: remove any recorder left over from an earlier run. Uninstalling it also wipes its App Group folder, so old recordings cannot produce a false result. A recorder that cannot be removed ends the run as `DIRTY_STARTING_STATE`.
2. Install the recorder IPA, re-signing it first when its profile has expired or the install is rejected for its signature (`recorder_app.resign`).
3. Acquire and install the target app IPA.
4. Open the recorder, fill its dummy sign-in (its start button stays disabled until both fields are filled), tap **Start Phone Screen Recording**, then **Start Broadcast** on the iOS sheet, and wait for the countdown.
5. When controls are enabled (the default), hold the recorder onscreen for six seconds and save a reference before the target window. Its dummy username displays a unique control marker. Then record the capture-window start time, bring the target app to the front, focus a text field (with auto-navigation when configured), and type a unique canary. When the screen has a password field, type a second canary into it. Save an Appium reference of the target canary, hold for `capture_window_seconds` while polling its foreground state once per second, then save a second reference and record the window end time. Device clock readings bound the timing uncertainty.
6. Return to the recorder, hold it for a second six-second control window and save its reference, then tap the start button again, which brings up **Stop Broadcast**. Then tap **Export Evidence**, which copies the newest recording and the screenshots taken since it started into the recorder's Documents folder, writing `done.json` last.
7. Pull `@<recorder>:documents/Evidence` through Appium, retrying until `done.json` arrives or `export_timeout_seconds` passes.
8. Keep the video as `recording.mp4` in the report directory.
9. OCR the target Appium reference first to verify the plain canary was observable. Check the recorder marker in references and broadcast frames from each separate control window. OCR the broadcast screenshots whose timestamps fall inside the target capture window with Tesseract. When none do, still frames are pulled from the `.mp4` instead.
10. When no target canary was matched and controls passed, decode the complete video to verify target coverage and recorder markers in this same recording.
11. In `finally`: stop the broadcast if it is still running, uninstall both apps, and write the result.

| Outcome | `final_status` | Verdict | Severity |
| --- | --- | --- | --- |
| The configured probe cannot reach an editable text field | `TEXT_FIELD_UNREACHABLE` | Inconclusive | info |
| The target app is not in the foreground after launch, prompt handling or a navigation tap | `TARGET_NOT_FOREGROUND` | Inconclusive | info |
| A canary is readable in a frame | `RISK_EXISTS` | At Risk | high |
| All decoded frames in a verified canary window are uniform, with visibility, foreground, timing, coverage and same-video controls passing | `SCREEN_CAPTURE_OBSCURED_VERIFIED` | Reduced Risk | low |
| Sampled frames are blank or uniform, but required coverage checks did not pass | `CANARY_NOT_OBSERVED` | Inconclusive | info |
| Frames are readable but no canary was recognised | `CANARY_NOT_OBSERVED` | Inconclusive | info |
| Required OCR or video-processing dependencies are missing, or `ocr_provider: none` | `OCR_UNAVAILABLE` | Inconclusive | info |
| Video extraction, image processing or OCR fails, and no canary is recognised in another frame | `OCR_FAILED` | Inconclusive | info |
| The broadcast never started, or no usable capture evidence was available without a processing error | `BROADCAST_NOT_STARTED` | Inconclusive | info |
| The export, pull or `max_pull_bytes` limit failed | `RECORDING_RETRIEVAL_FAILED` | Inconclusive | info |
| A leftover recorder could not be removed | `DIRTY_STARTING_STATE` | Inconclusive | info |

`TEXT_FIELD_UNREACHABLE` means the initial search and any configured navigation did not reach an editable field. It does not establish that the app has no fields or that capture is protected. The canary was not typed, so OCR and coverage analysis were not reached. Review the saved `target_page_source*.xml`, `target_text_field_candidates*.json` and `target_screen*.png`; apps without a reachable field need a different capture probe. Unexpected device or implementation errors remain `FAILED`.

`TARGET_NOT_FOREGROUND` means the target app was not in front when the probe checked: after launch and prompt handling, before each navigation tap or text-field focus, or before the capture window. A forced-update prompt that hands off to the App Store is a typical cause. No further taps are made in the other app and no canary is typed after the check fails. Test a current build of the app, or one whose launch screen stays in the app.

A sampled frame counts as blank when its thumbnail pixels are nearly uniform. This alone does not establish protection. `SCREEN_CAPTURE_OBSCURED_VERIFIED` additionally requires a video whose complete decoded frame sequence passes the coverage checks below. The result describes the exercised canary window; it does not establish the app's protection mechanism, other screens, or password masking. Older reports may contain `SCREEN_CAPTURE_OBSCURED`, Reduced Risk, from the previous blank-frame classification; that status retains its qualified historical wording.

`capture_controls.json` and `launch_result.capture_controls` contain `target_start_reference`, `target_reference`, `target_window`, `before`, `after` and `coverage` results, capture timestamps, foreground checks, matched control frames and processing errors. Fresh references must confirm the expected app was foreground both before and after saving the screenshot. Recorder controls require both the Appium reference and the matching ReplayKit sample. Strict control-window boundaries prevent a boundary sample from satisfying two phases. Control matches are separate from target canary counts.

A failed visibility control reports that the target canary was not independently verified. Failed recorder controls identify which phase was missing, unreadable or unavailable. Successful controls with an absent target canary remain `CANARY_NOT_OBSERVED`, Inconclusive, unless every coverage check passes. A control processing failure without a target match produces `OCR_FAILED`; a recognised target canary still establishes At Risk when a control fails. Reduced Risk requires all of these coverage checks:

- The plain canary is readable in fresh Appium references at both ends, with foreground confirmation. Foreground observations during the hold must all report the target app, with no observation gap exceeding two seconds.
- Whole-second device clock readings before and after the hold must have consistent offset bounds; excessively slow readings or a changed clock prevent verification.
- A retrieved recording passes its MP4 header check. `recording.valid` is a prerequisite, not a completeness check. The companion's first saved broadcast screenshot must be within the recording's first wall-clock second, bounding the recording origin uncertainty.
- The verified interval excludes three seconds at each end of the canary hold and must last at least five seconds. The default twelve-second hold verifies an interior six-second interval. Shorter holds can still find At Risk but may be unable to establish Reduced Risk.
- The complete video decodes successfully, contains exactly one unedited video track, and has valid increasing presentation timestamps. Empty or edited track segments are rejected because AVFoundation can rebase sample times across missing initial media. Samples must bracket the uncertainty-expanded target interval, with no decoded-frame gap greater than one second.
- Every decoded frame spanning that interval is uniform at full RGB resolution (channel range at most four levels); even a small visible region blocks this verdict. Both unique recorder markers must also be recognised in this same video, in separate uncertainty-bounded control windows.

`launch_result.capture_controls.coverage` and `capture_controls.json` retain the selected interval, offset/origin bounds, decoded frame count, maximum gap, same-video controls and any reason verification failed. Missing evidence or visible target pixels remains Inconclusive. Expected video/control processing failures produce `OCR_FAILED`; unexpected implementation errors remain `FAILED`. A confirmed target match remains At Risk independently of recording validity or these coverage checks.

The canary is unique per run: `canary_prefix` (default `SCR`) followed by the last six letters or digits of the run timestamp, and `PWD` plus the same suffix for the password field. A reused string could match an old recording and give a false positive. OCR ignores spaces and case when matching.

When the password-field canary is readable, `launch_result.ocr.secure_field_exposed` is `true` and the summary reports that the password canary was readable in captured frames. That links the finding to control-01 (use a `SecureField`). A missing OCR match does not verify password masking: the summary distinguishes a canary typed but not observed, a disabled password probe, a secure field that could not be focused, and an unknown probe outcome. `launch_result.probe.typed.secure.status` records `TYPED`, `DISABLED`, or `FOCUS_FAILED`; `TYPED` means the typing command completed, without independently verifying the field's value. A focus failure does not prove the app has no secure field.

`launch_result.ocr.matched_frame_count` counts distinct frames with a match, while `match_count` counts canary hits. Plain and password canaries in one frame therefore produce one matching frame and two hits. The summary uses the distinct frame count and flags any partial OCR failures. Historical reports without a distinct frame count are summarized as canary matches, and absent probe metadata is reported as unknown; normalizing a report does not rewrite its stored contents.

OCR runs through the provider interface in `screen_capture/providers.py`; capture selection, canary matching, verdicts and reporting stay independent of the recognition adapter. The default is Tesseract with English, sparse-text segmentation and dictionary correction disabled. `ocr_matches.json` and `launch_result.ocr` record the selected provider and effective recognition settings. There is no automatic fallback to another OCR engine. Quartz image sampling and AVFoundation video extraction remain macOS-specific.

Whenever the recording is retrieved it is attached as `screen_recording` evidence, whatever the verdict, so a person can review it.

`launch_result.recording.valid` is a header/size sanity check; successful frame decoding determines whether video fallback has usable samples. `ocr_matches.json` lists the attempted frame count, blank frames, canary matches, and `failed_frames` with each failed frame's path and processing error. Processing failures are also retained in `launch_result.ocr` and the result's errors.

Video fallback checks AVFoundation and CoreMedia when extraction is needed. Missing bindings produce `OCR_UNAVAILABLE`; decoding and PNG-writing failures produce `video_errors` containing the recording path, processing stage and sample time where available. Successfully extracted frames are still scanned.

A successfully recognised canary establishes At Risk even when another frame or video sample fails; without a match, any processing failure produces `OCR_FAILED`, Inconclusive. A successful OCR request returning no text is not a processing failure. Frames with a readable canary are copied to `frames/` and attached as screenshots.

## ios-feature-04-risk-01

`ios-feature-04-risk-01` tests whether a third-party custom keyboard can collect text typed into a target app field.

Workflow:

1. Install or verify the keyboard host app.
2. Start the local collection server.
3. Launch the keyboard host app and configure the server URL if accessibility IDs are configured.
4. Wait for the keyboard app to call `POST /pair` and receive a token.
5. Install and launch the target app IPA.
6. Focus a text field, optionally using auto-navigation.
7. Attempt to switch to the custom keyboard.
8. Type a configured probe string, for example `hello123`.
9. Launch the keyboard host app and verify its local log UI contains the probe string, or verify collection events posted to the local server.

`RISK_EXISTS` means the tested app/field allowed the configured custom keyboard to observe and store the probe text. This is the collection-side risk: with user-enabled Full Access, a third-party keyboard can collect keystrokes from non-secure fields where the app allows custom keyboards.

`KEYSTROKE_COLLECTION_NOT_OBSERVED` means the field was reached and the probe was typed, but the configured evidence source did not contain the probe text before timeout.

When the same probe string is reused across apps, clear the keyboard app's local log or use a unique probe per run to avoid stale findings.

The framework's local-log matching supports two evidence shapes:

- full-string evidence, where the log contains the complete configured probe such as `hello`;
- ordered-keystroke evidence, where the log stores separate entries such as `h`, `e`, `l`, `l`, `o` and they appear in order.

See [Manual LocalKeyboard Server](manual-local-keyboard-server.md) to run the collection server by itself for phone-side manual testing.

## Artifact Sources

- `local_ipa`: validates the local IPA at the exact path in `artifact.ipa` and places it in the run folder as a hard link to its stored copy in `acquired/.by-sha256/`.
- `intake_ipa`: stores no path, and needs no bundle ID. Looks the IPA up in `artifacts/intake/ios/ipas/` (override with `artifact.intake_dir`) by reading each candidate's `Info.plist`: by `artifact.expected_bundle_id` when one is set, otherwise by matching the app's `name` against the build's `CFBundleDisplayName`, case-, space- and punctuation-insensitively. Several versions of one app resolve to the newest; several *different* apps sharing a display name are reported as ambiguous rather than guessed. Built for the install-from-App-Store-then-extract-off-the-device workflow, where a pinned path goes stale on every version bump: drop a newer extraction in and the next run picks it up with no config change. Validates with no file present and no `bundle_id` — both are filled in from the matched build — so an app can be registered before its build has been obtained. Otherwise identical to `local_ipa`.
- `ci_artifact`: currently validates a configured local IPA path; future CI fetching can be added.
- `vendor_ipa`: currently validates a configured local IPA path with separate reporting identity.
- `xcode_archive_export`: currently accepts a configured local IPA path; it does not create certificates or profiles.
- `installed_app_reference`: verifies an installed bundle ID through Appium for black-box-only risks and does not produce an IPA.

## Binary Mutability Inspection

The framework inspects the main executable before static analysis. If `otool -l` reports `LC_ENCRYPTION_INFO` or `LC_ENCRYPTION_INFO_64` with non-zero `cryptid`, the binary is reported as `PROTECTED_OR_ENCRYPTED_BINARY` in the detailed analysis metadata.

The framework reports this condition and does not try to bypass it.

## Risk Metadata

**`configs/split/ios/risks.yaml` is the source of truth for everything a dashboard displays about a risk** — its `name`, `description`, `tactic` and `demonstration`. The `Risk` class holds none of that text: it carries only `risk_id`, `feature_id` and the flags that change what a run does (`is_blocking`, `requires_ipa_artifact`, `requires_device`, `automation_available`). `list_risks()` returns the class's empty defaults and `GET /platforms/{platform}/risks` overlays the YAML entry on top, so the API response is what the YAML says.

These YAML entries are the **fallback**, not the first source. Where the playbook has a document for the risk, `GET /platforms/{platform}/risks` overlays the parsed `### Title` onto `name`, the parsed `### Description` onto `description`, and the description's MITRE annotation onto `tactic` and `tactic_id`; the YAML value is used only where the playbook says nothing. `tactic` is the MITRE ATT&CK for Mobile tactic name (`"Discovery"`, `"Collection"`, or `null` where the risk isn't mapped) and `tactic_id` its `TA####` identifier. Risks that have no playbook document — Android today — still read entirely from the YAML. See [the playbook document format](../developer-playbook.md#document-format) for how those sections are authored.

Both halves of an entry are editable over HTTP without a code change: `PUT /platforms/ios/risks/{risk_id}` replaces the metadata fields (any of `name`, `description`, `tactic`; anything else is a 422), and `PUT /platforms/ios/risks/{risk_id}/demonstration` replaces the demonstration. Neither touches the run.

A missing or unreadable `risks.yaml` is treated as "no entries" rather than an error. The catalogue endpoint is what a dashboard uses to list an app's tests at all, so a read failure there would show an app with *no tests* — a far worse outcome than showing tests with empty descriptions. The file is gitignored and machine-local, so its absence is a normal state on a fresh checkout, not a defect.

Similarly, a feature_id's own `name`/`description` (there's no `Feature` class — `feature_id` is just a string tag on each risk) live in `configs/split/ios/features.yaml`, exposed at `GET /platforms/ios/features` and editable at `PUT /platforms/ios/features/{feature_id}`.

A demonstration step can also carry screenshots from the playbook, cited relative to `playbook_dir` at the top of `risks.yaml` so relocating the playbook doesn't break them — see [Playbook images](../api.md#playbook-images).

## Adding An iOS Risk

1. Add a new class under `mobile_playbook/platforms/ios/risks/`.
2. Subclass `Risk` and set a unique `risk_id`; the displayed text comes from the playbook document or the YAML entry, never from the class.
3. Reuse artifact providers, Appium operations, and report writing where possible.
4. Add mocked pytest coverage for device and external-tool behavior.

Risks are discovered automatically: `mobile_playbook/platforms/ios/risks/registry.py` scans the folder for concrete `Risk` subclasses and picks up any file that defines one, keyed by its `risk_id`. Adding the file is enough — nothing else needs editing.

If the risk needs settings shared across every app (an analyzer endpoint, a companion app's IPA path, and so on) rather than repeated per app, give it a global settings field: add it to `GlobalConfig` in `platforms/ios/models.py`, map the risk ID to that field name in `RISK_GLOBAL_SETTINGS_FIELD` (`platforms/ios/config.py`), and read the risk's effective config in `run()` via `effective_risk_config(global_config, self.risk_id, app_config.risks.get(self.risk_id))` — this merges the shared defaults with whatever the app's own `risks.<risk_id>` entry overrides. See [Configuration](configuration.md#global-risk-settings) for the config-file side.

## Adding iOS Apps

Add another object under `apps` in `configs/ios.yaml`. App identity, artifact source, expected behavior, and enabled risks all live in config, not in code.
