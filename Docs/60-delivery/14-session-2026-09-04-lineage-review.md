# Session Addendum -- 2026-09-04 -- Parsed lineage-edge review (P1-05)

> **Purpose.** New tracker rows and evidence for the 2026-09-04 P1-05 fix
> covering the five non-governed parser-produced lineage edge tables that
> were writing straight to ACTIVE with no review. Staged here rather than
> merged into `03-tracker.md` directly for the same reason
> `10-session-2026-09-04-auto-enqueue.md`,
> `11-session-2026-09-04-governance-unify.md`,
> `12-session-2026-09-04-reaper.md`, and
> `13-session-2026-09-04-certification.md` were: `03-tracker.md` has
> extensive uncommitted concurrent edits and a landing here would
> conflict. Fold these rows into `03-tracker.md` on the next tracker
> rebase; the file citations, event names, and test names below are what
> belongs in each row's evidence column.

## ADR

- `10-architecture/adr/ADR-0026-per-edge-type-lineage-review.md` -- **Proposed**. Per-edge-type review columns on the five non-governed lineage edge tables, NOT a `LineageEdge` supertype. Records why (five read sites, irreducibly different natural keys, no polymorphism dividend) and how the default `auto_active` config keeps every existing deployment on the pre-P1-05 write contract.

## Rows to add / update

### LR-1 -- Review lifecycle columns + migration + config (P1-05)

Section: **F. Lineage / relationships** (peer to RL-4 relationship-candidate review already in tracker).

**Problem.** Only `RelationshipCandidate` had a PENDING → APPROVED / REJECTED review gate. `ViewLineageEdge`, `ProcedureLineageEdge`, `DbtLineageEdge`, `OpenLineageTableEdge`, `OpenLineageColumnEdge` all wrote straight to storage the moment their parser succeeded -- a wrong parse or an untrusted OpenLineage push became an active fact in the shared unified-lineage graph until the next re-parse (which may never come).

**Fix.**

- Migration `0026a6f31c05_p1_05_parsed_lineage_review_state.py` adds `review_status STRING(20) NOT NULL DEFAULT 'ACTIVE'`, `reviewed_by`, `reviewed_at`, `review_reason`, `previous_edge_id` (self-FK), and `created_by` to each of the five edge tables, plus an `ix_<table>_review_status` index per table. Same migration also merges the five pre-existing parallel heads (P2-08, Group I procedure lineage, tool certification corpus, catalog bulk actions, BI lineage) so the P1-05 revision is a single new head. Adds `Datasource.trusted_for_lineage BOOL DEFAULT FALSE`.
- Model changes on the five edge classes and `DataSource` in `src/aida/models.py` and `src/atlas/modules/connectivity/models.py`. `server_default="ACTIVE"` on `review_status` is the backward-compat hinge: every existing row and every row written under the default `auto_active` config still lands ACTIVE.
- Two new `Settings` fields in `src/atlas/platform/config.py`:
  - `AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE: Literal["auto_active", "require_review"] = "auto_active"` -- the config knob is OFF by default; existing deployments see zero behavior change.
  - `AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD: float = 0.9` -- under `require_review`, a parse whose confidence is at or above the threshold still lands ACTIVE, matching ADR-0025's spirit.
- Parser wiring: `view_lineage_api._persist_edges` (used by both view and procedure parses) now:
  1. In `auto_active` mode, keeps the pre-P1-05 delete-then-insert.
  2. In `require_review` mode, ONLY deletes prior PROPOSED rows for the same target table(s), leaves any human-approved ACTIVE row untouched, and skips inserting a duplicate that collides with an existing ACTIVE row on the natural-key uniqueness constraint (idempotency).
- `dbt_api.import_dbt_manifest` and `openlineage_api.ingest_openlineage_run_event` both call `parsed_lineage_review_service.resolve_review_status_for_new_edge(...)` for every edge they insert. Connector-pushed edges from a datasource with `trusted_for_lineage=True` bypass PROPOSED under `require_review`.

**Evidence.**

