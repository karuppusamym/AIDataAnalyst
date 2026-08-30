# Atlan screen-capture review — decisions

**Source.** `Docs/Atlan-context.docx`, a 43-screen capture of Atlan's live product UI
(Context Engineering Studio, Context Bootstrapping, Context Agents, Data Lineage, Context
Lakehouse), captured by hand 2026-08-30. Extracted text and images: `scratch/atlan-media/`
(git-ignored). Four independent reviews, one per section, are the working papers:

| # | Section | Screens | Working paper |
|---|---|---|---|
| 1 | Context Engineering Studio, bootstrapping | 1–17 | `01-context-studio.md` |
| 2 | Context Agents | 18–28 | `02-context-agents.md` |
| 3 | "Flying blind", Data Lineage | 29–39 | `03-lineage.md` |
| 4 | Context Lakehouse | 40–43 | `04-context-lakehouse.md` |

This file is the decision layer. Where a working paper argues, this file rules. Tracker rows
land in `60-delivery/03-tracker.md` §L, continuing the `AT-` series that the earlier
`Atlan_Concept_Deep_Dive.docx` review opened.

**Why the UI capture beat the marketing pages.** The earlier review read Atlan's product
pages. This one reads their screens, and a screen shows the object model: `revenue.yml v3.1.2`
with `approved_by`, `consumed_by: 6 agents`, a `tests/` directory, a rollout at 68 %, a
rollback commit. Four claims in the earlier review were too strong or too weak against what
the screens actually show; §6 lists them.

---

## 1. The one finding that outranks the rest

**We cannot reconstruct what a model saw.** Classification, certification, quality, business
assignment, glossary, semantic/metric/tool/policy versions are all point-in-time
reconstructable. But `MetadataBusinessAnnotation` is unique-on-`table_id`, mutated in place,
with no history table; and `AgentRun.retrieval_evidence` records *which* objects were
retrieved and why, never *what they said*. So for a bank asking "why did the model answer
that, in March" the honest answer today is: we can name the objects, not the content.

Atlan states the requirement better than our own documents do — *"when an agent makes a
mistake, you need to reconstruct exactly what context it saw and when. Without it, you can
observe the output but never explain the reasoning."* That is the correct standard, and it is
the one place in 43 screens where they are ahead of us on something a regulator will ask
about.

It is also the item with the shortest fuse. **You cannot backfill history you never kept**, so
every week without it is a permanently unauditable week. And N10 (knowledge compilation)
multiplies the number of unversioned fragments an agent reads, so building receipts *after*
N10 means retrofitting a hash boundary through a compiler. **AT-6 is P0 and is not tradeable
against anything else in this list.**

---

## 2. Adopt

| ID | Decision | Why | Est |
|---|---|---|---|
| AT-6 | **Context receipts** — hash every grounding fragment at assembly, store the digest set on the run; plus a history table for `MetadataBusinessAnnotation` | §1. Point-in-time reconstruction of model input | 3.5 w |
| AT-7 | **Version support window + consumer bindings** for context products | Publishing v(n+1) currently flips v(n) to `SUPERSEDED` in the same transaction while every read path filters `PUBLISHED`, so a version-pinned MCP URI pins nothing and the holder of v2 gets the anti-enumeration "not found" the instant a steward approves v3 | 0.5 w fix + 3 w registry |
| AT-8 | **Context-path evals** — a regression suite that asserts on the resolved context path (objects, versions, tool/plan selected, policy decision), not on business values | The eval stage is right and Phase 3 is too late. Asserting on values would put regulated figures in the control plane (INV-6 / ADR-0014) and go stale on every restatement; the path is replayable and does not | 2 w, pull to Phase 2 |
| AT-9 | **Scope-aware definitions** — term and metric uniqueness per business node, most-specific-wins resolution, and a refusal carrying *both* definitions and *both* owners when the asking context does not disambiguate | A bank cannot agree one definition of exposure, balance, default or customer, and several of those differences are mandated. Today `UniqueConstraint(organization_id, term_key)` forces one. Module 08 treats two definitions as a conflict to resolve — right for disagreement, wrong for legitimate difference | 3–4 w |
| AT-10 | **One canonical lineage graph** — join the orphaned edge producers (gateway query lineage, view edges, procedure edges, BI edges, AI-decision edges, consumption edges) to the unified graph and to impact analysis | `LN-9` was closed as "one canonical graph"; three new edge producers shipped afterwards without joining it. The claim is currently false | 2 w |
| AT-11 | **Classification propagation** — `classification_derived` stored separately from `classification_asserted`, raise-only, propagating only along `DECLARED`/`VIEW_DDL`/`EXECUTED_QUERY`/`OPENLINEAGE` methods and never along `INFLUENCES`, with the edge chain and graph version as evidence | For us this is an *enforcement input* (`abac.py` gates on classification), not a label, which is exactly why it must be separated and reviewable rather than merged | 3 w |
| AT-12 | **Semantic mining of warehouse query history** — jargon, real business questions, usage ranking. Explicitly *not* a lineage source (see §4) | The single most-cited source of quality in their customer quotes. Our gateway corpus is empty at cold start; the warehouse's own history is rich on day one. Extends AT-5 from a ranking worklist to a meaning source | 3 w |
| AT-13 | **`get_asset_context`** — one composite MCP call returning certification, quality, classification, lineage depth and owner, plus a **server-computed `usage_decision`** | The composite shape is right for governance reasons — one policy evaluation, one audit record, one correlation id. The server-computed decision is where we diverge deliberately: their transcript shows the *model* concluding "safe to use… ensure your pipeline respects that policy", which is a model acting as policy oracle and delegating enforcement to the caller | 2 w |
| AT-14 | **Sampling-based bulk review** for drafted prose — seeded, reproducible, with the seed and drawn member ids in the audit record; language fields only | Their cold-start speed comes from targeting, batch-as-unit and acceptance sampling, none of which requires model output to become authoritative. Our 0.70 cap is not what costs us speed; our review queue's unit of work is | 2 w |
| AT-15 | **Evidence by signal source** in the review UI | We capture the evidence and show a steward a bare confidence number, which module 06 already concedes is unreviewable. This is what makes AT-14 tractable | 1 w |
| AT-16 | **Answer-contract provenance block** — columns, derivation methods and the pinned graph version, not just table and metric names | For BCBS 239 that is the difference between an audit answer and an anecdote | 0.5 w |
| AT-17 | **Metric-formula collision detection** | We detect glossary synonym collisions but not two metrics computing the same thing differently, which is the collision that actually corrupts an answer | 2 w |
| AT-18 | **Context export + contractual exit** — Parquet with a published schema, a manifest, and an exit clause in the contract; plus a documented read-only reporting schema on a replica | Answers the procurement question Atlan aims at a bank's vendor-lock-in reviewer, without putting a second read path on the authoritative store. Amends AT-2 | 2.5 w |

