# Atlan screenshot review — Lineage (images 29–39)

> Status: review finding, 2026-08-30. Scope: `image29`–`image39` from `Docs/competitors/Atlan-context.docx`
> (extracted to `scratch/atlan-media/`), plus the "Everything you need to know about data lineage"
> FAQ block in `atlan-context.txt` (lines 125–132).
>
> This reviewer covered lineage only. It cross-checks — and in three places corrects — the
> docs-level lineage section in `../research/02-atlan.md` §5, which was written from vendor
> documentation before these screenshots existed. Nothing here is endorsed vendor claim;
> every claim about Atlan is attributed to the image it was read off.

Images 29–33 are connector setup and the App Framework (`image29`–`image31`: the three-step
crawler wizard; `image32`: the 104-connector grid; `image33`: the App Framework). They belong
to the connector reviewer's scope and are referenced here only where lineage depends on them.
Lineage proper starts at `image34`.

---

## 1. Findings

| # | What Atlan ships (image) | What we have | Verdict | Weeks | Slots into |
|---|---|---|---|---|---|
| **L1** | Four extraction methods feeding **one** canvas — the same graph panel redrawn under each method (`image35`–`image38`) | Six producers writing to **five** stores; the unified graph merges four of them and none of the three newest (`unified_lineage_api.py:200,252,341,405`) | **Gap worth closing** — this is the finding | 2 | Phase 2, immediately before `C9` |
| **L2** | "SQL parsing reads **millions of queries** from Snowflake/BigQuery/Redshift/Databricks" (`image35` caption; FAQ line 127) | We parse only what runs through our own gateway (`query_gateway.py:105`), and never persist it as an edge — it is a JSON column on `QueryExecution` | **Materially weaker, and half of it is a deliberate decline** | 1 (persist) | Phase 2, with L1 |
| **L3** | Column-level view parse rendered as the product's centrepiece (`image35`) | `sql_lineage_parser.py` shipped today: it drops `SELECT *` silently, stamps `FULL` confidence on every edge, and inverts the `FILTERED` class — with a test asserting the inversion | **Gap worth closing; `N2`/`N3` should not be marked done** | 3 | Phase 2, reopen `N2` |
| **L4** | `POST /api/lineage/edges` → `201 Created`, CSV import, and a visual Lineage Builder (`image38`) | `target/02` §6 specifies `lineage_manual`; nothing is built | **Build the API. Decline the free-drawing canvas** | 2 | Phase 2, inside `N4` |
| **L5** | "Tag a column as PII once — lineage propagates that classification to every downstream asset **and syncs bi-directionally with Snowflake and Databricks**" (`image39`, panel 2) | Nothing. `grep -rn propagat src/aida` returns two unrelated hits. But `ui-next/src/components/PropagationLog.tsx` already draws the propagation log | **Build one-directional, raise-only, provisional. Refuse the bi-directional sync outright** | 3 | Phase 2, new item `N21` |
| **L6** | Impact analysis delivered **inside the GitHub/GitLab pull request** before the change ships (`image39`, panel 3) | A bounded BFS behind `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}` | **Real product gap — but the answer is a review-gate, not a GitHub app** | 2 | Phase 2, inside `N4` |
| **L7** | "How does Atlan help trace AI answers back to source data?" — raised as a heading, **left unanswered** (line 129); a lineage agent doing root cause in `image34` | `interpretation` before the number (`30-contracts/09` §, line 46), pinned versions, `ai_decision_lineage.py` built | **Already covered, and ahead — but the answer contract's `lineage` block is the weakest field in it, and no customer-facing doc makes the claim** | 0.5 | Phase 0-style doc fix, do now |
| **L8** | Lineage agent does schema-history root cause: "`amount` renamed → `net_amount` on Jan 8 — JOIN silently returned nulls" (`image34`) | Nothing; no versioned graph publish, no schema-history diff over lineage | **Gap we should defer, not decline** — it is unbuildable until L1 and versioned publishes land | 4 | Phase 3 |
| **L9** | 104 connectors, each a lineage source (`image32`) | Five connectors, three declared planned | **Already decided — decline** (`research/02-atlan.md` §10). Not re-litigated here | — | — |

Three of these change what we build: **L1** (one graph, not five stores), **L3** (our new
parser degrades silently, which is the INV-9 failure the plan explicitly warned about), and
**L5** (propagation is an enforcement input for us, not a label). **L7** is the place we are
ahead and do not say so.

---

## 2. Their four methods against ours

