# Target Design Brief — architectural stance

Status: Proposal, August 2026. Written clean-room, before reconciliation.

This document states the positions everything else in `target/` depends on. If you
disagree with a position here, the downstream designs change. Read it as a set of
arguments, each of which can be rejected on its own.

---

## 1. What the product actually is

Strip the feature list back and there are four verbs:

> **Ingest** an estate's metadata → **Understand** it (structure, meaning, flow) →
> **Publish** that understanding as human-readable knowledge and machine-consumable
> context → **Execute** against it under governance.

Everything else — wiki, business graph, lineage review, tool generation, MCP —
is one of those four verbs wearing a feature name.

The current vision document says Atlas is "the governed AI data operating system for
regulated enterprises." That is a positioning line, not a design constraint. The
design constraint is sharper and worth stating plainly:

> **The product's job is to make an organisation's data understandable enough that a
> machine can act on it safely, and to prove afterwards that it did.**

"Understandable enough that a machine can act on it" is the part the market has not
solved. Everyone catalogues. Almost nobody compiles the catalogue into something an
agent can consume without hallucinating. That is the opening.

---

## 2. Position: three axes, kept separate

This is the single most consequential disagreement with the current design.

Every metadata platform has to model three different hierarchies. They are
routinely conflated, and conflating them is the most common cause of a stalled
deployment.

| Axis | Question it answers | Shape | Changes how often? |
|---|---|---|---|
| **Technical** | Where does the data physically live? | `datasource → catalog → schema → table → column` | On every scan |
| **Organisational** | What does it mean, and who owns that meaning? | `LOB → sub-LOB → domain → sub-domain`, many-to-many with assets | On every reorg |
| **Access** | Who may see or do what? | `org → workspace → member/role`, plus attribute-based policy | Continuously |

**The current design fuses the organisational axis into the tenancy path.**
ADR-0005 defines `organization → legal_entity → line_of_business → data_domain →
project → datasource` as *the tenancy hierarchy*, and ADR-0017 proposes making
`data_domain` a real tenancy level with a tenancy path stamped on every graph node
and edge.

That is a mistake, and it is the same mistake Collibra makes with
Communities/Domains — the one their own implementation partners identify as the
root cause of stalled banking rollouts, because banks model "Community = LOB" for
permissions and "Line of Business asset" for taxonomy and then cannot reconcile the
two.

The concrete failure mode: **a bank reorganises its lines of business roughly every
18–36 months.** If LOB is a tenancy level, a reorg is a data migration across every
governed table, every graph node, and every audit record — and your audit history
now describes an org chart that no longer exists. If LOB is a *label*, a reorg is an
update to a classification tree, and history stays truthful because the label was
versioned.

### The proposal

- **Tenancy path stays short and stable: `organization → workspace`.** Two levels.
  A workspace is the unit of membership, grant, billing, and blast radius.
- **LOB / sub-LOB / domain / sub-domain become a versioned classification tree**
  (the *business graph*), attached to assets by many-to-many assignment with
  provenance and effective dates. An asset can belong to two domains; a domain can
  span three workspaces. Both are normal, and neither is expressible today.
- **Isolation that must be hard — Chinese walls between, say, an advisory desk and
  a trading desk — is expressed as an explicit `isolation_boundary`,** a small,
  auditable, rarely-changing set. Not every LOB needs one; the ones that do get one.
- **Everything else is attribute-based policy** keyed on classification, domain,
  certification, purpose and principal kind. This is the Databricks ABAC insight —
  governance by classification, not governance by enumeration — and it is the one
  genuinely superior access design in the market.

This costs one migration now and saves every reorg afterwards. It also collapses
ADR-0005 and the proposed ADR-0017 into something considerably smaller.

---

## 3. Position: knowledge is compiled, not authored

The wiki is not a CMS bolted to a catalogue. It is a **build target**.

