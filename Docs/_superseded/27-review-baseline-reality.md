# Baseline Reality — what Atlas/AIDA actually is today

Status: Independent audit, August 2026. Evidence-based; every claim below was checked
against code on disk, not against documentation.

Read this before anything else in `review-2026-08/`. It changes how the rest reads.

---

## 1. Shape of the thing

| Measure | Value |
|---|---|
| Python source | 34,669 LOC |
| ...in `src/aida/` (flat package) | ~34,600 LOC across ~78 modules |
| ...in `src/atlas/modules/` (the documented structure) | **69 LOC, one module, self-labelled "scaffold only"** |
| Tests | 338 test functions, 44 files, 8,961 LOC |
| Migrations | 34 versions, 5,505 LOC |
| `TODO` / `FIXME` in `src/` | **0** |
| `NotImplementedError` in `src/` | 6, all in the `Connector` abstract base class |
| CI pipeline | **Does not exist** (no `.github/workflows`) |
| Import-linter contracts | **2**, both narrow; no layering contract over the flat package |
| UI | Vanilla JS, no framework, `app.js` 1,500 lines + 4 feature modules |

The absence of `TODO` markers alongside 338 tests and 34 incremental migrations is the
signature of a codebase that was built deliberately rather than scaffolded and
abandoned. That matters for the recommendation in `target/00-design-brief.md` §7.

---

## 2. The headline structural finding

**The architecture documents describe a 21-module decomposition that has not been
built.** `src/atlas/modules/` contains exactly one directory, `identity_tenancy`,
whose `service.py` reads in full:

```python
"""identity tenancy -- domain logic. The only place this module's business rules live.
Status: scaffold only (tracker ST-01).
"""
from __future__ import annotations
```

Every architecture, contract and module document phrased in terms of
`src/atlas/modules/<module>/api.py` is describing an intended structure. The
tracker is honest about this (`ST-05/06/07` are TODO); the architecture documents
read as though it is done.

This is not a code defect. It is a documentation-truth defect, and it is the reason
the doc set cannot currently be used to onboard an engineer.

---

## 3. What is genuinely built and genuinely good

These were verified by reading the implementing code, not the specs.

**Query Execution Gateway** — `src/aida/query_gateway.py` (396 lines) plus
`sql_guard.py`. Real SQLGlot AST parse; single-statement and read-only enforcement
against an explicit forbidden-node set (`Alter/Create/Delete/Drop/Insert/Merge/Update`);
forbidden-function list (`dblink`, `pg_sleep`, `pg_read_file`); `SELECT *` blocked
except inside `COUNT`; cross-join and unbounded-join rejection; row-limit clamping;
table and column references extracted from the parsed tree rather than by string
matching; catalog resolution and per-object authorisation via `allowed_tables()`;
cost/byte-budget gate via `estimate_read_query()`; masking that walks `exp.Select`
projections and follows column→alias chains.

**The choke point holds.** `connector.execute_read_query` has exactly one call site,
in the gateway. Other `connector_registry.create(...)` call sites only reach
`test_connection()`, `discover()` and `profile_table()`. The 70+ `session.execute(...)`
hits elsewhere are SQLAlchemy against the platform's own control-plane database, not
governed datasources. INV-2 is true in fact today.

**AST literal binding in tools** — `src/aida/tool_rendering.py` genuinely mutates a
parsed tree:

```python
statement = parse_one(sql_template, read=dialect)
rendered = statement.transform(lambda node: exp.convert(normalized[node.name]) ...)
```

Placeholders located by walking `exp.Placeholder`, values type-checked and coerced per
the parameter definition, then substituted through `exp.convert`. This is not string
interpolation dressed up. The "injection is impossible by construction" claim is real.

**Prompt-risk screening** — `src/aida/prompt_risk.py`, 7 weighted regex signals,
deterministic, thresholded, and confirmed to run at line 236 of the orchestrator
before retrieval transitions at line 316. Ordering claim verified.

**Maker–checker** — `semantic_api.py:571` blocks `review.requested_by ==
context.principal_id` with a 409, and `tests/test_governed_tool_lifecycle.py` has a
test asserting exactly that.

