# Configuration

Every setting the backend reads: YAML config files, their include mechanism,
environment variables, and the secret boundary between the API process and the
dashboard sync worker. For first-time setup steps see [setup.md](setup.md).

All examples use placeholders. Substitute your own identifiers.

## Config file layout

`run`, `validate` and the API all take one entry-point config per platform.
That file carries the single `device` and `runner` profile inline, and pulls
everything that does not fit comfortably in one file — the app roster, per-risk
settings — in through `include:`.

```text
configs/
  ios.yaml                          entry point: device, runner, include
  android.yaml                      entry point: device, runner, include
  split/ios/apps.yaml               the iOS app roster
  split/ios/<risk_settings>.yaml    one file per risk's global settings
  split/ios/risks.yaml              authored risk text and demonstrations
  split/android/...                 the same shape for Android
```

`*.example.yaml` files are tracked; the real files are not. `risks.yaml` is
tracked because its content is authored documentation rather than local state.

### YAML includes

`load_yaml_config` in `mobile_playbook/orchestration/preflight.py` resolves
`include:` (or `includes:`) after loading the entry-point file.

```yaml
include:
  apps: split/ios/apps.yaml
  ipa_static_analysis: split/ios/ipa_static_analysis.yaml
```

Rules:

- Paths are relative to the entry-point config's own directory.
- An included file may contain either the raw section value or the same value
  wrapped under the section name — both `device: {...}` and a bare `{...}`
  work.
- **Inline values in the parent config win over included values**, so a single
  field can be overridden without copying the whole file.
- A section's value may be a *list* of paths instead of one path:

  ```yaml
  include:
    apps: [split/ios/templates.yaml, split/ios/apps.yaml]
  ```

  Listed files are concatenated as raw text in order and then parsed as one
  YAML document. This exists so a `templates.yaml` can define reusable anchors
  (`&name`) that a later file references (`*name`) — anchors only resolve
  within a single parsed document, so merging separately-parsed files would not
  work.

### App entries

```yaml
apps:
  - id: example-app
    name: Example App
    bundle_id: com.example.placeholder
    test_bundle_id: com.example.placeholder.bundle
    artifact:
      source: local_ipa
      ipa: intake/ios/ipas/example-app.ipa
      expected_bundle_id: com.example.placeholder
    risks:
      ios-feature-01-risk-01:
        enabled: true
```

Android entries use `package_name` in place of `bundle_id`/`test_bundle_id`.

`--apps` on the CLI and `apps` in `POST /runs` match against an app's `id`,
`name`, `package_name` or `bundle_id`. Matching is case-insensitive and ignores
non-alphanumeric characters, so `example-app`, `Example App` and `exampleapp`
all select the same entry. Risk selection (`--risks`) is an exact,
whitespace-trimmed match on the risk ID.

Device, runner and per-platform risk settings are documented per platform:
[ios/configuration.md](ios/configuration.md),
[android/configuration.md](android/configuration.md).

## Environment variables and the secret boundary

`.env` at the repository root holds local secrets. It is read by different
processes in deliberately different ways.

| Variable | Read by | Purpose |
| --- | --- | --- |
| `MOBSF_API_KEY` | run process | authenticates to a local MobSF instance for the iOS static-analysis risk |
| `SUPABASE_URL` | sync worker only | dashboard project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | sync worker only | bypasses row-level security |
| `DASHBOARD_SYNC_AUTO_TRIGGER` | API and CLI | `false` disables the post-run worker launch |
| `CORS_ALLOWED_ORIGINS` | API process | exact browser origins allowed to call the API |

### Who may read what

- **The dashboard sync worker** loads the whole `.env` through
  `mobile_playbook/env_file.py`'s `load_env_file()`, because it genuinely needs
  the service-role key. It is the only process that ever holds that key.
- **The API** must not. `mobile_playbook/api/settings.py` reads one allowlisted
  key at a time. `ALLOWED_ENV_KEYS` currently contains only
  `CORS_ALLOWED_ORIGINS`; any other key raises `DisallowedSettingError`. The
  API package does not import `load_env_file` at all, so there is no code path
  from it to whole-file loading.

The practical consequence: putting `SUPABASE_SERVICE_ROLE_KEY` in `.env` does
not put it in the API process's environment, even though both read the same
file.

### Precedence

```text
exported shell variable  >  repository .env  >  application default
```

An explicitly exported value always wins — including an exported **empty**
string, which falls back to the application default rather than reaching into
`.env`. Values in `.env` may be quoted; surrounding quotes are stripped.

Resolution happens when the value is needed, not at import of a launcher, so it
behaves identically under `python -m mobile_playbook.api`, the same command with
`--reload` (whose worker subprocess re-imports the app), and
`uvicorn mobile_playbook.api.app:app`.

### `CORS_ALLOWED_ORIGINS`

```env
CORS_ALLOWED_ORIGINS="http://localhost:5173,https://dashboard.example.com"
```

- Comma-separated, exact origins. Scheme, host and port must all match;
  nothing is normalised or wildcard-expanded.
- Whitespace around each origin is trimmed; empty entries are dropped.
- `*` is **rejected** — the server raises at startup rather than serving a
  wildcard.
- Unset, empty or absent everywhere leaves the defaults
  `http://localhost:5173` and `http://127.0.0.1:5173`.

### `DASHBOARD_SYNC_AUTO_TRIGGER`

Defaults to true. Set `false` on an installation that does not use a dashboard;
runs still write reports, and `GET /sync/status` reports `enabled: false`.

### Frontend-safe variables

None of the above belong in a browser. The dashboard's own variables
(`VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`) live in the
frontend repository and are documented there. The anon key is designed to be
public and is constrained by row-level security; the service-role key is not and
must never appear in frontend source, a `VITE_` variable, a bundle, a committed
file, or a launchd plist.

## Deployment postures

| Posture | Bind | CORS | Notes |
| --- | --- | --- | --- |
| Local development | `127.0.0.1` (default) | defaults are enough | Frontend on `localhost:5173` |
| Trusted LAN | `--host 0.0.0.0` | list the dashboard host's exact origin | Anyone who can route to the host can drive runs and read reports |
| Production / internet | never expose directly | set to the proxy's public origin | Requires an authenticating reverse proxy or VPN in front — see [api.md](api.md#security-model) |

The API has no authentication of its own in any posture. That is a real
limitation, not a configuration option — see
[api.md](api.md#current-limitations-and-follow-up-work).