> A wiki page is the *compiled output* of catalog + semantics + lineage + glossary +
> uploaded documents, at pinned versions, through named generators, with per-block
> provenance.

Consequences, each of which is a design requirement:

- A page is an ordered list of **blocks**. Each block records its **generator**
  (deterministic template, or a specific model route version), its **inputs** (the
  exact records and versions consumed), and its **provenance class** (derived,
  inferred, or human).
- When an input changes, dependent blocks go **stale**, not wrong. Deterministic
  blocks recompile automatically. Inferred blocks recompile into a **proposal**.
- A human edit **pins** a block. Recompilation then produces a diff proposal against
  the pin; it never silently overwrites a human. This is the property that decides
  whether stewards trust the system after month three.
- Provenance is visible in the UI at block level. Select Star renders AI-suggested
  text grey and human-confirmed text black; that instinct is right and should be
  generalised from field level to document level.

Nobody ships this. Alation has the document structure (Articles, Article Groups,
Document Hubs, templates with typed custom fields and per-hub permissions) but
generation is not AI-native. Secoda and Select Star have AI-native generation but
only at field level — a description on a column, not a document about a domain.
The combination — **structured, compiled, provenance-tracked, review-gated
knowledge** — is uncontested space, and it is exactly what the requirement asks for.

---

## 4. Position: the deterministic/LLM boundary is a data model decision, not a prompt

"Mostly deterministic, LLM for descriptions and business names" is the right
instinct. It needs to be enforced structurally, not by convention.

The rule: **a model may propose a value for a field whose correctness is a matter of
language. It may never propose a value whose correctness is a matter of fact.**

| Field class | Source | Example | Review |
|---|---|---|---|
| **Fact** | Deterministic only. A model may never write it. | Column type, nullability, PK/FK, row count, dependency edge, join cardinality, SQL text | None — it is measured |
| **Judgement** | Deterministic rules first; model only where rules abstain | Table role (fact/dim/bridge/history), grain, candidate join, PII classification | Threshold-gated |
| **Language** | Model proposes, human disposes | Business name, description, synonym, analytical question, page prose | Always reviewable |

Two structural enforcements make this real rather than aspirational:

1. **The model returns a typed proposal object, never a value written to a fact
   field.** The write path for fact fields has no code path that accepts model
   output — this is the existing INV-3 and it is genuinely well done.
2. **Model-only inference is confidence-capped below the auto-publish threshold.**
   The existing design already does this (capped at 0.70 against a 0.95 gate), which
   means a model-only conclusion *structurally cannot* self-publish. Keep this. It
   is the cleanest single control in the current architecture.

The corollary worth stating: **volume of LLM calls should fall as the estate
matures, not rise.** One inference per table family, not per table. Cached and
versioned. If model spend scales linearly with column count, the design is wrong.

---

## 5. Position: the execution choke point is the asset — extend it, do not dilute it

The existing INV-2 / ADR-0004 — every source query passes through one Query
Execution Gateway — is correct, is genuinely implemented, and is the hardest thing
in the codebase for a competitor to copy, because it is an *absence* property
(there is no other path) rather than a feature.

The audit confirms it holds: `execute_read_query` has exactly one call site.

Two things follow.

**First, it must be mechanically enforced, and today it is not.** The import-linter
contract that would prove no second execution path can be introduced (`QG-7`) does
not exist in `pyproject.toml`. The invariants document and ADR-0004 both describe
this contract as the enforcement mechanism, in the present tense. It is the single
highest-value few-hours-of-work item in the entire backlog: it converts the
platform's most-marketed property from a convention into a proof.

**Second, cross-source federation must not break it.** The requirement asks for data
pulled from multiple sources, joined, and returned through one tool. The naive
implementation opens two connections and joins in application code — which creates
a second execution path and destroys the invariant.

The design that preserves it:

> A federated tool compiles to a **plan of per-source leaf queries**, each of which
> is validated, costed, policy-checked and executed **through the same gateway**,
> against exactly one datasource. Results land in a bounded, ephemeral, in-process
> relational engine (DuckDB) where the join executes. The join layer never holds a
> connection and never sees a credential.

This keeps one choke point, keeps per-source policy and masking intact, makes cost
control per-leaf, and makes the federated result set bounded by construction. It
also means cross-source joins inherit the strictest masking of any participating
source, which is the correct default for a bank.

---

## 6. Position: the stack is heavier than the problem

You asked whether the stack should be re-decided. It should, and the honest answer
is that it should get **smaller**.

| Component | Verdict | Reasoning |
|---|---|---|
| **PostgreSQL** | Keep, authoritative | Correct call. One authoritative store with rebuildable projections is the right shape and is already implemented. |
| **pgvector** | **Add** | Retrieval is lexical-only today, with the code's own comment deferring vectors to an unscheduled "Phase 2." Wiki search, document mapping, semantic asset search and agent context retrieval all need it. This is not optional for the stated product. |
| **Temporal** | Keep | Genuinely earns it. Long-running ingestion, scans and analysis DAGs need durable execution with heartbeats, cancel and resume. Nothing lighter does this honestly. |
| **Neo4j** | **Drop** | The graph is explicitly a rebuildable projection, traversal is bounded to 1–4 hops, and the requirement is metadata-only. A well-indexed Postgres edge table with recursive CTEs serves bounded traversal at this scale. Neo4j costs a second datastore, a rebuild SLO nobody has ever drilled, a projection-lag failure mode, licensing, and operator training — for traversal depth the product deliberately refuses to offer. Keep the projection *abstraction* so it can be reintroduced if traversal requirements genuinely change. |
| **Kafka** | **Defer** | Every event already originates from a transactional Postgres outbox — which is the correct pattern and the hard part. Kafka currently distributes those events to consumers that are all internal. Until there is a real external consumer, the outbox plus a worker is sufficient, and Kafka is a broker to run, secure, monitor and drill inside a bank for no delivered capability. Keep the envelope and the topic naming so the migration is a transport swap. |
| **Python** | Keep for the core | `sqlglot` has no equivalent in any other ecosystem, and it is doing the load-bearing work in the gateway, the tool renderer and lineage extraction. Rewriting the SQL-analysis core in C# would mean rebuilding a multi-dialect SQL parser, which is a multi-year project on its own. If the team is a C# shop, the pragmatic split is a Python metadata/SQL core with C# surfaces around it — not a rewrite. |
| **Vanilla-JS UI** | **Replace** | A 1,500-line `app.js` plus four feature modules is already carrying more surfaces than it declares, and the product needs a wiki editor, a lineage review canvas, a graph explorer and a tool studio. This is the one place where "start from scratch" is the right answer. |

Net effect: **two fewer distributed stores to run in a regulated environment, three
fewer overdue operational drills, one added Postgres extension.** For a bank, the
number of independently-failing stateful services in the topology is itself a
compliance and staffing cost.

---

## 7. Position: do not start from scratch

You said you were ready to. I do not think you should, and the reason is specific.

The audit found ~34,600 lines of real working logic — not scaffolding. Six modules
were sampled directly and every one contained genuine algorithmic work: SQLGlot AST
parsing and transformation, a regex rule engine, JSON-RPC dispatch, driver-level
cursor code across five real connectors, state machines with real guards. Zero
`TODO`/`FIXME` markers in `src/`. Six `NotImplementedError`s, all in an abstract base
class where they belong. 338 test functions. 34 migrations, one per feature.

That is not a prototype. That is a codebase somebody built carefully.

What is wrong with it is **structural, not substantive**: the modular decomposition
described across the architecture documents has 1 of 21 modules built, and that one
is a stub, so essentially all logic still lives in a flat `src/aida/` package with no
enforced boundaries. That is a refactor. Refactors are tractable; rebuilding a
validated SQL execution gateway, five certified connectors and an AST-safe tool
renderer is not.

