# Envelope v1.1 on Oracle, Snowflake and BigQuery

> Gap item **N1** (`02-gap-diff-and-plan.md` §4). Written 2026-08-30.
> Status: **implemented for all three connectors.** Eleven of the twelve
> connector × axis cells are populated; the twelfth (BigQuery grants) is answered
> "the source does not work that way", not deferred.
>
> Scope note: this document covers **Oracle, Snowflake and BigQuery**. PostgreSQL and
> SQL Server were taken to v1.1 in parallel by another workstream in the same wave
> and are summarised in §6 for completeness only; nothing here is authoritative for
> them.

---

## 1. What the envelope added

`src/aida/connectors/base.py` carries the v1.1 contract, which this workstream
consumed and did not change. Four axes, four dataclasses, four capability flags:

| Axis | Contract | Flag |
|---|---|---|
| View definitions | `DiscoveredTable.view_definition` → `DiscoveredViewDefinition` | `views` |
| Routines with bodies | `DiscoveredSchema.routines` → `DiscoveredRoutine` / `DiscoveredRoutineParameter` | `routines` |
| Object comments | `source_description` on catalog, schema, table, column, routine | `object_comments` |
| Source grants | `DiscoveredSchema.grants` → `DiscoveredGrant` | `grants` |

All four flags default to `False`, so a connector that has implemented nothing keeps
reporting honestly (INV-9) with no edit. Each flag below is set because the
connector reads the named source object and lands the result on the envelope.

---

## 2. Per-connector matrix

### 2.1 Oracle — `src/aida/connectors/oracle.py`

| Axis | State | Read from | Flag |
|---|---|---|---|
| View definitions | **Implemented** | `ALL_VIEWS.TEXT` + `ALL_VIEWS.TEXT_LENGTH`; `ALL_MVIEWS.QUERY` + `QUERY_LEN` for materialized views | `views=True` |
| Routines | **Implemented** | `ALL_OBJECTS` (`PROCEDURE`, `FUNCTION`, `PACKAGE`) left-joined to `ALL_PROCEDURES` for `DETERMINISTIC` and `AUTHID`; bodies from `ALL_SOURCE`; parameters from `ALL_ARGUMENTS` | `routines=True` |
| Object comments | **Partially — table and column** | `ALL_TAB_COMMENTS`, `ALL_COL_COMMENTS` | `object_comments=True` |
| Source grants | **Implemented** | `ALL_TAB_PRIVS` left-joined to `ALL_USERS` to separate a user grantee from a role grantee | `grants=True` |

**Where Oracle stops, and why.**

* **No schema, catalog or routine comment.** Oracle's `COMMENT ON` accepts a table,
  a column, a materialized view, an indextype, an operator, an edition and a mining
  model — not a schema, a database or a procedure. `DiscoveredCatalog`,
  `DiscoveredSchema` and `DiscoveredRoutine` therefore carry
  `source_description=None` on Oracle. Source cannot, not chose-not-to.
* **No `is_updatable`, no `check_option`.** `ALL_VIEWS` has no column for either.
  `READ_ONLY` exists from 12.1 but says only that the view was declared read-only,
  not that a writable view is actually updatable (a join view is not), so it is not
  mapped. Both fields stay `None`.
* **`DBMS_METADATA.GET_DDL` is deliberately not used.** `ALL_SOURCE` is the
  least-privilege path: it returns text for objects the session owns or holds a
  privilege on, where `GET_DDL` in practice wants `SELECT_CATALOG_ROLE`. Using
  `GET_DDL` would make the connector work in a lab and fail in a bank.
* **A package's parameters are empty on purpose.** `ALL_ARGUMENTS` keys a packaged
  subprogram's arguments to the *subprogram*, not to the package object the envelope
  reports. Merging them onto the package would invent a parameter list no PL/SQL
  caller could use, so a `PACKAGE` routine carries `parameters=()` plus an
  `attributes["packaged_subprogram_parameters"]` note.
* **Parameter defaults are always `None`.** `ALL_ARGUMENTS.DEFAULT_VALUE` is a LONG
  that Oracle does not populate. Reading `DEFAULTED` would tell us a default exists
  without telling us what it is, which `DiscoveredRoutineParameter` has no field for.

### 2.2 Snowflake — `src/aida/connectors/snowflake.py`

