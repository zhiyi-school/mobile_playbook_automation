# Developer remediation playbook

The backend serves two different kinds of instruction, and keeping them apart
is the point of this document.

## Terminology

| Term | Meaning | Who follows it | Where it lives |
| --- | --- | --- | --- |
| **Risk** | The security problem an assessment discovered | — | `configs/split/<platform>/risks.yaml` |
| **Security demonstration** | The steps security uses to demonstrate or validate the risk | Security | the risk Markdown `Demonstration` section; `risks.yaml` is fallback |
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

Both risk and control documents are read **section by section**. A level-3
heading names the section; matching is case-insensitive.

| Section | Risk document | Control document |
| --- | --- | --- |
| `### Title` | The risk's displayed name | The control's displayed name |
| `### Description` | The risk's description, including its MITRE annotation | The control's summary, and its title when no `### Title` is written |
| `### Demonstration` | Manual-testing steps | Remediation steps |
| `### Remediation` | — | Remediation steps (alias of `Demonstration`) |
| `### References` | Reference links | Reference links and the archive link |

`Title`, `Description`, `Demonstration`, `Remediation` and `References` are
structural: their headings are never rendered as ordinary content. Any other
level-3 heading — `### Additional context`, say — stays visible.

The title is the first paragraph of its section; anything else written under
`### Title` stays as body content rather than being swallowed by the heading.
There is no `### Goal` section: the tactic a risk leads to is now written as a
MITRE annotation inside the Description, described below.

#### The MITRE annotation

A risk's tactic is written inside its `### Description` as
`(MITRE ATT&CK: ***Tactic Name*** - TA0000)`. The tactic name may carry any
combination of emphasis markers — `***Name***`, `_**Name**_`, `**Name**` or no
emphasis at all — and the separator may be a hyphen or an en/em dash. The parser
records the tactic name and its `TA####` identifier.

Nothing else supplies a tactic. Prose that merely mentions a tactic word is not
an annotation, and a tactic is never inferred from the risk's identifier,
filename, demonstration or configuration. A description that names MITRE ATT&CK
without a well-formed tactic and identifier is reported as
`malformed_mitre_annotation`; one that names two different tactics is reported as
`conflicting_mitre_annotation` and no tactic is recorded, rather than one being
chosen arbitrarily. Both are validation errors.

A risk document:

```markdown
## example-feature-01-risk-01

### Title

<The risk's displayed name>

### Description

Because the platform provides <feature>, your app is at risk of <threat>. (MITRE ATT&CK: ***Tactic Name*** - TA0000).

### Demonstration

| Configuration | Detail |
| ------------- | ------ |
| Prerequisite  | example-feature-01 |

#### 01. Prepare the example environment

Install the placeholder tooling.

``` shell
example --prepare
```

#### 02. Perform the example test

Capture the result.

Feature-01-Risk-01 control measures:

- [example-feature-01-risk-01-control-01](example-feature-01-risk-01-control-01.md)
```

A control document:

```markdown
## example-feature-01-risk-01-control-01

### Description

Detect an example repackaging attempt

### Demonstration

<!-- playbook-step-id: example-signing-key -->
#### 01. Configure an example signing key

Generate the key outside the repository.

``` shell
export EXAMPLE_KEY_PATH="/placeholder/key.pem"
```

_What the command configures._

##### Recommended solution

A nested heading stays inside the step above it.

#### 02. Verify the example control

1. An ordinary numbered point inside the step.
2. Another ordinary numbered point.

<img src="attachments/example_control_ss1.png" width="400" alt="Alt text">

*What the screenshot shows*

### References

- https://example.test/reference

The source code with the implemented control can be found [here](implemented_controls/example-feature-01-risk-01-control-01.zip).
```

#### Heading-based steps

Inside `Demonstration` or `Remediation`, a **numbered level-4 heading** starts a
step. `#### 01. Title`, `#### 1. Title`, `#### 02) Title` and `#### 3) Title`
are all accepted. The number becomes the step's `number` and the remainder
becomes its `step_title`.

Everything after the heading belongs to that step until the next numbered
heading or the next structural section:

- The first paragraph becomes the step's `text`; the rest becomes `content`.
- A deeper heading (`#####`) is content, not a new step.
- An ordered list inside a step is content, not extra steps.
- A number inside a paragraph or a code block never starts a step.

#### Fenced code blocks

Fenced blocks follow CommonMark closely enough that what you write is what the
dashboard shows and copies:

- Either ``` ``` ``` or `~~~` opens a block; the info string after it becomes the
  block's `language`, with or without a separating space.
- Only a fence of the **same marker and at least the opening length** closes the
  block, so a three-backtick fence inside a four-backtick block stays content.
- A closing fence may not carry an info string; ` ```swift ` part-way through a
  block is content, not a terminator.
- An indented opening fence strips up to that much leading whitespace from each
  content line. Deeper indentation inside the block is preserved.
- Blank lines are preserved everywhere inside the block, including immediately
  after the opening fence and before the closing one.
- An unclosed fence runs to the end of the document.

