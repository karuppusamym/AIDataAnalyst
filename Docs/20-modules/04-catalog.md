# Module 04 — Catalog

> Layer L2 · Schema `catalog` · Owner: Data Platform

## 1. Purpose

The authoritative inventory of the data estate: what objects exist, what shape they have, when they changed, and what happened to the ones that disappeared. Every other intelligence module hangs off catalog identity, so **stable object identity is this module's most important property**.

## 2. Jobs served

A3 (find the right table among 400,000), S3 (bulk assignment), S4 (impact), P2.

## 3. Responsibilities

- Catalogs, schemas, tables, views, columns, constraints, indexes, partitions.
- **Stable internal identity** that survives re-scan, drift, deprecation, and reactivation.
- Change detection via content fingerprints.
- Soft deprecation (tombstones) and reactivation.
- Drift evidence: created, changed, deprecated counts per run.
- Asset certification state and tagging.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| How metadata arrived | 03 ingestion |
| Statistics about content | 05 profiling |
| Business meaning | 07 semantic-layer |
| Search ranking | 12 retrieval |
| Graph traversal | 10 knowledge-graph |

## 5. Domain model

```text
catalog, schema, table (object_type: BASE_TABLE | VIEW | MATERIALIZED_VIEW | EXTERNAL)
column, constraint, index, partition
object_fingerprint, object_tombstone, drift_record
asset_tag, asset_certification
```

**Scale note.** `column` is the dominant table — 1M tables × ~30 columns = 30M rows. Partitioned by `datasource_id`.

## 6. Identity model

| Concern | Mechanism |
|---|---|
| Stable ID | Derived from `(datasource, catalog, schema, name, object_type)` |
| Rename | Currently a delete + create. Rename detection is open work (CT-4). |
| Change detection | Content hash per object; unchanged objects are skipped |
| Deletion | Soft deprecation via tombstone; reactivation restores the **same** ID |
| Drift | Counts and per-object evidence recorded per analysis run |

Stable identity is why a semantic annotation, a lineage edge, or a governed tool survives a re-scan. Losing it silently orphans everything downstream.

## 7. Public interface

```python
# catalog/api.py
def get_table(scope, table_id) -> TableDTO
def list_tables(scope, filt: TableFilter, page: Page) -> Page[TableDTO]
def get_columns(scope, table_id) -> list[ColumnDTO]
def get_constraints(scope, table_id) -> list[ConstraintDTO]
def resolve_reference(scope, ref: QualifiedName) -> TableRef | None
def apply_inventory(scope, datasource_id, inventory: Inventory) -> DriftReport   # ingestion-only
def get_drift(scope, run_id) -> DriftReport
def certify_asset(scope, table_id, decision: CertificationDecision) -> AssetCertification
```

`resolve_reference` is the function the query gateway uses to turn parsed SQL references into allowlist decisions. It is on the hot path and must be fast and exact.

## 8. HTTP surface

| Method | Path |
|---|---|
| GET | `/v1/tables`, `/v1/tables/{id}`, `/v1/tables/{id}/columns` |
| GET | `/v1/schemas`, `/v1/catalogs` |
| GET | `/v1/tables/{id}/impact` |
| POST | `/v1/tables/{id}/certification` |
| POST | `/v1/tables/bulk-tag` |
| GET | `/v1/analysis-runs/{id}/drift` |

## 9. Events

Emits `catalog.object.created`, `catalog.object.changed`, `catalog.object.deprecated`, `catalog.object.reactivated`, `catalog.drift.detected`, `catalog.asset.certified`.

Consumed by: 10 knowledge-graph (projection), 12 retrieval (indexing), 09 lineage (reference resolution).

## 10. Dependencies

03 ingestion.

## 11. Controls

| Control | Behaviour |
|---|---|
| INV-5 | Every object carries tenancy; list operations are scope-required |
| INV-6 | No sample values; default expressions and descriptions are bounded and producer-responsible |
| INV-1 | Catalog is authoritative; Neo4j and search are projections |

## 12. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Table/column/constraint inventory | Implemented — stable identity, fingerprints, drift, tombstones, reactivation | Unchanged |
| Index / partition inventory | Data model and connector pattern established | Normalized models + connector extraction |
| Asset certification | Partial (tools/metrics only) | First-class asset certification with owner and expiry |
| Bulk operations | Not implemented | Bulk tag, classify, own, certify — **entry-ticket gap** |
| Million-object UX | Not implemented | Virtualization — **entry-ticket gap** |
| Rename detection | Not implemented | Heuristic + steward confirmation |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| CT-1 | Bulk actions (tag, classify, own, certify) | P1 |
| CT-2 | Virtualized million-object browsing | P1 |
| CT-3 | Index and partition normalized models | P1 |
| CT-4 | Rename detection with steward confirmation | P2 |
| CT-5 | Asset certification lifecycle with expiry | P1 |
| CT-6 | Cross-source object resolution | P1 |
