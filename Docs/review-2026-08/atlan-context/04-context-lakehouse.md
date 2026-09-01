# Atlan Context Lakehouse — architectural review

> Source material: `atlan-context.txt` lines 133–231 and images `image40`–`image43`,
> captured from atlan.com by the owner, August 2026.
> Scope: the Context Lakehouse only. Context Engineering Studio, Context Agents and
> Data Lineage are other reviewers' sections.
> Prior coverage checked: `review-2026-08/research/02-atlan.md` §"Context Lakehouse"
> (lines 35–45, 120, 150, 194, 208) and `research/04-cross-vendor-synthesis.md` lines
> 30, 150 already record *that* the Iceberg story exists and *that* it does not
> reconcile with Atlan's documented Cassandra/Elasticsearch/Atlas-fork backend. This
> document does not repeat that. It takes the argument seriously on its merits and
> asks what of it we should build.

---

## 1. Findings

Verdict vocabulary: **Covered** · **Gap — close** · **Gap — decline** · **Wrong**.

| # | Their claim | Where | Our position | Verdict | Cost |
|---|---|---|---|---|---|
| **Storage substrate** |||||
| S1 | Metadata stored in Apache Iceberg; ACID, schema evolution | text 215, 227; `image40` "Iceberg Native Metadata Store" | PostgreSQL is authoritative with ACID and Alembic-versioned schema (ADR-0003, 34 revisions). Iceberg adds nothing to a transactional, approval-governed store of this size | **Gap — decline** | 0 |
| S2 | "Your context is your IP — you can query it independently of us" | text 227 | Partially answered. Context compiler emits 7 open targets per *product*; there is no **estate-wide** export, no documented export schema, no exit clause | **Gap — close** | 3w + object-store prerequisite |
| S3 | Standard SQL over the metadata from any engine (Spark/Trino/DuckDB/Snowflake) | text 169, 227 | The *use case* (auditors and analysts running SQL over lineage/policy/history) is real and we serve it worse today. The *mechanism* (external engines against our store) we should refuse | **Gap — close** (reporting views), **decline** (external engines) | 1.5w |
| S4 | Time travel — every historical state queryable, point-in-time reconstruction of what an agent saw | text 143, 219, 224; snapshot timeline text 174–211 | Six of eight context classes are already point-in-time reconstructable. Two are not, and nothing *binds* a run to the content it read | **Gap — close** | 3.5w |
| S5 | "Context is big data" — needs a lake | text 138–139 | Wrong for a metadata estate. Our own ceiling is 500M lineage edges and 25M audit events/day — partitioning, not a lakehouse. The *write-rate* observation underneath it is correct | **Wrong** (with a concession) | 0 |
| **Access surface** |||||
| A1 | MCP for governed reads | text 164–165; `image43` | Covered and ahead: per-read policy, purpose ABAC, budgets, immutable consumption evidence, eligible-tools (module 19 §5–§7, `mcp_server.py`) | **Covered** | 0 |
| A2 | `get_asset_context` — certification + quality + classification + lineage depth + owner in one call | text 149–161; `image43` | The *shape* is right and we do not have it. Our nearest surface returns columns, classification and a hardcoded `"business_description": None` (`mcp_server.py` ≈L1506) | **Gap — close** | 2w |
| A3 | The agent then reasons about policy from those facts | text 161 ("Yes — ... ensure your pipeline respects that policy"); `image43` "Agent ready to reason" | Model as policy oracle. Direct INV-3 failure. Return a server-computed decision, not only its inputs | **Wrong** | included in A2 |
| A4 | A2A — agents write observations, quality signals and usage back | text 139, 166–167; `image40` "A2A" | Their bundle contains three different things with three different risk classes. Two we already do; one needs a proposal lane. **Our published non-goal "No write-back" is the thing that is wrong** | **Gap — close** (and correct our own doc) | 4w |
| A5 | REST + Graph APIs, "open by design" | text 170–171; `image40` "Open APIs / SDKs" | REST covered. GraphQL: their own claim is unverified (`research/02-atlan.md` line 120) and we have no requirement for it | **Covered** / **decline** | 0 |
| **Retrieval model** |||||
| R1 | Knowledge graph traversal "at depth in under 100ms" | text 228 | We measure **10.8 ms p50 at 12 hops over 880,000 column-level edges** on PostgreSQL (ADR-0020 §2). We beat the claim. But our *published* p95 target is 2 s (perf model §3) and no p95 has ever been measured | **Covered**, badly documented | 0.5w doc fix |
| R2 | Vector-native search "across billions of assets" | text 217 | No enterprise has billions of assets; our own scale model tops out at ~30M columns. And searching a billion assets means you did not filter by entitlement first (ADR-0019) | **Wrong** | 0 |
| R3 | Every asset stored with its embedding | text 217 | Port and store built (ADR-0019), provider chosen (amendment), **nothing has embedded the corpus yet** and recall@10 is unmeasured | **Gap — already tracked** as `N5`/`RT-1` | tracked |
| R4 | Bidirectional writes mean context improves on every interaction | text 228 | The compounding loop is real and is `usage_factor` in module 12 §5, marked *planned*, `RT-6`. It is measured usage, so it is INV-3-safe | **Gap — close** (cheap) | 1w |

New backlog rows proposed: **N20** (context receipt + annotation history), **N21**
(`get_asset_context`), **N22** (estate context export + exit clause), **N23** (A2A
observation lanes). Total **~11 engineer-weeks**, of which I would insist on N20.

---

## 2. What they are actually claiming

`image40` is the load-bearing picture and it is worth reading carefully, because it
says more than the prose does. Four stacked layers under a "Context Lakehouse" box,
with agent clients above it:

1. **Activation** — SDKs, Open APIs, MCP, A2A, Webhooks & Alerts, App Framework,
   Orchestration Engine, Design System.
2. **Context Intelligence** — Query Parser, Hybrid Search (keyword + semantic),
   Knowledge Graph, Vector Search, **"Your Compute"**, **"Your Models or LLMs"**.
3. **Iceberg Native Metadata Store** — Open & Extensible, Version-Controlled,
   Decentralized Compute, Technical REST Catalog, Time-Travel & Auditable, Object
   Registry, **"Your Lake"**.
