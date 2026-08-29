# Module 09 — Lineage

> Layer L2 · Schema `lineage` · Owner: Data Intelligence

## 1. Purpose

Answers *where did this data come from* — and, uniquely, *why did the agent choose this path*. Data lineage is table stakes; **AI decision lineage is whitespace W3** and no competitor models it.

See `../competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` for the Collibra Data Lineage feature comparison that opened LN-9 through LN-12.

## 2. Jobs served

A2 (can I trust this), S4 (what breaks if this changes), U1/U2 (audit), P2.

## 3. Responsibilities

- Query lineage from executed SELECTs: referenced tables/columns, output-to-source mapping, direct vs. derived.
- View and stored-procedure lineage from definitions.
- ETL lineage via OpenLineage ingestion.
- dbt transformation intelligence from manifests.
- BI lineage from Tableau, Power BI, Looker.
- **AI decision lineage** — the agent's path, choices, and refusals as traversable edges.
- Impact analysis across all edge kinds.
- Transformation artifact storage with literal redaction.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Graph rendering | 10 knowledge-graph |
| SQL execution | 16 query-gateway |
| Inferred table relationships | 06 relationships |
| Running dbt | dbt + warehouse (out of scope) |

## 5. Domain model

```text
lineage_node (table, column, metric, tool, report, agent_run, semantic_version)
lineage_edge (edge_kind, from, to, confidence, evidence_ref, observed_at)
lineage_evidence
transformation_artifact (dbt manifest, view DDL, procedure body — literals redacted)
```

`edge_kind ∈ {QUERY, VIEW, PROCEDURE, ETL, DBT, BI, AI_DECISION}`. Partitioned by time.

## 6. AI decision lineage — the differentiator

Conventional lineage says: *this column came from that column.*

AI decision lineage says: *this answer used this metric, at this semantic version, selected by this retrieval ranking, executed through this approved tool, under this policy version — and it declined to use that other table because its quality incident was open.*

| Edge captured | Why it matters |
|---|---|
| Question → retrieved objects (with rank and reason) | Explains why *this* table and not another |
| Retrieved objects → selected tool or generated plan | Explains the execution choice |
| Plan → semantic version and policy version pinned | Makes the decision replayable |
| Plan → refusals, with the control that fired | **Explains what did not happen** — nobody else records this |
| Execution → output-to-source column mapping | Conventional lineage, tied to the decision |

**Why this is defensible.** Recording refusals requires a runtime where refusal is a first-class, deterministic event — which requires the execution choke point (ADR-0004) and prompt-risk screening (ADR-0013). A product whose agent simply runs cannot record what it declined to do, because it declines nothing.

## 7. Value-freedom

| Artifact | Treatment |
|---|---|
| Executed SQL | Literals redacted; hash retained |
| dbt compiled SQL | Literals redacted; SQL hash retained; **raw artifact not persisted** |
| View / procedure definitions | Literals redacted |
| Output-to-source mappings | Column identity only — never values |
| Query results | Never stored here |

dbt artifact SQL is **never executed** by Atlas. dbt remains the transformation compiler and executor; Atlas ingests its output as evidence.

## 8. Public interface

```python
# lineage/api.py
def get_upstream(scope, node: NodeRef, depth: int, kinds: set[EdgeKind]) -> LineageGraphDTO
def get_downstream(scope, node: NodeRef, depth: int, kinds: set[EdgeKind]) -> LineageGraphDTO
def get_impact(scope, node: NodeRef) -> ImpactReportDTO
def record_query_lineage(execution_id, parsed: ParsedQuery) -> None     # gateway-only
def record_ai_decision(run_id, decisions: list[DecisionEdge]) -> None   # runtime-only
def ingest_openlineage(scope, event: OpenLineageEvent) -> IngestResult
def ingest_dbt_manifest(scope, project_id, manifest) -> DbtImportDTO
```

## 9. HTTP surface

