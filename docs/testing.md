# Testing

How to run and extend the backend test suite. When a test and the docs
disagree, inspect the current code and intended contract; either can be stale.

## Running

```bash
python -m pytest -q
```

The whole suite runs in roughly ten seconds and needs **no device, no Appium,
no MobSF, no network and no Supabase project**. Anything that would touch those
is faked or injected, which is why it is safe to run on any checkout.

Useful subsets:

```bash
python -m pytest tests/reporting/test_sarif_writer.py -q    # one module
python -m pytest tests/dashboard_sync -q                    # one package
python -m pytest -q -k "sync_status"                        # by name
python -m pytest tests/dashboard_sync/test_sync.py -q -x    # stop at first failure
python -m pytest -q --lf                                    # last failures only
```

Compile check without running anything:

```bash
python -m compileall -q mobile_playbook/
```

Maintenance checks are independent so a failure retains its own diagnostics:

```bash
python scripts/check_requirements.py
python scripts/check_docs.py
python -m ruff check .
python -m pytest -q tests/api/test_reports.py tests/api/test_report_evidence.py \
  tests/api/test_playbook.py tests/playbook/test_validator.py
```

The first command enforces dependency-declaration synchronization. The second
checks local Markdown links and anchors, referenced repository paths, required
documentation entry points, external URL syntax, and accidental user-specific
paths without executing code blocks or using the network. Exact intentional
cross-repository references live in `scripts/doc-validation-allowlist.txt`; stale
exceptions fail validation. The third runs the correctness rules selected in
`pyproject.toml` (syntax errors and undefined or unused names and imports),
and CI runs it on every push.

