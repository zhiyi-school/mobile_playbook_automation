# Runtime storage

The backend keeps the files it reads and writes under one root. Source code,
configuration, secrets, database records and the external authored playbook are
not part of this layout.

## Layout

```text
artifacts/
  intake/
    ios/ipas/            IPA inputs
    android/apks/        APK inputs
  derived/
    icons/               extracted application icons
    artifacts/           artifact metadata, keyed by sha256
  reports/               one directory per run, plus the sync ledger,
                         lock, worker state and job registry
  work/
    ios/, android/       working directories, acquired originals and
                         unpacked bundles still referenced by evidence
```

`work/` is **not** disposable: report evidence references files inside it, and
interrupted runs resume from it.

## Configuration

Each location resolves in this order, highest first:

1. Its own setting — `INTAKE_DIR`, `ARTIFACT_STORE_DIR` (derived),
   `REPORTS_DIR`, `WORK_DIR`. Point one of these somewhere else and it wins.
2. `ARTIFACTS_DIR`/`<name>`, defaulting to `<repository>/artifacts/<name>`.

A relative value resolves against the repository root, never the process
working directory, so the API, CLI and workers agree wherever they are started
from. Everything goes through `mobile_playbook/storage/paths.py`; no module
builds its own path string. `ARTIFACTS_DIR`, `INTAKE_DIR` and `WORK_DIR` sit on
the API's `.env` allowlist alongside `ARTIFACT_STORE_DIR` and `REPORTS_DIR`, so
no other key in `.env` becomes readable by the API process.

## Historical path references

A few older reports record absolute paths under the previous repository-root
layout, such as `<repository>/work/ios/acquired/...`. `resolve_recorded_path()`
in the same module maps such a path onto its file under `artifacts/`, and
returns anything else unchanged. It is bounded to the four known locations, so
it grants no wider filesystem access, and reports are never rewritten — doing so
would change their digests and make the sync ledger reprocess them.

Evidence references are unaffected. An evidence `ref` encodes a location name
plus a relative path and is resolved at request time, so evidence follows the
configured location with no rewriting. Artifact hashes, `icons/<id>.png`
references and database record identities are unchanged by the layout.