The four-panel footer in `image35`–`image38` is identical in all four screenshots; only the
canvas above it changes. That repetition is the argument the page is making, and it is the
right one: *the method varies, the graph does not*.

| Atlan method (image) | Ours | Reality |
|---|---|---|
| **SQL Parsing** — "millions of SQL queries from Snowflake, BigQuery, Redshift, Databricks"; `image35` shows a `CREATE TABLE revenue_agg AS SELECT …` fanning into four column edges, with the parsed source SQL pinned beside the graph | `extract_column_lineage()` (`query_gateway.py:105`) + `sql_lineage_parser.py` (`parse_view_lineage`, `parse_procedure_lineage`) | Two different things. See §3 |
| **Native Integrations** — per-tool API crawl; `image36` shows a live crawl progress panel: "dbt Cloud · 14 models · 2,341 column dependencies extracted", Looker 8 explores, Tableau 22 workbooks | `dbt_artifacts.py` + `dbt_column_lineage.py`; `bi_lineage.py` (Tableau Metadata API, Power BI Scanner API, shipped 18:26 today) | **Closest to parity.** We have dbt column lineage and BI extraction. What we lack is `image36`'s *counter* — "2,341 column dependencies extracted" is a coverage report, and coverage reporting is `target/02` §1 rule 3 |
| **OpenLineage** — `image37` shows the Airflow DAG, the raw `RunEvent COMPLETE` payloads with `inputs`/`outputs`, and the resulting three-node chain, side by side | `openlineage.py` / `openlineage_api.py`, incl. the `columnLineage` facet | **Already covered on ingestion.** Still zero test coverage of a real Airflow event end-to-end (`20-modules/09-lineage.md` §12) — that is an evidence gap, not a design gap |
| **Custom Lineage** — `image38`: REST tab showing `POST /api/lineage/edges {"from":"orders_raw/amount","to":"revenue_agg/net_revenue","type":"TRANSFORM"}` → `201 Created · Edge registered`; a CSV Import tab; and a visual Lineage Builder named in the caption | Nothing built. `target/02` §6 specifies `lineage_manual` | **Gap.** See §5 |

**The finding that matters is not any single row — it is the column header.** Atlan's four
methods converge on one canvas. Ours converge on nothing:

| Producer | Where it lands | In the unified graph? |
|---|---|---|
| Gateway executed-query parse | `QueryExecution.column_lineage`, a JSON column (`models.py:1375`) | **No** |
| View DDL parse | `view_lineage_edge` table | **No** |
| Procedure body parse | `procedure_lineage_edge` table | **No** |
| BI extraction | `bi_report_metric_edge`, `bi_metric_column_edge` | **No** |
| AI decision edges | `ai_decision_lineage.py` | **No** |
| Consumption edges | `ContextProductConsumptionEdge` | **No** |
| Foreign keys | catalog | Yes — `edge_source="FOREIGN_KEY"` |
| Relationship candidates | catalog | Yes — `"SUGGESTED_RELATIONSHIP"` |
| dbt `depends_on` | dbt tables | Yes — `"DBT_DEPENDENCY"` |
| OpenLineage | OpenLineage tables | Yes — `"OPENLINEAGE_ETL"` |

Six of ten producers are invisible to `GET /v1/datasources/{id}/unified-lineage/graph`, and the
three built most recently are all in the invisible half. `LN-9` was closed on 2026-08-29 as "one
canonical graph"; three new edge producers shipped on 2026-08-30 without joining it. That is not
a design decision, it is drift — and it is the drift that `image35`–`image38` is selling against.

The fix is small because `UnifiedLink` (`unified_lineage.py:31`) already carries `edge_source`,
`confidence`, `source_columns` and `target_columns`. Adding four `_collect_*` builders in
`unified_lineage_api.py` alongside the existing four is roughly two weeks including catalog
matching for the view/procedure tables, which today have `source_table_id`/`target_table_id`
columns that **nothing populates** (`view_lineage_api.py` writes text names only).

> **Change.** `Docs/20-modules/09-lineage.md` §12, add a row at the end of the table:
>
> | Edge kind | Now | Target |
> |---|---|---|
> | **Unification** | **Four of ten producers merged.** `unified_lineage_api.py` merges FK, suggested relationships, dbt and OpenLineage. Gateway query lineage (JSON on `QueryExecution`), view edges, procedure edges, BI edges, AI-decision edges and consumption edges are each stored in their own table and are **not reachable from the unified graph or from impact analysis** | All ten producers behind one `UnifiedLink` builder set, each carrying its derivation method. `LN-9` is reopened as `LN-13` until they are |

