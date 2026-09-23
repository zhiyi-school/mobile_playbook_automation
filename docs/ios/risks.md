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

[tools/burp_traffic_capture_extension.py](../../tools/burp_traffic_capture_extension.py) is a ready-to-load Jython Burp extension that writes this format. One-time setup:

1. Download a standalone Jython JAR from `https://www.jython.org/download`.
2. In Burp Suite, open Extender > Options > Python Environment and point it at that JAR.
3. Open Extender > Extensions > Add, choose extension type `Python`, and select `tools/burp_traffic_capture_extension.py`.
4. Compare the capture file the extension prints when it loads with the one [tools/check_burp_interception.py](#checking-the-whole-chain-before-a-batch-of-runs) prints when it runs — both halves must name the same path. The extension resolves it on its own, as `artifacts/work/ios/traffic_interception/capture.jsonl` under the repository it was loaded from, so nothing in the file needs editing. It is dependency-free and does not read the backend's storage settings, so set `MPA_BURP_CAPTURE_PATH` to an absolute path when Burp runs somewhere that path does not exist, or when `WORK_DIR` puts the capture file elsewhere. If it cannot locate the repository it records nothing and says so in the Extender output.

It hasn't been exercised against a real Burp Suite instance; treat it as a starting point to verify, not a guaranteed-working drop-in.

### Checking the whole chain before a batch of runs

Device proxy configuration, CA trust, and the capture extension are all set up once, outside this framework, and nothing re-verifies them before every run. If any one of them is wrong, every app just comes back `Inconclusive` — which looks identical to "the app actually has good pinning." Before trusting a batch of `Inconclusive` results across many apps, run [tools/check_burp_interception.py](../../tools/check_burp_interception.py) once:

```bash
python tools/check_burp_interception.py --config configs/ios.yaml
```

It checks Burp's proxy is reachable, opens a known HTTPS test URL (`https://example.com` by default) on the device via `mobile: deepLink` (no need to interact with Safari's address bar UI), and confirms that a matching HTTPS exchange shows up in the capture file — i.e. the full proxy → CA trust → extension chain is genuinely decrypting traffic, not just configured-looking. On `PASS`, it atomically writes the capture file's sibling `.health.json` record with the verification time, normalized proxy, canonical capture path, canary host, valid-entry count, hashed device UDID, and tool version. A selected traffic-interception run compares that record's proxy, path, device, and age during preflight and reports any mismatch or stale state as a non-blocking warning. A fresh record proves only that the canary worked at that recorded time; it does not make a later `Inconclusive` application result proof of pinning, because the capture source, app activity, or handshake outcome may differ during the later run. `FAILED` does not create or update the record and points at which part of the chain to check.

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

- `local_ipa`: validates and copies a local IPA from the exact path in `artifact.ipa`.
- `intake_ipa`: stores no path, and needs no bundle ID. Looks the IPA up in `intake/ios/ipas/` (override with `artifact.intake_dir`) by reading each candidate's `Info.plist`: by `artifact.expected_bundle_id` when one is set, otherwise by matching the app's `name` against the build's `CFBundleDisplayName`, case-, space- and punctuation-insensitively. Several versions of one app resolve to the newest; several *different* apps sharing a display name are reported as ambiguous rather than guessed. Built for the install-from-App-Store-then-extract-off-the-device workflow, where a pinned path goes stale on every version bump: drop a newer extraction in and the next run picks it up with no config change. Validates with no file present and no `bundle_id` — both are filled in from the matched build — so an app can be registered before its build has been obtained. Otherwise identical to `local_ipa`.
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