4. **Enterprise Ready Foundation** — Single Tenant, Cloud Agnostic, Multi-Region
   Redundancy, RBAC/ABAC, Disaster Recovery, Audit Trails.

The three "Your ..." chips are the actual argument. Atlan is positioning itself as a
control plane over substrate the customer owns — your object store, your query
engine, your models — and Iceberg is how the storage half of that is made credible.
Layer 4 is aimed squarely at a bank's third-party risk questionnaire, and every item
in it is something we also claim (`09-deployment-topology.md`, ADR-0018 ABAC).

`image41`, `image42` and `image43` are the same "living graph" page under three tabs
— Governance Propagation, Impact Analysis, and AI Agent Context. `image43` is the one
that belongs to this section: a `mcp.atlan.query({asset, column, include:[lineage,
quality, policy, owner]})` returning four rows (LINEAGE `orders_raw → revenue_agg →
CFO_Dashboard`, QUALITY `100% · 15/15 DQ checks pass`, POLICY `PII-free · cleared for
use`, OWNER `finance-team · @jsmith`) and closing with **"Context complete · Agent
ready to reason."** That last line is the claim I disagree with most, and §6 is about
why.

The snapshot timeline (text 174–211) is the other concrete artefact: nine entries
across four snapshot ids (`snp_a1b2` → `snp_c4d1`), each attributed to an actor —
`pipeline/dbt`, `lineage-agent`, `dq-agent`, `trust-agent`, `policy-agent` — with a
timestamp and a one-line change. Read it as an audit ledger with a snapshot id
attached to each entry, and it is a good design. Read it as "agents mutating the
catalogue," and it is the thing a bank's model-risk function will refuse.

Separating the argument into three:

**The storage substrate.** Iceberg tables underneath everything: ACID, schema
evolution, time travel, SQL from any compatible engine, "open formats you own"
(text 215, 227).

**The access surface.** Four protocols: MCP for governed agent reads, A2A for
agent-to-agent writeback, SQL over Iceberg, REST/Graph APIs (text 164–171).

**The retrieval model.** A knowledge graph for relationships traversable at depth in
under 100 ms, plus vector-native semantic search across billions of assets, plus
hybrid keyword+semantic search (text 217, 228, `image40` layer 2).

---

## 3. Portability: "your context is your IP"

This is the strongest sentence on the page, and the question I was asked to answer.

**Why it lands.** A vendor-lock-in reviewer at a bank is not asking "can I export?" —
every vendor says yes. They are asking three sharper questions: *in what format, on
what schedule, and can I read it without you?* "It is already in Iceberg on your
object store" answers all three in one breath. It converts an exit plan from a
project into a fact. That is genuinely good positioning and we should not pretend
otherwise.

**Is "you can query our Postgres and we will export everything" a sufficient answer?**
No, and for a specific reason that has nothing to do with Iceberg.

"We will export everything" is a *promise*. Iceberg-native is a *state*. A third-party
risk reviewer discounts promises, and rightly — an export path that has never been
run against a full estate, whose schema is undocumented, and which is not in the
contract, is not an exit plan. The gap is not the format. **The gap is that our
portability story is unbuilt, unwritten, undrilled and uncontracted.**

What we actually have today:

- `context_compiler.py` compiles an approved context product to **MCP, REST, YAML,
  OSI, ODCS, Snowflake Semantic View, Databricks Metric View** with a stable artifact
  hash and a drift report (module 19, CP-5). This is real, and for the *semantic*
  layer it is a better portability story than Atlan's, because ODCS and OSI are
  standards a consumer already parses, whereas an Iceberg table of Atlan's internal
  typedefs is portable bytes in a proprietary shape. Worth saying out loud: **format
  openness is not schema openness.** A customer who exits Atlan gets Iceberg files
  full of `Referenceable`/`Asset` typedefs from an Atlas fork; they own the bytes and
  still need Atlan's model to read them.
- Everything else — catalogue, lineage, classification history, certifications,
  quality observations, audit — has no export at all. And `06-data-architecture.md`
  §1 records that **object storage is not wired**: MinIO is in `compose.yaml` but
  there is no `boto3` or `minio` dependency and nothing in `src/` reads or writes it.
  Any export lands on that prerequisite first.

**Is Iceberg worth it for a metadata estate that is gigabytes?** No, and the size
argument is not even the main one.

Our published ceiling (`10-performance-and-scale-model.md` §2, `06-data-architecture.md`
§7): 100M–500M lineage edges, 30M columns, 25M audit events/day. Call it low hundreds
of GB. That is not a lake. But set size aside — Iceberg is wrong here for shape:

- The workload is **transactional read-modify-write under approval workflow**:
  maker-checker decisions locking a review row, a partial unique index guaranteeing at
  most one `PUBLISHED` version per product (module 19 §17), `SELECT ... FOR UPDATE` on
  governance decisions. Iceberg's ACID is snapshot isolation over immutable files with
  copy-on-write or merge-on-read deletes. Every steward's one-field edit becomes a new
  data file and a compaction obligation.
- **Small-file amplification.** 25M audit events/day arriving as a stream into Iceberg
  is a compaction job, a maintenance schedule and an operational drill nobody has
  budgeted. In PostgreSQL it is a monthly range partition that gets detached and
  archived (`06-data-architecture.md` §7) — which is the same time-travel property for
  a fraction of the operational surface.
- **It buys a second store.** ADR-0003's whole argument is that a projection must
  never become a second source of truth. An Iceberg mirror of authoritative state is
  precisely that, unless it is a one-way export — at which point it is an export, and
  we should build the export.

### The recommendation: build the export, not the lakehouse — and put it in the contract

**N22 — Estate Context Export.** A scheduled, verifiable, self-describing dump of the
customer's context in open formats:

- One Parquet dataset per logical entity: `asset`, `column`, `business_annotation`
  (with history, see §5), `classification_evidence`, `asset_certification`,
  `lineage_edge` (unified across the four sources), `glossary_term_version`,
  `asset_term_link`, `data_quality_observation`, `context_product_version`,
  `audit_event`.
- A **published, versioned export schema document** in `30-contracts/`, so the
  customer's exit plan does not require reading our migrations.
- A manifest per run: row counts per dataset, a content hash, the schema version, the
  `as_of` timestamp, and the Alembic revision it was taken at.