---

## 3. Is our gateway parse the same as their "SQL parsing over query history"?

**No, and the difference cuts both ways.**

`image35` is explicit about the corpus: *"Atlan's parser reads millions of SQL queries from data
systems like Snowflake, BigQuery, Redshift, and Databricks."* That is the warehouse's own query
history — `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` and its equivalents. It sees every ETL job,
every scheduled load, every analyst's ad-hoc CTAS, whether or not Atlan was in the path.

Ours (`extract_column_lineage`, `query_gateway.py:105`) sees exactly what our gateway executed.
In a bank, the overwhelming majority of the transformation logic that produces a regulatory
number was written years before we existed and runs on a scheduler we are not in. **We are not
weaker at parsing; we are looking at a corpus that is nearly empty of the edges that matter.**

Three responses, and only two of them are worth taking:

1. **Persist what we already extract as edges — 1 week, do it.** Today the parse result is a JSON
   blob on the execution row and is discarded from the graph's point of view. It is rank 3 in
   `target/02` §2 (confidence 0.9, "observed, not declared") and it is free. This alone does not
   close the coverage gap, but it stops us throwing away the highest-trust evidence we produce.
   It also makes the gateway an *emitter* in the direction `C4`/`ST-11` already mandates.

2. **Harvest view and procedure definitions from envelope v1.1 and parse those — this is the
   real answer, and it is already `N2`/`N3`.** A view definition is a better artefact than a
   query log entry: it is the declared transformation, it is stable, it is rank 2 (0.95) rather
   than rank 4 (0.85), and it does not require us to filter an analyst's one-off `SELECT` out of
   the pipeline graph. `target/02` §2 already ranks these correctly; nothing needs to change in
   the design, only in the implementation (§4).

3. **Mine full warehouse query history — decline for this horizon.** It is rank 4 for a reason.
   Query-log mining needs a frequency/recurrence filter to separate pipelines from ad-hoc noise,
   it needs `ACCESS_HISTORY`-grade privileges on the customer's warehouse that a bank will
   contest, and it produces exactly the "appearance of completeness" `target/02` §1 warns about.
   Decline it explicitly rather than leaving it as an unstated absence — and, per INV-9, **do not
   write "SQL parsing over query history" in any of our own material until we do it.**

---

## 4. `N2`/`N3` shipped, and they degrade silently

`sql_lineage_parser.py` (479 lines, written 12:44 today) and `view_lineage_api.py` give us a view
and procedure parser. Measured against `image35` — which shows the parsed SQL, the highlighted
source expression, and four resolved column edges, in one frame — and against our own
`target/02` §3–§4, the current cut has five defects, of which two are INV-9 failures:

**a. `SELECT *` is dropped without a trace.** `sql_lineage_parser.py`, in
`_extract_edges_from_select`:

```python
# Star expansion - we cannot resolve individual columns
if isinstance(source_expr, exp.Star):
    continue
```

`target/02` §3 says the opposite: *"`SELECT *` expansion must be recorded as an expansion, with
the schema version it was expanded against. Otherwise the lineage silently rots on the next
column add."* A `CREATE VIEW v AS SELECT * FROM t` currently produces **zero edges and no
error** — indistinguishable, downstream, from a view with no upstreams. This is the exact shape
INV-9 forbids: a capability that fails by returning nothing rather than by reporting that it
could not.

**b. `<UNKNOWN>` is a magic string, not an `UNRESOLVED` node.** When an alias will not resolve,
the edge is written with `source_table = "<UNKNOWN>"`. `target/02` §1 rule 2 says
*"Unresolvable is a node kind, not a dropped edge"* — with a reason attached. A string literal in
a `String(500)` column is not a node kind, is not countable in a coverage report, and will
silently collide with itself across datasources.

**c. Every edge is `Confidence.FULL`.** The `LineageEdge` dataclass hard-codes
`confidence=Confidence.FULL.value` on construction; only the *ParseResult* varies. So a
procedure-body edge and a view-DDL edge are stored with identical confidence — flattening the
0.95-vs-0.6-to-0.9 distinction `target/02` §2 exists to preserve. And the edge carries `dialect`
but no `method`: a reader of `view_lineage_edge` cannot tell a view parse from a procedure parse
except by which table it is in. **This is `C9` — "store the derivation method, not just a
confidence number" — arriving late and being violated by the first thing that shipped after it
was written.**

