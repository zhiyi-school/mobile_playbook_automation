# Developer remediation playbook

The backend serves two different kinds of instruction, and keeping them apart
is the point of this document.

## Terminology

| Term | Meaning | Who follows it | Where it lives |
| --- | --- | --- | --- |
| **Risk** | The security problem an assessment discovered | — | `configs/split/<platform>/risks.yaml` |
| **Security demonstration** | The steps security uses to demonstrate or validate the risk | Security | the `demonstration` block in `risks.yaml` |
| **Control** | The remediation approach that addresses a risk | Developers | one Markdown file in the external playbook directory |
| **Control step** | One action a developer performs to implement a control | Developers | a numbered item inside that Markdown file |
| **Developer progress** | How far a developer has got through a control's steps | Developers | Supabase, in the dashboard repository |

A security demonstration is never presented as developer remediation
instructions, and a control is never presented as a way to reproduce the risk.
They come from different files and different endpoints.

## Where the playbook lives

The playbook is **external to this repository** and read-only from the
backend's point of view. Nothing is copied in, and nothing is written back.

Resolution order for a platform's directory, first match wins:

1. The platform's environment variable — `IOS_PLAYBOOK_DIR`, `ANDROID_PLAYBOOK_DIR`
2. The `playbook_dir` key in `configs/split/<platform>/risks.yaml`
3. Nothing configured — every control endpoint answers `503` and
   `GET /platforms/{platform}/risks` reports `controls_available: false`

```env
IOS_PLAYBOOK_DIR=/opt/app/playbooks/ios
ANDROID_PLAYBOOK_DIR=/opt/app/playbooks/android
```

