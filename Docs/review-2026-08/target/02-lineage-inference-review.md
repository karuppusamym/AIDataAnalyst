# Target Design 2 — Lineage: inference, review, persistence

Status: Proposal, clean-room.

Today the platform produces column-level lineage from queries it executes, ingests
OpenLineage events and dbt manifests, and does nothing else. There is no view
parser, no procedure parser, no BI extraction, no cross-source stitching, and no
review workflow specific to lineage. This document specifies all five.

---

## 1. The principle the market gets wrong

Every vendor sells "automated lineage" and every customer discovers the same thing:
coverage is bounded by the parser list, and the gaps are invisible.

Collibra's technical lineage renders stitched nodes yellow and unstitched nodes grey —
a good instinct, but coverage is still bounded by a documented supported-source
matrix that excludes stored procedures on several major databases, Power BI
DirectQuery, and effectively all legacy on-prem ETL beyond a named handful.
Databricks' lineage is structurally more complete because it is captured at the
runtime layer rather than parsed after the fact — but only for workloads that ran on
Databricks, with no history before Sept 2024 and a rolling one-year window.

The lesson for a bank platform, where the estate is heterogeneous by definition:

> **Coverage will always be partial. Design for honest partiality, not for the
> appearance of completeness.**

Concretely, three rules:

1. **Every edge carries its derivation method and confidence.** An edge parsed from a
   view DDL is not the same claim as an edge inferred from column-name similarity, and
   the graph must say which it is.
2. **Unresolvable is a node kind, not a dropped edge.** Dynamic SQL, an unparseable
   dialect construct, an external system — these become explicit `UNRESOLVED` nodes
   with a reason. A gap you can see is a work item; a gap you cannot see is a
   correctness failure that surfaces during an audit.
3. **Coverage is a reported metric, per source and per domain.** "78% of tables in
   Retail Banking have upstream lineage; 12% are blocked on dynamic SQL in three
   procedures" is a useful sentence. "Lineage: enabled" is not.

---

## 2. Derivation methods, ranked by trust

| Rank | Method | Confidence | Notes |
|---|---|---|---|
| 1 | **Declared** — FK constraints, dbt `depends_on`, OpenLineage `columnLineage` facet | 1.0 | Facts. Never reviewed |
| 2 | **View DDL parse** | 0.95 | The definition *is* the transformation. Ambiguity only from `SELECT *` against a drifted schema |
| 3 | **Executed-query parse** (existing gateway path) | 0.9 | Observed, not declared. High trust, but only covers what ran through us |
| 4 | **Query-log parse** (source history) | 0.85 | Broad coverage, but includes ad-hoc queries that are not real pipeline edges — needs frequency filtering |
| 5 | **Procedure body parse** | 0.6–0.9 | Varies sharply by construct. Static `INSERT...SELECT` is near-certain; control flow and temp tables degrade it; dynamic SQL breaks it |
| 6 | **Name/type similarity inference** | 0.3–0.6 | Last resort. Always requires review. Never auto-published |

**Never merge derivation methods into one confidence number.** Store the method, and
let policy decide what each method may do. Rank 1–3 may auto-publish. Rank 4–5 publish
flagged. Rank 6 is human-required, always.

---

## 3. View parsing

Straightforward and high-value; it is the largest single coverage win available.

```
harvest view DDL (envelope v1.1)
  → sqlglot.parse_one(ddl, read=dialect)
  → qualify against catalog (resolve unqualified names, expand SELECT * to current columns)
  → walk projections: for each output column, collect source column refs
  → classify per output column:
        DIRECT      col            -> col
        DERIVED     expr(col,...)  -> col
        AGGREGATED  agg(col)       -> col            + grain change recorded
        FILTERED    appears only in WHERE/JOIN/HAVING -> influence edge, not a value edge
  → emit column edges with method=VIEW_DDL, plus one table-level edge per source
```

Three details that determine whether this is useful or noise:

- **`SELECT *` expansion must be recorded as an expansion, with the schema version it
  was expanded against.** Otherwise the lineage silently rots on the next column add.
