# Status Matrix Verification — DONE / Implemented Claims vs. Actual Tests

> Audit date: 2026-08-28. Scope: every row in `Docs/60-delivery/04-status-matrix.md` marked
> **Implemented** (fully or "for X slice"), plus every row in `Docs/60-delivery/03-tracker.md`
> marked **DONE**. 25 claims total.
>
> Method: bundled the repo (excluding `.git`/`.venv`/caches), built a clean `python3.13` venv,
> ran `pip install -e ".[dev]"`, then `pytest`, `ruff check .`, and `mypy src` from scratch —
> not the developer's existing environment. Every "Verified today" cell below reflects that run,
> not the docs' own claims about earlier runs.

## Ground truth from this run

| Check | Result |
|---|---|
| `pytest` | **226 passed, 0 failed, 0 skipped** in 4.56s |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |
| Doc claim (status matrix retest register) | "121 tests passing" |

**First finding: the doc is stale, not wrong-in-spirit.** There are 226 tests today, not 121 — the
number was accurate at some earlier commit and was never updated as tests were added. Harmless on
its own, but it means the retest register isn't being kept current, which matters for anyone
trusting it as a live dashboard.

## The pattern that applies to nearly every row below

`tests/test_mcp_server.py` says this about itself, and it is true of the whole suite:

> "This module has no database-integration test harness in this repository (there is no
> conftest.py and no sqlite/test-database fixture anywhere in tests/ — every existing test either
> exercises pure functions directly ... or mocks its collaborators.)"

Confirmed independently: **zero** files in `tests/` reference `AsyncSession`, a real Postgres
connection, Temporal, Kafka, or Neo4j. Every one of the 226 tests is one of three kinds:

1. **Behavioral / logic** — calls a real deterministic function with inputs and asserts on outputs
   (SQL validation, masking, classification, quality scoring, prompt-risk detection). This is
   genuine, reliable coverage of the algorithm it tests.
2. **Schema validation** — constructs a Pydantic request model with bad input and asserts it
   raises. Genuine, but only proves the *shape* of a request is validated, not that the endpoint
   behind it does the right thing with valid input.
3. **Route-existence ("contract") tests** — call `app.openapi()["paths"]` and assert a URL
   pattern is present. This proves the route is *registered*. It does not call the route, does
   not touch a database, and would pass even if the handler behind that route were a stub that
   always raised `NotImplementedError`.