Both keys are on the API's `ALLOWED_ENV_KEYS` allowlist, so the API process can
read them from `.env` without loading the rest of that file. See
[configuration.md](configuration.md#who-may-read-what).

An unreadable directory is always reported, never silently treated as "this
risk has no controls".

The same resolution serves the security demonstration screenshots referenced
from `risks.yaml` (`/platforms/{platform}/playbook/images/{path}`), so the two
subsystems always read from one root. They stay separate in every other
respect: a demonstration is security's, a control is the developer's, and
neither is ever rendered as the other.

## Expected directory layout

```text
<playbook root>/
  <prefix>-feature-01-risk-01.md                     a risk
  <prefix>-feature-01-risk-01-control-01.md          a control for it
  <prefix>-feature-01-risk-01-control-02.md
  attachments/*.png                                  screenshots
  implemented_controls/*.zip                         reference implementations
```

`.DS_Store`, `__MACOSX/`, `.obsidian/` and anything inside them are ignored, so
a directory that arrived as a zip works without cleaning up first.

### Document format

A risk document:

```markdown
## example-feature-01-risk-01

### Description

Because the platform provides <feature>, your app is at risk of <threat>.

### Goal

As a result, this could lead to _**Tactic**_ — <consequence>.

### Demonstration

1. A step security performs to demonstrate the risk.

Feature-01-Risk-01 control measures:

- [example-feature-01-risk-01-control-01](example-feature-01-risk-01-control-01.md)
```

A control document:

```markdown
## example-feature-01-risk-01-control-01

Your app can reduce this risk by taking the following steps:

<!-- playbook-step-id: example-first-change -->
1. The first thing the developer changes.

<img src="attachments/example_control_ss1.png" width="400" alt="Alt text">

*What the screenshot shows*

<!-- playbook-step-id: example-second-change -->
2. The second thing the developer changes.

### References

- https://example.test/reference

The source code with the implemented control can be found [here](implemented_controls/example-feature-01-risk-01-control-01.zip).
```

Both HTML `<img>` tags and Markdown `![]()` images are read. An italic-only
paragraph directly under an image becomes that image's caption.

## Step identifiers

A developer's progress is recorded against a step's **identifier**, never
against its position or its wording. Declare one on the line directly above a
numbered step:

```markdown
<!-- playbook-step-id: rotate-example-key -->
1. Rotate the example API key.
```

The comment is a directive, not content: it is never rendered, and it is the
only place a step id should ever be written.

| Rule | Why |
| --- | --- |
| Pick a short, meaningful slug (`[A-Za-z0-9][\w.-]*`) | It is a permanent handle, and it shows up in progress records |
| Never reuse an id for a different instruction | Reuse silently marks the new instruction complete for everyone who finished the old one |
| Never change an id to reflect new wording | Changing it discards every developer's progress on that step |
| Give a genuinely new instruction a new id | That is what makes it appear as outstanding work |

Rewriting a step's text, swapping its screenshot, changing its command or
moving it up or down the list all keep the same id, so progress follows the
step. Only the author deciding to issue a new id makes it a new step.

### Documents without declared ids

A step with no directive falls back to an id derived from its own instruction
text (`auto-<hash>`). That fallback is deliberately conservative:

- Renumbering or reordering steps keeps their ids, so progress survives.
- Inserting or deleting a step does not shift anyone else's id.
- **Rewording a step changes its id**, so it is treated as a new step that
  nobody has completed yet, rather than silently carrying a tick across to a
  different instruction.

That last point is the reason to declare ids: without one, a typo fix costs
every developer their progress on that step. Declared ids are the supported
format; the fallback exists so an existing document keeps working untouched.

Every step is also served with a `content_hash` covering its text and
everything rendered under it. The dashboard uses it to flag a step that changed
while a developer had the ticket open; it is never used to store or re-render
an older version.

## Identity

**The level-2 heading is the identity, not the filename.** A file called
`example-feature-01-risk-01-control-01-draft-v2.md` whose heading reads
`## example-feature-01-risk-01-control-01` is catalogued as that control, and
the mismatch is reported as a `heading_filename_mismatch` warning rather than
causing the control to be dropped.

Playbook documents are written with a generic prefix (`platform-…`). The
catalogue rewrites that prefix to the real platform, so
`platform-feature-01-risk-01-control-01` is served as
`ios-feature-01-risk-01-control-01` and lines up with the risk ids in
`risks.yaml`. A control belongs to the risk its own id names — the links in the
risk document are used to *validate* that relationship, never to define it, so
a wrong link is reported instead of silently attaching a control to the wrong
risk.

## Control status

Only an **active** control counts as required developer work.

| Status | Required? | Meaning |
| --- | --- | --- |
| `active` | yes | Current guidance a developer is expected to implement |
| `deprioritized` | no | Known guidance that is not being asked for right now |
| `deprecated` | no | Superseded guidance kept for reference |

Status is resolved in this order, first match wins:

1. **YAML front matter** in the control document — the explicit, in-source form:

   ```markdown
   ---
   status: deprioritized
   required: false
   title: A human-readable control name
   ---
   ## example-feature-01-risk-01-control-01
   ```

2. **`configs/split/<platform>/controls.yaml`** — for when the playbook
   directory cannot be edited. See the comments in that file for the format.

3. **A naming marker** — a filename or heading containing `(deprioritise)` or
   `(deprecated)`. This is a safety net for the existing corpus, not the
   intended mechanism; a control resolved this way reports
   `status_source: "naming"` so it is visible as inferred rather than declared.

4. Otherwise `active`.

## Caching

The catalogue is built lazily on first request and cached in memory per
platform. It is rebuilt automatically when the fingerprint of the source
changes — the relative path, modification time and size of every `*.md` file,
plus the names of everything in `attachments/` and `implemented_controls/`.

That means an edit to a control appears on the next request with no restart. A
`POST /platforms/{platform}/playbook/reload` forces a rebuild for the cases the
fingerprint cannot see, and a restart always rebuilds. The files on disk are
authoritative; the cache is only a performance optimisation.

## Warnings

`GET /platforms/{platform}/playbook/status` reports everything the catalogue
found wrong. Nothing here stops a control being served — the point is that a
problem is visible rather than silently swallowed.

| Code | Meaning |
| --- | --- |
| `missing_control_file` | A risk links to a control document that is not in the directory |
| `malformed_control_link` | A link's label names one control and its target is a different one |
| `cross_risk_control_link` | A risk links to a control belonging to another risk |
| `missing_image` | A control references a screenshot that is not in the directory |
| `missing_source_archive` | A control references an implemented-control archive that is not there |
| `mismatched_source_archive` | A control offers an archive built for a different control |
| `empty_step` | A numbered step has no instruction text |
| `heading_filename_mismatch` | The heading and the filename disagree; the heading won |
| `missing_heading` | A document has no level-2 heading; the filename was used |
| `risk_without_controls` | A risk document has no developer controls |
| `control_without_risk` | A control's risk has no document in the directory |
| `document_unreadable` | A document could not be read or decoded |

## Assets and archives

Screenshots are served through
`GET /platforms/{platform}/controls/{control_id}/assets/{asset_path}`. The
resolved file must sit inside the configured root, must not be an ignored file,
and must carry an approved image extension (`.png`, `.jpg`, `.jpeg`, `.gif`,
`.webp`, `.svg`). Anything else — including a `../` escape, an absolute path,
or the control's own `.zip` — is a `404`.

Implemented-control archives stay on the filesystem. They are never extracted,
never read into the database, and never returned inline. `…/source` reports the
filename, size and SHA-256; `…/source/download` serves the bytes.

**Limitation.** This API has no authentication of its own, so an archive
download is protected only by the network posture of the host (see
[architecture.md](architecture.md)). On any host reachable beyond the trusted
LAN, set `PLAYBOOK_SOURCE_DOWNLOAD_ENABLED=false`; `…/source` then reports
`download_enabled: false` and `…/source/download` answers `403`, while the
metadata and the rest of the control stay available.

An implemented-control archive is a worked example for a developer to read. It
is not evidence that the developer fixed anything, and the dashboard presents it
that way.

## What the backend does not do

- It does not write to the playbook directory.
- It does not copy the playbook into this repository.
- It does not import controls into a database, and there is no second control
  catalogue anywhere.
- It does not store developer progress. That lives in Supabase, owned by the
  dashboard, keyed by the `control_id` and `step_key` this catalogue reports.