**d. The `FILTERED` transformation class is inverted, and a test asserts the inversion.**
`_classify_transformation` returns `FILTERED` whenever the *statement* has a `WHERE` clause,
before it ever looks at the projection. So:

```sql
CREATE VIEW v AS SELECT col_a FROM t WHERE col_a > 0
```

produced `t.col_a → v.col_a` typed **`FILTERED`**, when it is a `DIRECT` value edge on a
row-filtered view — and a test asserted that this was correct (renamed by AT-D2's fix; see
below). Meanwhile a column that appeared *only* in a `WHERE` or `JOIN`
clause — the case `FILTERED` is for — produced no edge at all, because the walker only visited
projections. `target/02` §3: *"Filter-only columns produce a distinct edge kind (`INFLUENCES`).
A column in a `WHERE` clause affects which rows appear but does not flow into a value.
Conflating the two is how impact analysis becomes uselessly broad."* This was conflated in
the opposite direction, which is worse: impact analysis that discounts `FILTERED` edges as
non-value-carrying would drop real value edges. The same bug affected `AGGREGATED`, which was
also computed per-statement — in `SELECT department, COUNT(id) … GROUP BY department`, the
`department → department` edge was typed `AGGREGATED`.

**Resolved by AT-D2 (2026-09-01).** `_classify_transformation` now evaluates aggregation
per-column-expression rather than per-statement, and WHERE-clause presence no longer overrides a
SELECT-list column's own classification at all — the two facts are recorded independently. A
column referenced only in a WHERE clause now gets its own `FILTERED` evidence edge (targeting the
reserved `FILTER_EVIDENCE_TARGET_COLUMN` marker) instead of being silently dropped —
`target/02`'s `INFLUENCES` naming was not adopted verbatim, but the shape (a distinct, real edge
kind for filter-only evidence, not silence) is the same fix it was asking for. See
`tests/test_sql_lineage_parser.py::test_where_clause_does_not_override_a_selected_columns_own_classification`,
`::test_filter_only_column_produces_filtered_evidence_not_silence`,
`::test_union_branch_where_clause_does_not_leak_into_sibling_branch`, and
`::test_aggregation_does_not_mark_a_sibling_non_aggregated_column`.

**e. `parse_procedure_lineage` is `parse_view_lineage` with a different docstring.** Both call
`_parse_sql`. There is no per-dialect statement splitter, no temp-table or table-variable
resolution, no control-flow branch union, and — critically — **no dynamic-SQL detection**:
`EXEC(@sql)`, `sp_executesql` and `EXECUTE IMMEDIATE` appear nowhere in the module. A T-SQL
`CREATE PROCEDURE` body will typically fail `sqlglot.parse`, return `Confidence.LOW` with an
empty edge list, and be persisted as *nothing* by the endpoint, which only iterates
`result.edges`. `target/02` §4's degradation table — every row of it — is unimplemented.

Two further defects worth naming because they will bite in a demo: `view_lineage_edge` and
`procedure_lineage_edge` have **no unique constraint** and the endpoint does a bare
`session.add` per edge, so re-parsing the same view doubles the graph; and the endpoint never
resolves `source_table_id`/`target_table_id`, so the edges are text-only and cannot participate
in any traversal even once L1 lands.

> **Change.** `Docs/review-2026-08/gap/02-gap-diff-and-plan.md` §4, replace the `N2` and `N3`
> rows:
>
> | # | Item | Weeks | Risk | Why it matters |
> |---|---|---|---|---|
> | N2 | **View DDL parsing → column-level lineage** — *first cut landed 2026-08-30 (`sql_lineage_parser.py`, `view_lineage_api.py`) and is **not** the item closed.* Remaining: `SELECT *` recorded as an expansion against a schema version; `UNRESOLVED` nodes with reasons instead of the `<UNKNOWN>` string; per-projection (not per-statement) transformation classification, with filter-only columns emitted as `INFLUENCES`; catalog matching to populate `*_table_id`/`*_column_id`; a uniqueness key so re-parsing is idempotent; feed from harvested envelope v1.1 definitions rather than a caller-supplied SQL body | 3 | Low | The parser exists; what is missing is everything that makes it honest |
> | N3 | **Procedure body parsing (T-SQL, PL/SQL first)** — *not started. `parse_procedure_lineage` currently delegates to the view parser and has no procedural handling at all.* Needs the per-dialect statement splitter, temp/table-variable scope resolution, branch union, and the dynamic-SQL `UNRESOLVED` path from `target/02` §4 | 8–10 | **High** | Uncontested in the market; genuinely hard; degrade explicitly rather than silently |
>
> And in §4, immediately after `N3`, add:
>
> | N20 | **One unified graph** — merge gateway query lineage, view, procedure, BI, AI-decision and consumption edges into `unified_lineage_api.py` behind the existing `UnifiedLink`, each carrying its derivation method | 2 | Low | Atlan's whole lineage pitch is four methods, one canvas (`image35`–`image38`). We have ten producers and four of them are on the canvas |
> | N21 | **Classification propagation along lineage** — derived, raise-only, provisional, with the propagation path as evidence. No write-back to the source system | 3 | Medium | The one governance capability `image39` shows that we have no answer to. See `atlan-context/03-lineage.md` §6 |