| Axis | State | Read from | Flag |
|---|---|---|---|
| View definitions | **Implemented** | `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`, with `GET_DDL('VIEW', …, TRUE)` as a second pass | `views=True` |
| Routines | **Implemented** | `INFORMATION_SCHEMA.FUNCTIONS` and `.PROCEDURES`; parameters parsed from `ARGUMENT_SIGNATURE` | `routines=True` |
| Object comments | **Implemented at all five levels** | `INFORMATION_SCHEMA.DATABASES.COMMENT`, `.SCHEMATA.COMMENT`, `.TABLES.COMMENT`, `.COLUMNS.COMMENT`, `.FUNCTIONS`/`.PROCEDURES.COMMENT` | `object_comments=True` |
| Source grants | **Partially — schema level** | `SHOW GRANTS ON SCHEMA <db>.<schema>`, one statement per discovered schema | `grants=True` |

**Where Snowflake stops, and why.**

* **`SHOW GRANTS` is not a view over `INFORMATION_SCHEMA`.** It is a metadata
  command: it cannot be joined, filtered or aggregated, and it returns grants for
  exactly one named object per call. The only set-returning grant surface Snowflake
  offers is `SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES`, which needs access to the
  shared `SNOWFLAKE` database and lags reality by up to two hours. So the connector
  issues one `SHOW GRANTS ON SCHEMA` per schema — bounded by schema count — and does
  **not** issue one per table, which would be unbounded against a real estate.
  Table-level grants are therefore *chose-not-to-yet*, and the axis is honest about
  covering schema objects only. Each schema's refusal is recorded separately, under
  the key `grants:<schema>`.
* **No `INFORMATION_SCHEMA.PARAMETERS` exists.** Snowflake carries a routine's whole
  argument list as one text column, `ARGUMENT_SIGNATURE`, shaped like
  `(A NUMBER(38,0), B BOOLEAN DEFAULT FALSE)`. Parsing it is the only route to a
  parameter list, so `_parse_argument_signature` is a pure, separately tested
  function that splits on top-level commas only (`NUMBER(38,0)` must not split at its
  own comma). Every Snowflake argument is an input argument, so `mode` is always
  `IN`.
* **No `security_mode`, no `is_deterministic`.** A procedure's
  `EXECUTE AS OWNER | CALLER` and a function's volatility are reachable only through
  `SHOW PROCEDURES` / `DESCRIBE`, not through `INFORMATION_SCHEMA`. Both stay `None`
  rather than being guessed. Chose-not-to-yet: closing this means another per-object
  metadata command.
* **`CHECK_OPTION` is normalised away.** Snowflake returns the literal string
  `'NONE'`, which is not a check option; it is mapped to `None`. `IS_UPDATABLE` is
  read as given (Snowflake views are not updatable, and it reports `'NO'`).

### 2.3 BigQuery — `src/aida/connectors/bigquery.py`

| Axis | State | Read from | Flag |
|---|---|---|---|
| View definitions | **Implemented** | `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`; `INFORMATION_SCHEMA.TABLES.DDL` for materialized views | `views=True` |
| Routines | **Implemented** | `INFORMATION_SCHEMA.ROUTINES` (incl. `ROUTINE_DEFINITION`), `.PARAMETERS`, `.ROUTINE_OPTIONS` | `routines=True` |
| Object comments | **Implemented for schema, table, column, routine** | `SCHEMATA_OPTIONS`, `TABLE_OPTIONS`, `COLUMN_FIELD_PATHS.DESCRIPTION`, `ROUTINE_OPTIONS` | `object_comments=True` |
| Source grants | **Not implemented — the source has none** | — | `grants=False` |

**Where BigQuery stops, and why.**

* **Grants: BigQuery has no SQL grant surface.** Access is Cloud IAM policy, bound at
  project, dataset, table, column and row level, inherited down the resource
  hierarchy and optionally conditional on a CEL expression.
  `INFORMATION_SCHEMA.OBJECT_PRIVILEGES` does expose those bindings, but its
  `privilege_type` is an **IAM role name** (`roles/bigquery.dataViewer`) — a bundle
  of permissions — where `DiscoveredGrant.privilege` everywhere else holds one SQL
  privilege (`SELECT`). Writing a role bundle into that field would make "who can
  already see this" answer differently on BigQuery than on Oracle or Snowflake while
  looking identical, which is worse than declining the axis. `grants=False` is the
  correct, honest answer; closing it properly needs an IAM-binding axis of its own,
  not `DiscoveredGrant`. The reason is also carried in-band, on
  `DiscoveredCatalog.attributes["grants"]`, so a reader of the envelope sees it
  without reading this document.