`text` is the block's content lines joined by `\n` after CRLF is normalised to
LF, with no trailing newline. A block holding a single blank line and an empty
block therefore share the same empty `text`.

Blank lines inside a fence are content, so adding or removing one changes that
step's `content_hash` and the dashboard asks the developer to re-read the step.
Step identity is derived from the instruction text, not from block content, so
reformatting a code block never moves recorded progress to a different step.

#### Ordered-list fallback

Documents written before heading-based steps keep working. The precedence is:

1. Numbered headings inside `Demonstration` or `Remediation`.
2. Otherwise, top-level ordered-list items inside that section.
3. If the document names no section at all, top-level ordered-list items before
   the references, exactly as before.

#### Title and summary

A control's title comes from, in order: the front-matter `title`, the first
paragraph of `### Description`, then a generated `Control N`. The Description
is also the summary, so a control that gives only a short Description will have
the same text for both — the dashboard shows it once.

Front matter still carries `title`, `status`, `required` and `risk_id`.

Both HTML `<img>` tags and Markdown `![]()` images are read. An italic-only
paragraph directly under an image becomes that image's caption.

## Step identifiers

A developer's progress is recorded against a step's **identifier**, never
against its position or its wording. Declare one on the line directly above a
numbered step or a numbered step heading:

```markdown
<!-- playbook-step-id: rotate-example-key -->
#### 01. Rotate the example API key
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

A step with no directive falls back to an id derived from its own wording
(`auto-<hash>`) — the **step title** for a heading-based step, the instruction
text for an ordered-list step. That fallback is deliberately conservative:

- Renumbering or reordering steps keeps their ids, so progress survives.
- Inserting or deleting a step does not shift anyone else's id.
- Editing a step's body, commands or screenshots keeps its id.
- **Retitling a step changes its id**, so it is treated as a new step that
  nobody has completed yet, rather than silently carrying a tick across to a
  different instruction.

That last point is the reason to declare ids: without one, a typo fix costs
every developer their progress on that step. Declared ids are the supported
format; the fallback exists so an existing document keeps working untouched.

Every step is also served with a `content_hash` covering its title, its text
and everything rendered under it. The dashboard uses it to flag a step that changed
while a developer had the ticket open; it is never used to store or re-render
an older version.

### Edit compatibility

Document/control identity is the canonical heading-derived `control_id`.
Progress identity is the independent `step_key`. `playbook_revision` identifies
the parsed document and `content_hash` identifies rendered step content; neither
is a progress key. Supabase stores completion against `(ticket_control_id,
step_key)` and reconciliation adds missing current keys without deleting history.

| Edit | Identity and stored progress |
| --- | --- |
| Reorder or renumber | Preserved for declared and generated ids |
| Add a step | New row on reconciliation; other rows preserved |
| Remove a step/control | Historical rows remain, but disappear from current rendering/counts |
| Change body, command, or image | Key preserved; content hash/revision changes |
| Retitle a generated-id step | New key; old completion is not transferred |
| Retitle a declared-id step | Key preserved; content hash/revision changes |
| Add/change/remove a directive | Preserved only if the resulting key is exactly the old key |

There is no fuzzy title mapping. An unchanged exact key is the only supported
way to retain completion.

### Adopting declared ids

1. Keep an unedited copy of the playbook and validate it.
2. Inventory current ids with the catalogue or the JSON validator result.
3. Add directives using the existing effective generated key when progress
   should survive; do not replace it with a nicer slug merely because wording
   is similar.
4. Preview the exact before/after mapping:

   ```sh
   python -m mobile_playbook.playbook.identity_preview \
     --platform ios --old-root /path/to/before --new-root /path/to/after
   ```

5. Review every added, removed, content-changed, ambiguous, or colliding key.
   Intentionally changed work gets a new key and begins incomplete. Resolve all
   identity errors before publishing.

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

## Validation and diagnostics

`GET /platforms/{platform}/playbook/status` reports everything the catalogue
found wrong. Nothing here stops a control being served — the point is that a
problem is visible rather than silently swallowed. One malformed document does
not take the catalogue down; only a document that cannot be read at all is
skipped, with a warning.

Use the read-only validator for maintenance and CI. The root is mandatory, so
the command cannot silently fall through to machine-local configuration:

```sh
python -m mobile_playbook.playbook.validator \
  --platform ios --root /path/to/playbook