> **Change.** `Docs/10-architecture/01-principles-and-invariants.md`, INV-9, append to
> **Statement**:
>
> "This applies to lineage parsers per dialect and per construct. A parser that cannot resolve a
> construct — `SELECT *` against an unknown schema, dynamic SQL, an unsupported procedural
> dialect — must emit an `UNRESOLVED` node carrying the reason, and must not return an empty
> edge set that is indistinguishable from an asset with no upstreams. Advertised lineage
> coverage per source is derived from the certification corpus (`E12`), never hand-declared."

---

## 5. A visual builder and a custom-lineage API

`image38` is the clearest screenshot in the set about what Atlan actually offers here: a REST
tab with `POST /api/lineage/edges` carrying `{"from": "orders_raw/amount", "to":
"revenue_agg/net_revenue", "type": "TRANSFORM"}`, a `201 Created · Edge registered` receipt, a
CSV Import tab beside it, and — per the caption — "a visual Lineage Builder for manual
modeling."

**Should a human-drawn edge exist? Yes.** In a bank there are real edges no parser will ever
find: a mainframe extract landed by a nightly `FTP` + `SQL*Loader`, a vendor feed reconciled by
hand, an Excel-mediated adjustment that a controller performs monthly. `target/02` §6 already
has the object — `lineage_manual`, *"a human-asserted edge, with justification and author. Ranks
above all inferred methods and survives re-extraction."* Build the API and the CSV import.

**Should it be a free-drawing canvas? No — and this is the part to decline.** A canvas where a
steward drags a line invites edges drawn from belief rather than knowledge, at exactly the
moment nobody is asking for a justification. The `201 Created` in `image38` is the tell: the
response is an edge id, with nothing in the receipt distinguishing it from a parsed one. What
we should build instead is the *edit* action already specified in `target/02` §6 step 4 —
correcting an inferred edge's endpoints or transformation class, in the review queue, with the
original inference retained as evidence — plus a bulk CSV path for the mainframe/vendor cases,
which are enumerable and can be justified per row.

**How provenance survives.** This is `C9`, and `C9` is what makes the manual edge safe:

- The edge carries `method = HUMAN_ASSERTED` in the same field a parsed edge carries
  `VIEW_DDL` or `OPENLINEAGE`. **Not a confidence number** — a manual edge has confidence 1.0 in
  the asserter's belief and 0.0 in any parser's evidence, and one float cannot say that.
- `lineage_evidence` for a manual edge holds the author, the justification text, the approval
  record from the single review queue, and — where the edge replaced an inference — the
  superseded inference.
- **Re-extraction never silently overwrites it, and never silently agrees with it.** If a later
  parse produces the same edge, the edge gains a second evidence row and its method becomes a
  set: an edge asserted by a human *and* confirmed by a view parse is a stronger claim than
  either alone, and the graph should be able to say so. If a later parse *contradicts* it, that
  is a conflict for the review queue, not a write.
- Every read surface renders the method. An agent asking `traverse_lineage` gets the method back
  in the payload; the wiki cites it; the answer contract's provenance block (§7) carries it. A
  hand-drawn edge that reaches an auditor looking like a parsed one is the single worst outcome
  available here, and it is prevented by making the method a required field rather than by
  discipline.

