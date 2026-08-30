# Metadata ingestion envelope 1.1 — views, routines, comments, grants

> Gap item **N1**. Phase 1. Written 2026-08-30.
> Status: **shipped for the four axes named above.** Index and partition inventory, which an
> earlier draft of the contract's §10 listed against 1.1, are deliberately **not** included and
> remain tracker row `CN-8`.
> Contract: `Docs/30-contracts/05-metadata-ingestion-envelope.md` (updated in the same change).

## 1. Why 1.1 exists, field by field

`gap/02` row N1 says of this item: *"Unblocks N2, N3, N6, N7. Nothing else can start without it."*
That is the whole justification, and it is worth being literal about which consumer needs which
field, because a field with no named consumer is a field nobody will maintain.

| Field | Added to | Consumed by | Why it cannot be derived instead |
|---|---|---|---|
| `view_definition.definition_sql` | table | **N2** view-DDL → column lineage | The only place a view's real inputs are written down. Column names alone give a shape, not a derivation |
| `view_definition.is_materialized` | table | N2 | A materialized view's freshness is a different lineage claim from a live one's |
| `view_definition.is_updatable`, `check_option` | table | N2, N11 | A tool generator must not offer a write path through a non-updatable view |
| `routines[].body_sql` | schema | **N3** procedure parsing, **N12** procedure → tool | A read-only proof for procedure-to-tool generation is proved *against the body*. Without it, N12 cannot exist at all |
| `routines[].parameters[]` | schema | **N12** | A generated tool binds arguments by position and type. This is the tool's signature |
| `routines[].routine_type`, `security_mode`, `is_deterministic` | schema | N12 | `SECURITY DEFINER` and non-determinism are both refusal conditions for a generated tool |
| `source_description` | catalog, schema, table, column | **N6**/**N7** workspace and ABAC review, N10 knowledge compilation | The strongest meaning signal an estate already carries. Cheaper and more trustworthy than model inference, and it is what the confidence caps (K3) exist to prefer |
| `grants[]` | schema | **N7** ABAC, source-binding review | "Who can already see this in the source" is not answerable from anything else, and it is the first question a binding reviewer asks |

Two axes were considered and left out:

- **Indexes and partitions.** Real, but they serve cost estimation rather than lineage or meaning, and nothing in Phase 1 or 2 consumes them. Adding them here would have widened the connector work by two more system-view families per source for no unblocked consumer. Tracker `CN-8`.
- **Negative privileges (`DENY`).** Modelling revocation needs a resolution rule — which DENY beats which GRANT, at which scope — and nothing downstream consumes it. A half-represented DENY reads as an absent one, which is worse than an honest omission.

## 2. Backward compatibility, stated plainly

**A 1.0 producer keeps working, unchanged, forever.** Every 1.1 field is optional; `envelope_version` still *defaults* to `"1.0"`; the 1.0 JSON in the contract's §2 validates byte-for-byte as it did before. This is asserted, not asserted-to: `tests/test_envelope_v11.py::test_a_10_envelope_with_no_11_content_is_still_accepted` validates the literal 1.0 payload shape.

The default was deliberately **not** moved to `"1.1"`. Promoting silent producers would also opt them in to 1.1 FULL reconciliation, and a producer that had never heard of 1.1 would retire the estate's view definitions on its next full scan.

The one thing 1.1 refuses is a payload that declares `"1.0"` while carrying 1.1 fields. That is a **422** naming every offending field, not a 201 with the fields dropped. Accepting and dropping is the failure this check exists to prevent: a producer that ships view definitions and is told "created" has every reason to expect lineage to follow, and would find out otherwise only months later by noticing an absence.

## 3. The storage model, and why it is shaped that way

New module `src/aida/envelope_models.py`. Five tables, all declared against the same
`aida.db.Base` as `models.py`, so `Base.metadata.create_all` and Alembic autogenerate see them
identically. `models.py` is untouched — see §7.

| Table | Grain | Key | Note |
|---|---|---|---|
| `metadata_view_definition` | one view | `table_id` unique | 1:1 with `metadata_table`, not a column on it |
| `metadata_routine` | one routine | `(schema_id, name, signature)` | `signature` derived from parameter types |
| `metadata_routine_parameter` | one parameter | `(routine_id, ordinal_position)` | Ordered, typed, not JSON |
| `metadata_object_description` | one described object | one of `catalog_id` / `schema_id` / `column_id` | `TABLE` deliberately absent |
| `metadata_source_grant` | one privilege | `(schema_id, grant_key)` | `grant_key` = SHA-256 of the natural key |

Every table carries `organization_id` **and** `datasource_id`, `status` / `deprecated_at`, and a
`fingerprint`, matching the 1.0 tables exactly. `datasource_id` is not redundant with the parent
FK: FULL reconciliation needs "every 1.1 row for this datasource" as one indexed query per axis,
and reaching that through `metadata_schema` → `metadata_catalog` would be a three-way join on the
hottest path in ingestion. `tests/test_envelope_v11.py::test_every_11_row_carries_its_tenant_boundary`
asserts the boundary on *rows*, not on the mapping — a column that exists but is never populated
satisfies the schema and breaks INV-5.

Four shape decisions worth defending:

**A view definition is its own table, not a column.** The definition of a large view is multi-kilobyte
text that no catalog listing, search projection or drift comparison ever reads. It is also, separately,
not addable to `models.py` in this workstream.

**Routine parameters are rows, not JSON.** A tool generator (N12) binds arguments by position and type.
A generator that reads its argument list out of an unconstrained JSON blob has no schema to fail against
when a source changes shape — and failing loudly is the entire safety story for generated tools.

**Routines are keyed on a derived signature.** PostgreSQL permits overloading, so `(schema, name)` is not
an identity there. Keyed on the name alone, the second overload would overwrite the first and a FULL
reconciliation would retire whichever arrived earlier — losing half the procedural estate on every scan.
The signature is derived from parameter physical types rather than stored from a source-side identifier,
so it is stable across snapshots and works on sources that have no such identifier.
Proven by `test_two_overloads_of_one_routine_are_two_rows`.

**`metadata_object_description` holds catalogs, schemas and columns — not tables.**
`metadata_table.source_description` already exists in `models.py` and is written by the 1.0 path.
Two homes for one fact is how they diverge, so a check constraint restricts `object_type` to the three
that have nowhere else to go. The three subject FKs are real columns with `ON DELETE CASCADE` and an
"exactly one is set" check constraint, rather than a bare polymorphic `object_id`: a deleted column
takes its description with it instead of leaving a row pointing at nothing.

## 4. Truncation and unavailability

The distinction the whole storage model is arranged around:

| The source… | `availability` | `definition_sql` / `body_sql` | `truncated` | `unavailable_reason` |
|---|---|---|:--:|---|
| gave the full text | `AVAILABLE` | the text | `false` | `NULL` |
| gave a prefix | `AVAILABLE` | the prefix | `true` | `NULL` |
| **would not give it** | `UNAVAILABLE` | **`NULL`** | `false` | **required** |
| has nothing to give | `AVAILABLE` | `''` | `false` | `NULL` |

Rows 3 and 4 are the pair that matters. A parser that cannot tell them apart either reports a view as
having no lineage when the truth is "we were not allowed to look" — a silent coverage hole that looks
like a complete answer — or retries forever against a source that will never answer.

Enforced in three places, deliberately: a Pydantic validator rejects a null definition with no reason
(and a reason alongside a present definition); the persistence layer derives `availability` from the
value rather than trusting a flag; and a database `CHECK` constraint asserts
`(availability = 'AVAILABLE') = (definition_sql IS NOT NULL)` so no future writer can produce the
inconsistent row at all. `test_an_unavailable_definition_is_not_stored_as_an_empty_one` runs against a
real database precisely so the constraint executes.

**Truncation is reported, never guessed.** PostgreSQL's `pg_get_viewdef` and `pg_get_functiondef` return
the complete reconstructed text, so the PostgreSQL connector reports `truncated = false` because that is
true. SQL Server reads `sys.sql_modules.definition` (`nvarchar(max)`), **not**
`INFORMATION_SCHEMA.ROUTINES.ROUTINE_DEFINITION`, which is `nvarchar(4000)` and silently truncates every
longer body — precisely the long ETL procedure whose text is worth parsing.
`test_routine_bodies_are_read_from_sys_sql_modules_not_information_schema` asserts against the statement
constants so the explanatory comment beside them cannot satisfy its own test.

## 5. Reconciliation — INV-11 preserved, not approximated

The contract's §4 rule ("a `FULL` batch accumulates stable object identities across every chunk and runs
omission reconciliation **only after all chunks have succeeded**") is the property `gap/02` K11 calls the
hardest one already right in this codebase. The 1.1 axes were given the identical mechanism rather than a
convenient approximation:

- `ingestion.EnvelopeScope` mirrors `workflows.activities.SnapshotScope` — a set of observed identities per axis, threaded through every chunk.
- `batch_ingestion._process_chunk` persists 1.1 rows with `deprecate_missing=False`, always.
- `batch_ingestion._complete_batch` calls `deprecate_missing_envelope_extensions` once, after every chunk has succeeded, and only for a `FULL` batch.
- `test_a_partial_full_delivery_never_retires_anything` drives a two-chunk delivery through one shared scope and asserts the intermediate state retires nothing.

One rule is **new** in 1.1 and is a genuine semantic addition rather than a mechanical port:

> **1.1 reconciliation is gated on the declared version as well as on `FULL`.**

A `FULL` 1.0 envelope is authoritative for the 1.0 inventory only. It makes no statement about views,
routines, descriptions or grants, so its silence is not omission. Without this gate, a producer rolling
back to 1.0 for one release would wipe the estate's view definitions on its next full scan — the same
class of failure as reconciling a partial delivery, arriving through a different door.
Asserted by `test_a_10_producer_downgrade_does_not_retire_11_metadata`.

## 6. Connectors — what each one now claims, and the evidence

INV-9 requires that a `true` mean "implemented", not "the source supports it". Four new flags were added
to `ConnectorCapabilities`, all defaulting to `False`, so every connector that has not implemented an
axis keeps reporting honestly with no edit.

| Connector | `views` | `routines` | `object_comments` | `grants` | Evidence |
|---|:--:|:--:|:--:|:--:|---|
| **postgres** | ✅ | ✅ | ✅ | ✅ | `pg_get_viewdef` over `pg_class relkind in ('v','m')`; `pg_proc` + `pg_get_functiondef` + a `pg_proc`/`unnest(proallargtypes)` parameter query; `shobj_description` / `obj_description` / `col_description`; `information_schema.role_table_grants` |
| **sqlserver** | ✅ | ✅ | ✅ | ✅ | `sys.views` + `sys.sql_modules.definition`; `sys.objects` (`P`,`FN`,`IF`,`TF`) + `sys.sql_modules` + `sys.parameters`; `sys.extended_properties` `MS_Description` at class 0/1/3; `sys.database_permissions` class 1, states `G`/`W` |
| oracle | ✅ | ✅ | ✅ | ✅ | Taken to 1.1 in parallel by another workstream — see `gap/08-envelope-v11-connectors.md`, which is authoritative for these three |
| snowflake | ✅ | ✅ | ✅ | ✅ | As above |
| bigquery | ✅ | ✅ | ✅ | ❌ | As above. `grants = false` because BigQuery has no SQL grants — an honest "the source does not work that way", not a deferral |

The bottom three rows are **not** this workstream's work and are recorded here only so the matrix is
complete as of 2026-08-30; `gap/08` is the authoritative record for them, and it was written
concurrently, so re-read it rather than trusting this table if the two disagree.

Both connectors this workstream owns keep `indexes = False` and `partitions = False`. Both sources
expose them; this code does not read them, and a flag that describes the source rather than the
connector is exactly the optimism INV-9 forbids.

The capability tests assert the flag **and** the query that backs it
(`test_postgres_advertises_exactly_the_11_axes_it_implements`,
`test_sqlserver_advertises_exactly_the_11_axes_it_implements`), so a flag cannot outlive the behaviour
behind it. Neither connector's new SQL has been run against a live source — there is no PostgreSQL or
SQL Server fixture in this repository — which is the same standing limitation `CN-1c` and `CN-2a` record
for BigQuery and Snowflake. The row shapes are tested end-to-end through the assembly helpers; the SQL
text is not.

Two SQL Server readings are judgement calls, recorded so a reviewer can disagree:

- `is_materialized` is `OBJECTPROPERTY(..., 'IsIndexed')`. SQL Server has no materialized views; an indexed view is the nearest true thing, and reporting `false` for everything would be less informative than reporting a defensible approximation.
- `grantee_type` collapses `sys.database_principals.type` into `ROLE` / `USER`. Application roles (`A`) are reported as `ROLE`.

## 7. Changes wanted and deliberately not made

**`models.py` — `metadata_column.source_description` (a `Text` column, nullable).**
This is the change I would make. Tables already have `source_description` on `metadata_table`; columns do
not, so column comments are stored in `metadata_object_description` with a `column_id` FK instead. That is
a working design and the check constraint keeps it honest, but it is asymmetric: the same fact lives in a
column for tables and in a row for columns, and a reader has to know which. `models.py` is owned by a
concurrent session, so this was designed around rather than made. If it is added later, the migration is
a column add plus a backfill from `metadata_object_description WHERE object_type = 'COLUMN'`, and
`DESCRIBABLE_OBJECT_TYPES` narrows to `('CATALOG', 'SCHEMA')`.

**`models.py` — `AnalysisRun.discovered_views` / `_routines` / `_grants`.**
`AnalysisRun` has a `discovered_*` integer column per 1.0 axis. The 1.1 axes have no equivalent, so their
counts reach the operator only through `MetadataIngestionJob.object_counts` / `MetadataIngestionBatch.object_counts`
(both JSON, so no migration was needed). Not blocking; noted so the asymmetry is a decision rather than an oversight.

**`workflows/activities.py` — the pull path was not wired when this was written. It is now (2026-08-30):**
`src/aida/workflows/activities.py` imports `persist_envelope_extensions` at line 27 and calls it at
line 587, so all three transports persist the 1.1 axes. The tracker row this section proposed
(`IN-5b`) is closed. The description below is kept as the record of what the gap was and how it
was closed, not as a live claim.

*Original text follows.*

**`workflows/activities.py` — the pull path is not wired.**
`persist_envelope_extensions` is called from `ingestion_api.ingest_metadata_envelope` and from
`batch_ingestion._process_chunk`, so **both push paths** persist the 1.1 axes. The Temporal pull path
(`activities.discover_datasource` → `persist_discovery_snapshot`) is not wired, because
`workflows/activities.py` is owned by another workstream in this increment. **This is the one functional
gap in N1 as delivered**: a PostgreSQL or SQL Server source scanned via native pull collects the new axes
in `discover()` and then drops them at persistence. The fix is one call, mirroring the API path exactly:

```python
# in aida.workflows.activities.discover_datasource, immediately after the existing
# persist_discovery_snapshot(...) call, inside the same session/transaction:
from aida.ingestion import persist_envelope_extensions

await persist_envelope_extensions(
    session,
    datasource,
    catalogs,
    deprecate_missing=(run.mode == "FULL"),
)
```

No version gate is needed there: a pull snapshot is produced by a connector whose capability flags already
say whether it collected the axes, so a connector that collects them is authoritative for them.

**`schemas.py` — kept to field-level edits.**
Eight anchored edits, no rewrite: four new nested models, five optional fields on existing models, the
`Literal["1.0", "1.1"]` widening on the two version fields, and two lines inside the existing validator
(screening `routines[].attributes` for value-bearing keys per INV-6, and a 50,000-routine synchronous
bound). Nothing was reordered or reformatted.

## 8. Migration

| | |
|---|---|
| Revision | `a1c9f4b7e230` — `migrations/versions/a1c9f4b7e230_metadata_envelope_v11_axes.py` |
| Parent (`down_revision`) | `c9d1a83e6b47` (workspace access rules and shadow mode) |
| Originally chained onto | `b4e2f70a9c15` — the single head when the revision was written |
| Verified | `alembic heads` prints exactly one head: `a1c9f4b7e230` |
| Reversible | Yes — creates five tables and touches nothing existing; `downgrade()` drops only what it created |

The parent moved once already. `b4e2f70a9c15` was the single head at the moment of writing; a concurrent
workstream then added `c9d1a83e6b47` on the same parent, producing two heads and a CI failure, so this
revision was re-pointed onto theirs. Only this file was touched to do it.

**If the head moves again, re-point `down_revision` and change nothing else.** This revision depends only
on the 1.0 metadata tables (`metadata_catalog`, `metadata_schema`, `metadata_table`, `metadata_column`,
`datasource`, `organization`), all of which long predate every candidate parent, so its body is
parent-independent.

## 9. Verification

Run from a clean checkout on the Linux virtualenv:

```
ruff check .                              All checks passed!
mypy --cache-dir=... src                  Success: no issues found in 116 source files
lint-imports                              Contracts: 4 kept, 0 broken.
alembic heads                             a1c9f4b7e230 (head)
pytest -p no:cacheprovider --no-header    683 passed, 1 xfailed
```

Baseline at the start of this workstream was 575 passed / 2 xfailed on 114 source files. Two other
workstreams were writing to the same tree throughout, so the totals above are **not** attributable to
this change alone: 25 of the added tests are this workstream's, and the second `xfail` (INV-7,
endpoints committing governed state with no audit row) was closed by another workstream, not by this
one. Re-run the suite after the tree settles; nothing here depends on the totals.

New and changed tests:

| File | Covers |
|---|---|
| `tests/test_envelope_v11.py` (new, 17 tests) | Axis persistence, tenancy on rows, unavailable-vs-empty, malformed-1.1 rejection, idempotency and drift, overload identity, FULL reconciliation, the INV-11 partial-delivery rule, the 1.0-downgrade rule, version discipline, declared counts, scope counts, key stability |
| `tests/test_connectors.py` (+4) | PostgreSQL capability honesty with query evidence; 1.1 assembly through the shared helpers; unavailable-with-a-reason; a routine-only schema survives assembly |
| `tests/test_connectors_sqlserver.py` (+4) | SQL Server capability honesty with query evidence; the `sys.sql_modules` truncation rule; 1.1 assembly including an encrypted module; the 1.0 assembly signature still works |
| `tests/test_ingestion.py` (2 updated) | Declared counts now report the 1.1 axes and are zero for a 1.0 payload |

## 10. Proposed tracker rows

For `Docs/60-delivery/03-tracker.md` §B (Connectors and ingestion). Not applied — that document is
owned elsewhere.

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| IN-5a | Envelope 1.1: views + DDL, routines + body, source comments, source grants | 03 | B | P0 | DONE | — | Shipped 2026-08-30 (gap N1). Additive contract: 1.0 accepted unchanged and still the default; declaring 1.0 while sending 1.1 content is a 422 naming the fields, never a silent drop. Five tables in `src/aida/envelope_models.py`, migration `a1c9f4b7e230` (parent `b4e2f70a9c15`, single head verified). Unavailable is distinguishable from empty in the stored row, enforced by a Pydantic validator, the persistence layer and a `CHECK` constraint. 1.1 FULL reconciliation preserves INV-11 (accumulate across chunks, reconcile once) and is additionally gated on the declared version so a 1.0 downgrade cannot retire 1.1 metadata. 25 new tests; suite green (`ruff`, `mypy` clean, 4 import-linter contracts kept, 1 Alembic head, full suite green). Record: `Docs/review-2026-08/gap/07-envelope-v11.md` |
| IN-5b | Envelope 1.1 on the native pull path | 03 | B | P0 | TODO | — | `activities.discover_datasource` calls `persist_envelope_extensions` after `persist_discovery_snapshot`, so a PostgreSQL or SQL Server source scanned via pull persists the axes its connector already collects. One call; exact form in `gap/07-envelope-v11.md` §7. Until then only the two push paths store the 1.1 axes |
| IN-5c | Envelope 1.1 axes on Oracle, Snowflake, BigQuery | 02 | B | P1 | DONE | — | Delivered in parallel by a separate workstream; eleven of twelve connector × axis cells populated, BigQuery `grants` answered "the source has no SQL grants" rather than deferred. Record: `Docs/review-2026-08/gap/08-envelope-v11-connectors.md`. **Confirm against that document before accepting this row** — it landed concurrently with N1 |
| IN-5d | Live-source verification of the 1.1 discovery SQL | 02 | B | P1 | TODO | — | No 1.1 discovery statement on **any** connector has run against a live source; row shapes are tested, SQL text is not. Same standing limitation as `CN-1c`/`CN-2a`. Exit: one live fixture per source returns a non-empty view, routine, comment and (where the source has them) grant inventory |
| IN-5e | `metadata_column.source_description` on `metadata_column` | 04 | B | P2 | TODO | — | Column comments currently land in `metadata_object_description` because `models.py` was owned elsewhere during N1, making the same fact a column for tables and a row for columns. Exit: column added, backfilled from `object_type = 'COLUMN'`, `DESCRIBABLE_OBJECT_TYPES` narrowed to `('CATALOG', 'SCHEMA')` |
| CN-8 | Index and partition extraction | 02/04 | A | P1 | TODO | — | *(existing row — amend the exit note)* Deliberately excluded from envelope 1.1: both serve cost estimation rather than lineage or meaning, and no Phase 1/2 consumer reads them. The contract's §10 no longer lists them against 1.1 |

## Related documents

- Contract: `Docs/30-contracts/05-metadata-ingestion-envelope.md`
- Gap plan: `Docs/review-2026-08/gap/02-gap-diff-and-plan.md` row N1, §7 Phase 1
- Invariant suite: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` (INV-5, INV-6, INV-9, INV-11)
- Oracle / Snowflake / BigQuery 1.1 connectors: `Docs/review-2026-08/gap/08-envelope-v11-connectors.md`