- Value-free by construction — INV-6 applies to the export exactly as it applies to
  the control plane, and the export is the easiest place to leak, because it is the
  one artefact designed to leave.

Then, and only if a customer asks: writing Iceberg table metadata over those Parquet
files is a few days with `pyiceberg`, and it is *additive*. That is the right order.
Build the thing that has value on its own; add the format that has value in a
procurement conversation when a procurement conversation asks for it.

**The part that costs nothing and is worth more than the code:** a **contractual exit
clause** naming the export format, the cadence, and a maximum time-to-delivery on
termination. Atlan's Iceberg answer is a technical answer to a commercial question.
We can give the commercial answer directly, and we can give it this week.

### S3 — SQL over metadata, reframed

Strip the Iceberg from "query metadata exactly as you query your data" and what is
left is a real use case: **an auditor or an internal analyst wanting to run SQL over
lineage, policy and audit history for a compliance report, without going through a
paginated REST API.** `research/02-atlan.md` line 45 already flags this as worth
stealing. It is.

We should serve it, and we should serve it better than Iceberg does, because our data
is live rather than snapshot-lagged. The mechanism is a **documented, versioned,
read-only reporting schema** — a set of stable SQL views over authoritative tables,
on a read replica, with its own grant, its own tenancy predicate, and INV-6 applied
(no question text, redacted SQL). It is ~1.5 weeks and it makes a compliance analyst
self-sufficient.

What we decline, and should be able to defend: **external engines connecting to our
store.** Trino or Spark against the authoritative database is a second read path
outside every control we have built — it bypasses the policy filter that runs *before*
ranking (module 12 §6), bypasses the tenancy predicate, and bypasses audit. The
defensible line in a customer conversation is: *"Your context leaves in an open format
you own, on your schedule, and you get live SQL through a governed reporting schema.
What you do not get is an unaudited socket into the authoritative store, and you would
not accept one from us if we offered it."*

---

## 4. A2A writeback — the important question

> Are they reckless, are we too strict, or are these compatible if the writeback lands
> in a proposal lane?

**Answer: they are careless with language, we are wrong in our documentation, and the
designs are compatible — but only after the claim is split into three, because it is
currently one word covering three different risk classes.**

Read the snapshot timeline (text 174–211) rather than the marketing sentence. Nine
entries, five distinct actors:

| Timeline entry | Actor | What it actually is |
|---|---|---|
| "2 columns added · revenue_net, revenue_adj" | `pipeline/dbt` | A **measured fact** from a scan |
| "revenue_raw column dropped · schema change" | `pipeline/dbt` | A measured fact |
| "+3 downstream nodes linked · lineage expanded" | `lineage-agent` | Output of a **parser**, deterministic |
| "DQ score hit 98.2%" / "91.3% → 95.7% · profiling complete" | `dq-agent` | A **measurement**, computed |
| "Trust score 71 → 89 · threshold cleared" | `trust-agent` | A **derived score** over measurements |
| "PII-Restricted policy applied · auto-classified" | `policy-agent` | A **judgement** — the only genuinely contested one |

Eight of nine are not model output at all. Calling a dbt scan an "agent write-back" is
marketing; it is ingestion with a nicer noun. So the honest decomposition:

**Lane 1 — Measured facts written by programs.** Schema changes, profiling results,
quality observations, parsed lineage edges. INV-3 does not apply, because no model
authored them. **We already do all of this.** `DataQualityObservation` (immutable,
value-free), `view_lineage_edge`/`procedure_lineage_edge`/`dbt_lineage_edge`,
`object_fingerprint`/`drift_record`. Nothing to build; something to *say* — our own
non-goal wording currently reads as though we refuse this, which is false.

**Lane 2 — Observations about our own system.** Which assets agents read, which tools
they invoked, what was retrieved and what was rejected, what refused and why. This is
where Atlan's "context compounds with every interaction" actually lives, and it is
**the one part of their pitch we are already better at**: `ContextProductConsumptionEdge`
(with `product_fingerprint` and `quality_snapshot`), `McpConsumptionEvidence`,
`consumption_lineage.py`, and `ai_decision_lineage.py` — which records
`RETRIEVAL_SELECTED`, `RETRIEVAL_REJECTED`, `TOOL_SELECTED`, `TOOL_REJECTED`,
`REFUSAL`, i.e. **rejections as well as selections**, which nobody else keeps.

We collect it and then do nothing with it. `usage_factor` in the module 12 ranking
model is marked *planned*; `RT-6` is P1. **Closing the loop from consumption evidence
into retrieval ranking is the compounding claim, it is one week, and it carries no
INV-3 risk because usage is measured, not asserted.** This is the cheapest genuinely
differentiating item in the section.

**Lane 3 — Model-authored judgements.** A proposed description, a proposed
classification, a proposed glossary link, a proposed lineage edge below the
determinism threshold. This is the only lane where their design and INV-3 collide,
and it is also the lane where **we already have the answer and have not connected the
wire**: `metadata_enrichment_proposal`, the 0.70 confidence cap against a 0.95
auto-publish gate (design brief §4, keep-item K3), maker-checker (INV-8), and one
shipped precedent on the MCP surface itself — `request_data_product_access`, exposed
with `"writePosture": "MAKER_CHECKER_REQUEST_ONLY"` (`mcp_server.py` ≈L256, L600).
An agent can *ask*; it cannot *grant*. That is the pattern, already in production, and
it generalises.

### So: are we too strict?

No — but we are **inconsistent**, and inconsistency is worse than strictness because
it makes the strictness look accidental.

`target/05-target-architecture.md` §7 reaffirms: *"**No write-back.** Read-only stays
read-only."* That statement is already false. We ship an MCP write (MCP-2, partial),
we write consumption edges on every read, and `metadata_enrichment_proposal` is a
write path for model output by design. The non-goal was written about *source data*
and has drifted into sounding like a rule about *context*. It needs replacing, and
this is the correction I care most about in this review because it is free and because
leaving it means a customer or an auditor reads our own document as forbidding a thing
we do.

**Proposed replacement text for `target/05-target-architecture.md` §7, first bullet:**