- **Filter-only columns produce a distinct edge kind (`INFLUENCES`).** A column in a
  `WHERE` clause affects which rows appear but does not flow into a value. Conflating
  the two is how impact analysis becomes uselessly broad.
- **Nested views resolve transitively**, with a depth cap and a cycle guard.

Materialized views are identical plus a refresh-dependency edge.

---

## 4. Procedure parsing

Harder, dialect-specific, and worth doing because in a bank a large share of real
transformation logic lives in T-SQL and PL/SQL procedures that no vendor parses.

**Approach: statement-level dataflow, not full program semantics.** Do not attempt to
interpret the procedure. Extract the statements that move data and connect them.

```
parse body → statement list
for each data-moving statement:
    INSERT INTO t SELECT ...        -> column edges into t
    UPDATE t SET c = expr FROM ...  -> column edges into t.c
    MERGE INTO t USING s ...        -> column edges per matched/unmatched clause
    CREATE TABLE t AS SELECT ...    -> column edges into t
    SELECT ... INTO #tmp            -> edges into a temp node
resolve temp/CTE/table-variable nodes transitively within the procedure scope
collapse temp nodes at the boundary: emit only edges between durable objects,
    retaining the temp path as evidence
```

**Degradation is explicit:**

| Construct | Handling |
|---|---|
| Static DML | Full column-level edges, confidence 0.9 |
| Control flow (`IF`/`WHILE`/`CASE` branches) | Union of all branches' edges, confidence 0.8, branch noted in evidence |
| Cursor loops | Edges from the cursor's `SELECT` and the loop body's DML, confidence 0.7 |
| Temp tables / table variables | Resolved within scope, collapsed at boundary |
| Dynamic SQL (`EXEC(@sql)`, `sp_executesql`, `EXECUTE IMMEDIATE`) | **`UNRESOLVED` node** with the procedure, line number, and the variable name. If the string is statically constructible from literals, attempt constant-folding once; if not, stop and mark it |
| Calls to other procedures | Edge to the callee's node; resolved transitively with a depth cap |
| External/CLR/Java procedures | `UNRESOLVED`, kind `EXTERNAL` |

Dialect coverage order, by bank prevalence: **T-SQL → PL/SQL → PostgreSQL PL/pgSQL →
Db2 SQL PL**. `sqlglot` handles the SQL statements inside these bodies; the
procedural wrapper needs a thin per-dialect statement splitter, which is a bounded,
testable piece of work per dialect — not a compiler.

**Certification harness.** Each dialect parser ships with a corpus of real procedure
shapes and expected edges, and the connector's advertised lineage capability is
derived from that suite's pass rate. This is the existing INV-9 (honest capability
reporting) applied to lineage, and it is the right way to stop over-promising.

---

## 5. Cross-source stitching

The estate does not stop at a datasource boundary; the lineage graph must not either.

Two sources are stitched when an edge crosses them. Edges arrive from:

- **ETL/orchestration events** — OpenLineage from Airflow, Spark, dbt. These are
  declared and cross sources natively. Highest trust.
- **Replication and CDC configuration** — a Fivetran/GoldenGate/Qlik Replicate config
  is a declaration that `src.schema.table → tgt.schema.table`. Ingest it as declared
  lineage rather than trying to infer it.
- **Identity matching** — same fully-qualified name and compatible schema across two
  sources, one of which is known to be a landing zone. Inference, confidence ≤0.6,
  review required.

**Boundary control.** Cross-source edges are subject to authorisation like everything
else. Where a workspace may not see the far side, the edge is returned as
`withheld: no_grant` — present, counted, and honestly labelled, but not traversable.
This is the right behaviour and the current ADR-0017 proposal already has it; keep it,
but key it on workspace grant and classification rather than on a tenancy path.

---

## 6. The review workflow

This is the requirement — *"how the inferred lineage will be reviewed, updated,
further saved"* — and it does not exist today.

### Object model