- `src/aida/models.py` -- review columns on `OpenLineageTableEdge`, `OpenLineageColumnEdge`, `DbtLineageEdge`, `ViewLineageEdge`, `ProcedureLineageEdge`.
- `src/aida/parsed_lineage_review_service.py` -- `resolve_review_status_for_new_edge`, `list_parsed_lineage_review_queue`, `EDGE_TYPE_TO_MODEL`, `EDGE_TYPES`, `REVIEW_STATUSES`.
- `src/aida/view_lineage_api.py:_persist_edges` -- new signature (`context=` kwarg), the delete-only-PROPOSED guard, existing-ACTIVE idempotency skip.
- `src/aida/dbt_api.py:import_dbt_manifest` -- both `DbtLineageEdge(...)` insertions carry `review_status=` and `created_by=`.
- `src/aida/openlineage_api.py:ingest_openlineage_run_event` -- both `OpenLineage*Edge(...)` insertions carry `review_status=` and `created_by=`.
- Migration: `migrations/versions/0026a6f31c05_p1_05_parsed_lineage_review_state.py`.

### LR-2 -- Review endpoint + audit + outbox (P1-05)

Section: **F. Lineage / relationships**.

**Problem.** A steward had no path to say "no, this parsed edge is wrong" without deleting the row directly through database access -- no maker-checker, no audit, no negative-knowledge write.

**Fix.**

- New router `src/aida/parsed_lineage_review_api.py`, registered in `src/aida/main.py`.
- `POST /v1/lineage/parsed-edges/{edge_id}/decision` (body `{edge_type, decision, reason}`) mirrors `decide_relationship_candidate` exactly: maker-checker (creator cannot decide own edge, 409), already-decided rejected (409), audit `LINEAGE_PARSED_EDGE_APPROVED` / `LINEAGE_PARSED_EDGE_REJECTED`, outbox `lineage.parsed_edge.approved.v1` / `lineage.parsed_edge.rejected.v1`. On APPROVE the edge flips ACTIVE and the next unified-lineage read / graph-projector run picks it up; on REJECT it flips REJECTED and stays out of the graph.
- `POST /v1/lineage/parsed-edges/bulk-decide` accepts up to 100 items with per-item SAVEPOINT (`session.begin_nested()`); one failure does not stop the rest -- same partial-success shape as `bulk_decide_relationship_candidates`.
- `GET /v1/lineage/parsed-edges/review-queue` -- one paginated view across the five tables composed by `parsed_lineage_review_service.list_parsed_lineage_review_queue`, filtered by `review_status="PROPOSED"`. Filters: `edge_type`, `min_confidence` (0..1 -- string enum FULL/PARTIAL/LOW coerced), `limit`, `offset`.
- Read filter: every existing `unified_lineage_api.py` edge query now filters `review_status == "ACTIVE"` unless the caller passes `include_pending_edges=True`. The graph projector (`src/aida/projectors/graph_projector.py::load_unified_lineage_projection`) passes `include_pending_edges=False` explicitly, keeping PROPOSED edges out of the shared Neo4j graph.

**Evidence.**

- `src/aida/parsed_lineage_review_api.py` -- `decide_parsed_lineage_edge`, `bulk_decide_parsed_lineage_edges`, `get_parsed_lineage_review_queue`; `_reject_maker_checker`, `_reject_already_decided`.
- `src/aida/main.py` -- `include_router(parsed_lineage_review_router)`.
- `src/aida/unified_lineage_api.py` -- `_build_unified_graph`, `build_unified_lineage_graph_payload`, `build_domain_unified_lineage_graph_payload`, `get_unified_lineage_graph`, `get_domain_unified_lineage_graph` all thread `include_pending_edges`.
- `src/aida/projectors/graph_projector.py::load_unified_lineage_projection` -- explicit `include_pending_edges=False`, with the P1-05/ADR-0026 comment explaining why.
- `src/aida/schemas.py` (end of file) -- `ParsedLineageEdgeReviewQueueItemRead`, `ParsedLineageEdgeReviewQueueRead`, `ParsedLineageEdgeDecisionRequest`, `ParsedLineageEdgeDecisionRead`, `ParsedLineageEdgeBulkDecisionItem`, `ParsedLineageEdgeBulkDecisionRequest`, `ParsedLineageEdgeBulkDecisionItemRead`, `ParsedLineageEdgeBulkDecisionResultRead`.
- Tests: `tests/test_parsed_lineage_review.py` -- see the LR-3 row for the enumeration.