> **Change.** `Docs/30-contracts/06-lineage-contract.md` §1, replace the `edge` object:
>
> ```json
> "edge": {
>   "id": "edge_...",
>   "kind": "QUERY | VIEW | PROCEDURE | ETL | DBT | BI | AI_DECISION",
>   "method": "DECLARED | VIEW_DDL | EXECUTED_QUERY | QUERY_LOG | PROCEDURE_BODY | SIMILARITY | HUMAN_ASSERTED",
>   "from_node": "lin_...",
>   "to_node": "lin_...",
>   "relation": "FLOWS_TO | INFLUENCES | REFRESHES",
>   "transformation": "DIRECT | DERIVED | AGGREGATED | FILTERED",
>   "confidence": 1.0,
>   "status": "PROPOSED | ACCEPTED | REJECTED | SUPERSEDED",
>   "evidence_ref": ["ev_..."],
>   "graph_version": 44,
>   "observed_at": "2026-08-28T10:00:00Z"
> }
> ```
>
> and replace the field table's `confidence` row with two rows:
>
> | Field | Notes |
> |---|---|
> | `method` | **Required.** How this edge was derived. Policy keys on the method, not on the confidence float — an edge asserted by a human and an edge inferred from column-name similarity may both be low-evidence, and must not be treated alike. `HUMAN_ASSERTED` edges survive re-extraction; a contradicting parse raises a review conflict rather than overwriting |
> | `confidence` | Within a method, not across methods. Never the sole input to a publish decision |
> | `relation` | `INFLUENCES` is a filter/join dependency: it changes which rows appear but no value flows along it. Impact analysis traverses it; provenance of a *number* does not |

---

## 6. Governance propagation — build it, one-directional, and refuse the sync

`image39` panel 2 states it in full: *"Tag a column as PII once — lineage propagates that
classification to every downstream asset and syncs bi-directionally with Snowflake and
Databricks."* The screenshot above it shows the mechanism generically for quality — `RAW_SALES`
with "12 rules failed", `REVENUE_AGG` and `CFO_DASHBOARD` both marked "Affected upstream", and a
**propagation log** narrating each hop: *"Downstream impact detected: revenue_agg depends on
raw_sales via column lineage."*

**Where we are.** Nothing propagates. `grep -rn "propagat" src/aida` returns two hits, both
about Python exceptions. Note though that `ui-next/src/components/PropagationLog.tsx` — written
tonight, fixture-driven — already renders exactly `image39`'s log, with a deliberate design
choice we should keep: each hop states the *mechanism* that carried it ("via column lineage"),
because "affected" is a claim and "affected via column lineage from raw_sales" is an argument.
**The UI is ahead of the backend here, which is a claim risk under INV-9 until `N21` lands.**

**The argument against propagating is stronger for us than for Atlan, and it still loses.**
For Atlan a wrong propagated PII tag mislabels an asset. For us it is an *enforcement input*:
`abac.py` (does not exist any more; `aida.policy_engine` is the live equivalent, PG-1/AU-11
2026-08-31) gated resource conditions on `classification` and `sensitivity`, so a propagated tag
changes who can query what. The brief's framing — "a propagated PII tag that is wrong is worse
than no tag" — is right about the *authoritative* label and wrong about the derived one, and the
distinction is the whole design:

1. **Propagate as a derived attribute, stored separately from the asserted one.** An asset has
   `classification_asserted` (a steward said so) and `classification_derived` (it inherits from
   an upstream, along this path, at this graph version). They are never merged into one column.
2. **Raise-only.** Propagation can add a restriction; it can never remove or downgrade one. The
   failure mode of over-restriction is a steward filing a request; the failure mode of
   under-restriction is a PII disclosure. Under ADR-0016's fail-closed posture this is the same
   direction quality already travels.
3. **The path is the evidence.** Every derived classification stores the edge chain that carried
   it and the graph version it was computed against. When the chain changes, the derivation is
   recomputed; when an edge in it is rejected, the derivation is withdrawn. This is why L1 and
   `C9` are prerequisites: propagation over a graph whose edges do not know their own derivation
   method will carry PII along a similarity-inferred edge with confidence 0.4, which is
   precisely the wrong outcome. **Propagate along `method ∈ {DECLARED, VIEW_DDL, EXECUTED_QUERY,
   OPENLINEAGE}` only; queue everything else for a human.**
4. **Derived never becomes asserted without a steward.** The derived tag enforces immediately —
   because a possible PII leak is not something to hold in a queue — but the asset's
   authoritative classification changes only through the existing review queue, and the review
   surface shows the propagation path as its justification.
5. **`INFLUENCES` edges do not carry classification.** A column used only in a `WHERE` clause
   does not put its values into the downstream asset. This is the second reason §4(d) matters: a
   parser that types every edge in a filtered view as `FILTERED` will, once propagation exists,
   either propagate along nothing or propagate along everything, depending on how the rule
   reads.