* **No catalog description.** A GCP project has no description in
  `INFORMATION_SCHEMA`. `DiscoveredCatalog.source_description` is `None` on BigQuery.
* **`IS_DETERMINISTIC` and `SECURITY_TYPE` are read and normally arrive `None`.**
  BigQuery documents both `ROUTINES` columns as always NULL. They are read rather
  than assumed so that the day BigQuery starts populating them the envelope carries
  them; today they map to `None`.
* **Materialized-view text is the whole `CREATE` statement.** A materialized view has
  no `INFORMATION_SCHEMA.VIEWS` row at all, so its text comes from `TABLES.DDL`,
  which is `CREATE MATERIALIZED VIEW … AS SELECT …` rather than the bare query.
  View-DDL lineage (N2) must expect a statement, not a `SELECT`, for these.
* **A defect fixed on the way past.** `discover()` previously hardcoded
  `'BASE TABLE' AS table_type` for every object, so BigQuery reported views and
  materialized views as base tables. The real type now comes from
  `INFORMATION_SCHEMA.TABLES`; if that query is refused, every object keeps the old
  `BASE TABLE` default and the refusal is recorded rather than discovery failing.

---

## 3. Summary matrix

| Axis | Oracle | Snowflake | BigQuery |
|---|---|---|---|
| `views` | ✅ incl. materialized | ✅ incl. materialized via `GET_DDL` | ✅ incl. materialized via `TABLES.DDL` |
| `routines` | ✅ procedures, functions, packages | ✅ functions, procedures | ✅ scalar/table/aggregate functions, procedures |
| `object_comments` | ⚠️ table + column only (source limit) | ✅ all five levels | ⚠️ no catalog level (source limit) |
| `grants` | ✅ object-level via `ALL_TAB_PRIVS` | ⚠️ schema level only (our bound) | ❌ source has no SQL grants |

---

## 4. Truncation and permission behaviour a reader would be surprised by

The envelope's rule is narrow and load-bearing: **`definition_sql is None` with a
populated `unavailable_reason` means the source would not give it to us; an empty
string means the definition is empty; `truncated=True` means we got a prefix.**
Downstream, view-DDL lineage would read a silent empty as a lineage gap *in the
estate* rather than a gap in our extraction, and would read a silent clip as a set of
tables the view does not reference. Every path below exists to keep those apart.

1. **Oracle's LONG columns can hand back an empty value for a non-empty view.**
   `ALL_VIEWS.TEXT` and `ALL_MVIEWS.QUERY` are LONG. When the session cannot
   materialise one, the text arrives empty or NULL while the companion length column
   (`TEXT_LENGTH` / `QUERY_LEN`) still reports the real size. That combination is
   recorded as **unavailable with the declared length in the reason**, never as an
   empty definition. This is the single most likely way a view's text goes missing on
   Oracle, and it is the reason both length columns are selected at all.
2. **A short fetch against a longer declared length is a prefix, not a definition.**
   Same two columns: if `len(text) < TEXT_LENGTH`, `truncated=True`. The 4000-character
   shape of `ALL_VIEWS.TEXT_VC` is exactly this case, and it does not reach the
   envelope silently.
3. **Wrapped PL/SQL is reported as unavailable, not as a body.** `ALL_SOURCE` returns
   Oracle's `wrap` output verbatim for a wrapped package or procedure. That blob is
   present but obfuscated, so handing it to procedure-body parsing (N3) would produce
   a confident wrong answer. The routine gets `body_sql=None`, a reason naming
   wrapping, and `attributes["wrapped"] = True`.
4. **Snowflake nulls a secure view's definition.** `VIEW_DEFINITION` is NULL for a
   secure view unless the session's role owns it, and `GET_DDL` refuses the same
   object for the same reason. The view records "is a secure view; Snowflake
   withholds VIEW_DEFINITION from a session whose role does not own it". The same
   holds for secure functions and procedures, whose `*_DEFINITION` is likewise NULL.
5. **BigQuery remote functions have no body in BigQuery.** `ROUTINE_DEFINITION` is
   NULL for a remote function, whose body lives in a Cloud Function. That is recorded
   as an `unavailable_reason`, not as an empty body.