Total: roughly 26 engineer-weeks, of which AT-6, AT-7's fix and AT-16 are the urgent 4.5.

---

## 3. Correct — our own documents are wrong

| ID | Correction |
|---|---|
| AT-C1 | `10-architecture/10-performance-and-scale-model.md` publishes **2 s p95** for graph neighbourhood traversal. ADR-0020 measured **10.8 ms p50 for 12 hops over 880,000 column-level edges** on PostgreSQL. Atlan advertises "under 100 ms". We are faster than both and our own spec sheet says we are 20× slower. Restate with the measurement and its provenance attached |
| AT-C2 | `target/05-target-architecture.md` §7 states a blanket "no write-back" non-goal that the platform already breaks — consumption edges are written on every read, `metadata_enrichment_proposal` is a model-output write path by design, and MCP-2 shipped `request_data_product_access` with `writePosture: MAKER_CHECKER_REQUEST_ONLY`. A prohibition we violate is worse than a narrow rule that holds. Replace with the three-lane rule in §5 |
| AT-C3 | `01-principles-and-invariants.md` INV-6 should record *why* the value-free constraint is affordable: eight of the nine Atlan context agents visible in the capture consume only identifiers, lineage, SQL text, dbt logic and prior human prose. Their quality does not come from reading values. The opposite belief is how an invariant gets quietly relaxed |
| AT-C4 | INV-9's honest-capability rule must extend to lineage parsers explicitly, and one live breach fixed: `query_history=True` is advertised on the Snowflake connector and nothing consumes it |

---

## 4. The one place the reviewers disagreed

Reviewer 2 wants warehouse query-history mining; reviewer 3 declines it. Both are right about
different uses, and the split is the decision:

- **Adopt it as a meaning and usage signal** (AT-12). Jargon that no dictionary carries, the
  questions users actually ask, and which 2–5 % of the estate anyone touches. Redaction does
  not block this: we lose filter *values*, not filter *columns*, and popularity ranking loses
  nothing at all.
- **Decline it as an authoritative lineage source.** Query logs rank fourth (0.85) behind
  declared constraints and view DDL (0.95). Envelope-harvested view DDL is the real answer to
  lineage coverage, and it is already built. A query-log-derived edge may be a *candidate*
  into the review queue and never an asserted edge.

Per INV-9, we do not write "we mine query history" in any customer-facing material until
AT-12 ships.

---

## 5. Decline — and be able to defend