```

It uses the production parser/catalogue without FastAPI, devices, Supabase, or
secrets. `--format json` emits stable fields (`code`, `severity`, relative
`path`, optional `line`/`section`, and `message`). Errors return nonzero;
`--strict` also fails on warnings. The validator reads files and archive
metadata only: it never writes, extracts or executes archives/code blocks, and
never fetches external URLs.

Errors cover identity conflicts, broken risk/control relationships, malformed
or duplicate step ids, generated-id collisions, missing local files/images/
archives, and references outside the root. Warnings cover actionable authoring
quality, unsupported link schemes, malformed external URLs, and missing
heading anchors. Supporting Markdown that does not claim a risk/control
identity is allowed. The API status endpoint retains its non-blocking catalogue
warnings for runtime observability; it is not a substitute for this validator.

| Validator severity | Codes | Meaning |
| --- | --- | --- |
| Error | `duplicate_document_id`, `duplicate_step_id`, `duplicate_generated_step_id`, `malformed_step_id`, `unrecognized_document_identity` | Identity is ambiguous, colliding, or claims an invalid risk/control form |
| Error | `control_without_risk`, `cross_risk_control_link`, `missing_control_file` | The risk/control relationship cannot be resolved safely |
| Error | `missing_local_link`, `missing_image`, `missing_source_archive`, `reference_outside_root` | Required local content is absent or escapes the allowed root |
| Warning | `missing_heading_anchor` | The document exists but its referenced heading does not |
| Warning | `invalid_external_url`, `unsupported_link_scheme` | An external/custom target cannot be validated as a supported local or HTTP(S) link |
| Warning | Other catalogue codes below | Content remains parseable but needs author review |

| Code | Meaning |
| --- | --- |
| `missing_control_file` | A risk links to a control document that is not in the directory |
| `malformed_control_link` | A link's label names one control and its target is a different one |
| `cross_risk_control_link` | A risk links to a control belonging to another risk |
| `missing_image` | A control references a screenshot that is not in the directory |
| `missing_source_archive` | A control references an implemented-control archive that is not there |
| `mismatched_source_archive` | A control offers an archive built for a different control |
| `empty_step` | A numbered step has no instruction text |
| `control_without_steps` | An active control parsed to zero steps, so the developer sees an empty walk-through |
| `empty_step_section` | A `Demonstration` or `Remediation` section contains no numbered steps |
| `duplicate_step_id` | Two steps declare the same id; the second was given a suffixed key |
| `duplicate_step_number` | Two steps carry the same number |
| `missing_description` | A sectioned document has no `Description`, so its title falls back to `Control N` |
| `missing_title` | A risk document has no `Title` section, so its configured name is used |
| `malformed_mitre_annotation` | A description names MITRE ATT&CK without both a tactic and a `TA####` identifier |
| `conflicting_mitre_annotation` | A description names more than one MITRE tactic, so none was recorded |
| `heading_filename_mismatch` | The heading and the filename disagree; the heading won |
| `missing_heading` | A document has no level-2 heading; the filename was used |
| `risk_without_controls` | A risk document has no developer controls |
| `control_without_risk` | A control's risk has no document in the directory |
| `document_unreadable` | A document could not be read or decoded |

## Parser-to-frontend contract fixture

`tests/fixtures/playbook_contract/` is the sanitized source fixture. It covers
risk/control relationships, heading and list-compatible parsing, explicit and
generated ids, nested content, an SVG, and an inert archive. Backend tests parse
that source directly. The frontend consumes one generated transport artifact at
`src/test-fixtures/playbook-control-v1.json` in its repository. The filename is
the transport-fixture version; it is not an API version-negotiation mechanism.

Regenerate that artifact from the source parser rather than editing two
expectations independently:

```sh
python -m mobile_playbook.playbook.contract_fixture \
  --root tests/fixtures/playbook_contract --platform ios \
  --control-id ios-feature-01-risk-01-control-01 \
  --output /path/to/optimus-v1/src/test-fixtures/playbook-control-v1.json
```

Review the generated diff, then run backend validator tests and the frontend
`playbook-contract.test.tsx`. The frontend test renders blocks and asserts that
the same step keys and content hashes drive reconciliation. Invalid and
ambiguous cases stay in backend validator tests and temporary directories; they
are not a second expected transport payload.

For a read-only compatibility check against an explicit frontend checkout, use
`--check` instead of `--output`:

```sh
python -m mobile_playbook.playbook.contract_fixture \
  --root tests/fixtures/playbook_contract --platform ios \
  --control-id ios-feature-01-risk-01-control-01 \
  --check /path/to/optimus-v1/src/test-fixtures/playbook-control-v1.json
```

The check returns nonzero and prints a unified diff when the explicit
counterpart artifact differs. It never discovers or checks out a sibling
repository on its own; record the backend and frontend revisions being tested
when using it for a release.

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

## Source of truth

| Lives in | Owns |
| --- | --- |
| The Markdown playbook | Titles, descriptions, MITRE tactics, demonstrations, remediation instructions, references, media |
| The configuration files | Automation execution settings and environment-specific configuration |
| Supabase | Assessment data, workflow state, conversations and step progress |

A risk's manual-testing demonstration comes from its Markdown `Demonstration`
section. The configured `demonstration:` block in `risks.yaml` is only a
fallback, used when the Markdown document supplies none — narrative content is
not duplicated into configuration or the database.

## What the backend does not do

- It does not write to the playbook directory.
- It does not copy the playbook into this repository.
- It does not import controls into a database, and there is no second control
  catalogue anywhere.
- It does not store developer progress. That lives in Supabase, owned by the
  dashboard, keyed by the `control_id` and `step_key` this catalogue reports.
