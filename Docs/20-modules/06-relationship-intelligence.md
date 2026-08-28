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
table_family, table_family_member, canonical_table_mapping
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

## 11. Events

Emits `relationship.candidate_generated`, `relationship.approved`, `relationship.rejected`, `table_family.detected`, `canonical_table.resolved`.

## 12. Dependencies

04 catalog, 05 profiling.

## 13. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Declared PK/FK | Implemented — constraint inventory and graph edges | Projection performance at millions of nodes |
| Inferred candidates | Implemented — bounded metadata-only, enriched edges, confidence/evidence, durable review, negative knowledge | Composite candidates, statistical evidence policy, projection of approvals to Neo4j |
| Table families | **Pending** — architecture and evidence model documented only | History/snapshot/delta/SCD inference and canonical-table review |
| Cross-source relationships | Not implemented | Required for a heterogeneous estate |

## 14. Open work

| ID | Item | Priority |
|---|---|---|
| RL-1 | Table family and temporal intelligence | P1 |
| RL-2 | Canonical table resolution with steward override | P1 |
| RL-3 | Composite relationship candidates | P1 |
| RL-4 | Project approved relationships to Neo4j | P1 |
| RL-5 | Cross-source relationship inference | P1 |
| RL-6 | Bulk review for large candidate sets | P1 |
| RL-7 | Confidence calibration against a labelled banking corpus | P1 |
