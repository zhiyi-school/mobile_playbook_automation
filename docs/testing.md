# Testing

How to run and extend the backend test suite. The tests are the source of truth
for behaviour: where this documentation and a test disagree, the test is right.

## Running

```bash
python -m pytest -q
```

The whole suite runs in roughly ten seconds and needs **no device, no Appium,
no MobSF, no network and no Supabase project**. Anything that would touch those
is faked or injected, which is why it is safe to run on any checkout.

Useful subsets:

```bash
python -m pytest tests/test_sarif_writer.py -q          # one module
python -m pytest -q -k "sync_status"                     # by name
python -m pytest tests/test_dashboard_sync.py -q -x      # stop at first failure
python -m pytest -q --lf                                 # last failures only
```

Compile check without running anything:

```bash
python -m compileall -q mobile_playbook/
```

## What is covered

| Area | Modules |
| --- | --- |
| Config loading, includes, validation | `test_config.py`, `test_android_config.py`, `test_ios_preflight.py` |
| Plugin discovery for risks | `test_discovery.py` |
| Artifact intake, IPA handling, zip safety | `test_artifacts.py`, `test_artifact_intake.py`, `test_intake_ipa_artifact.py`, `test_ipa_mutability.py`, `test_safe_zip.py` |
| Individual risks | `test_risk_feature_01_risk_01.py`, `test_risk_feature_02_risk_01.py`, `test_risk_feature_04_risk_01.py`, `test_android_screen_capture_verdict.py`, `test_android_apk_tools.py` |
| Appium session handling and recovery | `test_appium_client.py`, `test_appium_process.py`, `test_platform_runner_appium_recovery.py`, `test_scan_runner_appium_recovery.py` |
| Run orchestration, manifests, events | `test_run_manifest.py`, `test_run_events.py`, `test_run_all_report_isolation.py`, `test_report.py` |
| CLI | `test_cli.py` |
| API routes, models, CORS, logging | `test_api_*.py` |
| Dashboard sync and idempotency | `test_dashboard_sync*.py`, `test_sync_status.py`, `test_job_registry.py` |
| SARIF export | `test_sarif_writer.py`, `test_api_sarif.py` |

## Conventions worth following

**Tests must not read your machine.** Anything that resolves a real path —
`.env`, `reports/`, a config file — takes an injectable path so the test can
point it at `tmp_path`. A test that reads the repository's real `.env` will
pass on your checkout and fail on someone else's.

**Fakes enforce the real constraints.** `FakeStore` in `test_dashboard_sync.py`
emulates the database's unique indexes, `sync_key` deduplication and column
defaults. A fake that accepts anything proves nothing about production, so
extend the fake when you add a constraint.

**Prove a new test can fail.** After writing a test for a fix, revert the fix
and confirm the test fails, then restore it. Several tests in this suite exist
because that step caught an assertion that would have passed either way.

**Prefer structural assertions over golden files** where a format is involved.
`test_sarif_writer.py`'s `assert_valid_sarif` checks SARIF 2.1.0 structure —
required fields, enum membership, rule/result index consistency, relative URIs,
and the §3.27.10 rule that only a `fail` result may carry a level other than
`none`. That last one matters: the official SARIF JSON Schema does **not**
encode it, so schema validation alone would not catch a regression. The project
deliberately carries no schema-validation dependency; see
[api.md](api.md#sarif-export).

## Adding a risk

A risk is discovered by the plugin registry, not registered by hand. Add a
module under `mobile_playbook/platforms/<platform>/risks/` exposing a `Risk`
subclass with a unique `risk_id`, then:

1. Add its authored text to `configs/split/<platform>/risks.yaml`.
2. Add a settings file under `configs/split/<platform>/` if it needs one, and
   an `include:` entry.
3. Add a test module. `test_discovery.py` checks the registry sees it;
   `list-risks` and `GET /platforms/{platform}/risks` pick it up automatically.

## Frontend tests

The dashboard has its own suite in the frontend repository — `npm test`,
`npm run typecheck`, `npm run lint`, `npm run build`. See its
`docs/testing.md`.