**Refuse the bi-directional sync.** Writing our derived classifications back into Snowflake and
Databricks tags, as `image39` claims, would make an inference authoritative in a system whose
own governance is audited independently, and would let a parser bug become a warehouse-level
access change with no maker–checker in the path. Read their tags in as `DECLARED` evidence;
never write ours out. This is the same argument `research/02-atlan.md` §7a makes about policy
push-down, pointed the other way, and it is worth stating explicitly because it is the one place
we are deliberately shipping *less* than the screenshot.

---

## 7. Impact analysis: theirs is a workflow, ours is an endpoint

`image39` panel 3: *"Before a data engineer ships a change, Atlan shows the full blast radius
inside the GitHub or GitLab pull request — downstream dashboards, pipelines, AI agents, data
products."*

Two things are true at once. **They are ahead on delivery**: the blast radius arrives where the
change is being made, unasked, at the moment it can still be cheaply reversed. Ours is a `GET`
that someone has to think to call. That is a product gap, and it is the cheapest meaningful one
in this review. **And their claim about the affected set is one we can beat**: `image39` lists
"AI agents" among the affected, but an agent in Atlan is a consumer of the graph, not a
registered object with a version and a published status. Our impact report already returns
`tools` with `version` and `status: PUBLISHED` (`30-contracts/06` §5) because tools are
registry objects generated from the estate. "This column change breaks tool `tool_revenue_by_state`
v3, which is published and bound to two agents" is a sentence they cannot currently produce.

**Do not build a GitHub app.** A bank's transformation logic does not arrive predominantly by
pull request; it arrives as a change request against a stored procedure and a DBA ticket. The
right shape for us is the one already written in `target/02` §7 — *"a change to a column with
high downstream impact raises the review requirement automatically"* — plus an impact report
rendered into the change record itself. The delivery mechanism is our review queue, not a
third-party CI hook; the CI hook is a later, small addition once the report exists.

---

## 8. Tracing an AI answer back to source data — we are ahead, and silent about it

The FAQ heading *"How does Atlan help trace AI answers back to source data?"* (line 129) is one
of five headings left unanswered in the captured text; the only thing the screenshots show
against it is `image34`, where the lineage agent walks backwards from "CFO Dashboard revenue
fell 12.3%" through `REVENUE_AGG` → `revenue_model` → `ORDERS_RAW + CUSTOMERS` and lands on a
root cause: *"`amount` renamed → `net_amount` in ORDERS_RAW on Jan 8 — JOIN silently returned
nulls."* That is impressive, and it is an *investigation*, not a trace. It answers "why is the
number wrong", not "what produced this number".

We answer the second question better than they do, structurally:

- `interpretation` before the number (`30-contracts/09`, line 46) — the user can reject the
  question before reading the answer. `K9` in the gap plan is right that this is small, unusual
  and correct.
- Versions pinned per answer: `semantic`, `policy`, `prompt_risk_classifier`.
- `ai_decision_lineage.py` records retrieval selections **and rejections**, tool selection, and
  refusals with the control that fired. Nothing in `image34` or the FAQ suggests Atlan records
  what the agent declined to consider.
- Every execution carries output-to-source column mappings (`extract_column_lineage`).

**The weakness is one field.** The answer contract's provenance block is:

```json
"lineage": {"tables": ["transaction_fact", "customer_dim"], "metrics": ["total_revenue"]}
```

Table names and metric names, with no columns, no derivation methods, and no pinned graph
version — so a number produced today cannot be re-traced against the graph as it stood today.
For BCBS 239 that is the difference between an audit answer and an anecdote, and it is
inconsistent with `target/02` §6 step 7 ("agents read a pinned version").

> **Change.** `Docs/30-contracts/09-runtime-request-and-audit-contracts.md`, in the completed
> response example, replace the `lineage` line with:
>
> ```json
> "provenance": {
>   "graph_version": 44,
>   "tables": ["transaction_fact", "customer_dim"],
>   "metrics": [{"id": "met_1", "version": 4}],
>   "columns": [
>     {"output": "revenue", "sources": ["transaction_fact.amount_net"], "methods": ["VIEW_DDL", "EXECUTED_QUERY"]}
>   ],
>   "unresolved": []
> }
> ```
>
> and add after the `interpretation` paragraph:
>
> "**`provenance` is pinned, not live.** It names the lineage graph version the answer was
> produced against, so the chain can be reconstructed exactly as it stood at answer time even
> after the graph is republished. `methods` states how each column edge in the chain was
> derived; `unresolved` names any point in the chain where a parser could not resolve an
> upstream, so a partial trace is legible as partial rather than as complete."