| Declined | The defence |
|---|---|
| **The git-shaped Context Repo as the authoritative primitive** | A merge becomes a publish, which our own studio rules forbid, and a YAML blob has no rows for per-read ABAC to evaluate — the exact criticism we make of everyone else. We keep versioned rows and compile *to* their formats |
| **Auto-applied model-authored fixes; auto-certification** | Their screens show "8 fixes auto-applied" and one-click apply at 92 % confidence: the model writes the test, fails it, and writes the fix. Self-certification. Our 0.70/0.95 caps already make it impossible, and a quality score may gate runtime but may not certify (INV-8) |
| **Blind A/B splits of a definition across live agents** | A BCBS 239 consistency failure. Staged rollout over *named* bindings is fine, because our answer states its version |
| **Iceberg as the live store, and external engines against the authoritative store** | Wrong shape, not merely wrong size: the workload is transactional read-modify-write under maker-checker with row locks and partial unique indexes. A metadata estate is hundreds of GB, not petabytes. Trino or Spark on the authoritative store is a second read path outside policy-before-ranking. AT-18 serves the real need |
| **Free-drawing visual lineage builder** | Ship the REST/CSV manual-edge path and a review-queue edit action instead, both carrying author and justification, with `method = HUMAN_ASSERTED` so a hand-drawn edge can never read as a parsed one |
| **Bi-directional classification sync into Snowflake/Databricks** | It would make our inference authoritative inside an independently-audited system with no maker–checker in the path |
| **"Billions of assets" as a positioning claim** | Two orders of magnitude beyond any real estate, and searching a billion assets means you did not filter by entitlement first. Ours: *we search the few thousand you are entitled to see, exactly, and refuse with a reason code rather than score an arbitrary slice* |
| **The named agent fleet as an architecture** | It is a DAG with mascots; their own foundational → derived → compounded staging is just the dependency order. ADR-0002 settled this |

### The three-lane write-back rule (replaces the "no write-back" non-goal)

1. **Measured facts** — profiling, quality, freshness, row counts. Deterministic program
   output. Written directly. Already done.
2. **Platform observations** — consumption, usage, refusals, drift. Written directly, never
   authoritative for meaning. Mostly collected; `ai_decision_lineage` records *rejections*,
   which nobody else keeps, and is currently unused.
3. **Model judgements** — descriptions, business names, inferred relationships. Proposal lane
   only: inert, typed, capped at 0.70, maker ≠ checker.

The hazard neither vendor has named: write-back is a feedback channel into future model
input, so lane 3 must route through ingestion-time prompt-risk screening (N18). That is a
model-risk control, not a nicety, and it is a differentiator worth stating.

---

## 6. Defects this review surfaced

These are not gaps in the plan. They are things that are wrong in code that has shipped.

| ID | Defect | Severity |
|---|---|---|
| AT-D1 | Publishing a context-product version silently breaks every pinned consumer (see AT-7) | P0 |
| AT-D2 | `sql_lineage_parser.py`: `FILTERED` and `AGGREGATED` are assigned per-*statement*, so `SELECT col_a FROM t WHERE col_a > 0` types the value edge `FILTERED` — and a test asserts this. `SELECT *` is dropped with a bare `continue`, so a star view produces zero edges, indistinguishable from a view with no upstreams. `"<UNKNOWN>"` is written as a magic string. `Confidence.FULL` is hard-coded, so a procedure parse and a view parse store identically. No unique constraint, so re-parsing doubles the graph. `source_table_id`/`target_table_id` are never populated, so the edges cannot be traversed | P0 |
| AT-D3 | INV-9 breach: `query_history=True` advertised, nothing consumes it | P1 |
| AT-D4 | `PropagationLog.tsx` renders a classification-propagation mechanism that does not exist in the backend | P1 |
| AT-D5 | `parse_procedure_lineage` is `_parse_sql` with a different docstring — no dynamic-SQL detection at all. N3 is not started; the plan counts it as in progress | P1 |

---

## 7. Where we are simply fine

Worth writing down, because the temptation after a competitive review is to treat every
difference as a deficit.

- **Multi-runtime consumption.** Our compiler emits seven targets with a stable artifact hash
  and a drift report. "Any framework can parse the YAML" is weaker, and we undersell ours.
- **Refusals.** Every one of the 43 screens shows a trace of an answer that was given; none
  shows one that was declined and the control that declined it. We record refusals, and a
  refusal is a free regression test.
- **Tracing an answer to source.** `interpretation` before the number, pinned versions, and
  recorded rejections answer a question their own FAQ raises and leaves unanswered — subject
  to AT-16.
- **Graph traversal performance**, subject to AT-C1.
- **Change sets, diffs, impact preview, test gate, bootstrapping, OpenLineage ingestion** —
  all already delivered.

One observation worth keeping for a customer conversation: screen 1 shows
`last_edited_by: "ai + @jsmith"` beside `approved_by: "@jsmith"` on the same file. The editor
approves their own edit, and machine and human authorship are fused into a single unqueryable
string. Our maker ≠ checker rule and typed provenance exist precisely so that neither is
possible.