The focused pytest command covers the report/evidence HTTP contract and the
sanitized playbook parser-to-renderer boundary. A combined playbook artifact
check against an explicit frontend checkout is documented in
[developer-playbook.md](developer-playbook.md#parser-to-frontend-contract-fixture).

### Dependency ownership

`pyproject.toml` is authoritative for runtime and test dependencies.
`requirements.txt` is a generated mirror for tools that require that format;
regenerate it with `python scripts/check_requirements.py --write`. The check
preserves constraints, extras and markers exactly. Neither declaration locks
transitive versions, so a clean installation is reproducible only within those
constraints; review resolver changes when refreshing an environment.

Python 3.11 is the minimum supported runtime and the CI runtime. The repository
uses Ruff for correctness linting, plus `compileall` and pytest. There is no
configured type checker.

### Continuous integration

The GitHub-hosted origin had no CI provider configuration before the maintenance
workflow was added, so `.github/workflows/verify.yml` uses GitHub Actions. Its
jobs run documentation/dependency checks, the full backend suite and compile
check, and the focused API/playbook contract suite. Jobs use synthetic fixtures,
need no secrets or devices, and perform no deployment or synchronization.

## What is covered

The layout of `tests/` mirrors `mobile_playbook/`, so a module's tests live under
the matching path. Paths below are relative to `tests/`.

| Area | Where |
| --- | --- |
| Config loading, includes, validation | `platforms/ios/test_config.py`, `platforms/ios/test_config_work_dir.py`, `platforms/android/test_config.py`, `platforms/ios/test_preflight.py` |
| Example configs and repository scripts | `repository/` |
| Shared helpers and plugin discovery | `common/` |
| IPA acquisition, handling and zip safety | `platforms/ios/acquisition/`, `platforms/ios/ipa/`, `orchestration/test_selection.py` |
| Individual risks | `platforms/ios/risks/`, `platforms/android/risks/`, `platforms/android/test_apk_tools.py` |
| iOS capabilities | `platforms/ios/keyboard/`, `platforms/ios/traffic_interception/`, `platforms/ios/screen_capture/` |
| Appium session handling and recovery | `platforms/ios/test_device_client.py`, `orchestration/test_appium_server.py`, `orchestration/test_*_appium_recovery.py` |
| Run orchestration, manifests, events | `orchestration/`, `reporting/` |
| CLI | `test_cli.py` |
| API routes, schemas, CORS, logging | `api/` |
| Dashboard sync mapping, worker entry point and idempotency | `dashboard_sync/`, `api/test_sync_status.py`, `api/test_run_registry.py` |
| SARIF export | `reporting/test_sarif_writer.py`, `api/test_sarif.py` |
| Developer playbook catalogue | `playbook/`, `api/test_playbook.py` |
| App icons and background workers | `artifact_store/`, `workers/` |

## Conventions worth following

**Tests must not read your machine.** Anything that resolves a real path —
`.env`, `artifacts/reports/`, a config file — takes an injectable path so the test can
point it at `tmp_path`. A test that reads the repository's real `.env` will
pass on your checkout and fail on someone else's. An autouse fixture in
`tests/conftest.py` points `storage_paths.CONFIG_ROOT` at each test's
`tmp_path / "configs"`, so anything resolved through `config_path()` never
touches the real `configs/`.

**Fakes enforce the real constraints.** `FakeStore` in `tests/dashboard_sync/test_sync.py`
emulates the database's unique indexes, `sync_key` deduplication and column
defaults. A fake that accepts anything proves nothing about production, so
extend the fake when you add a constraint.

Pure identity and report mapping code under `dashboard_sync/` is tested
without HTTP, environment credentials or a real database. Worker tests inject
the store factory and pass an argument list; they do not start a long-running
worker or contact Supabase. Configuration-editor tests use `tmp_path`, including
lock serialization and rollback after an introduced validation error.

**Prove a new test can fail.** After writing a test for a fix, revert the fix
and confirm the test fails, then restore it. Several tests in this suite exist
because that step caught an assertion that would have passed either way.

**Prefer structural assertions over golden files** where a format is involved.
`tests/reporting/test_sarif_writer.py`'s `assert_valid_sarif` checks SARIF 2.1.0 structure —
required fields, enum membership, rule/result index consistency, relative URIs,
and the §3.27.10 rule that only a `fail` result may carry a level other than
`none`. That last one matters: the official SARIF JSON Schema does **not**
encode it, so schema validation alone would not catch a regression. The project
deliberately carries no schema-validation dependency; see
[api.md](api.md#sarif-export).

## Testing the developer playbook

The validator, exact identity preview, sanitized contract source, and frontend
artifact regeneration process are documented in
[developer-playbook.md](developer-playbook.md#validation-and-diagnostics).

`tests/playbook/test_catalogue.py` builds a whole playbook directory under `tmp_path`
from the fixtures at the top of the module and points `IOS_PLAYBOOK_DIR` at it.
Three things make that work and are worth preserving:

- **The fixtures use placeholder names** — `example-feature-01-risk-01`,
  `example-feature-01-risk-01-control-01`. No test refers to an application that
  is actually being assessed.
- **The fixture also monkeypatches `settings.ENV_FILE`, `source.RISK_CONFIG_FILES`
  and `catalogue.CONTROL_OVERRIDE_FILES`.** Without `ENV_FILE`, a test would fall
  through to the repository's real `.env` and pass or fail depending on whose
  checkout it ran on. Emptying the two config maps also keeps the catalogue from
  reading any override file under the isolated config root.
- **`catalogue.clear_cache()` runs before and after each test.** The catalogue is
  a module-level cache keyed by platform, so a leaked entry makes a later test
  read the previous test's directory.

`tests/api/test_playbook.py` imports `write_playbook` from that module and exercises
the route functions directly, the same way the other `tests/api/` modules do
— the suite carries no HTTP client dependency.

Two cases in `tests/playbook/test_catalogue.py` exist because mutation testing found
the tests were not actually checking what they claimed: a control whose filename
genuinely differs from its heading — which uncovered a real bug, since the
catalogue was dispatching on the filename and dropping such a control entirely —
and the `__MACOSX` case, whose original form could never reach the ignore filter
it was meant to exercise.

`tests/playbook/test_validator.py` runs the actual catalogue against the portable
fixture and generated invalid directories. It covers stable diagnostics,
strict/JSON behavior, exact identity previews, and the backend half of the
parser-to-renderer contract without reading the external playbook.

## Adding a risk

A risk is discovered by the plugin registry, not registered by hand. Add a
module under `mobile_playbook/platforms/<platform>/risks/` exposing a `Risk`
subclass with a unique `risk_id`, then:

1. Add its authored text to `configs/split/<platform>/risks.yaml`.
2. Add a settings file under `configs/split/<platform>/` if it needs one, and
   an `include:` entry.
3. Add a test module. `tests/common/test_plugin_discovery.py` checks the registry sees it;
   `list-risks` and `GET /platforms/{platform}/risks` pick it up automatically.

## Frontend tests

Report API coverage injects temporary roots and changes the process working
directory. This verifies that creation, lookup, history, evidence enrichment,
registry state and synchronization follow configuration instead of the test
runner's current directory.

The dashboard has its own suite in the frontend repository — `npm test`,
`npm run typecheck`, `npm run lint`, `npm run build`. See its
`docs/testing.md`.