| Method | Path |
|---|---|
| GET | `/v1/lineage/upstream`, `/v1/lineage/downstream` |
| GET | `/v1/tables/{id}/impact` |
| GET | `/v1/datasources/{id}/unified-lineage/graph` — merged FK + suggested + dbt + OpenLineage graph |
| GET | `/v1/datasources/{id}/unified-lineage/impact/{node_id}` — transitive upstream/downstream impact |
| POST | `/v1/lineage/openlineage` |
| POST | `/v1/dbt-projects`, `/v1/dbt-projects/{id}/manifests` |
| GET | `/v1/agent-runs/{id}/decision-lineage` |

## 10. Events

Emits `lineage.edge_created`, `lineage.artifact_ingested`, `lineage.impact_computed`.

## 11. Dependencies

04 catalog, 16 query-gateway.

## 12. Current state → target

| Edge kind | Now | Target |
|---|---|---|
| QUERY | Implemented — value-free output-to-source, direct/derived, transformation names, tool dependencies | Historical lineage search |
| DBT | Implemented — manifest v12-compatible, model/source/test inventory, catalog matching, SQL hash + redacted SQL, dependency DAG, impact; `run_results.json` test-outcome ingestion now reconciles into `DataQualityIncident` rows (`dbt_quality_bridge.py`) | CI/dbt Cloud auth, column-level manifest lineage, retention, large-DAG virtualization |
| VIEW / PROCEDURE | **Not implemented** | Entry-ticket gap |
| ETL / OpenLineage | Partial — `POST /v1/lineage/openlineage` ingests RunEvents, extracts column-lineage edges from the `columnLineage` facet, matches against the catalog, and persists idempotently (`openlineage.py`, `openlineage_api.py`); **zero test coverage**, and no Airflow-sourced event has ever been verified producing real edges | Test coverage; live Airflow e2e evidence |
| BI | **Not implemented** | Entry-ticket gap |
| AI_DECISION | Partial — traces exist; not modelled as lineage edges | **Differentiator — model as first-class edges** |
| Impact | Implemented (direct) — physical table to metrics, tools, relationships. **Transitive impact delivered 2026-08-29** — `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}` does bounded upstream/downstream BFS over a graph merged from FK + suggested + dbt + OpenLineage edges (`unified_lineage.py`, `unified_lineage_api.py`) | Column-level edges; view/procedure and BI edges folded in (LN-2, LN-4, LN-11) |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| LN-1 | OpenLineage ingestion endpoint | P0 |
| LN-2 | View and stored-procedure lineage | P0 |
| LN-3 | AI decision lineage as first-class edges | P0 |
| LN-4 | BI tool lineage (Tableau, Power BI, Looker) | P1 |
| LN-5 | Column-level dbt manifest lineage | P1 |
| LN-6 | dbt `run_results.json` operational evidence | P1 |
| LN-7 | Transitive cross-kind impact traversal | **Delivered 2026-08-29** (table-level; see LN-10/LN-11 for the remaining edge kinds) |
| LN-8 | Large-DAG virtualization | P1 |
| LN-9 | One canonical graph merging FK + suggested + dbt + OpenLineage edges | **Delivered 2026-08-29** — `unified_lineage_api.py` |
| LN-10 | Authoritative column-to-column mapping (replace dbt's identical-name matching) | P1 |
| LN-11 | View/stored-procedure/BI nodes folded into the unified graph | P1, depends on LN-2/LN-4 |
| LN-12 | Unified graph export: SVG, PNG, PDF, CSV | P2 |

### 13.1 Runtime scaling controls

- `node_limit` and `edge_limit` are global response/build budgets shared across FK,
  relationship-candidate, dbt, and OpenLineage sources; synthetic nodes cannot bypass them.
- dbt resource and edge reads are query-bounded, and edges whose endpoints were excluded by
  the node budget are not emitted.
- Redis response caching is available through `AIDA_LINEAGE_CACHE_ENABLED=true` with a bounded
  `AIDA_LINEAGE_CACHE_TTL_SECONDS`; cache failures fall back to PostgreSQL.
- PostgreSQL remains authoritative. The existing Kafka/Neo4j projector remains the intended
  asynchronous estate-scale projection path; unified dbt/OpenLineage projection is still open.