**Recommendation: keep the engine, restructure the chassis, and add four genuinely
new bounded contexts** — Knowledge (wiki), Document Ingestion, Transformation
Analysis (view/procedure parsing), and Federation. Those four are where the
greenfield energy should go, because they are the four things the product needs and
does not have.

---

## 8. Position: "production-grade" has a definition, and it is currently unmet

You said production, not MVP. Then this list is not optional, and none of it is
architecture — it is evidence:

- **There is no CI pipeline.** No `.github/workflows`. Multiple documents state that
  checks "fail CI." Nothing fails CI, because there is no CI.
- **Import-linter has two narrow contracts** and no layering contract over the
  50-module flat package. The boundaries the architecture is built on are unenforced.
- **Every operational drill has never been run.** Projection rebuild, PITR restore,
  Temporal failover, credential rotation, kill switch, regional failover,
  break-glass — all "Never," all overdue against their own stated cadence. The kill
  switch in particular is a control the AI-safety document leans on heavily.
- **Performance is "Not measured."** The vision document publishes p95 targets
  (control plane <300ms, authz <50ms, 1M+ objects) that have never been tested.
- **No penetration test, no SOC 2, no ISO 27001.** The security document says so
  honestly, which is to its credit, but a bank's third-party risk process stops here.

A platform can be architecturally excellent and still be un-shippable into a bank
for these reasons alone. The gap plan sequences them.

---

## 9. What to take from the market, and what to refuse

**Take:**

| From | What | Why |
|---|---|---|
| Databricks UC | ABAC: tag-driven row filters and column masks that auto-apply to every current *and future* matching object | Governance by classification is the only access model that survives estate growth |
| Databricks Genie | The curated knowledge store: verified question→SQL pairs as ground truth, benchmark suites with expected answers, in-session knowledge extraction proposed for admin approval | This is the correct architecture for a SQL agent, and it is a curation loop, not a model choice |
| Alation | Articles / Article Groups / Document Hubs / typed templates with per-hub permissions | The proven shape for a structured knowledge layer |
| Alation | Behavioural analysis: query-log-driven popularity feeding *stewardship prioritisation and DQ rule suggestion from the same signal* | Tells you what to document first, which is the difference between a used catalogue and shelfware |
| Atlan | Personas (role bundles) vs Purposes (tag-driven policy that applies to future assets too) | The cleanest separation of "who you are" from "what the data is" in the market |
| Select Star | Visual provenance: AI-suggested text rendered differently from human-confirmed text | Cheap to build, disproportionate effect on steward trust |
| Purview | Glossary terms that *carry* access policy, cascading to anything they are attached to | Makes the business glossary load-bearing instead of decorative |

**Refuse:**

- **Collibra's operating-model burden.** Its flexibility is a curation tax; reviewers
  cite a steep learning curve on hierarchies and lineage configuration, run-rate
  staffing reported at multiples of licence cost, and a ~25-month average
  time-to-ROI. Ship an opinionated default model and let it be extended, rather than
  requiring one to be designed before value appears.
- **The two-catalogue seam.** Microsoft ships both a Fabric OneLake catalogue and
  Purview Unified Catalog and tells customers they need both. Do not create an
  internal equivalent — one catalogue, one identity for an object.
- **Marketing architecture.** Atlan's "Context Lakehouse" (Iceberg + knowledge graph
  + vector search) does not reconcile with its own documented Cassandra/Elasticsearch
  /Atlas-fork backend. Whatever is written in the architecture documents here should
  be the thing that is running.
- **Unbounded agent write paths.** Both Atlan's and Secoda's MCP servers expose live
  SQL execution to agents (`query_asset`, `run_sql`). That is defensible for them and
  wrong here — the entire differentiator is that an agent gets *approved tools*, not
  a SQL socket.
