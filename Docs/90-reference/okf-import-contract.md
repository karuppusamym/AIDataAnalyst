# OKF import and the round-trip contract

Reference for importing an edited Atlas OKF bundle, delivered for tracker row R11-OKF03
(design item 14C, [items 13–17 design](../10-architecture/21-graphql-okf-and-workspace-design.md),
section 14). The bundle format itself is the [Atlas OKF export profile](okf-export-profile.md).

Implementation: `src/aida/okf_import_bundle.py` (the pure half: archive limits, safe YAML,
document comparison), `src/aida/okf_import.py` (preview and apply against the database) and
`src/aida/okf_import_api.py` (the two routes). Tests: `tests/test_okf_import.py` (the journeys
and the check that this page matches the code) and `tests/test_okf_import_hostile.py` (one
hostile archive or document per limit).

## Status: disabled by default

`okf_import_enabled` ships `False`. While it is off, both routes answer 403
`OKF_IMPORT_DISABLED` before the upload or any row is read. The decision is recorded in
`scripts/configuration_decisions.py` as off by design until the row's evidence is accepted.
No UI is built for import; the routes are the only surface.

## What import is, and is not

Import turns edits made to a downloaded bundle into **pending proposals in existing families**,
reviewed like any other proposal. It never writes approved state, never publishes, never
regenerates a bundle, and keeps no store of its own -- no wiki table, no copy of the uploaded
file, no record of unknown fields. A re-export shows an imported change only after a principal
other than the importer approved it, because only then does the approved state the exporter
reads change.

| Method and path | Does |
|---|---|
| `POST /v1/context-product-versions/{version_id}/okf-bundle/imports/preview` | Reads the archive (raw request body, `filename` as a query parameter) and reports every edit's decision. Writes an audit record only. |
| `POST /v1/context-product-versions/{version_id}/okf-bundle/imports` | Repeats the preview; refuses with `PREVIEW_STALE` unless it reproduces the `preview_digest` passed; raises the listed proposals as pending reviews. |