So "DONE" / "Implemented" in this codebase currently means, almost without exception: **the route
exists and the request/response schemas are validated** — not **"we ran this against real state
and confirmed the persisted behavior is correct."** That's not a defect in the tests that exist
(they're well-written for what they cover) — it's a gap between what the status matrix's prose
implies ("Implemented") and what "Implemented" is actually proven to mean here.

Legend used in the table:

- ✅ **Behavioral** — real logic exercised, real assertions on results
- 📄 **Schema-only** — Pydantic validation tested, not the handler behind it
- 🔗 **Route-only** — only proves the URL is registered in the OpenAPI spec
- ❌ **No test found** — grepped for every plausible keyword; nothing matched

---

## A. Status-matrix rows marked "Implemented"

| # | Claim (status matrix) | What the doc says is proven | Test(s) found | Verified today | Proof gap |
|---|---|---|---|---|---|
| 1 | **Org/LOB/project tenancy** | Stable hierarchy, organization enforcement, paginated tenant inventory, guided onboarding, audit/outbox evidence | `test_oidc.py::test_oidc_rejects_invalid_organization_claim` 📄, `test_outbox_controls.py::test_tenant_fleet_inventory_avoids_hierarchy_fanout` ✅ (partial) | Pass | No test creates an org/LOB/project hierarchy and asserts enforcement across it. "Paginated tenant inventory" and "guided onboarding" — zero matches for `onboarding` or `paginat*` anywhere in `tests/`. |
| 2 | **PostgreSQL connector** ("for the current contract") | Discovery, read-only execution, EXPLAIN cost, timeouts, bounded profiling, conformance evidence | `test_connectors.py` (3 tests, registry-only) ✅/🔗, `test_ingestion.py::test_connector_certification_is_deterministic` ✅ | Pass | Registry/certification-scoring logic is real. No test opens a Postgres connection, runs discovery, or executes a query — same DB-free pattern as everything else. |
| 3 | **Connector certification** ("control-plane conformance") | Immutable runs scoring implementation/secrets/connection/hierarchy/inventory | `test_ingestion.py::test_connector_certification_is_deterministic` ✅ | Pass | Solid — this one genuinely builds a `DataSource` fixture and checks scoring logic. Best-covered row in this table. |
| 4 | **Catalog inventory** | Stable identity, fingerprints, drift counts, **tombstones, reactivation** | None specific to the module | Pass (suite) | `tombstone` and `reactivat` appear **nowhere** in `src/` or `tests/` — not under those names or any synonym I could find (`is_active`/`deprecat*` exist elsewhere but nothing tests a table disappearing from a scan and being tombstoned, or reappearing and being reactivated). |
| 5 | **Safe profiling** | Table estimates, value-free null/distinct/length stats, sampling, hard bounds | `test_connectors_bigquery.py` profile-expression tests ✅, `test_connectors_oracle.py::test_profile_expressions_disable_distinct_and_lengths_for_unsupported_types` ✅ | Pass | Per-connector expression-building logic is genuinely tested. No test of the cross-connector orchestration (sampling, hard bounds enforcement) described in the claim. |
| 6 | **Classification** | Deterministic rules with evidence and algorithm version | `test_classification.py::test_deterministic_sensitive_name_classification` (1 test) ✅ | Pass | Real, but a single test for the whole capability. No test of "algorithm version" evidence being recorded. |
| 7 | **Durable analysis workflow** | Temporal discovery + retryable table-task DAG, **heartbeats, cancellation, resume** | None | Pass (suite) | The only mention of "temporal" in the whole test suite is `temporal_enabled=False` in an unrelated fixture. Heartbeats, cancellation, and resume — the actual durability claims — have **zero** automated coverage. |
| 8 | **Fleet scheduling** | HA-safe polling, **priority**, maintenance windows, org **quotas**, per-source admission, **backpressure** | `test_fleet_scheduling.py` (5 tests) — maintenance-window math ✅, datasource-update validation ✅ | Pass | `backpressure` and `quota` — zero matches anywhere in `tests/`. `priority` appears once, as a fixture field value (`priority=50`), never asserted on. Of six sub-claims, only "maintenance windows" is actually tested. |
| 9 | **Schema drift / reconciliation** | Created/changed/deprecated evidence, soft deprecation, reactivation, drift-lag summary | None specific | Pass (suite) | Same gap as row 4 — no test for drift detection or reconciliation behavior. |
| 10 | **Declared PK/FK graph** | Constraint inventory **and Neo4j FK relationships** | `test_connectors_sqlserver.py`/`test_connectors_oracle.py` FK-assembly tests ✅ (source side only) | Pass | `neo4j` appears **nowhere** in `tests/`. The FK-assembly tests cover extracting constraints from a source schema, not projecting them into the graph — the half of this claim that's actually named in the row. |
| 11 | **Impact analysis** | Table → semantic metrics → governed tools → relationship evidence | None | Pass (suite) | The endpoint (`GET /metadata/tables/{table_id}/impact` in `intelligence_api.py`) exists in source. Zero references to `impact` anywhere in `tests/` — not the route, not the underlying function, nothing. |
| 12 | **Business-semantic inference** ("for the metadata-structure slice") | Deterministic + optional LLM domain/entity/grain/synonym inference, strict schema validation, maker-checker | `test_semantic_inference.py` (5 tests) ✅✅📄 | Pass | This is one of the better-covered rows — two tests genuinely run the rule-based inference engine and check its output, plus a strict-schema-rejection test. Maker-checker *approval workflow* itself isn't exercised (only the inference output). |
| 13 | **Glossary and stewardship** ("governed table-stewardship slice") | Categories, term lifecycle, ownership, conflict resolution, coverage scoring, certification — see GL-1..GL-8 below | See section B | Pass | See below — this row is almost entirely route-existence and schema-validation, not behavior. |
| 14 | **Query execution gateway** | SQLGlot AST validation, allowlists, EXPLAIN/cost, timeout, masking, redaction, **HMAC evidence** | `test_sql_guard.py` (8 tests) ✅, `test_query_masking.py` (5 tests) ✅ | Pass | The AST-validation and masking core — the platform's flagship differentiator — is genuinely, thoroughly tested. HMAC: the only related test (`test_config.py::test_production_requires_strong_audit_hmac_key`) checks that a short key is *rejected at startup*, not that HMAC evidence is correctly computed or verified on an actual audit record. |
| 15 | **Governed tools** | Versioning, parameter schemas, AST literal binding, RBAC, maker-checker publish/deprecate, audited execution, **retrieval ranking**, tool-first binding | `test_tool_rendering.py` (5 tests) ✅, `test_agent_intelligence.py::test_planner_prefers_published_role_bound_tool_over_candidate_sql` ✅ (partial) | Pass | AST literal-binding safety is genuinely well tested. No test for the versioning lifecycle, the maker-checker publish/deprecate *workflow* (only a planner given an already-published tool), or retrieval ranking. |
| 16 | **Data-quality observability** ("for profile-baseline controls") | Deterministic volume/null-rate/schema-fingerprint comparison, incident lifecycle | `test_data_quality.py` (6 tests) ✅✅ | Pass | Well covered — this is genuine, deterministic scoring logic with real assertions, including edge cases (zero baseline, missing estimate). One of the stronger rows. |
| 17 | **Audit and event delivery** | Attributable ledger, transactional outbox, idempotent publication, retry/backoff, **dead-letter**, **authorized requeue** | `test_outbox_controls.py` (4 tests) ✅ (backoff only) | Pass | Retry/backoff math is genuinely tested. `dead_letter`/`dead-letter` and `requeue` — zero matches in `tests/`. Two of the claim's five sub-behaviors are untested. |
| 18 | **Agentic product portal** ("for current API scope") | Role-oriented product, 21 workflow areas, ARIA roles, live-region announcements, contrast fix | `test_ui_accessibility.py` (3 tests) — see note | Pass | All three tests parse `index.html`/`app.js`/`styles.css` as **static text** (`HTMLParser`, string `in` checks) — e.g. `assert "prefers-reduced-motion: reduce" in css`. Nothing renders the page, runs in a browser, or checks computed behavior. This matches the doc's own admission ("no browser was available... certification remains") — flagged here because the status-matrix row says "Implemented," which reads as stronger than "three string-presence checks on 300KB of hand-written HTML/CSS/JS." |

## B. Tracker rows marked "DONE" (glossary module + MCP)

| # | ID | Claim | Test(s) found | Verified today | Proof gap |
|---|---|---|---|---|---|
| 19 | GL-1 | Term lifecycle with versions (categories, immutable synonyms, maker-checker publish/supersede/deprecate) | `test_glossary_contracts.py::test_glossary_and_documentation_api_contracts_are_exposed` 🔗, `test_glossary_stewardship.py::test_glossary_term_deprecation_requires_a_real_reason` 📄 | Pass | The "DONE" evidence is `assert "/v1/glossary-terms/{term_id}/deprecate" in paths` — route registration, not a term actually being created, versioned, and deprecated. |
| 20 | GL-2 | Ownership assignment (individual/group/bulk/rule-based) | `test_glossary_stewardship.py::test_ownership_rule_supports_domain_schema_tag_and_pattern_matching` 📄, `test_bulk_ownership_assignment_requires_owner_fields` 📄 | Pass | Schema-validation only — no test assigns an owner and checks it's persisted or resolvable. |
| 21 | GL-3 | Conflict detection and resolution, losing position retained | `test_glossary_stewardship.py::test_glossary_conflict_resolution_requires_rationale` 📄 | Pass | Schema-validation only. No test creates a conflicting claim and checks detection or that the losing position is actually retained. |
| 22 | GL-4 | **Coverage scoring** — six dimensions, org/source/domain/LOB, durable snapshots | `test_glossary_stewardship.py` line 34-36: `assert ".../stewardship/coverage" in paths` 🔗 | Pass | This is the clearest example in the whole audit: the *only* test for "coverage scoring" is a route-existence assertion. No test computes a coverage percentage from a fixture and checks the number. |
| 23 | GL-5 | Bulk certification with expiry | `test_glossary_stewardship.py::test_certify_operation_requires_an_expiry` 📄 | Pass | Schema-validation only (a certify request without an expiry is rejected). No test of expiry actually causing a certification to stop counting, as the status-matrix claim states. |
| 24 | GL-8 | Term linkage inference (approved-annotation evidence → bounded proposals) | `test_glossary_stewardship.py::test_glossary_link_proposal_generation_bounds_are_enforced` 📄 | Pass | Schema-validation of the *request* bounds, not a test that inference actually runs and produces correct proposals. |
| 25 | CX-5 | Eligible-tool exposure + governed invocation (role-filtered `tools/list`, `tools/call` denies with anti-enumeration, audit evidence) | `test_mcp_server.py::test_tool_role_eligible_*` (7 tests) ✅ | Pass | The underlying boolean function (`_tool_role_eligible`) is genuinely and thoroughly tested. But no test calls `tools/call` end-to-end and checks it returns the *same* not-found shape for a denied tool as for a nonexistent one — the specific "no existence leak" claim — because that requires the JSON-RPC handler path, which isn't exercised. |

---

## What this adds up to

Of the 25 claims: **3** have genuinely strong behavioral test coverage matching their stated
scope (Connector certification, Data-quality observability, SQL guard/masking within the Query
execution gateway). **5–6** have real logic tests for part of the claim but leave named
sub-behaviors (HMAC evidence, retrieval ranking, tool versioning, profiling orchestration)
untouched. The remaining majority — most of the glossary module, catalog inventory, fleet
scheduling, tenancy, impact analysis, the durable workflow layer, and the Neo4j graph
projection — are proven at the level of "the route exists and rejects malformed input," not at
the level of "the described behavior happens and produces the right result."

None of this means the underlying code doesn't work — the accomplishment log describes specific,
plausible manual verification sessions (real run IDs, real bug fixes found live) for several of
these, especially the connector and model-gateway work. What it means is that **none of that
manual verification is captured as a regression test**, so there is currently nothing in CI that
would catch it breaking again. That gap is exactly the tension flagged earlier: a roadmap that
prioritizes Phase 0 structural work is still shipping Phase A features into a repo whose test
suite provides only schema/route coverage for most of that new surface — which is the more
concrete version of "are we implementing the right way."

## Suggested next step, if useful

Pick the 3–5 highest-stakes claims (glossary coverage scoring, catalog tombstoning, the
durable-workflow heartbeat/resume path) and write one real behavioral test each — even a
single in-memory or SQLite-backed test per claim would convert "route exists" into "we watched it
work," and would surface whether the underlying implementation actually matches its own
description.