> - **No write-back to sources.** Read-only stays read-only against every connected
>   data source; procedure-derived tools are eligible only when proven read-only by
>   parse. Writes *into Atlas's own context store* are permitted and are governed by
>   lane, not forbidden: **measured facts** (scans, profiles, parsed lineage) are
>   written by programs; **observations** about platform usage are written by the
>   platform; **model-authored judgements** enter only as typed inert proposals under
>   INV-3, confidence-capped below the auto-publish gate, and resolved by maker-checker
>   (INV-8). No agent, internal or external, writes an authoritative field.

### The hazard neither of us has named

An agent writes an observation. A later agent retrieves that observation as grounding
context. That is a **feedback channel into the model's own input**, and at scale it is
two failure modes at once: quiet drift (a plausible-but-wrong description becomes
consensus by repetition) and an injection surface (text authored through an agent
channel becomes retrievable context for every subsequent agent).

Atlan's page does not mention this. A bank's model-risk function will ask about it in
the first meeting, and "it's a proposal, a human approves it" is only half an answer,
because the reviewer is approving *text a model wrote about text a model wrote*.

We already have the control and would only need to point it at the new lane:
`ingest_screening.py` (N18, shipped) screens all model-reachable text at write time
and **quarantines rather than deletes**, excluding flagged text from model context.
Any Lane-3 write must route through the same screening, and the proposal record must
carry the provenance chain — *this description was proposed by model route R at
version V, from inputs I* — so a reviewer can see they are reviewing a second-order
inference. That provenance requirement is the same one the design brief §3 already
makes for compiled wiki blocks; it generalises for free.

**This is a differentiator, not just a control.** "Agents write back" is a feature
everyone will ship in 2026. "Agents write back into a lane that cannot reach an
authoritative field, is screened for injection at write, and carries the provenance
of the inference that produced it" is the version a bank can put through model risk.

Because this reverses a published non-goal, it needs an ADR. Draft at §8.2.

---

## 5. Time travel — can we reconstruct what a model saw?

> "When an agent makes a mistake, you need to reconstruct exactly what context it saw
> and when. Without it, you can observe the output but never explain the reasoning."
> (text 143)

This is the best sentence Atlan wrote on this page. It is exactly the requirement,
stated exactly right, and it maps directly onto our own quality-attribute ordering
(`01-principles-and-invariants.md` §4: explainability 3rd, reproducibility 4th, latency
6th). We should agree with it in public.

**Can we do it today? Mostly — and the gaps are small and precisely locatable.** I
went looking for this expecting a hole and found six-eighths of a floor.

| Context class an agent reads | Point-in-time reconstructable today? | Mechanism |
|---|---|---|
| Column classification | **Yes** | `ClassificationEvidence` — append-only ledger, never mutated, `is_current` + `created_at`, records rule id, source type, confidence and matched signal |
| Certification | **Yes** | `AssetCertification` rows retained; the docstring is explicit that status is never mutated by a clock — expiry is a query-time projection (`asset_certification_is_active`) |
| Quality signals | **Yes** | `DataQualityObservation` — immutable, value-free time series |
| Business classification / domain assignment | **Yes** | `business_graph.py` effective-dated `_effective(effective_from, effective_to, as_of)`, with `as_of` threaded through `descendant_ids`, `ancestor_closure` and roll-up (N9, shipped) |
| Glossary terms, READMEs, semantic models, metrics, tools, policies, model routes | **Yes** | Immutable versions pinned per run (`06-data-architecture.md` §3, "the replay guarantee") |
| dbt lineage | **Yes** | `dbt_lineage_edge` is scoped to `artifact_import_id` — each import is a snapshot |
| **Business annotations** (business name, description, table role, grain statement, synonyms, suggested questions) | **No** | `MetadataBusinessAnnotation` has `UniqueConstraint("table_id")` and an integer `version` counter but **no history table**. The row is mutated in place; `source_proposal_id` points only at the proposal behind the *current* text. Last quarter's description is gone |
| **View / procedure lineage edges** | **Unverified** | `view_lineage_edge` and `procedure_lineage_edge` carry `created_at`/`updated_at` but no import id and no supersession marker, unlike `dbt_lineage_edge`. Whether re-parse replaces or appends needs checking before any claim is made |

And then the structural gap that matters more than either:

**Nothing binds an agent run to the content it read.** `AgentRun.retrieval_evidence`
persists `{object_type, object_id, display_name, score, reason_codes, metadata}` per
hit (`retrieval.py` ≈L181). That is a strong record of **which objects were selected
and why** — genuinely better than most competitors, and it is what makes ranking
debuggable. What it is not is a record of **what those objects said**. So on an
incident:

- We can name every asset the agent considered, its score, and its selection reason.
- We can reproduce exactly the semantic model, metric, tool, policy and model route it
  pinned.
- We can reconstruct the classification, certification, quality state and domain
  membership as of that timestamp.
- We **cannot** show what the table's business description said at the time, and — more
  importantly — we cannot *prove* whether anything changed. Today the answer to
  "was the context the same as it is now?" is a shrug.

That last point is the whole game. An auditor does not need us to reproduce the
context in every case. They need us to be able to say, with evidence, **whether it
changed.**

### The fix: a context receipt (N20)

Not bitemporal-everything. That is expensive, touches every table, and ADR-0018's
business graph already shows we adopt effective-dating where the axis genuinely needs
it rather than everywhere.

At the moment the grounding set is assembled, persist alongside the existing retrieval
evidence, per included fragment:

```
(object_type, object_id, object_version | null, content_sha256, byte_length,
 source_table, row_updated_at)
```

plus one `context_bundle_sha256` over the assembled, ordered set.

Three properties fall out, in increasing order of value:

1. **Change detection is exact and permanent.** Re-hash today; if it differs, the
   context drifted, and we know precisely which fragment. "We don't know" becomes "it
   changed, here is the object, here is what it says now, here is when it was last
   written." That alone answers most of what a post-incident review needs.
2. **Where the object is versioned, reconstruction is exact** — the receipt names the
   version and the version is immutable.
3. **It bounds the history problem.** Once you can see which unversioned objects
   actually drift under real use, you add history to those and only those, on
   evidence. Right now we would be guessing.

Then add history to the one object we already know matters: **business annotations.**
An `metadata_business_annotation_version` table (or converting the existing row to a
current-pointer + version rows, mirroring `asset_documentation` /
`asset_documentation_version`, which is already the right shape and already in the
codebase). Business descriptions are the single most likely thing to be *wrong* in a
way that changes an agent's answer, and the most likely thing a steward silently
edits after an incident.