```
lineage_edge          id, source_ref, target_ref, level ∈ {TABLE, COLUMN},
                      relation ∈ {FLOWS_TO, INFLUENCES, REFRESHES},
                      transformation ∈ {DIRECT, DERIVED, AGGREGATED, FILTERED},
                      method, confidence, status ∈ {PROPOSED, ACCEPTED, REJECTED,
                      SUPERSEDED}, first_seen, last_seen, published_version
lineage_evidence      edge_id, evidence_kind, artifact_ref, excerpt (literal-redacted),
                      parser_version, extracted_at
lineage_proposal      batch of edges from one extraction run, with a diff against
                      the current published graph: added / removed / changed
lineage_negative      a rejected edge, with reason, so re-extraction does not
                      re-propose it — unless the underlying evidence changes materially
lineage_manual        a human-asserted edge, with justification and author. Ranks
                      above all inferred methods and survives re-extraction
```

### Flow

1. **Extraction run** produces a `lineage_proposal` — always a *diff*, never a
   wholesale replacement. Reviewing 40 changes is possible; reviewing 40,000 edges is
   not, and this is where most lineage review UX fails.
2. **Auto-accept** methods 1–3 within an unchanged schema. Do not make a human
   approve a foreign key.
3. **Queue** methods 4–6, ordered by *blast radius* — an edge feeding a certified
   metric or a regulatory report is reviewed before an edge between two staging
   tables. Order by impact, not by alphabet.
4. **Review surface** shows, per edge: the two endpoints in context, the evidence
   excerpt (the actual view SQL, literals redacted), the confidence and method, what
   else depends on it, and three actions — **accept**, **reject with reason**, **edit**.
   "Edit" means correcting the endpoints or the transformation class, which converts
   the edge to a `lineage_manual` assertion with the original inference retained as
   evidence.
5. **Bulk decisions with per-item rationale**, grouped by pattern — "all 340 edges
   from `stg_*` to `dw_*` produced by procedure `usp_load_dw`" is one decision, not
   340. This is the item the current design has in its persona doc and its module
   interface but not in its implementation.
6. **Reject writes negative knowledge.** The next extraction does not re-propose it.
   If the view definition later changes, the negative record is invalidated and the
   edge returns to the queue flagged as previously-rejected. This mechanism already
   exists for relationship candidates; extend it to lineage rather than inventing a
   second one.
7. **Publish** produces a new immutable graph version. Impact analysis, the wiki,
   context products and agents all read a pinned version, so a review in progress
   never destabilises a running agent.

### Maker–checker

Lineage is a governed object type in the single unified review queue — not a
parallel approval system. The existing architectural choice to have exactly one
approval service is correct and should not be diluted.

---

## 7. Impact analysis

Once edges are versioned and typed, impact is a bounded traversal, not a feature:

- **Downstream impact** of a column change: traverse `FLOWS_TO` and `INFLUENCES`
  forward, depth-bounded, returning affected tables, views, procedures, metrics,
  tools, wiki pages and published context products. **Tools and context products in
  the impact set is the part nobody else has** — it answers "if this column changes,
  which agent capabilities break," which is the question that matters once agents are
  in production.
- **Upstream provenance** of a reported number: traverse backward to source columns,
  returning the transformation chain with evidence. This is the BCBS 239 answer, and
  it is worth being explicit that it must survive an audit — hence immutable versions
  and retained evidence excerpts.
- **Blast radius as a governance input**: a change to a column with high downstream
  impact raises the review requirement automatically.

---

## 8. What this replaces in the current design

- Lineage stops being an enum with one populated value.
- `AI_DECISION` edges — the differentiator the current documents claim and the code
  does not build — become a natural case of the model above: an agent run produces a
  proposal-free, auto-accepted set of edges (question → retrieved assets → chosen tool
  → pinned versions → refusals with the control that fired), method `AGENT_DECISION`,
  confidence 1.0 because it is observed, not inferred. It is a *recording*, not an
  inference, and it belongs in the same graph so impact analysis can answer "which
  agent decisions depended on this column."
- The `09 lineage ↔ 16 query-gateway` import cycle flagged as `ST-11` disappears:
  the gateway *emits* lineage events; the lineage module *consumes* them. One
  direction, no cycle. The gateway should never call into lineage.
