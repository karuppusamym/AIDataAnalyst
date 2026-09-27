# Atlas OKF export profile

Reference for the Open Knowledge Format bundles Atlas produces, the `atlas` frontmatter
extension they carry, and the Atlas manifest that travels beside them. Delivered by tracker row
R11-OKF01 for design item 14A ([items 13–17 design](../10-architecture/21-graphql-okf-and-workspace-design.md),
section 14).

Implementation: `src/aida/okf_export.py` (pure renderer and validators),
`src/aida/okf_snapshot.py` (the frozen snapshot, loaded under the caller's authority) and
`src/aida/okf_export_api.py` (the read surfaces). Tests: `tests/test_okf_export.py`, with
conformance fixtures under `tests/fixtures/okf_conformance/`.

R11-OKF02 (design item 14B) made the bundle a stored, incrementally rebuilt thing every surface
reads: `src/aida/okf_store.py` (the one read path, staleness, incremental rebuild and atomic
publication), `src/aida/okf_store_models.py` (the three tables) and the MCP resource reader in
`src/aida/mcp_server.py`. Tests: `tests/test_okf_store.py`. See
[Stored publications](#stored-publications-r11-okf02) below.

Also under R11-OKF02, a bundle has two scopes: a context product version's, and one
datasource's (`DATASOURCE`). See [Source bundles](#source-bundles-r11-okf02). Tests:
`tests/test_okf_source_bundles.py`.

The stored bundle is also read over GraphQL (`src/aida/graphql_okf.py`, R11-GQL01; tests
`tests/test_graphql_okf.py`) and, for a datasource, asked a question of over MCP
(`atlas__get_source_knowledge_context`; tests `tests/test_mcp_source_knowledge.py`). Both go
through the same store functions as the REST routes, so they apply the same gates. See
[Surfaces](#surfaces).

## The pinned specification, and what is actually tested

| Field | Value |
|---|---|
| Format | Open Knowledge Format, version `0.2` |
| Upstream | `https://github.com/GoogleCloudPlatform/open-knowledge-format`, file `SPEC.md` |
| Pinned revision | `0b87c52c6ef999286c745e19998fdfcd03d5dbee` |
| SHA-256 of the pinned `SPEC.md` | `26aa5da029278939f914e578107242d9607d4f2dc5fe153272b82f9ed1030101` |
| Conformance status | `SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES` |

The revision is pinned rather than tracking `main`, and the digest of the specification text is
recorded beside it, so "written against OKF v0.2" names one text and a later pass can prove the
pin still resolves to the same words without vendoring upstream content into this repository.

**No conformance certification is claimed.** `validate_okf_conformance` implements the §11
clauses of the pinned text — every non-reserved `.md` file parses a YAML frontmatter block, every
block carries a non-empty `type`, and `index.md` / `log.md` follow §8 and §9 — and
`tests/fixtures/okf_conformance/` exercises the clauses that a careless validator gets wrong: a
concept carrying only `type` is conformant, an unknown `type` and unknown keys must be tolerated,
and a bare `verified` mapping is a one-element list. There is no upstream conformance suite to
run against, so what is proven is "satisfies those clauses as this repository reads them", which
is exactly what the status string above says.

## Two verdicts, deliberately separate

The Atlas publication gate also refuses raw HTML/angle autolinks in document bodies and
checks reference-style Markdown link definitions against the same destination rules as
inline links. These restrictions do not change the general OKF conformance verdict. A
future wiki renderer still needs its own sanitization; the exporter is not an HTML sanitizer.

**Catalog labels are literal text (profile `4`).** A name is chosen by the source, and a
quoted identifier may hold `]`, `|`, a backtick or a line break. Every label is written through
`md_text` (a link label or plain text: Markdown punctuation backslash-escaped, `_` only at a
word edge, control characters flattened to a space) or `md_code` (a code span whose fence
outruns any backtick inside it, with `|` escaped in a table cell). A table named
`` x](https://outside) [y `` is therefore shown, not linked, and a column named `amount|total`
stays one cell of one row. An ordinary identifier renders exactly as it did under profile `3`.
The publish policy reads a backslash escape the way a renderer does -- the escaped character is
text -- so escaped names no longer trip its link and markup checks, while a real link or tag in
approved prose, including one after an escaped backslash, is still refused. Links are checked
in the body and in the frontmatter `description`; a `title` is a name and is displayed as text.
`tests/test_okf_markdown_safety.py` renders the output with a CommonMark and GFM-table parser.

| Function | Question it answers |
|---|---|
| `validate_okf_conformance` | Is this a conformant OKF v0.2 bundle? Nothing stricter. |
| `validate_atlas_publish_policy` | May Atlas publish it? Conformance plus the Atlas rules below. |

An upstream reference bundle full of external links and fenced code blocks is perfectly
conformant and must pass the first. It fails the second, and that is a statement about Atlas
rather than about the format. The Atlas rules are:

* Opaque safe path segments only (lower-case, digits, single hyphens). Names live in the content.
* No external links. A `resource`-style `atlas://` reference is allowed; `http(s)`, `mailto` and
  protocol-relative targets are not.
* No dangling internal links. OKF consumers must tolerate a broken link; a producer of governed
  knowledge must not publish one.
* No fenced code blocks. The only text that would ever need fencing is a definition body, which
  INV-6 forbids — this is the defence in depth that makes a future field which reintroduced one
  fail the gate rather than ship.
* Bounded document, frontmatter and bundle sizes, with an explicit failure instead of a silent
  truncation.

## Bundle layout

```text
atlas-manifest.json                      # Atlas extension, OUTSIDE the OKF bundle root
bundle/index.md                          # bundle root; the only index with frontmatter
bundle/sources/source-<key>/index.md
bundle/sources/source-<key>/schemas/schema-<key>/index.md
bundle/sources/source-<key>/schemas/schema-<key>/index-pages/page-<nnnn>/index.md  # large schemas
bundle/sources/source-<key>/schemas/schema-<key>/tables/table-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/tables/table-<key>-columns-<n>.md  # wide objects
bundle/sources/source-<key>/schemas/schema-<key>/views/view-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/routines/routine-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/packages/package-<key>.md
bundle/concepts/index.md
bundle/concepts/concept-<key>.md
bundle/tools/index.md                    # R11-OKF02
bundle/tools/tool-version-<key>.md       # R11-OKF02
bundle/log.md                            # R11-OKF02, stored bundles only
bundle/sources/source-<key>/log.md       # R11-OKF02, stored bundles only
```

A `log.md` records the refresh history of its scope (spec §9: it "MAY appear at any level of the
hierarchy"): one at the bundle root and one per source directory, date headings in ISO
`YYYY-MM-DD` form, newest first. A source's log lists only the publications that moved a
document under that source, so a change elsewhere leaves its bytes, and its hash, alone. Paths are
written as code spans rather than links, because an old entry may name a document a later
publication removed. A bundle rendered directly from a snapshot, with no stored history, has no
log.

The manifest sits outside `bundle/` so a reader pointed at the OKF bundle root never needs an
Atlas extension to consume it.

**Wide objects are split into column sets** (profile `3`). A table or view with more than
`MAX_COLUMNS_PER_DOCUMENT` (100) columns keeps its own document -- purpose, dependencies,
coverage, and a `# Schema` that lists every column *name* grouped by set -- and its full column
rows move into `Atlas Column Set` documents of at most 100 rows each, beside it. The design asks
for exactly this: "split unusually large definitions by stable structural section only when
retrieval limits require it". Set `n` holds ordinals `(n-1)*100+1 .. n*100`, so a column
appended at the source lands in the last set and a dropped one leaves a gap: neither rewrites
the sets before it. A range the source reported no usable ordinals for is split again by
position (`-columns-<n>-<m>`), so no set exceeds the limit. Each set says which object and which
part it is, and links back to the object and to its neighbours, so a reader handed one set
alone can place it. An object at or under the limit renders as it did under profile `2`.

**Large schemas list their index in pages** (profile `5`, R11-OKF02, 2026-09-25). A schema's
`index.md` lists every table, view, routine and package in it, so at a few thousand entries it
passed `MAX_DOCUMENT_BYTES` (256 KiB) and the whole bundle was refused -- measured at 5,000
tables (`scripts/measure_okf_source_bundle_cost.py`). Past `SCHEMA_INDEX_PAGE_ENTRIES` (1,000)
entries the schema index keeps its `# Schema` section and lists its pages under `# Index
pages`, each with its entry count and first and last name; each page is itself an `index.md`
in `index-pages/page-<nnnn>/`, so it carries no frontmatter and an OKF reader navigates it like
any index. A page lists at most 1,000 entries, in the schema index's own order, under the same
section headings, and links back to the schema index. Pages are cut by position, so an entry
added early shifts every later page: an incremental rebuild re-renders all of a schema's pages
whenever any entry in it moves. A schema at or under the limit renders as it did under
profile `4`. The document kind is `SCHEMA_INDEX_PAGE`.

### Stable identities

Every `<key>` is the first 128 bits of a SHA-256 over the JSON-encoded Atlas identity tuple, not
over a row's UUID: a UUID changes when an object is dropped and rediscovered and differs between
environments holding the same catalog, so a UUID-keyed bundle would report a content change where
there is none.

| Kind | Identity tuple |
|---|---|
| Source | `("source", datasource_id)` |
| Schema | `("schema", datasource_id, catalog, schema)` |
| Table / view / materialized view | `("object", datasource_id, catalog, schema, name)` |
| Routine | `("routine", datasource_id, catalog, schema, package_name, name, signature)` |
| Routine package | `("package", datasource_id, catalog, schema, package_name)` |
| Business concept | `("concept", ontology_key, ontology_version, concept_name)` |
| Tool version | `("tool-version", project_id, tool_slug, version)` |

`signature` keeps PostgreSQL overloads apart, `package_name` keeps a packaged member apart from a
standalone routine of the same name, and the leading kind token keeps a routine apart from a table
of the same name. Object kind is deliberately *not* part of a table-like identity: tables and
views share one namespace in every supported engine, so a rescan that reclassifies one must not
move its document. A collision refuses the export rather than overwriting a document.

## Concept types

`type` values are producer-chosen (§4.1) and consumers must tolerate unknown ones (§11):
`Atlas Data Source`, `Atlas Schema`, `Atlas Table`, `Atlas View`, `Atlas Materialized View`,
`Atlas Routine`, `Atlas Routine Package`, `Atlas Business Concept`, and since R11-OKF02
`Atlas Tool Version` and `Atlas Column Set` (one ordinal range of a wide object's columns).

An `Atlas Business Concept` document prints the approved version's aliases under
`# Also called` -- the words people actually use for the concept, which is how a reader gets
from a question to the concept at all. They are screened one by one like the definition, and a
withheld alias is left out rather than blanked. Its `# Mapped objects` lists the tables,
views and routines the approved version maps it to inside the product's scope -- a column
mapping names its column's table -- and it is `stable` and `verified` when the version's
governance approval is recorded and the concept is not `DEPRECATED`.

An `Atlas Tool Version` document describes one approved governed-tool version the product makes
eligible: its purpose (the approved description), its inputs (name, type, required -- never a
declared default, allowed value or bound, each of which is a literal), and how to invoke it
through Atlas. It is deliberately **not** upstream's `Attested Computation` (spec §10), whose
`computation` / `executor` fields are runnable embedded code: the design forbids "runnable
embedded code auto-executed by import", so no SQL, executor or credential is exported and an
importer finds nothing to run. A tool over a datasource the reader was refused is absent from the
documents, the counts and the manifest's pin list.

## Standard frontmatter, as Atlas fills it

| Key | Atlas rule |
|---|---|
| `type` | Required. One of the values above. |
| `title` | The object's real source-qualified name. |
| `description` | The first sentence of the **approved** Atlas description. Omitted when there is none; never synthesized from a name. |
| `resource` | An `atlas://` URI built from the stable keys. Identifies; never grants access. |
| `tags` | Derived and sorted: `atlas`, the object kind, the engine dialect. |
| `status` | `deprecated` when the object is retired at the source; `stable` when an approved description exists; otherwise `draft`. |
| `generated.by` | `process:atlas-okf-export` — the process that assembled the document. |
| `generated.at` | The latest **content** change the document can evidence: a description approval, or a captured definition version. Never the export time, and never a discovery completion time. |
| `sources` | Keyed provenance entries (`approved-description`, `captured-definition`) with the spec's `author` and `last_modified` credibility signals filled from recorded events only. Body claims attribute to them by footnote. |
| `verified` | See below. Almost always absent. |

### Lifecycle and verification are mapped, never borrowed

OKF's `verified` family and Atlas's draft / approval / withdrawal states are separate mappings.

* An imported `verified` claim is evidence supplied by its author and never an Atlas approval.
  Import (R11-OKF03, disabled by default) reports it as a claim and discards it; what an
  edited bundle can and cannot bring back is the [OKF import reference](okf-import-contract.md).
* Document-level `verified` is emitted **only** where every asserted statement in the document is
  covered by a recorded Atlas approval event. For a catalog object it never is: its columns,
  dependencies and coverage are captured rather than reviewed. Those documents therefore carry no
  document-level verification at all, and read as `unverified` under §5.3 — which is the true
  answer. Publishing a bundle is not a reviewer's signature on an inferred statement.
* A business-concept document qualifies, because its definition, mappings and relations are all
  content of the approved ontology version the product is pinned to. Its `verified` entry comes
  from the governance review that decided that version; with no recorded decider *and* instant,
  no entry is written.
* `human:` is written only where the recorded principal does not look automated
  (`aida.okf_snapshot._is_human_principal` fails closed), because §5.3 has consumers read that
  prefix as "human-reviewed".
* Atlas's `WITHDRAWN` and `NONE` description states both map to `draft`: in both cases there is no
  current approved statement. The distinction a consumer might act on stays visible in
  `description.state` and in the document body.

## The `atlas` extension

Producer keys are permitted by §4.1 and may not be rejected by §11. `tests/test_okf_export.py`
asserts that every key the renderer emits is named in this table, so the two cannot drift.

Keys below are written **relative to the `atlas` mapping**: `object.qualified_name` means
`object` then `qualified_name` inside the `atlas` block. Spelled relatively because across
`Docs/` a backticked dotted path under one of the shipped package names is a citation of a Python
module, resolved as one by `tests/test_doc_claims.py`, and a frontmatter key is not that.

| Key | Meaning |
|---|---|
| `profile` | Export profile and version that rendered the document. |
| `object` | The catalog object: `object.key`, `object.kind`, `object.native_object_type`, `object.qualified_name`, `object.engine`, `object.lifecycle`. |
| `description` | Standing of the Atlas-authored description: `description.state` (`APPROVED`, `PROPOSED`, `WITHDRAWN`, `WITHHELD`, `NONE`), `description.version`, `description.approved_by`, `description.approved_at`, `description.withheld_reason_codes`. |
| `definition` | Coverage of a definition, never a definition: `definition.available`, `definition.digest`, `definition.truncated`, `definition.lineage`, `definition.capture_version`, `definition.captured_at`, `definition.reason_codes`. |
| `routine` | Routine identity and behaviour: `routine.routine_type`, `routine.signature`, `routine.package_name`, `routine.language`, `routine.return_type`, `routine.is_deterministic`, `routine.security_mode`. |
| `concept` | Business concept identity: `concept.key`, `concept.name`, `concept.ontology_key`, `concept.ontology_version`, `concept.lifecycle`. |
| `tool` | On a tool-version document only: `tool.key`, `tool.tool_version_id`, `tool.slug`, `tool.version`, `tool.lifecycle`, `tool.fingerprint`, and `tool.invocation` (the MCP tool name and the REST execute route). No SQL and no executor. |
| `statements` | Which parts of the document an Atlas approval covers (`statements.approved`) and which are derived (`statements.derived`). This is the per-statement answer OKF's single document-level `verified` cannot give. |
| `scope` | The export's scope: `scope.kind`, `scope.product_key`, `scope.product_version`, `scope.policy_partition_digest`. In a source bundle (`scope.kind` is `DATASOURCE`) the two product keys are replaced by `scope.source_key`, the opaque key of the one source the bundle holds. |
| `withheld` | On a concept document only: `withheld.reason_codes` when screening refused released text. |
| `column_sets` | On a wide object's own document only: one entry per column set, with its `path`, `first_ordinal`, `last_ordinal` and `columns` count. |
| `part_of` | On a column-set document only: the object it belongs to and where it sits -- `part_of.key`, `part_of.path`, `part_of.qualified_name`, `part_of.set`, `part_of.sets`, `part_of.first_ordinal`, `part_of.last_ordinal`. |

## The Atlas manifest

`atlas-manifest.json` is an Atlas extension, not an OKF requirement. Keys: `manifest_version`,
`atlas_extension`, `okf_version`, `bundle_root`, `specification` (repository, path, revision,
sha256, conformance), `compiler`, `captured_at`, `scope`, `policy_partition` (with its digest),
`scope_digest`, `content_snapshot_digest`, `bundle_content_digest`, `counts` (including `tools`
since R11-OKF02), `files` (path, sha256, bytes), `source_objects` and `source_freshness`.

`scope` for a product bundle holds `kind`, `organization_id`, `product_key`, `product_version`,
`product_version_id`, `product_fingerprint` and `eligible_tool_version_ids`. For a source bundle
it holds `kind` (`DATASOURCE`), `organization_id`, `datasource_id` and `source_key`, and the
datasource id is also hashed into `scope_digest`; neither changes anything in a product
bundle's manifest.

`captured_at` and `source_freshness` are the only clock readings anywhere in an export, and both
live here rather than in a concept document. A scan that completes without changing anything
moves `last_scan_completed_at`; a document carrying it could not satisfy acceptance OKF-C
("no-op scans reproduce hashes"). This is the same split `context_compiler.freshness_section`
already makes, and the same reason the design warns against equating a new export timestamp with
fresh source evidence.

### The three digests, and what each one answers

| Digest | Over | Answers |
|---|---|---|
| `content_snapshot_digest` | The frozen snapshot, excluding `captured_at` | Did the governed content change? |
| `bundle_content_digest` | Each concept document's path and SHA-256 | Did the rendered bundle change? |
| `scope_digest` | The authorized keys plus the policy partition | Was this exported from the same scope under the same policy? |

Two bundles whose documents match but whose scope differs are not the same export, which is why
scope is hashed and not merely printed.

## Determinism

An export is a pure function of a frozen snapshot: `aida.okf_export` reads no database, no clock,
no network and no model. Two exports of one snapshot are byte-identical, including the archive —
every ZIP member carries a fixed timestamp, because `zipfile` would otherwise stamp the current
time into each local header. A snapshot round-trips losslessly through
`snapshot_to_document` / `snapshot_from_document` and renders the same bytes again, which is what
makes the freeze a property rather than a phrase.

The snapshot is frozen in one read decision, which is the design's requirement that "OKF
determinism requires a frozen content snapshot, not just a product version pointing to mutable
current catalog rows". R11-OKF02 stores that snapshot with the bundle rendered from it; see below.

## Stored publications (R11-OKF02)

Every surface -- the REST routes, the MCP resource reader and the Catalog / Context Products
knowledge views -- reads a bundle through `aida.okf_store.read_published_bundle`, and nothing else
in `src/` freezes or renders one for a consumer (`tests/test_okf_store.py` fails if that changes).
So two surfaces cannot give one reader different knowledge for the same product version.

**Lineage.** A stored bundle belongs to one product version under one *authority digest*: a digest
of the version and of the exact set of datasources the reader's own authorization admitted,
decided on every request by `aida.okf_snapshot.admit_datasources` before anything stored is looked
up. Readers with the same authority share one publication; a reader whose cross-boundary grant,
source binding or policy changed computes a different digest, so a bundle built under a revoked
grant is not reachable from their request -- not as the current bundle and not by publication id.
There is no stored entry keyed without the reader's authority.

**Tables.** `okf_bundle_publication` holds one immutable publication: the frozen snapshot, the
manifest, the digests, what changed and the refresh history. `okf_bundle_document` holds each
file's exact bytes and `rendered_in_sequence`, the first publication that produced those bytes.
`okf_bundle_head` points at the current publication of a lineage and records when it was last
confirmed current.

**Atomic publication.** The publication row, every document row and the head move in one
savepoint; a reader sees the old complete bundle or the new complete bundle and never a mixture.
Publications are immutable and the newest five of a lineage are retained, so a manifest's
`publication_id` passed to the download returns exactly the inspected bytes after a newer publish.

**Staleness.** A head records a digest of the *change marks* on the product's scope inside a
six-hour window: FP15 change signals for its tables and routines, approved-description versions,
reviewed view and procedure lineage, captured routine definition versions and the pinned tool
versions. Every read recomputes it, and a new mark -- including one committed late with an earlier
timestamp -- triggers a rebuild. A change that leaves no mark (a column reclassified in place) is
caught by revalidation once a head is older than fifteen minutes.

**Incremental rebuild (OKF-C).** A rebuild freezes once and renders only the documents whose
subject, source or link targets moved, plus the schema index listing them; the root, source,
concept and tool indexes and the logs are re-derived. Every other document's stored bytes are
carried without being rendered. A rebuild whose snapshot differs from the stored one only in
source read times publishes nothing, so a no-op scan moves no hash -- not even the log's.

**Consistency and bounds.** The marks are read before any content and again after the freeze; if
one moved in between, nothing is published and the read is refused with a retryable 409 rather
than storing a mixed snapshot. A scope whose pins or column count exceed the bundle limits is
refused before the freeze runs. A bundle whose documents contain a fenced code block is never
stored.

## Source bundles (R11-OKF02)

Design section 14: "Source bundles are scoped exports of discovered, authorized objects. Product
bundles contain only the selected approved references and permitted dependencies." A source
bundle is one datasource as Atlas discovered it, read through
`aida.okf_store.read_published_source_bundle`.

**What it holds.** The datasource's `ACTIVE` tables, views and materialized views, routines and
packages in the schemas the reader may read, each with the same document a product bundle would
render for it: captured structure, the approved descriptions Atlas holds, definition coverage and
reviewed lineage between objects of the bundle. The routine, view and freshness coverage comes
from the context compiler's own resolvers, called with the source's ids instead of a product's
pins. A deprecated object is not a discovered object and leaves the bundle -- its removal is in
`log.md` -- while a product that pins it keeps rendering it as deprecated. A dependency on an
object in another datasource is not linked, whatever the reader may see there.

**What it does not hold, deliberately: business concepts and tool versions.** Both are
*selected, approved and pinned* by a context product -- a concept from a pinned ontology version,
a tool from the product's eligible versions -- and a source has no pin to read them at. Exporting
the ontology's head, or every tool over the source, would put unselected meaning into a bundle an
agent reads as governed. The root index says so and points a reader to a context product; the
renderer refuses a `DATASOURCE` snapshot carrying a concept, a tool or a second source, so a
source bundle cannot count or link past its one source.

**Authorization.** A source bundle read is `READ_METADATA` -- the decision every catalog read of
the datasource already takes -- because it carries nothing a catalog read of that datasource does
not return. It is taken on every request, before anything stored is looked up
(`aida.okf_snapshot.admit_source`):

1. The datasource's own decision. A refusal is a 403 with the gate's bare reason code, as the
   catalog's read gives, and nothing exists for that reader: no lineage, no publication by id.
   A binding scoped to named schemas refuses this step, exactly as it refuses the catalog's read
   of the whole datasource.
2. Where step 1 reached a workspace, one decision per schema with the schema named. A schema a
   policy's `schema_pattern` refuses is absent from the text, the links and every count
   (acceptance OKF-D inside one source). When no workspace resolves, every schema would get
   step 1's own undecided answer, so the per-schema calls are skipped.

**Lineage.** A source lineage is keyed on the datasource and the admitted schema set, not on
which workspace decided: readers who may see the same schemas share one publication. The key is
stored in `datasource_id` beside `authority_digest` on the same publication and head tables a
product uses (migration `f7c2d9a4b61e`; a check constraint holds that a row is one scope or the
other), so publication, pinning, retention and pruning are the product's own code.

**Staleness.** The product's mark kinds, over every object the datasource holds, plus every
change signal its scans recorded and the object rows' own update times -- because a source's
membership is content: a table discovered or retired moves its indexes and counts whether or not
a signal named it. Revalidation, the read-consistent capture, the no-op rule, incremental rebuild
and atomic publication are the shared `_serve` and `_publish`. Early refusal counts the ACTIVE
objects and their columns in the admitted schemas before anything is loaded.

**The profile was not bumped.** The source scope's new field (`datasource_id`) is omitted from a
product snapshot's written form, and the manifest `scope` block, the extension's `scope` key and
`scope_digest` keep their product shape, so the bytes rendered for unchanged product content are
what profile `4` rendered before. Evidence is recorded against R11-OKF02.

## Question-specific context

A bundle is many small documents so that a reader can take the few a question needs.
`aida.okf_context` is how Atlas takes them, for the REST context route, the MCP knowledge tool
and Ask's SQL generation alike, all reading through `aida.okf_store.read_okf_context` and so
through the same `read_published_bundle` as every other surface:

1. **Rank from the frozen snapshot**, before any body is loaded: names, approved descriptions,
   column names and approved column meanings, concept names, labels and aliases, tool names and
   inputs. A term's weight is its rarity across this publication's subjects (BM25's smoothed
   idf) times the strongest field it appears in; a multi-word name or alias the question
   contains whole counts again. Only approved prose ranks. Deterministic, with no model and no
   embedding.
2. **Load only what ranked**, from the stored rows: at most six subjects, the column sets of a
   wide object whose columns the question names, and at most four documents one link away
   (a concept to its mapped table, a table to what it depends on).
3. **Hand out sections, not files.** Documents are cut at their top-level headings; a schema
   table longer than 30 rows keeps only the rows the question names and says how many it kept.
   Sections are taken meaning first, within a character budget, and everything the budget cut
   is listed. The budgets are settings -- `okf_context_default_max_chars` (16,000) for REST and
   MCP when the caller names none, `okf_context_ask_max_chars` (8,000) inside Ask -- under a
   fixed ceiling of 48,000.
4. **Receipts.** Each document carries its path and SHA-256 and each section its heading
   anchor, beside the publication id and digests. The audit record names `path#anchor` for
   every section handed out and never the question.

A question nothing matches is `NO_MATCH`, not a handful of unrelated documents. Two subjects of
one kind the question cannot tell apart are returned together and flagged `ambiguous`. The
bundle holds no source values, so the selection says so: a current figure needs an approved
tool through the query gateway, and a matching `Atlas Tool Version` document is returned like
any other. Inside Ask, the sections join the SQL-generation payload as grounding for which
tables and columns answer the question; every identifier the SQL uses must still come from the
metadata context, and the product boundary is enforced on the statement afterwards.

## Value freedom

No exported document contains a routine body, a view definition, a column or parameter default
expression, a source comment, a sample row or a profile statistic. This is structural: no field on
any snapshot value type can hold one, and a column's `default_expression` is never selected. A
definition is represented by a SHA-256 of the **stored value-free** text plus availability,
truncation and parse state. A connector's free-text `unavailable_reason` is reduced to a bounded
reason code rather than exported verbatim. Approved Atlas description text is screened at export
with the platform's own detector, and text screening refuses is reported as withheld rather than
quietly dropped.

## Authorization

An OKF bundle read is `CONSUME_CONTEXT`, not `READ_METADATA`: it is assembled context a reader
takes away. Scope resolution reuses the context compiler's own resolver, so the capability
envelope, consumer-role, purpose and quality gates all apply unchanged. On top of that, every
datasource reached by the product's scope is admitted or refused once, before anything is
assembled, and a refused datasource's objects are absent from the text, the indexes, the links
**and every count**. No "withheld: 1" is reported anywhere, because a withheld count is itself the
existence leak acceptance OKF-D exists to catch.

A source bundle read is `READ_METADATA` on its one datasource, and per schema where a workspace
decides; see [Source bundles](#source-bundles-r11-okf02).

## Surfaces

| Method and path | Returns |
|---|---|
| `GET /v1/context-product-versions/{version_id}/okf-bundle` | The stored manifest, the file index, the publish-policy verdict and the publication it describes. No document bodies. Optional `publication_id`. |
| `GET /v1/context-product-versions/{version_id}/okf-bundle/download` | One deterministic ZIP of the stored bundle. Refused unless it satisfies the publish policy. Optional `publication_id`. |
| `GET /v1/context-product-versions/{version_id}/okf-bundle/document?path=` | One stored document's exact bytes (R11-OKF02). |
| `GET /v1/context-product-versions/{version_id}/okf-bundle/publications` | The reader's own lineage of publications: trigger, counts, what changed (R11-OKF02). |
| `GET /v1/metadata/tables/{table_id}/okf-knowledge` | One catalog object's document from each product bundle the reader may read (R11-OKF02). When no product bundle holds the object, a `source` entry answers from the object's own datasource bundle: `DOCUMENT` (the document, its publication and coverage), `NOT_IN_BUNDLE` (nothing about the bundle is counted) or `REFUSED` (the bare reason code, nothing else). The datasource's `READ_METADATA` decision applies; a bundle that cannot be built is an HTTP error, never a state. |
| MCP `resources/read` of `atlas://context-products/{key}/versions/{n}/okf` | The same stored manifest and file index; append a bundle path to read one document (R11-OKF02). |
| `POST /v1/context-product-versions/{version_id}/okf-bundle/context` | The sections of the stored bundle a question needs, with receipts: see [Question-specific context](#question-specific-context). The question travels in the body, never the URL. |
| MCP `tools/call` of `atlas__get_knowledge_context` | The same selection for an agent that has a question rather than a path: Markdown to read, then the structured selection. |
| `GET /v1/datasources/{datasource_id}/okf-bundle` | One datasource's stored source bundle: manifest, file index, verdict and publication, as for a product. Optional `publication_id`. |
| `GET /v1/datasources/{datasource_id}/okf-bundle/download` | The source bundle as one deterministic ZIP, refused unless it satisfies the publish policy. Named by datasource id. Optional `publication_id`. |
| `GET /v1/datasources/{datasource_id}/okf-bundle/document?path=` | One stored document of the source bundle. Optional `publication_id`. |
| `GET /v1/datasources/{datasource_id}/okf-bundle/publications` | The reader's own lineage of source-bundle publications. |
| `POST /v1/datasources/{datasource_id}/okf-bundle/context` | The sections of the source bundle a question needs, with receipts; the product context route's contract, with the datasource in place of the product. |
| MCP `tools/call` of `atlas__get_source_knowledge_context` | The source bundle's selection for an agent that has a question: the source context route's store function, so the datasource's `READ_METADATA` decision applies and a refused caller is told what an unknown datasource is told. What it returns is screened live -- a section that fails is withheld, counted and audited by path and anchor, never returned -- and the question is never recorded. Behind the same native-tool gates as its product sibling (kill switch, `native_tools`), workload identity and the MCP budgets. |
| GraphQL `contextProductOkfBundle(versionId, publicationId)` and `datasourceOkfBundle(datasourceId, publicationId)` on `POST /graphql` | The stored bundle's manifest summary (profile, digests, counts, validity), the publication read, and beneath it connections priced before they run: `documents` (path, kind, digest, size, citation -- never text), `publications` (the reader's own lineage) and `findings`; `document(path)` returns one document's exact stored text. Read-only, through `read_published_bundle` / `read_published_source_bundle`, with the same role, envelope, workspace and lineage decisions as the routes above; recorded once per request on the `GRAPHQL_OKF_*` channels. Worked examples: [graphql-examples.md](graphql-examples.md#knowledge-bundles). |

These are new surfaces beside the single-file context compiler, which is untouched: no
`ContextCompilerTarget` value was added and no compile response shape changed, so an existing
consumer sees no difference. A downloaded file cannot be remotely revoked; export permissions,
classification and expiry notices govern distribution, and no offline revocation is promised.