**Why now rather than later, stated plainly:** you cannot backfill history you never
kept. Every week without this is a permanently unauditable week. `N10` (knowledge
compilation, 10–12 weeks, Phase 3) multiplies the number of unversioned things an
agent reads by an order of magnitude — compiled pages made of blocks, each with its
own generator and inputs. Adding receipts *after* N10 means retrofitting the hash
boundary through a compiler. Adding them before costs ~1.5 weeks and makes N10's
per-block provenance requirement (design brief §3) fall out of an existing mechanism
rather than needing a new one.

Estimate: context receipt 1.5w; annotation history + migration 2w; total **3.5w**.
Phase 2, sequenced *before* N10.

### One thing we should say before a customer finds it

We deliberately do not store the user's question — `AgentRun.question_hash` only, with
the model docstring stating "raw user questions are intentionally not persisted"
(INV-6, `06-data-architecture.md` §8: user question text is *fingerprint only*). So a
literal "show me exactly what the model saw" reconstruction is incomplete on the input
side **by design**.

That is the correct trade and we should lead with it rather than be caught by it:
*"We can reconstruct every governed input, prove whether any of it changed, and show
every selection and rejection the retriever made. We cannot show you the analyst's
literal question, because we never stored it — we store a keyed fingerprint, so we can
prove two runs asked the same question without holding the text. If your model-risk
process requires the verbatim prompt, that is a policy decision to take deliberately,
not a capability to bolt on."*

Draft ADR at §8.1.

---

## 6. `get_asset_context` — right shape, wrong last mile

`image43` shows the call and the response; text 149–161 shows the richer variant —
`get_asset_context(asset: orders.revenue, include: quality, policies, lineage)`
returning `certification: VERIFIED`, `quality_score: 98.2%`, `classification: PII –
Restricted`, `lineage_depth: 3 upstream · 7 downstream`, `owner: analytics-team`.

**Is one composite call the right shape? Yes — and for governance reasons, not
ergonomic ones.**

The ergonomic argument is obvious (five round trips inside an agent loop is latency
and five chances at a partial view). The governance argument is better and is the one
to make in an architecture review: a composite call gives us **one policy evaluation,
one audit record, one consumption edge, one correlation id, one purpose declaration,
one budget decrement.** Five separate calls give us five of each, individually
authorised, with no record that they were one question. Composite is *more* auditable,
not less. It also means the quality gate and critical-incident gate in
`context_product_policy` evaluate once against the whole context rather than per
fragment, which is the only way "deny on critical incident" can be coherent.

**Where we are.** Our nearest surface is the MCP catalog table resource
(`mcp_server.py` ≈L1506). It returns catalog/schema/table, object type, row count
estimate from the latest completed `TableProfile`, and per column: name, ordinal,
physical type, nullable, `classification`, and `"business_description": None` —
hardcoded. No certification. No quality score. No lineage depth. No owner. On this
specific shape **we are behind**, and it is a two-week build over components that all
already exist: `asset_certification.asset_certification_is_active`, `data_quality`,
`unified_lineage` (the four native lineage tools shipped 2026-08-29), `ownership_rule`,
`MetadataBusinessAnnotation`.

**Where they are wrong.** The transcript ends: *"Yes — VERIFIED badge, quality 98.2%
badge. Note: carries PII – Restricted — column masking is active, ensure your pipeline
respects that policy."* And `image43` closes with **"Context complete · Agent ready to
reason."**

The model is being handed policy inputs and asked to produce a policy verdict. Three
things are wrong with that, in ascending order:

1. It is unreliable — it is an LLM inferring a control decision from five badges.
2. It is unauditable — the verdict exists only in generated prose. There is no record
   of what the platform would have decided.
3. It is the exact failure INV-3 exists to prevent: model output as authority over a
   governed decision.

Note the second-order problem in their own example: the agent says "ensure your
pipeline respects that policy" — the enforcement is delegated to the *caller*, on
trust. Our whole architecture (INV-2, one execution choke point) is built on the
premise that you never do that.

**Our version.** Same composite shape, one field more:

```jsonc
{
  "asset": "analytics.prod.orders.revenue",
  "certification": {"status": "VERIFIED", "certified_by": "...", "expires_at": "..."},
  "quality":       {"score": 98.2, "as_of": "...", "open_critical_incidents": 0},
  "classification":{"level": "PII_RESTRICTED", "source": "RULE", "evidence_id": "..."},
  "lineage":       {"upstream_depth": 3, "downstream_depth": 7, "truncated": false},
  "ownership":     {"owner_principal": "analytics-team", "steward": "..."},

  // the field Atlan does not have — computed server-side, not inferred
  "usage_decision": {
    "decision": "ALLOW_WITH_CONDITIONS",
    "reason_codes": ["MASKING_REQUIRED", "PURPOSE_MATCHED"],
    "policy_version": "policy-v41",
    "eligible_tool_version_ids": ["toolv_..."],
    "evaluated_at": "..."
  },
  "_governance": {"value_scope": "METADATA_ONLY"}
}
```

The agent may *explain* the decision. It may not *derive* it. And it cannot act on its
own conclusion regardless, because the only path to data is a governed tool that
re-evaluates policy at invocation (INV-2, module 19 §7). The decision field is also
what makes the interaction replayable: `usage_decision` with a pinned `policy_version`
is evidence; a sentence in generated prose is not.

**Does returning a classification to an agent leak anything?**

Two separate questions, and they have different answers.

*Does the label itself leak?* No. `PII_RESTRICTED` is metadata of the same class as
the column name `customer_ssn`, which we already return. And withholding it is
actively worse: an agent that does not know a column is restricted will plan around it
badly and will surface it to a user who then asks why. INV-6 governs *values*, not
*labels about values*, and this is the correct side of that line.

*Does the surface leak?* Yes, potentially — and this is the requirement to write down.
A composite endpoint returning classification is an **enumeration oracle**: a caller
probing guessed asset names could map where the restricted data lives without ever
reading a row, which is a genuine reconnaissance capability for an insider. Three
constraints, all of which we already implement elsewhere and must extend to this tool:

1. **Anti-enumeration**: unauthorised and nonexistent must be indistinguishable
   (module 19 §15.3 already requires this for context products; the catalog resource
   already returns a single `inaccessible` payload for both).
2. **Entitlement-scoped granularity**: the classification is returned at the
   granularity the caller may already see the asset at. No classification on an asset
   outside their workspace binding, ever — not even "exists but restricted."
3. **Budgeted and evidenced**: this tool decrements the existing Redis per-consumer
   budget (CX-6) and writes an `McpConsumptionEvidence` row per call, so a sweep is
   visible in the audit trail rather than inferable afterwards.

Add to module 19 §8's boundary table, as a new row: *"Classification labels on assets
within the caller's entitlement scope"* under **crosses**, and *"Any signal — including
existence, denial shape or classification — about an asset outside the caller's
entitlement scope"* under **never crosses**.

---

## 7. Performance claims

### "Traversable at depth in under 100ms" (text 228)

**We beat it, on the cheaper store, and we have the measurement they do not publish.**

ADR-0020 §2, on a bank-shaped column-level DAG — 12 layers, 40,000 columns per layer,
fan-in 2, **880,000 column-level edges**, PostgreSQL 16, upstream from one report
column:

| Depth | p50 | Nodes reached |
|---:|---:|---:|
| 6 | 0.7 ms | 127 |
| 10 | 4.4 ms | 1,957 |
| **12** | **10.8 ms** | **3,637** |

Two honest caveats we must carry with the number:

- **Direction matters enormously and ADR-0020 §3 says so.** Downstream through a hub:
  50,000 fan-out at depth 12 is **3,402 ms** enumerated, and **1.5 ms** bounded to
  1,000 nodes. Atlan's "under 100ms at depth" is almost certainly a bounded traversal
  too — nobody materialises 480,000 nodes in 100 ms, Neo4j included, because
  index-free adjacency does not make half a million rows free. So the correct posture
  is not "they are lying," it is "that number is only meaningful with the bound
  attached, and we publish ours."
- **Our own documentation undersells us by a factor of 200.**
  `10-performance-and-scale-model.md` §3 publishes *Graph neighbourhood (bounded, 1–4
  hops): p95 **2 s***, and §9 states plainly that no p95 in the document has ever been
  measured. Against a competitor publishing "under 100ms," a bank's architecture
  reviewer comparing spec sheets sees 2 s versus 100 ms and concludes we are twenty
  times slower at a thing we are in fact faster at.

**Doc fix (0.5w), `10-performance-and-scale-model.md` §3.** Split the row and label
the state of evidence:

| Operation | p50 | p95 | Status |
|---|---|---|---|
| Graph traversal, store only (bounded, ≤12 hops, ≤1,000 nodes) | 11 ms | *unmeasured* | Measured p50 on a synthetic 880k-edge DAG, ADR-0020 §2 |
| Graph neighbourhood, end-to-end API (policy filter + payload) | 400 ms | 2 s | **Target. Never measured** (§9) |

The distinction is the difference between an engineering number and a product number,
and conflating them is how a good result gets published as a bad one.

### "Vector-native AI search across billions of assets" (text 217)

**Wrong, or the unit is doing dishonest work.**

- Our own scale model tops out at ~1M tables × ~30 columns ≈ 30M columns
  (`06-data-architecture.md` §2). "Billions of assets" is two orders of magnitude
  beyond any single enterprise estate. It only reaches billions by counting *chunks*
  or *embeddings* of unstructured documents — a different unit, quietly substituted.
- More to the point: **searching billions of assets is a symptom, not a capability.**
  ADR-0019 is explicit that retrieval filters by workspace binding and policy *before*
  ranking, that this ordering removes an information-leak class and is not negotiable,
  and that the candidate set reaching the scorer is what one principal may see. A
  system that searches a billion assets and filters afterwards is leaking result
  counts, orderings and existence — module 12 §6's exact argument.

The line for a customer conversation: *"We do not search billions of assets. We search
the few thousand you are entitled to see, exactly rather than approximately, and when
the candidate set is too large we refuse with a reason code instead of silently
scoring an arbitrary slice."* That last clause is ADR-0019's refusal-not-truncation
rule, and it is a stronger claim than a big number because it is falsifiable.

**Where we must say "unmeasured," and mean it:**

- ADR-0019's latency table (45 ms @ 200 candidates, 100 ms @ 1,000, 427 ms @ 5,000) is
  a synthetic bench over 200,000 stored 768-dim embeddings, not an estate.
- ADR-0020's traversal numbers are a synthetic bank-shaped DAG, not a real one; the
  ADR itself schedules re-measurement once view and procedure parsing land.
- **Nothing has embedded the corpus.** ADR-0019's amendment, "Still open": the model is
  chosen, the corpus is not embedded, and the recall@10-after-policy-filtering
  evaluation has not been run. `RT-8` (large-catalog retrieval benchmarks) is P0 and
  not started.
- No p95 anywhere in the platform has ever been measured (`10-performance-and-scale-model.md`
  §9). Every published target is design intent.

INV-9 applies to us in a competitive comparison exactly as it applies to a connector.
We have two real measurements and a document full of targets, and we should be scrupulous
about which is which — because the *reason* we can attack Atlan's Iceberg story
(`research/02-atlan.md` line 150: the marketed architecture does not reconcile with the
documented one, and for a regulated audience that gap is disqualifying rather than
cosmetic) is a standard we then have to meet ourselves.

### "Context is big data" (text 138–139)

Wrong conclusion, correct pressure, and worth separating.

Wrong: our ceiling is 500M lineage edges and 30M columns. Low hundreds of GB,
time-partitioned, with old partitions detached and archived so an auditor's question
about Q3 two years ago is answerable (`06-data-architecture.md` §7). That is time
travel, and it costs a partition strategy rather than a lakehouse.

Correct: *"infrastructure that only reads collapses when agents write back at scale."*
Our own model already projects **25 million audit events per day** at the high end, and
INV-7 requires an audit record in the same transaction as every mutation. The write
amplification from agent traffic is real and it lands on the audit ledger first.

The mitigation is designed and **never drilled**: monthly range partitions, WORM export,
PITR (`E6`) and projection rebuild (`E5`) both listed as "Never run." So the right
response to their claim is not to build a lake — it is to run the two drills that prove
our retention and recovery design survives the write rate we ourselves predicted. That
is `E5`/`E6`, already in the plan, and this section is one more reason they are not
optional.