Roles: PlatformAdmin, MetadataAdmin or DataSteward -- the population that may author both
families import writes into. A role is necessary and never sufficient: the caller must be able
to read the bundle exactly as `aida.okf_store.read_published_bundle` decides for every OKF read
(`CONSUME_CONTEXT`, the version's consumer roles, per-datasource admission), and must pass the
workbook upload's `READ_METADATA` gate for each datasource a description proposal targets.

## What a bundle must be

Only an archive Atlas exported is imported, because the manifest is what names its source:

* `atlas-manifest.json` at the archive root, naming OKF `0.2`, the pinned specification revision
  and the `atlas-okf-export` profile (`MANIFEST_MISSING`, `MANIFEST_INVALID`,
  `MANIFEST_NOT_ATLAS`).
* The manifest's organization and product version must be the ones the route names
  (`MANIFEST_SCOPE_MISMATCH`). They are checked against the caller's authorization, never
  trusted instead of it.
* The publication it was exported from must still be retained in the caller's own lineage,
  found by the manifest's bundle and snapshot digests (`BASE_PUBLICATION_NOT_RETAINED`, 409).
  The newest five publications of a lineage are retained, so a bundle whose product has since
  published five more changes must be exported again. Every comparison's "before" is that
  stored publication, never the upload.

## The round-trip contract

### Supported: what an edit becomes

| Document type | Section | Family | Proposal |
|---|---|---|---|
| `Atlas Table`, `Atlas View`, `Atlas Materialized View` | `# Purpose` | `ASSET_DOCUMENTATION` | a `readme` change in a pending batch |
| `Atlas Table`, `Atlas View`, `Atlas Materialized View` | `# Schema`, Description column | `COLUMN_DESCRIPTION` | a `business_description` change in a pending batch |
| `Atlas Column Set` | `# Schema`, Description column | `COLUMN_DESCRIPTION` | a `business_description` change in a pending batch |
| `Atlas Business Concept` | `# Definition` | `ONTOLOGY_MEANING` | the concept's description in a new pending ontology version |
| `Atlas Business Concept` | `# Also called`, items added | `ONTOLOGY_MEANING` | the added aliases in a new pending ontology version |

Asset documentation and column descriptions go to the description family's own batch store:
one pending batch per datasource, `ModelImportBatch` / `ModelImportChange`, each change carrying
the version the export showed as its expected version. The batch waits in an
`OKF_IMPORT_BATCH` review with action `APPLY_OKF_IMPORT`, decided by the workbook batch's own
adapter, so approval publishes through the same append-only helpers and the same stale check a
workbook edit uses: a description approved after the export is `SKIPPED_STALE`, never
overwritten. The review type differs from `MODEL_IMPORT_BATCH` for one reason: the tier table
pins `OKF_IMPORT_BATCH` at T2 at every size, so no agent decides imported text. An import batch
is created `PENDING_REVIEW`, never as a draft, so the workbook routes cannot submit or edit it.

The design named the document-ingestion family for import. Its documents are constrained to
CSV data dictionaries and its claims take one review each, so using it would need a schema
change and would put one review per edited cell in the queue. The workbook batch is the
description family's own bulk proposal store and reviews one edited file as one decision, which
is what an edited bundle is; no table or migration was added.

Meaning goes to a new `OntologyVersion` on the base the bundle was exported from, created
`PENDING_APPROVAL` under its ordinary `ONTOLOGY_VERSION` review, with every other concept,
relation and mapping carried from that base. Approval re-checks the base: an ontology published
past it in the meantime is refused, not merged. A product pins ontology versions, so a
product's concept documents show approved meaning once a product version pins the new version.

Proposed text must be text Atlas would publish: no link, bare URL, raw markup, code fence,
heading, control or bidirectional-override character, and within the family's length limit
(`TEXT_LINK_NOT_ALLOWED`, `TEXT_RAW_MARKUP_NOT_ALLOWED`, `TEXT_CODE_FENCE_NOT_ALLOWED`,
`TEXT_HEADING_NOT_ALLOWED`, `TEXT_CONTROL_CHARACTERS`, `TEXT_TOO_LONG`). It is screened with the
platform's own detector (`TEXT_SCREENING_REFUSED`); screened-out text is never persisted or
echoed back.

### Decided against Atlas's current state

| Outcome | Reason code | Meaning |
|---|---|---|
| PROPOSE | -- | raised as a pending proposal by an apply |
| CONFLICT | `SOURCE_CHANGED_SINCE_EXPORT` | the approved version moved since the export; not proposed |
| UNCHANGED | `ALREADY_CURRENT` | Atlas already holds this text |
| UNSUPPORTED | `TARGET_NOT_FOUND`, `TARGET_NOT_ACTIVE`, `TARGET_AMBIGUOUS` | the object, column or concept is gone, retired, or not one thing |
| REFUSED | `DATASOURCE_NOT_AUTHORIZED` | the caller may not read that source's model |
| REFUSED | `MEANING_DEFINITION_INVALID`, `MEANING_MAPPING_INVALID` | the resulting ontology version would not validate, or a mapping target is not valid and readable now |

An apply also refuses `IMPORT_NOTHING_TO_PROPOSE` when no item would be proposed, and
`IMPORT_ALREADY_PENDING` when the same archive's proposals are already waiting for review.

### Claims: read, reported, never authority

`verified` and `status`, and the `description` and `statements` keys of the `atlas` mapping,
assert approval. In an imported file they are evidence supplied by whoever edited it, reported
as `CLAIM_NOT_AUTHORITY` with the claimed value in the preview, and discarded. Every proposal is
authored by the importing principal and decided by a different one; nothing the file says
changes who approved anything.

### Derived: regenerated by Atlas, edits ignored and listed

* Frontmatter `title`, `description`, `tags`, `generated`, `sources` and every other key of the
  `atlas` mapping (`FIELD_DERIVED`). The frontmatter `description` is the first sentence of the
  approved purpose; edit `# Purpose` instead.
* Sections other than the supported ones -- Dependencies, Coverage, Limitations, Interface,
  Mapped objects, Related concepts, a column set's neighbours and preamble -- and the Type,
  Nullable and Classification cells of a schema row, which ingestion owns (`SECTION_DERIVED`).
* Every `index.md` and `log.md` (`DOCUMENT_DERIVED`).

### Preserved but ignored: unknown fields

A frontmatter key the exported document did not carry is tolerated, never a refusal (OKF v0.2
section 11), and listed as `UNKNOWN_FIELD_TOLERATED` so the editor sees it was not imported. A
new heading is listed as `SECTION_UNKNOWN`. **Round trip of unknown fields is not supported:**
Atlas does not store them and the next export does not re-emit them, because the export is
regenerated from governed state and keeping them would need the parallel wiki store this row
forbids. They survive only in the editor's own copy of the file.

### Unsupported: read, dropped, listed

| What | Reason code |
|---|---|
| `Atlas Routine`, `Atlas Routine Package` and `Atlas Tool Version` documents | `FAMILY_NOT_SUPPORTED` |
| A document at a path the source publication never held (import creates no object or concept) | `DOCUMENT_NOT_IN_SOURCE_BUNDLE` |
| A blank description, definition or section: blank never means delete | `BLANK_IS_NOT_A_DELETION` |
| A removed key, section or alias list (absence never deletes) | `FIELD_REMOVAL_IGNORED` |
| An alias removed from a list that still exists (retire aliases through ontology authoring) | `REMOVAL_NOT_SUPPORTED` |
| An edit to text export screening withheld: nobody can show it was made against what they read | `BASE_TEXT_WITHHELD` |
| A schema row naming no exported column, a malformed row, or one column twice | `SCHEMA_ROW_UNMATCHED`, `SCHEMA_ROW_MALFORMED`, `SCHEMA_ROW_DUPLICATE` |
| A document whose sections cannot be matched to what the renderer wrote | `DOCUMENT_STRUCTURE_AMBIGUOUS` |
| `__MACOSX/`, `.DS_Store`, `Thumbs.db` or `desktop.ini` added by a re-zipping tool | `OS_METADATA_IGNORED` |

Renaming anything is unsupported by construction: names are catalog-owned, and a changed
identity refuses the document (below).

## Limits and hostile content

Limits are code constants in `aida.okf_import_bundle`, not settings, so a hostile archive cannot
wait for an operator to raise one. Each has a named refusal and a test in
`tests/test_okf_import_hostile.py`.

| Limit | Value | Refusal |
|---|---|---|
| Archive size | 32 MiB (also checked on the declared request length) | `ARCHIVE_TOO_LARGE` (413) |
| Members, counted by walking the central directory before it is parsed | 22,000 | `ARCHIVE_TOO_MANY_MEMBERS` |
| One document, uncompressed | 256 KiB, the export's own limit | `MEMBER_TOO_LARGE` |
| The manifest, uncompressed | 16 MiB | `MEMBER_TOO_LARGE` |
| Everything, uncompressed, declared and then actually produced | 80 MiB | `ARCHIVE_EXPANDS_TOO_LARGE` |
| Compression ratio of a member of 64 KiB or more | 100 to 1 | `ARCHIVE_COMPRESSION_RATIO` |
| A member whose size differs from its header, or whose data is corrupt | -- | `ARCHIVE_SIZE_MISMATCH`, `ARCHIVE_CORRUPT` |
| ZIP64 records, which no Atlas bundle needs | refused | `ARCHIVE_ZIP64_NOT_SUPPORTED` |
| An upload that is not a ZIP archive, or whose end record is unreadable | refused | `ARCHIVE_NOT_A_ZIP` |
| Frontmatter | 32 KiB, depth 16, 8,000 YAML events, scalars of 16,384 characters | `FRONTMATTER_TOO_LARGE`, `YAML_TOO_DEEP`, `YAML_TOO_MANY_NODES`, `YAML_SCALAR_TOO_LONG` |
| Links in one changed document, and one link's target | 20,000; 2,048 characters | `LINK_LIMIT_EXCEEDED`, `LINK_TOO_LONG` |
| Rows in one schema table | 1,000 | `SCHEMA_ROW_MALFORMED` |
| Changed documents, and edits, in one import | 5,000 each | `IMPORT_TOO_MANY_CHANGES` |
| Proposed text | 16,000 characters; a concept definition 4,000; an alias 200 | `TEXT_TOO_LONG` |

Refused before anything is decompressed, over every member: a name that climbs out of the
bundle (`PATH_TRAVERSAL`), an absolute or drive-lettered name (`PATH_ABSOLUTE`), a backslash,
NUL, control character or empty segment in a name, read from the name as the archive spelled it
(`PATH_UNSAFE`), a symbolic link (`MEMBER_SYMLINK`), a device, FIFO or socket
(`MEMBER_SPECIAL_FILE`), an encrypted member (`MEMBER_ENCRYPTED`), a compression method other
than stored or deflate (`MEMBER_COMPRESSION_UNSUPPORTED`), anything but the manifest and
lower-case Markdown documents under `bundle/` -- a script, a nested archive, an upper-case path
(`MEMBER_NOT_ALLOWED`) -- and two names that are the same ignoring case
(`ARCHIVE_DUPLICATE_MEMBER`). Nothing is ever written to a file system.

Refusing one document, while the rest of the bundle is still read: bytes that are not UTF-8
(`ENCODING_INVALID`), control characters (`DOCUMENT_CONTROL_CHARACTERS`), no frontmatter
(`FRONTMATTER_MISSING`), YAML that is not a single safe mapping of string keys (`YAML_INVALID`,
`YAML_MULTIPLE_DOCUMENTS`, `YAML_DIRECTIVE_NOT_ALLOWED`, `YAML_KEY_NOT_STRING`,
`YAML_DUPLICATE_KEY`, `FRONTMATTER_NOT_A_MAPPING`), any YAML anchor or alias -- the
billion-laughs expansion needs one, and Atlas never writes one (`YAML_ALIAS_NOT_ALLOWED`) -- any
explicit tag, so no `!!python` constructor is reachable (`YAML_TAG_NOT_ALLOWED`), upstream's
Attested Computation type or an `executor` / `computation` field (`EXECUTABLE_FIELD_REFUSED`),
a changed `type`, `resource` or subject key (`IDENTITY_MISMATCH`), and two documents claiming one
subject -- a table's document copied to a second path -- which refuses every document on that
subject (`DUPLICATE_STABLE_ID`).

**Links are counted, never followed**, and nothing is ever executed: neither import module
imports a network, process or dynamic-import facility, which the hostile suite checks
structurally and by previewing a bundle full of external links with the network disabled.

## Value freedom

A refusal persists its reason code and nothing from the upload: no member name, path, field
value or text reaches an audit record, and the preview's own audit record carries only the
archive digest, the base publication, counts and reason codes. Proposed text is persisted only
where each family already persists it -- a pending change's new value, a pending ontology
draft -- and the preview shows current text only where export screening would release it.

## What this does not claim

* No UI: import is two REST routes.
* Routine, package and tool-version documents are not imported (`FAMILY_NOT_SUPPORTED`); a
  routine's description still goes through the routine draft workflow.
* Unknown fields are tolerated and listed, not round-tripped.
* A bundle older than its lineage's retained publications cannot be imported; export again.
* No certification against the OKF specification is claimed for import, as none is for export.