6. **Every supplementary query can be refused without failing discovery.** All the
   v1.1 queries read dictionary views a least-privilege reader may not hold —
   `ALL_SOURCE`, `ALL_TAB_PRIVS`, `ALL_MVIEWS`, `INFORMATION_SCHEMA.ROUTINES`,
   `SHOW GRANTS`. A refusal is caught, converted to a reason string, and lands in
   **`DiscoveredCatalog.attributes["envelope_v11_unavailable"]`** as `axis → reason`.
   Where the axis is per-object — views — the reason is *also* pushed onto each
   affected object's `unavailable_reason`, so a consumer that never reads catalog
   attributes still cannot mistake a denial for an absence. Core discovery
   (catalogs, schemas, tables, columns, constraints) is unaffected by any of these
   refusals.
7. **A definition or body over 1,000,000 characters is stored as a flagged prefix.**
   `_MAX_DEFINITION_CHARACTERS` in each connector. `truncated=True`, and the reason
   field stays `None` because we *did* get text.
8. **Snowflake grant collection costs one statement per schema.** Not per table.
   A source with many schemas pays for them; a source with many tables does not.
9. **Oracle materialized views arrive typed `BASE_TABLE`.** Oracle registers a
   materialized view in `ALL_OBJECTS` as both a `MATERIALIZED VIEW` and a `TABLE`,
   and discovery reads the `TABLE` row. The object therefore has
   `object_type == "BASE_TABLE"` **and** a `view_definition` with
   `is_materialized=True`. Consumers should key on `view_definition`, not on
   `object_type`, to decide whether something has a definition to parse.

---

## 5. Tests

`tests/test_connectors_oracle.py` (42), `tests/test_connectors_snowflake.py` (27),
`tests/test_connectors_bigquery.py` (49) — 118 tests, all offline against fakes and
recorded row shapes in the style each file already used.

Per connector, the suite covers a view-definition round trip, a routine with
parameters, comments on table and column, grants where supported, **and** the
unavailable and truncated paths: NULL text, empty-text-with-declared-length, short
fetch, over-cap prefix, missing dictionary row, refused query, wrapped source, secure
view, secure procedure, remote function, refused grants. Two further tests pin the
"no envelope collected" path, so a connector that has not adopted v1.1 still produces
exactly the v1.0 graph rather than a graph of empty envelope objects.

---

## 6. What this item does not cover

* **PostgreSQL and SQL Server.** Owned by another workstream, which took them to
  v1.1 in the same wave; at the time of writing both advertise all four flags `True`,
  backed by `pg_get_viewdef` / `pg_proc` / `obj_description` /
  `information_schema.role_table_grants` and by `sys.sql_modules` / `sys.parameters` /
  `sys.extended_properties` / `sys.database_permissions` respectively. Read those
  connectors, not this document, for their exact scope and limits.
* **The duplicated assembly helpers.** That workstream landed shared v1.1 helpers in
  `aida.connectors.discovery` (`apply_view_definitions`, `apply_table_descriptions`,
  `apply_column_descriptions`, `build_routines`, `build_grants`) after these three
  connectors had already been written against a local rebuild in each file. The two
  approaches agree on the contract and both are tested, but the three connectors here
  do not yet use the shared helpers. Folding them onto it is a straightforward
  follow-up and is the right end state; it was not done in this pass because the
  shared module was still being edited under this workstream.
* **Persistence.** The connectors produce the envelope; storing the new axes in the
  catalogue is the ingestion side of N1 and is tracked separately.
* **Live verification.** Every connector in this wave remains unverified against a
  real instance (see `60-delivery/07-connector-implementation-backlog.md`). The SQL
  here is written against documented dictionary-view and `INFORMATION_SCHEMA` shapes
  and exercised against recorded row shapes; it has not been run against an Oracle
  database, a Snowflake account or a GCP project. Every supplementary query degrades
  to a recorded reason rather than to a failure, which bounds the blast radius of a
  shape that turns out to differ, but it does not substitute for a live run.
* **INV-9's enforcement clause.** Capability flags here are still hand-declared and
  agreed with the registry, not derived from a certification result. That gap is
  recorded, unchanged, by the strict xfail in
  `tests/test_inv9_capability_honesty.py`.

---

## Related documents

* Gap plan: `02-gap-diff-and-plan.md` row N1 (and N2, N3, N11, N12, which consume this)
* Connector backlog: `Docs/60-delivery/07-connector-implementation-backlog.md` §7
* Contract: `src/aida/connectors/base.py`
* INV-9: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md`