---

## 8. Proposed ADRs

Drafted here rather than created as files, per the review instruction.

### 8.1 ADR-0022 — Point-in-Time Context Reconstruction

**Status:** Proposed | **Date:** 2026-08-30 | **Owner:** Architecture

#### Context

`01-principles-and-invariants.md` §4 ranks explainability third and reproducibility
fourth, above latency. `06-data-architecture.md` §3 states the replay guarantee: given
an `agent_run`, every version it pinned is recoverable, so the decision can be
re-derived. That guarantee is real for the six object classes that are versioned —
semantic model, metric, tool, policy, model route, prompt-risk classifier — and it is
silent about everything else the agent actually reads.

An audit of what an agent consumes shows the floor is better than expected. Column
classification is an append-only ledger (`ClassificationEvidence`, never mutated,
`is_current` + `created_at`). Certifications are retained and expiry is a query-time
projection rather than a mutation. Quality observations are an immutable time series.
Business-domain assignment is effective-dated with `as_of` traversal (`business_graph.py`,
N9). Glossary terms and asset READMEs are versioned. dbt lineage is scoped per
`artifact_import_id`.

Two things are missing, and one of them is structural.

First, **business annotations have no history.** `MetadataBusinessAnnotation` carries
`UniqueConstraint("table_id")` and an integer `version` counter, and is mutated in
place. Business name, description, table role, grain statement, synonyms and suggested
questions are exactly the fields most likely to change an agent's answer and most
likely to be quietly corrected after an incident, and their prior state is
unrecoverable.

Second, and more important: **nothing binds a run to the content it read.**
`AgentRun.retrieval_evidence` records object identity, score and reason codes per hit.
It is a strong record of *which objects were selected and why*, and no record at all of
*what they said*. So the platform cannot answer, with evidence, the simplest question a
post-incident review asks: has this context changed since the run?

History cannot be backfilled. Every week without this record is a permanently
unauditable week, and `N10` (knowledge compilation) will multiply the number of
unversioned fragments an agent reads.

#### Decision

**Persist a content-addressed context receipt with every grounding set, and add history
to business annotations. Do not make the whole store bitemporal.**

1. At grounding-set assembly, `AgentRun` gains `context_receipt`: per included
   fragment, `(object_type, object_id, object_version | null, content_sha256,
   byte_length, source_table, row_updated_at)`, plus one `context_bundle_sha256` over
   the ordered set. The receipt is value-free — a hash of metadata text, never source
   values — and inherits INV-6 unchanged.
2. The same receipt shape is written for MCP context reads. `ContextProductConsumptionEdge`
   already carries `product_fingerprint` and `quality_snapshot`; this generalises that
   idea to every context surface rather than leaving it product-specific.
3. `MetadataBusinessAnnotation` becomes a current-pointer plus immutable version rows,
   mirroring the `asset_documentation` / `asset_documentation_version` pair already in
   the codebase. The current row is a view over the latest approved version.
4. A `reconstruct_context(agent_run_id)` operator API returns, per fragment: the exact
   content where the object is versioned or historied; and where it is not, the current
   content plus a `DRIFTED` / `UNCHANGED` verdict derived from the hash. **"We do not
   know" is not an allowed outcome.**
5. Other unversioned objects gain history only on evidence of observed drift, measured
   from receipts. The receipt is the instrument that tells us where to spend next.

Deliberately **not** decided here: bitemporal versioning of the catalogue; storing user
question text, which stays a keyed fingerprint under INV-6; and reconstruction of the
rendered prompt, which is a model-gateway concern.

---

### 8.2 ADR-0023 — Agent Writeback Lanes

**Status:** Proposed | **Date:** 2026-08-30 | **Owner:** Architecture
**Amends** `review-2026-08/target/05-target-architecture.md` §7, first bullet.

#### Context

`target/05-target-architecture.md` §7 reaffirms a non-goal: *"No write-back. Read-only
stays read-only."* The statement was written about connected data sources and has
drifted into reading as a rule about the context store. As a rule about the context
store it is already false: profiling and scans write measured facts; every MCP read
writes a consumption edge; `metadata_enrichment_proposal` is a write path for model
output by design; and MCP-2 shipped a governed write in 2026-08 —
`request_data_product_access`, exposed with `"writePosture":
"MAKER_CHECKER_REQUEST_ONLY"`.

Atlan markets "A2A — agents write observations, quality signals, and usage patterns
back into the store" as a single capability. Its own snapshot timeline shows the claim
is three different things: eight of nine entries are deterministic program output
(dbt scans, a lineage parser, a DQ profiler, a derived trust score) and one is a model
judgement (auto-classification). Bundling them under one word makes an unremarkable
capability sound reckless and a genuinely contested one sound settled.

The compounding argument underneath — context improves with every interaction — is
correct and we are positioned to serve it better than the claim: `ai_decision_lineage.py`
records rejections as well as selections, which nothing in the market does. We simply do
not feed it back into ranking (`usage_factor`, module 12 §5, marked planned; `RT-6`).

A blanket prohibition that the platform already violates is worse than a narrower rule
that holds.

#### Decision

**Replace the blanket non-goal with three named writeback lanes. No agent, internal or
external, writes an authoritative field in any lane.**

**Lane 1 — Measured facts, written by programs.** Scan results, profiles, quality
observations, parsed lineage edges, drift records. Authored by deterministic code, not
by a model, so INV-3 does not apply. Already implemented; named here so the boundary is
explicit rather than implied.

**Lane 2 — Platform observations, written by the platform.** Consumption edges, MCP
evidence, AI decision records including rejections and refusals. Facts about our own
behaviour, value-free, immutable, already implemented. **These become inputs to
retrieval ranking** (`usage_factor`), which is the compounding property, and it is
INV-3-safe because usage is measured rather than asserted.

**Lane 3 — Model-authored judgements, written as inert proposals.** Proposed
descriptions, classifications, glossary links, relationship or lineage candidates,
from internal agents or from external MCP clients. Governed by five constraints, none
optional:

1. The write target is a proposal type, structurally distinct from a command type, with
   no conversion function (INV-3, unchanged).
