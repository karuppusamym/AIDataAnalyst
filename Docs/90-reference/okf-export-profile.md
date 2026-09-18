# Atlas OKF export profile

Reference for the Open Knowledge Format bundles Atlas produces, the `atlas` frontmatter
extension they carry, and the Atlas manifest that travels beside them. Delivered by tracker row
R11-OKF01 for design item 14A ([items 13–17 design](../10-architecture/21-graphql-okf-and-workspace-design.md),
section 14).

Implementation: `src/aida/okf_export.py` (pure renderer and validators),
`src/aida/okf_snapshot.py` (the frozen snapshot, loaded under the caller's authority) and
`src/aida/okf_export_api.py` (the two read surfaces). Tests: `tests/test_okf_export.py`, with
conformance fixtures under `tests/fixtures/okf_conformance/`.

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
bundle/sources/source-<key>/schemas/schema-<key>/tables/table-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/views/view-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/routines/routine-<key>.md
bundle/sources/source-<key>/schemas/schema-<key>/packages/package-<key>.md
bundle/concepts/index.md
bundle/concepts/concept-<key>.md
```

The manifest sits outside `bundle/` so a reader pointed at the OKF bundle root never needs an
Atlas extension to consume it.

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

`signature` keeps PostgreSQL overloads apart, `package_name` keeps a packaged member apart from a
standalone routine of the same name, and the leading kind token keeps a routine apart from a table
of the same name. Object kind is deliberately *not* part of a table-like identity: tables and
views share one namespace in every supported engine, so a rescan that reclassifies one must not
move its document. A collision refuses the export rather than overwriting a document.

## Concept types

`type` values are producer-chosen (§4.1) and consumers must tolerate unknown ones (§11):
`Atlas Data Source`, `Atlas Schema`, `Atlas Table`, `Atlas View`, `Atlas Materialized View`,
`Atlas Routine`, `Atlas Routine Package`, `Atlas Business Concept`.

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
  This profile is export-only; import is R11-OKF03 and is not enabled.
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
| `statements` | Which parts of the document an Atlas approval covers (`statements.approved`) and which are derived (`statements.derived`). This is the per-statement answer OKF's single document-level `verified` cannot give. |
| `scope` | The export's scope: `scope.kind`, `scope.product_key`, `scope.product_version`, `scope.policy_partition_digest`. |
| `withheld` | On a concept document only: `withheld.reason_codes` when screening refused released text. |

## The Atlas manifest

`atlas-manifest.json` is an Atlas extension, not an OKF requirement. Keys: `manifest_version`,
`atlas_extension`, `okf_version`, `bundle_root`, `specification` (repository, path, revision,
sha256, conformance), `compiler`, `captured_at`, `scope`, `policy_partition` (with its digest),
`scope_digest`, `content_snapshot_digest`, `bundle_content_digest`, `counts`, `files` (path,
sha256, bytes), `source_objects` and `source_freshness`.

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
current catalog rows". Durable server-side snapshot storage is not part of this row: incremental
rebuild needs the stored prior bundle to diff against anyway, so it belongs with R11-OKF02.

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

## Surfaces

| Method and path | Returns |
|---|---|
| `GET /v1/context-product-versions/{version_id}/okf-bundle` | The manifest, the file index and the publish-policy verdict. No document bodies. |
| `GET /v1/context-product-versions/{version_id}/okf-bundle/download` | One deterministic ZIP. Refused unless the bundle satisfies the publish policy. |

These are new surfaces beside the single-file context compiler, which is untouched: no
`ContextCompilerTarget` value was added and no compile response shape changed, so an existing
consumer sees no difference. A downloaded file cannot be remotely revoked; export permissions,
classification and expiry notices govern distribution, and no offline revocation is promised.
