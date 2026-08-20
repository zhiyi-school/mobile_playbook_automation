# iOS Risks

## ios-feature1-risk1

`ios-feature1-risk1` demonstrates the risk that an acquired IPA can be analyzed on a workstation, leading to discovery of application metadata, bundled resources, frameworks, plugins, permissions, URL schemes, binary characteristics, API keys, credentials, and other embedded sensitive strings.

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

## Adding An iOS Risk

1. Add a new class under `mobile_playbook/platforms/ios/risks/`.
2. Subclass `Risk` and set a unique `risk_id`, plus `description`/`goal` describing the risk and what the test demonstrates.
3. Reuse artifact providers, Appium operations, and report writing where possible.
4. Add mocked pytest coverage for device and external-tool behavior.

Risks are discovered automatically: `mobile_playbook/platforms/ios/risks/registry.py` scans the folder for concrete `Risk` subclasses and picks up any file that defines one, keyed by its `risk_id`. Adding the file is enough — nothing else needs editing.

If the risk needs settings shared across every app (an analyzer endpoint, a companion app's IPA path, and so on) rather than repeated per app, give it a global settings field: add it to `GlobalConfig` in `platforms/ios/models.py`, map the risk ID to that field name in `RISK_GLOBAL_SETTINGS_FIELD` (`platforms/ios/config.py`), and read the risk's effective config in `run()` via `effective_risk_config(global_config, self.risk_id, app_config.risks.get(self.risk_id))` — this merges the shared defaults with whatever the app's own `risks.<risk_id>` entry overrides. See [Configuration](configuration.md#global-risk-settings) for the config-file side.

## Adding iOS Apps

Add another object under `apps` in `configs/ios.yaml`. App identity, artifact source, expected behavior, and enabled risks all live in config, not in code.
