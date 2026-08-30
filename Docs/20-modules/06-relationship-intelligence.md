# Module 06 — Relationship Intelligence

> Layer L2 · Schema `relationships` · Owner: Data Intelligence

## 1. Purpose

Determines how tables connect when the source does not say. Bank estates routinely have missing or partial foreign keys — the joins exist in application code and analyst folklore, not in the catalog. This module recovers them with **evidence**, subjects them to human review, and **remembers rejections**.

Retaining rejections (negative knowledge) is whitespace W4: no competitor does it, and without it the system re-proposes the same wrong join every scan.

## 2. Jobs served

S1 (curate rather than author), S4 (know what breaks), R1 (approve with context), A3 indirectly.

## 3. Responsibilities

- Declared PK/FK inventory from source constraints.
- Bounded candidate relationship generation (metadata-only).
- Evidence scoring and confidence assignment.
- Maker-checker review of candidates.
- **Negative knowledge** — rejected candidates retained and not re-proposed.
- Table family detection: history, snapshot, delta, SCD, append-only, reference.
- Canonical table resolution.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Graph rendering | 10 knowledge-graph |
| Business entity mapping | 07 semantic-layer |
| Join execution | 16 query-gateway |
| Query-derived lineage | 09 lineage |

## 5. Domain model

```text
relationship_candidate, relationship_evidence, relationship_decision
negative_knowledge
relationship_candidate_group, relationship_candidate_group_member
table_family_candidate, canonical_table_mapping
```

## 6. Candidate generation

**No brute-force N×N full-value comparison.** That is the naive approach and it is both unaffordable and a value-access violation.

The pruning order:

1. Declared constraints are taken as fact — they are not candidates.
2. Name and type compatibility prunes the space first.
3. Cardinality and nullability profiles from module 05 prune further.
4. Ordinal position and parent-table context refine.
5. Only survivors are scored.
6. Candidates per table are capped (P3).

Evidence is **metadata-only** (ADR-0014): names, types, constraint participation, deterministic profile statistics. Never values.

## 7. Evidence and confidence

Every candidate carries algorithm version, contributing signals with weights, a confidence score, and a decision state. **The evidence is inspectable by the reviewer** — a confidence number without its reasoning is not reviewable, and a reviewer who cannot see why will either rubber-stamp or reject everything.

## 8. Negative knowledge

| Property | Behaviour |
|---|---|
| Rejection is recorded | With reviewer, rationale, timestamp, and the evidence at rejection time |
| Re-proposal is suppressed | The same candidate is not surfaced again |
| Suppression is not permanent | If the underlying evidence changes materially, the candidate returns flagged as previously rejected |
| It is queryable | "What have we decided is *not* true" is a first-class question |

## 9. Table families

Distinguishing "current customer" from "customer as of date" is a correctness requirement, not a nicety. An agent that joins a history table as if it were current produces confidently wrong answers.

| Family type | Signals |
|---|---|
| History | Temporal columns, versioning patterns, near-duplicate keys |
| Snapshot | Date-partitioned full copies |
| Delta / CDC | Change indicators, operation columns |
| SCD | Effective/expiry date pairs, current flags |
| Append-only | Monotonic keys, no updates |
| Reference | Small, stable, widely referenced |

Canonical table resolution names which member the agent should use by default, with evidence and a steward override.

## 10. Public interface

```python
# relationships/api.py
def list_candidates(scope, filt, page) -> Page[CandidateDTO]
def get_candidate(scope, candidate_id) -> CandidateDTO           # includes evidence
def submit_decision(scope, candidate_id, decision) -> DecisionDTO # via module 17
def list_negative_knowledge(scope, table_id) -> list[RejectedDTO]
def get_table_family(scope, table_id) -> TableFamilyDTO | None
def resolve_canonical(scope, entity_ref) -> TableRef | None
```

Implemented today in `aida.intelligence_api` (not the `relationships/api.py` path above — this platform has not yet decomposed into per-module packages; see `Docs/40-engineering/06-refactor-plan.md`):