**And say it outside the contract.** No customer-facing document makes the trace-back claim.
`00-product/05-differentiation-and-whitespace.md` describes the evidence record (line 71) but
frames it as an audit artefact, not as an answer to the question Atlan poses and does not
answer. One paragraph in `00-product/01-vision-and-goals.md` — "every number this platform
produces can be walked back to the source columns that produced it, at the graph version it was
produced against, including the points where the walk is incomplete" — is worth more than most
of the build items on this page, and costs an afternoon. It is also only sayable once §7's
`provenance` block is real, so the two ship together.

---

## 9. What we should deliberately decline

| Decline | Why |
|---|---|
| **Warehouse query-history mining** (`image35`'s "millions of queries") | Rank 4 evidence, needs contested privileges, produces pipeline-vs-ad-hoc noise, and the same coverage is available at rank 2 through envelope-harvested view DDL. Revisit only after `N2` is honest |
| **A free-drawing visual lineage builder** (`image38`) | Invites belief-shaped edges with no justification at the moment of drawing. Ship the REST/CSV path and the review-queue *edit* action instead — both carry an author and a reason |
| **Bi-directional classification sync into Snowflake/Databricks** (`image39`) | Makes our inference authoritative inside an independently-audited system, with no maker–checker in the path. Read their tags in; never write ours out |
| **Schema-history root-cause agent** (`image34`) | Defer, not refuse. It is genuinely good and we should build it — but it is unbuildable before L1 (one graph) and versioned publishes, and building it early would mean building it over five disconnected stores |
| **Connector breadth as a lineage strategy** (`image32`, 104 connectors) | Already decided in `research/02-atlan.md` §10. Depth on the bank's five systems, `OpenLineage` for the rest |

---

## 10. Cost and sequencing

Everything below lands in **Phase 2 — Understanding**, which already contains `N2`, `N3`, `N4`
and `C9`. The ordering constraint is that `C9` (derivation method on the edge) must land before
`N20` (unification) and before `N21` (propagation), because both consume the method.

| Order | Item | Weeks | Note |
|---|---|---|---|
| 1 | `C9` — derivation method on every edge, incl. retrofitting `view_lineage_edge` / `procedure_lineage_edge` | 1 | Unchanged estimate; the retrofit is why it must go first |
| 2 | §8 answer-contract `provenance` + the vision-doc paragraph | 0.5 | Independent of everything else. Do it now |
| 3 | `N2` hardening — `SELECT *`, `UNRESOLVED`, per-projection classification, catalog matching, idempotency, envelope feed | 3 | Was carried as done; it is not |
| 4 | Gateway query lineage persisted as edges | 1 | Folds into `N20` |
| 5 | `N20` — one unified graph, all ten producers | 2 | New |
| 6 | `N4` — proposal/review/negative-knowledge workflow, incl. the manual-edge API and CSV import | 5 | Unchanged. `negative_knowledge.py` already provides `record_negative`, `check_re_proposal` and `auto_lift_on_material_change`, so §6 of `target/02` is largely wiring, not new mechanism |
| 7 | `N21` — classification propagation, raise-only, derived-vs-asserted | 3 | New |
| 8 | Impact-as-review-gate (§7) | 2 | Inside `N4`'s scope |
| 9 | `N3` — procedure parsing for real | 8–10 | Unchanged, still the largest single item and still the most defensible |
| 10 | `E12` — certification corpus per dialect | 3 | Unchanged. Without it, INV-9's extension to parsers is unverifiable |

Net new against the plan as written: **+6 weeks** (`N20` 2, `N21` 3, provenance 0.5, gateway
persistence 1, minus overlap), plus the 3 weeks of `N2` that were counted as spent and are not.

---

## Related documents

- `../research/02-atlan.md` §5 — the docs-level lineage teardown this corrects in three places
- `../target/02-lineage-inference-review.md` — the design all of the above is measured against
- `../gap/02-gap-diff-and-plan.md` §3 `C9`, §4 `N2`/`N3`/`N4` — the items changed here
- `../../20-modules/09-lineage.md`, `../../30-contracts/06-lineage-contract.md`
- `../../10-architecture/01-principles-and-invariants.md` INV-9
