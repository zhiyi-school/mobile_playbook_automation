# Backend artifacts

This directory holds local application builds, helper apps, generated metadata,
reports and working files used by the automation backend. Runtime contents stay
on the automation host; Supabase stores dashboard records and references to these
files. Only this README is tracked in Git.

## Folder layout

Paths below are relative to `artifacts/`. Run IDs, app IDs, risk IDs and checksums
are placeholders; folders are created as the corresponding workflows need them.

```text
artifacts/
├── README.md
├── intake/
│   ├── ios/ipas/
│   └── android/apks/
├── companion/
│   └── ios/ipas/
├── derived/
│   ├── artifacts/
│   └── icons/
├── reports/
│   └── <run_id>/
│       ├── evidence/
│       └── <platform>/<app_id>/<risk_id>/<test_case_id>/
└── work/
    ├── ios/
    │   ├── acquired/
    │   │   ├── .by-sha256/
    │   │   └── <run_id>/<app_id>/
    │   ├── <run_id>/<app_id>/<risk_id>/
    │   └── traffic_interception/
    ├── android/
    │   └── repackaging/<package_name>/
    └── logs/
```

## Inputs: `intake/`

| Folder | Contents and use |
| --- | --- |
| `intake/ios/ipas/` | Target iOS `.ipa` builds, supplied locally or through the upload API. The intake provider locates a matching application build for a test. |
| `intake/android/apks/` | Target Android `.apk` builds accepted by the upload API and inspected for metadata and icons. Some device tests obtain installed APKs through ADB instead. |

These are input builds, not reports or modified working copies. Keep builds needed
by configured applications and assessments. Upload behavior is documented in the
[API guide](../docs/api.md).

## Helper apps: `companion/`

| Folder | Contents and use |
| --- | --- |
| `companion/ios/ipas/` | Tester-owned helper app packages, such as LocalKeyboard and ReplayConsentRecorder. Selected tests install these alongside the target app. |

Companion source and build projects live under `tools/companion_apps/`; this folder
holds installable packages. Package selection depends on platform risk settings.
See the [iOS configuration guide](../docs/ios/configuration.md).

## Generated metadata and icons: `derived/`

| Folder | Contents and use |
| --- | --- |
| `derived/artifacts/` | `<sha256>.json` metadata records for inspected builds, including icon extraction information. |
| `derived/icons/` | `<sha256>.png` application icons extracted from those builds. |

The SHA-256 identifies the build bytes. An icon reference belongs to that build,
so uploading a newer version does not change the identity of an older result.
The backend serves these icons; dashboard records hold references to them.

## Results and publication state: `reports/`

Each `<run_id>/` directory represents one run. Current IDs use timestamps such as
`YYYY-MM-DD_HH-MM-SS`, with a numeric suffix when required to avoid a collision.
A folder existing does not prove that its run completed or reached the dashboard.

| Location | Contents and use |
| --- | --- |
| `<run_id>/` | Run-level `summary.md`, normalized `dashboard_results.json`, progress `events.jsonl`, terminal `run_manifest.json`, optional `results.sarif`, and synchronization state. Availability depends on the run's lifecycle. |
| `<run_id>/<platform>/` | Results grouped by `ios` or `android`. |
| `<run_id>/<platform>/<app_id>/` | Results for one configured application. |
| `<run_id>/<platform>/<app_id>/<risk_id>/` | Results for one security risk. |
| `<run_id>/<platform>/<app_id>/<risk_id>/<test_case_id>/` | Detailed `report.json`, `logs.txt`, and test-specific evidence such as images, recordings or analysis output. |
| `<run_id>/evidence/` | Run-level evidence directory created by the report writer. It may be empty because tests also save evidence in their own result directories or reference working files. |

Individual tests may create further evidence folders, for example `frames/`,
`video_frames/` or `pulled/`. Their contents and names depend on the test.

The report root also holds hidden operational files: `.job_registry.json` tracks
API runs; `.dashboard_sync_ledger.json` records published report digests;
`.dashboard_sync.lock` coordinates publication; and `.dashboard_sync_worker.json`
records worker state. A run may contain `sync_status.json` and `.sync_status.lock`.
These are runtime state, not source configuration.

Preserve report bytes and publication state: editing a report can change its
synchronization digest. See the [API guide](../docs/api.md) and
[worker operations](../docs/operations.md).

## Working data: `work/`

| Folder | Contents and use |
| --- | --- |
| `work/ios/acquired/<run_id>/<app_id>/` | Acquired input IPA, normally `original.ipa`, for that run and application. |
| `work/ios/acquired/.by-sha256/` | Shared read-only IPA copies, named by checksum. Per-run originals can be hard links to these files; copying is the fallback. Never modify a shared original in place. |
| `work/ios/<run_id>/<app_id>/<risk_id>/` | Test working files, such as unpacked bundles, inspection output and generated packages. Older runs may use short hexadecimal IDs or different internal layouts. |
| `work/ios/traffic_interception/` | Shared traffic-capture data, normally `capture.jsonl`, produced by the Burp integration and consumed by the traffic test. |
| `work/android/repackaging/<package_name>/` | Device-derived APK backups and intermediate package-processing files. Package folders use underscores in place of dots. This workflow is organized by package rather than run ID. |
| `work/logs/` | Optional API logs, including rotated copies when the API is launched with `--log-file` pointing here. This folder is a documented logging convention, not a required output of every run. |

Other logs can appear directly under `work/` or its platform folders, including
dashboard synchronization and Appium logs. Configured tools can create additional
working folders.

Working data is not a disposable build cache: report evidence can reference it.
API restart recovery marks interrupted API runs failed; it does not automatically
resume their tests. Before cleanup, review the documented `prune-work` plan.
Pruning protects active runs and references from reports inside the retention
window; older report files can remain after their working evidence is removed.
Shared capture files, logs and package-based folders are not all covered by that
run-folder pruning policy. See
[pruning old work](../docs/operations.md#pruning-old-run-work-folders).

## Configured locations

This is the default local layout. `ARTIFACTS_DIR` changes the runtime root;
`INTAKE_DIR`, `ARTIFACT_STORE_DIR`, `REPORTS_DIR` and `WORK_DIR` override individual
locations. Relative storage settings resolve against the repository root. Explicit
CLI output paths and individual risk settings can select other locations.

The authoritative path and retention references are
[runtime storage](../docs/storage.md) and [operations](../docs/operations.md).
Application configuration, credentials, database migrations and authored playbooks
have their own locations outside this runtime tree.