**MCP server** — `src/aida/mcp_server.py`, 1,776 lines. A real JSON-RPC 2.0
implementation: `initialize`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`, `prompts/get`; native lineage tools; context-product resources;
Redis-backed atomic budgets; and genuinely different policy paths for
`resources/read` versus `tools/call`. Governed SQL routes back through the
orchestrator and gateway rather than around them.

**Connectors** — five real implementations with live driver code, discovery,
estimation, execution and profiling: PostgreSQL (259 LOC, `asyncpg`, `pg_class`/
`pg_constraint` introspection), SQL Server (417), Oracle (430), Snowflake (517),
BigQuery (395, dry-run byte estimation).

**Model boundary** — `semantic_inference.py` tags every field sent to a model
`"value_scope": "METADATA_ONLY"`; docstrings state "Source rows never cross this
boundary." Verified by reading the payload builder.

---

## 4. Where documents and code disagree

| # | Doc says | Code says |
|---|---|---|
| 1 | 21 modules under `src/atlas/modules/` | 1 module, 69-line scaffold |
| 2 | Tenancy is `organization → legal_entity → line_of_business → data_domain → project → datasource` | `LegalEntity` **does not exist anywhere in `src/`**. Real hierarchy is `organization → line_of_business → data_domain → project → datasource`. ADR-0017's own line "legal_entity and data_domain exist in the ADR, not in the schema" is itself half-stale — `data_domain` has a model *and* a migration |
| 3 | INV-2 is mechanically enforced by an import-linter `gateway-exclusivity` contract (invariants doc + ADR-0004, present tense) | No such contract in `pyproject.toml`. Tracker `QG-7` says TODO. The most-marketed invariant's enforcement mechanism is not wired |
| 4 | Various checks "fail CI" (coding standards, contract strategy) | There is no CI |
| 5 | Hybrid retrieval with vector and graph expansion | Lexical/BM25 only. `retrieval.py`'s own comment: "can be replaced with pgvector in Phase 2 when the embedding column is added." No pgvector, no embedding column, anywhere |
| 6 | Seven lineage edge kinds (QUERY/VIEW/PROCEDURE/ETL/DBT/BI/AI_DECISION) | `ETL` is the only kind populated as a default, from OpenLineage ingestion. Column-level lineage from executed SQL is real (gateway `extract_column_lineage()`, DIRECT vs DERIVED). **No view-DDL parser, no stored-procedure parser, no BI extraction, no AI_DECISION edges** |
| 7 | 11-state agent runtime, each state a gated checkpoint | All 11 states exist with a real transition table, and the first six are individually gated. The last five (`VALIDATED, COSTED, EXECUTED, EXPLAINED, COMPLETED`) are applied in a single `for` loop *after* `query_gateway.execute()` has already returned (`agent_orchestrator.py:531-538`). The work is real — it happens inside the gateway — but at the orchestrator level these are retroactive trace entries, not five checkpoints |
| 8 | Databricks among supported connectors | No `databricks.py`. `registry.py:179` is `declare_planned(...)`, same as Teradata and Db2 |
| 9 | Neo4j projects approved relationship candidates | FK-based projection is real. Candidate approval/rejection events are referenced (`graph_projector.py:453-454`) but the inference algorithm was not located in the projector; candidates appear to arrive as pre-approved events |
| 10 | Reviewer bulk decisions (persona doc R3; `bulk_decide()` in module 17's interface) | Module 17 §11 and tracker `PG-3` both say not implemented |

To be fair in the other direction: **module 19's own documentation is appropriately
hedged** (`MCP-2 | Partial — agents can create a governed data-product access request
but cannot grant it`). The concentration of "Implemented 2026-08-29" markers there
looked like overclaiming from the documents alone; the code does not support that
suspicion. The MCP work is real.

---

## 5. What has no foundation at all

Five capabilities the product requirements depend on, all confirmed absent by
direct search of `src/` and `ui/`:

| Capability | Evidence |
|---|---|
| **Wiki / knowledge pages** | Zero matches for `wiki` in `src/` or `ui/` |
| **Document upload and mapping** | Zero matches for `document_upload`, `upload_document`, `file_upload`, `DocumentUpload` |
| **Workspace as a grantable container** | "workspace" appears only as CSS class names (`asset-workspace`, `graph-workspace`) and one line of marketing copy in `index.html`. No `Workspace` model or table |
| **Cross-source federated query** | "federated" appears twice, both describing the *lineage graph* as "a federated bounded view." The gateway executes against exactly one datasource per call. No join-across-sources execution path exists |
| **View / procedure → tool generation** | Zero matches for any `view_to_tool`, `procedure_to_tool`, `generate_tool_from_*` pattern |

Three of these five are load-bearing for the stated product. None is a
partially-built feature that needs finishing; all are greenfield.

---

## 6. Operational evidence

The status matrix's own summary is accurate and worth quoting: *"The control plane
is strong; the operational evidence is absent."*

- Every drill — projection rebuild, PITR restore, Temporal failover, credential
  rotation, kill switch, regional failover, break-glass — has **never been run**.
  All are overdue against their own stated cadence.
- Performance is **not measured**. The vision document publishes p95 targets that
  have never been tested.
- No penetration test, no SOC 2, no ISO 27001.
- Tracker: **171 items, 71 P0, none assigned.**

---

## 7. Honest summary in one paragraph

Atlas is a well-built engine sitting inside a chassis that exists only on paper,
described by documents that are accurate about intent and inaccurate about state.
The parts that are hard to build — a validating execution gateway, five driver-level
connectors, AST-safe parameter binding, a real MCP server — are done and done well.
The parts that are easy to claim and tedious to prove — CI, enforced module
boundaries, drills, benchmarks, certification — are not started. And the parts the
next version of the product depends on — knowledge compilation, document ingestion,
transformation parsing, federation, workspaces — do not exist yet.

That combination argues for restructure-and-extend, not rebuild. See
`target/00-design-brief.md` §7.