### LR-3 -- UI scaffold + tests (P1-05)

Section: **F. Lineage / relationships**, cross-linked to **UX**.

**UI scaffold.**

- `ui-next/src/lib/api.ts` -- `listParsedLineageReviewQueue`, `decideParsedLineageEdge`, `bulkDecideParsedLineageEdges`.
- `ui-next/src/lib/types.ts` -- `ParsedLineageEdgeType`, `ParsedLineageEdgeDecision`, `ParsedLineageEdgeReviewQueueItemRead`, `ParsedLineageEdgeReviewQueueRead`, `ParsedLineageEdgeDecisionRequest`, `ParsedLineageEdgeDecisionRead`, `ParsedLineageEdgeBulkDecisionItem`, `ParsedLineageEdgeBulkDecisionRequest`, `ParsedLineageEdgeBulkDecisionItemRead`, `ParsedLineageEdgeBulkDecisionResultRead`.
- `ui-next/src/screens/ParsedLineageReviewScreen.tsx` -- first-cut functional table view of PROPOSED edges with per-row Approve / Reject. Filters by `edge_type` and `min_confidence`. Bulk-decide is wired via the API helper but not surfaced in the toolbar (multi-select is a follow-up).
- `ui-next/src/App.tsx` -- new `parsed-lineage-review` NAV item under **Govern**, lazy-loaded screen, `case "parsed-lineage-review"` in the router switch.

**Tests.** `tests/test_parsed_lineage_review.py`.

| Test class | Covers |
|---|---|
| `TestResolveReviewStatusForNewEdge` | Auto-active always ACTIVE; require_review LOW → PROPOSED; require_review FULL (>= 0.9) → ACTIVE; trusted source bypass. |
| `TestAutoActiveMode` | Default config: parse writes ACTIVE, queue is empty. |
| `TestRequireReviewMode` | Low-confidence parse → PROPOSED with created_by populated; FULL-confidence parse → ACTIVE via threshold under require_review. |
| `TestDecideParsedLineageEdge` | APPROVED flips ACTIVE and emits `lineage.parsed_edge.approved.v1`; REJECTED records reason and reviewer; maker cannot review own edge (409). |
| `TestBulkDecide` | 5-item request, one poisoned to REJECTED → partial success (4 succeeded, 1 failed) under per-item SAVEPOINT. |
| `TestReparseIdempotency` | Under require_review, a re-parse does NOT delete an existing ACTIVE edge a human approved; no PROPOSED duplicate is added. |
| `TestUnifiedReadFilter` | `build_unified_lineage_graph_payload` returns 1 VIEW_DEFINITION edge by default (ACTIVE only), 2 with `include_pending_edges=True`. |
| `TestGraphProjectorFiltersPending` | Graph-projector payload with `include_pending_edges=False` returns zero VIEW_DEFINITION edges when only a PROPOSED row exists (unit test on the payload the projector hands Neo4j; no live Neo4j needed). |

## Gaps deliberately left

- **Per-org `require_review` overrides.** The config is global. A future ADR (build on `AutoApprovePolicy`-shaped rows) can add per-org / per-edge-type / per-datasource overrides on the same columns without a re-migration.
- **Full ParsedLineageReviewScreen UX.** Bulk multi-select, source-SQL drilldown, and pagination controls are placeholders; the screen is functional-first-cut.
- **Deep procedure lineage (`DeepProcedureLineageEdge`).** N3's routine-identity-aware table is out of scope here -- it has a different natural key and its own AT-D2/AT-D5 lifecycle. When it reaches production maturity it can adopt the same columns using this migration as the template.
- **`DbtLineageEdge` DEPENDS_ON edges use `confidence="FULL"` for the review decision.** Manifest depends_on has no per-edge confidence; using FULL keeps it high-confidence-auto-active by default. If a deployment wants those held for review too, they can lower the threshold or set the datasource trust flag to False.
- **`transformation_metadata_integration_enabled` skipped edges.** dbt column-lineage rows that a policy suppression skips are already not written; nothing new here to review.
