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

`ios-feature-02-risk-01` demonstrates whether the app's network traffic can be intercepted and read in cleartext through an operator-controlled MITM proxy (Burp Suite), which indicates the app does not enforce certificate/public-key pinning.

This risk assumes the device has already been configured to route its traffic through Burp and trust Burp's CA — that setup is device-level, one-time, and outside this framework's control (see [Configuration](configuration.md#traffic-interception)). The framework itself only checks that Burp's proxy port is reachable, then drives the app and reads what a companion Burp extension captured.

Workflow:

1. Check that `traffic_interception.burp.proxy_url` is configured and reachable.
2. Install and launch the target app IPA.
3. Optionally tap through a configured list of accessibility IDs to exercise the app and generate traffic.
4. Wait `exercise.exercise_wait_seconds` for the app to make network calls.
5. Read any new lines appended to `burp.capture_path` since this test started, filtering to `expected_hosts` if configured.

`RISK_EXISTS` means decrypted HTTP traffic matching `expected_hosts` was captured through Burp during the exercise window — the app's traffic can be read by anyone who can MITM the connection, i.e. no effective certificate/public-key pinning.

`TRAFFIC_INTERCEPTION_NOT_OBSERVED` means nothing matching was captured. This is reported as `Inconclusive`, not `Reduced Risk` — Burp's HTTP-message log only shows requests that completed a decrypted handshake, so this framework cannot distinguish "the app refused the MITM'd connection (pinning worked)" from "the app just didn't make a network call during this window." Treat a run of `Inconclusive` results here as a prompt to review the raw Burp proxy history manually, not as proof of pinning.

The dashboard-facing `summary`/`evidence` fields (in `dashboard_results.json`, via `GET /runs/{run_id}/summary`) go beyond the bare `final_status` for this risk specifically: `summary` reads as e.g. `"14 decrypted request(s) captured through Burp (api.example.com, ...)"` when traffic was captured, and `evidence` includes a `capture_log` entry pointing at that run's `burp_capture.json` — the full list of captured host/method/path/status_code entries.

### Capturing Burp's traffic into `capture.jsonl`

This risk doesn't talk to Burp's own APIs — Burp has no simple built-in "give me proxy history" endpoint. Instead, a small companion Burp extension (loaded once into Burp, like any other Burp extension) should append one JSON object per decrypted HTTP request to `burp.capture_path`, at least including a `host` field:

```json
{"host": "api.example.com", "method": "GET", "path": "/profile", "status_code": 200}
```

This mirrors how this framework already exposes evidence to itself elsewhere (`events.jsonl`, `appium.log`) — a plain append-only file that both sides agree on, rather than a live API integration.

[tools/burp_traffic_capture_extension.py](../../tools/burp_traffic_capture_extension.py) is a ready-to-load Jython Burp extension that writes this format — see its header comment for one-time setup (a standalone Jython JAR configured in Burp's Extender options, then loading the file as a Python extension). It hasn't been exercised against a real Burp Suite instance; treat it as a starting point to verify, not a guaranteed-working drop-in.

### Checking the whole chain before a batch of runs

Device proxy configuration, CA trust, and the capture extension are all set up once, outside this framework, and nothing re-verifies them before every run. If any one of them is wrong, every app just comes back `Inconclusive` — which looks identical to "the app actually has good pinning." Before trusting a batch of `Inconclusive` results across many apps, run [tools/check_burp_interception.py](../../tools/check_burp_interception.py) once:

```bash
python tools/check_burp_interception.py --config configs/ios.yaml
```

It checks Burp's proxy is reachable, opens a known test URL (`https://example.com` by default) on the device via `mobile: deepLink` (no need to interact with Safari's address bar UI), and confirms that host actually shows up in the capture file — i.e. the full proxy → CA trust → extension chain is genuinely intercepting, not just configured-looking. `PASS` means you can trust `Inconclusive` results from here on as meaning "pinning likely worked," not "the setup is broken." `FAILED` points at which of the three pieces to check.

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

- `local_ipa`: validates and copies a local IPA.
- `ci_artifact`: currently validates a configured local IPA path; future CI fetching can be added.
- `vendor_ipa`: currently validates a configured local IPA path with separate reporting identity.
- `xcode_archive_export`: currently accepts a configured local IPA path; it does not create certificates or profiles.
- `installed_app_reference`: verifies an installed bundle ID through Appium for black-box-only risks and does not produce an IPA.

## Binary Mutability Inspection

The framework inspects the main executable before static analysis. If `otool -l` reports `LC_ENCRYPTION_INFO` or `LC_ENCRYPTION_INFO_64` with non-zero `cryptid`, the binary is reported as `PROTECTED_OR_ENCRYPTED_BINARY` in the detailed analysis metadata.

The framework reports this condition and does not try to bypass it.

## Risk Metadata

Each risk carries descriptive metadata as class attributes alongside `risk_id`/`feature_id`/`name`: `description` (what the risk is), `goal` (what the test is trying to show), `is_blocking` (whether a positive finding should block a release/compliance sign-off), and `mitre_attack_mobile_technique_id` (the MITRE ATT&CK for Mobile tactic or technique this risk maps to, or `None` if not yet mapped — currently the tactic name, e.g. `"Discovery"`, since not every risk has a clean single-technique match). `list_risks()` and `GET /platforms/{platform}/risks` (see [HTTP API](../api.md)) return all of these alongside the existing fields.

`GET /platforms/{platform}/risks` also returns each risk's `demonstration` — the setup/steps content a dashboard shows for "how to demonstrate this risk". Unlike the fields above, this doesn't live on the `Risk` class itself: it's stored in `configs/split/ios/risk_demonstrations.yaml`, editable via `PUT /platforms/ios/risks/{risk_id}/demonstration` without a code change. Similarly, a feature_id's own `name`/`description` (there's no `Feature` class — `feature_id` is just a string tag on each risk) live in `configs/split/ios/features.yaml`, exposed at `GET /platforms/ios/features` and editable at `PUT /platforms/ios/features/{feature_id}`.

## Adding An iOS Risk

1. Add a new class under `mobile_playbook/platforms/ios/risks/`.
2. Subclass `Risk` and set a unique `risk_id`, plus `description`/`goal` describing the risk and what the test demonstrates.
3. Reuse artifact providers, Appium operations, and report writing where possible.
4. Add mocked pytest coverage for device and external-tool behavior.

Risks are discovered automatically: `mobile_playbook/platforms/ios/risks/registry.py` scans the folder for concrete `Risk` subclasses and picks up any file that defines one, keyed by its `risk_id`. Adding the file is enough — nothing else needs editing.

If the risk needs settings shared across every app (an analyzer endpoint, a companion app's IPA path, and so on) rather than repeated per app, give it a global settings field: add it to `GlobalConfig` in `platforms/ios/models.py`, map the risk ID to that field name in `RISK_GLOBAL_SETTINGS_FIELD` (`platforms/ios/config.py`), and read the risk's effective config in `run()` via `effective_risk_config(global_config, self.risk_id, app_config.risks.get(self.risk_id))` — this merges the shared defaults with whatever the app's own `risks.<risk_id>` entry overrides. See [Configuration](configuration.md#global-risk-settings) for the config-file side.

## Adding iOS Apps

Add another object under `apps` in `configs/ios.yaml`. App identity, artifact source, expected behavior, and enabled risks all live in config, not in code.