```python
# aida/intelligence_api.py
async def discover_relationship_candidates(datasource_id, body, ...) -> Page
async def discover_cross_source_relationship_candidates(domain_id, body, ...) -> Page
async def list_relationship_candidates(datasource_id, ...) -> Page
async def decide_relationship_candidate(candidate_id, body, ...) -> RelationshipCandidateRead
async def bulk_decide_relationship_candidates(body, ...) -> RelationshipCandidateBulkDecisionResultRead  # RL-6
async def get_relationship_candidate_confidence_calibration(datasource_id, bucket_width, ...) -> RelationshipCandidateCalibrationRead  # RL-7
```

## 11. Events

Target vocabulary: `relationship.candidate_generated`, `relationship.approved`, `relationship.rejected`, `table_family.detected`, `canonical_table.resolved` (see `Docs/30-contracts/04-event-catalog.md` for the platform-wide caveat that most catalog rows predate a `.v1` rename). What `intelligence_api.decide_relationship_candidate` and the new bulk-decision endpoint actually emit today is `relationship_candidate.approved.v1` / `relationship_candidate.rejected.v1` (2026-08-30, RL-4) — the approve/reject siblings of the two rows above. `graph_projector.run_projector` consumes exactly these two names to trigger unified-lineage projection; keep the emitter and the projector's `UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES` in lockstep if either changes.

## 12. Dependencies

04 catalog, 05 profiling.

## 13. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Declared PK/FK | Implemented — constraint inventory and graph edges | Projection performance at millions of nodes |
| Inferred candidates | Implemented — bounded metadata-only, enriched edges, confidence/evidence, durable review, negative knowledge, bulk maker-checker review (RL-6), same-source projection of approvals to Neo4j (RL-4) | Composite candidates (RL-3, in progress elsewhere), statistical evidence policy, cross-source projection of approvals to Neo4j (see RL-4 note below) |
| Table families | **Pending** — architecture and evidence model documented only | History/snapshot/delta/SCD inference and canonical-table review (RL-1/RL-2, in progress elsewhere) |
| Cross-source relationships | Implemented — bounded datasource-pair discovery within a domain and, with an ACTIVE `cross_boundary_grant`, across one (ADR-0017 SS4/SS8); matches by canonical name and physical-type family, not raw string equality (RL-5, 2026-08-30) | Federated cross-source graph projected to Neo4j (today only the single-datasource unified-lineage graph is projected; the domain-wide federated graph that includes cross-source edges has no Neo4j projection path) |

## 14. Open work

| ID | Item | Priority | Status |
|---|---|---|---|
| RL-1 | Table family and temporal intelligence | P1 | Open (separate concurrent work) |
| RL-2 | Canonical table resolution with steward override | P1 | Open (separate concurrent work) |
| RL-3 | Composite relationship candidates | P1 | Open (separate concurrent work) |
| RL-4 | Project approved relationships to Neo4j | P1 | Done for same-datasource candidates (2026-08-30) — the emitted event name/payload now matches what `graph_projector` listens for. Cross-source candidates still are not projected to Neo4j (see §13); that is federation work, not a name/payload fix, and is unscheduled. |
| RL-5 | Cross-source relationship inference | P1 | Done (2026-08-30) — naming/type matching now survives snake_case/camelCase/PascalCase/SCREAMING_CASE and cross-dialect type spelling (`aida.relationship_naming`), with the match strength recorded in each candidate's evidence. |
| RL-6 | Bulk review for large candidate sets | P1 | Done (2026-08-30) — `POST /v1/relationship-candidates/bulk-decision`, explicit ids or a PENDING-only filter, capped at 500, per-candidate partial-success reporting. |
| RL-7 | Confidence calibration against a labelled banking corpus | P1 | Partially done (2026-08-30) — `GET /v1/relationship-candidates/confidence-calibration` reports the *observed* approval rate per confidence bucket from this deployment's own real decision history, with an optional `RelationshipCandidateGroundTruthLabel` override. This is explicitly **not** a published calibration curve against an external labelled banking corpus: no such corpus exists in this environment, and the endpoint says so in its own response rather than implying one. |