2. Model-only inference is confidence-capped below the auto-publish gate (0.70 vs 0.95),
   so it structurally cannot self-publish.
3. Resolution is maker-checker; the proposing identity can never approve (INV-8).
4. **Every Lane-3 write passes ingestion-time prompt-risk screening** (`ingest_screening.py`,
   N18) before it is retrievable as context. Writeback creates a feedback channel into
   future model input, and that channel is an injection surface as well as a drift
   surface. Flagged text is quarantined, not deleted, and excluded from model context.
5. The proposal carries the provenance of the inference — model route and version,
   input record ids and versions, generator — so a reviewer can see when they are
   reviewing a second-order inference over model-authored text.

External MCP writeback is exposed only as Lane 3, with the existing
`MAKER_CHECKER_REQUEST_ONLY` write posture declared on the tool, per-consumer budgets
(CX-6) and an evidence record per call.

---

## 9. Cost and plan placement

Against `review-2026-08/gap/02-gap-diff-and-plan.md` §4 and §7.

| ID | Item | Weeks | Phase | Depends on | Note |
|---|---|---:|---|---|---|
| **N20** | Context receipt + business-annotation history (ADR-0022) | 3.5 | **2, before N10** | none | Cannot be backfilled. The one item in this section I would not trade |
| **N21** | `get_asset_context` composite MCP tool with server-computed `usage_decision` | 2 | 2 | none — all components exist | Also fixes the hardcoded `"business_description": None` |
| **N22** | Estate context export (Parquet + published schema + manifest) and a contractual exit clause | 3 | 2 | **object-storage client — not wired today** (`06-data-architecture.md` §1: no `boto3`, no `minio`) | Exit clause itself is free and should be drafted now |
| **N22b** | Read-only compliance reporting schema (documented SQL views on a replica) | 1.5 | 2 | none | The honest version of "SQL over metadata" |
| **N23a** | Lane 2 closed: consumption evidence → `usage_factor` in ranking | 1 | 2 | `N5`/`RT-1` | Already `RT-6`. Cheapest differentiating item here |
| **N23b** | Lane 3: external MCP proposal writes (ADR-0023) | 3 | 3 | N18 (shipped), MCP-2 | Extends the shipped `MAKER_CHECKER_REQUEST_ONLY` precedent |
| — | Doc corrections (§10) | 0.5 | 0 | none | Free, and one of them is currently misleading |

**Total ~14.5 weeks**, ~11 excluding the object-storage prerequisite. Against gap/02's
45–60 week Phase-3 total this is a ~20% addition, and I would defend only N20 (3.5w) and
the doc corrections (0.5w) as non-negotiable. N21 is the best value-per-week. N22's
*clause* should be written this week regardless of when its *code* lands.

**Sequencing constraint worth naming:** N20 before N10. Knowledge compilation makes
every wiki block a context fragment with its own generator and inputs; the receipt
mechanism is the natural place for block-level provenance to land, and building N10
first means retrofitting a hash boundary through a compiler.

---

## 10. Document changes proposed

| Document | Change |
|---|---|
| `review-2026-08/target/05-target-architecture.md` §7 | Replace the "No write-back" bullet with the three-lane text in §4 above. **Currently states something the platform already does not do.** |
| `10-architecture/10-performance-and-scale-model.md` §3 | Split the graph row into store-only (measured, 11 ms p50, ADR-0020) and end-to-end API (target, 2 s p95, never measured), per §7 above |
| `20-modules/19-context-products-and-mcp.md` §8 | Add to the boundary table: **crosses** — "classification labels on assets within the caller's entitlement scope"; **never crosses** — "any signal, including existence, denial shape or classification, about an asset outside the caller's entitlement scope" |
| `20-modules/19-context-products-and-mcp.md` §14 | Add `CX-8b` (`get_asset_context` composite, P1) and `MCP-4` (Lane-3 proposal writes, P2, gated on ADR-0023) |
| `20-modules/12-retrieval-and-search.md` §12 | Re-rank `RT-6` (usage signal) from P1 to P0 with the note that consumption evidence is already collected and unused — this is the compounding-context claim, and it is one week |
| `10-architecture/adr/ADR-0020` §"still not measured" | Add view/procedure lineage edge supersession as an open verification: `view_lineage_edge` and `procedure_lineage_edge` have no import-generation stamp, unlike `dbt_lineage_edge`, so historical lineage reconstruction is unproven for two of four sources |
| `review-2026-08/research/02-atlan.md` §"open questions" | Add: whether Atlan's Iceberg export preserves a documented schema or only the Atlas-fork typedefs — format openness is not schema openness, and it is the question that decides whether their portability claim is real |

---

## 11. What we should be able to say in a customer conversation

Five sentences, each defensible from a document or a measurement:

1. **On portability.** "Your context leaves in open formats on a published schema, on a
   cadence named in the contract, and you get live SQL over lineage, policy and audit
   through a governed reporting schema. Storing our authoritative state in Iceberg would
   buy you file-format openness while leaving you to reverse-engineer our data model —
   we would rather give you the schema."
2. **On writeback.** "Agents write into our store in three lanes. Measurements are
   written by programs. Usage observations are written by the platform, and they improve
   retrieval on every interaction. Model judgements enter only as inert proposals that
   are confidence-capped below the publish gate, screened for injection at write, and
   approved by someone other than the proposer. No agent writes an authoritative field."
3. **On reconstruction.** "For any agent run we can name every asset considered, every
   one rejected and why, reproduce every pinned version, and prove whether any of the
   context has changed since. We cannot show you the analyst's literal question, because
   we never stored it."
4. **On traversal.** "Twelve hops of column-level lineage across 880,000 edges in eleven
   milliseconds on PostgreSQL, bounded and with explicit truncation. Ask any vendor
   quoting a traversal latency what node cap it was measured under."
5. **On scale.** "We do not search billions of assets. We search what you are entitled to
   see, exactly rather than approximately, and we refuse with a reason code rather than
   score an arbitrary slice of a set that is too large."

And the one we decline, stated as a position rather than an absence: **we will not give
an external engine a socket into the authoritative store.** Every read passes the policy
filter before ranking and every write passes the choke point. An open format is not worth
an unaudited read path, and a bank that thought about it for ten minutes would agree.
