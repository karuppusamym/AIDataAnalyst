# ADR-0026 — Per-Edge-Type Review State for Parsed Lineage Edges

**Status:** Proposed | **Date:** 2026-09-04 | **Owner:** Architecture + Lineage

## Context

Atlas already gates one lineage-review flow through a real approval loop: `RelationshipCandidate` moves PENDING → APPROVED | REJECTED via `intelligence_api.decide_relationship_candidate`, with maker≠checker, an outbox event, and the Neo4j projection reading only APPROVED rows. That loop is the reference shape for reviewed graph facts on this platform.

Five other parser-produced lineage edge tables have no such gate today: `ViewLineageEdge`, `ProcedureLineageEdge`, `DbtLineageEdge`, `OpenLineageTableEdge`, `OpenLineageColumnEdge`. Every one of them writes straight to storage the moment its parser succeeds — a bad parse, a mislabeled dbt column, an OpenLineage run event ingested from an untrusted producer, and an active lineage fact appears in the shared graph immediately. Until the next re-parse (which may never come) that wrong fact answers "where did this column come from" for every consumer that reads the unified lineage graph.

P1-05 exists to close that gap. The question this ADR answers: **how do we add a review state to five separate edge tables without collapsing them into a supertype we'd then have to teach every read site about?**

## Decision

**Add the same six review-lifecycle columns to each of the five edge tables. Do NOT introduce a `LineageEdge` supertype.** The columns are:

* `review_status STRING(20) NOT NULL DEFAULT 'ACTIVE'` — `PROPOSED | ACTIVE | REJECTED | SUPERSEDED`.
* `reviewed_by`, `reviewed_at`, `review_reason`, `previous_edge_id (self-FK)`, `created_by` (nullable).
* `Index(review_status)` per table.

The default `ACTIVE` and the default config value `AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE=auto_active` together mean every existing deployment sees zero behavior change until an operator opts into `require_review`. Under `require_review`, a newly-parsed edge lands PROPOSED unless its confidence is at or above `AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD` (default 0.9, mirroring ADR-0025's auto-approve spirit) or the source datasource has `trusted_for_lineage=True`.

A new `parsed_lineage_review_api.py` router owns the decision endpoint (`POST /v1/lineage/parsed-edges/{edge_id}/decision`), the bulk endpoint (`POST /v1/lineage/parsed-edges/bulk-decide`), and the queue endpoint. It mirrors `decide_relationship_candidate` for maker≠checker, audit, and outbox emission, and it dispatches on `edge_type` to the right one of the five tables.

Every existing read path — `unified_lineage_api._build_unified_graph`, the domain-federated variant, and the Neo4j projection (via `build_unified_lineage_graph_payload`) — filters `review_status = "ACTIVE"` by default and accepts an `include_pending_edges` opt-in query parameter.

## Consequences

### Positive

- **Zero-cost on existing reads.** The 5 edge queries that already exist grow one `.where(model.review_status == "ACTIVE")` clause; the fetched rows still deserialize into the same ORM types, callers don't change, no polymorphic `isinstance` fan-out. A supertype would have forced a JOIN or a UNION in every read site to pick up per-type columns (`edge_kind`, `sql_hash`, `transformation_type`, `input_dataset_namespace`, `artifact_import_id` — no two of the five share the same natural key).
- **Additive, backward-compatible.** New columns are nullable or defaulted; no ORM row shape shifts under an existing caller. The default config keeps every deployment on the pre-P1-05 write behavior.
- **Each parser stays responsible for its own delete-and-insert semantics.** `view_lineage_api._persist_edges`'s scoped delete-then-insert is unique to that parser; forcing it into a shared supertype would have leaked the sentinel-target quirk (`PROCEDURE_RESULT_TARGET`) into every other table's insert path.
- **Reviewer trail lives with the edge.** `reviewed_by`, `reviewed_at`, `review_reason` are on the same row as the edge — no join required to answer "who approved this and why."
- **Aligns with the RelationshipCandidate review contract.** Same maker-checker guard, same outbox event naming (`lineage.parsed_edge.approved.v1` / `rejected.v1`), same 409-on-self-review, same partial-success bulk semantics.

### Negative — costs accepted

- **Six-column repetition across five tables.** A future edge table added to this set has to remember all six. Mitigated by keeping the migration in one file and colocating the column set in a documented list (`_EDGE_TABLES` in the migration; `EDGE_TYPE_TO_MODEL` in `parsed_lineage_review_service.py`). Not eliminated — a mistake would silently omit review from a new table.
- **Read-time filter is caller-remembered.** Adding a NEW read path in `unified_lineage_api.py` requires the author to add the `review_status == "ACTIVE"` filter by hand. A supertype with a base query would have carried the filter automatically. Mitigated by grep-level review gates and the tests below; a follow-up could add a lint that flags a `select(<edge_model>)` without a review filter.
- **The queue is a composed view, not a materialized one.** `list_parsed_lineage_review_queue` reads all five tables per call. At the current PROPOSED volumes this is fine; if a bad parser floods the queue, this becomes an obvious perf hotspot before it becomes a correctness problem, so it can be watched and materialized later.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Single `LineageEdge` supertype with per-type discriminator | Dozens of read sites already fetch each concrete model for its per-type columns. Consolidating would need a JOIN back to the concrete table on every read — the columns are irreducibly different (`edge_kind`, `sql_hash`, `input_dataset_namespace`, `artifact_import_id` — no two share). The supertype buys "one write path" that we don't need because each of the five write paths already lives in its own parser, not a shared function. |
| A separate `LineageEdgeReview` table with an edge_type + edge_id FK-like tuple | Fan-out cost same as the supertype on reads (join per query), plus a soft FK that no database enforces, plus two-writer atomicity problems (edge insert + review insert must be transactional). Nothing simpler than the per-column-on-the-edge shape. |
| Reuse `GovernanceReview` for parsed lineage edges | `GovernanceReview` is scoped to the semantic/annotation/description/glossary domain (see ADR-0025) and its schema, its RBAC, its supersede semantics, and its queue read model were shaped around those artifact types. Shoehorning lineage edges into it would either dilute those semantics or require enough new columns and case branches on `object_type` that we'd effectively have built ADR-0026 inside `GovernanceReview` — with the extra cost of a table that now serves two very different workflows. |
| Off by default, per-tenant flag only | The config knob IS off by default (`auto_active`), and per-tenant opt-in is a natural follow-up ADR building on the same columns; not doing it now is a scoping choice, not a design choice. |

## Related

- `ADR-0025-auto-approve-escape-hatch.md` — the high-confidence auto-active threshold in `require_review` mode mirrors ADR-0025's spirit for lineage: a reviewer would rubber-stamp a FULL-confidence parse anyway.
- `intelligence_api.decide_relationship_candidate` — the reference shape this ADR follows exactly for maker-checker, audit, and outbox.
- `60-delivery/14-session-2026-09-04-lineage-review.md` — the tracker rows LR-1/LR-2/LR-3 landing this change.
