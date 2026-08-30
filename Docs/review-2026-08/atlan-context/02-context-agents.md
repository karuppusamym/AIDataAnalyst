# Atlan Context Agents — review against Atlas/AIDA

> Status: Review input, 2026-08-30. Scope: screenshots `image18`–`image28` of
> atlan.com/context-agents plus the customer-quote block in the captured document.
> Sibling reviews cover the other screenshot ranges; this file only claims what is
> visible in images 18–28.
>
> Every claim about Atlan below names the image it is read off. Marketing adjectives
> in the captured text are ignored; the quotes are used only where they name a
> *mechanism* (what was read, from what) or a *number* (the 80% bar).
>
> Prior coverage checked before writing: `research/02-atlan.md` (covers Atlan AI and
> the MCP server generally, **not** the named Context Agents), `20-modules/08` GL-9
> (added the same day, covers Scribe/Doc description drafting), `20-modules/18` ST-8
> (covers usage-derived eval corpora), `20-modules/12` RT-6 (usage signal, P1, not
> started). Rows below say where they overlap.

---

## 1. Findings

| # | Capability (image) | What Atlan does | What we have | Verdict | Cost | Slots into |
|---|---|---|---|---|---|---|
| F1 | **Scout — usage & query-footprint mining** (19, 28) | Scans warehouse SQL query history across teams; ranks assets by query count; assigns enrichment priority. Reverse-engineers the business questions users ask | `query_history=True` is **advertised** on the Snowflake connector (`connectors/snowflake.py:749`) and **no code consumes it** — there is no `get_query_history()` on `Connector`. The idea is named in three docs with no owner: `target/01` §5 (`popularity × downstream_impact × documentation_deficit`), module 12 RT-6, module 18 ST-8 | **Gap worth closing — the single highest-value item in this review** | 3 wks | New `gap/02` §4 row **N20**; unblocks RT-6, ST-8, GL-9 |
| F2 | **Do their agents read data values?** (19–27) | **No.** Scout reads query counts (19); Scribe reads SQL patterns, column names, lineage (20); Lexis reads column naming patterns and existing definitions (21); Doc reads descriptions + lineage (22); Nexus reads column names vs. terms (23); Sage reads metric SQL (24); Atlas reads descriptions + metadata (25); Orion reads terms and domains (27). Only Vera (26) touches values, and it emits completeness/accuracy/freshness **scores**, not samples | Module 05 already computes value-free statistics at the source and emits statistics only (`20-modules/05` §"Computed (value-free)"). INV-6 forbids values in the *control plane*, not statistics computed *in* the source | **They are not doing what we forbade. Say so, loudly** | 0 | `10-architecture/01` INV-6 commentary |
| F3 | **80% bar + "review a draft, not a blank page"** (28 panel 2/3; quotes L50, L94) | Composite confidence per output (accuracy, clarity, style, completeness); high-confidence **auto-applies**; low routes to a human. Stewards "shift from documentation to certification — sampling, validating" (28 panel 3), "One click. Not 847 manual reviews" | 0.70 model-only cap vs. 0.95 auto-publish gate (`30-contracts/09` §127, `90-reference/04` §105). GL-9 already scopes drafting + scoring with mandatory review. Bulk maker-checker exists for ownership/links/certification (module 08 §7, cap 500) and for relationship candidates (RL-6) — **not** for drafted prose | **Keep the cap. Our bottleneck is review throughput, not the cap** — close it with acceptance sampling | 3 wks | Amend `20-modules/08` GL-9; new **GL-11** |
| F4 | **Orion — term meaning per context** (27) | One term graph where "Revenue" resolves differently in Finance vs. Product vs. Marketing; the agent gets the answer *for the asking context* | **Structurally impossible today.** `GlossaryTerm.__table_args__ = UniqueConstraint("organization_id", "term_key")` (`src/aida/models.py:2324`) — one definition per term per organization. Module 08 §6 retains both sides of a conflict but resolves to one | **Gap worth closing, and it matters more to a bank than to Atlan** | 4 wks | New `20-modules/08` **GL-10**; depends on N9 (shipped) |
| F5 | **Sage — metric conflict detection** (24) | Detects two teams defining `MRR` with different SQL formulas; states the consequence ("AI agents querying MRR return different numbers"); routes to both named stewards for approval | Module 08 §6 detects **exact synonym collisions between glossary terms**. Nothing compares **metric formulas**. `SemanticMetric`/`SemanticMetricVersion` exist and are versioned; no cross-metric collision scan | **Gap worth closing — small, and the substrate is already there** | 2 wks | New `20-modules/07` **SM-8** |
| F6 | **A fleet of twelve named agents** (18, 19–27) | Scout, Scribe, Lexis, Doc, Nexus, Sage, Atlas, Vera, Orion + 3 more, staged Foundational → Derived → Compounded, each with a "works best with" pairing (18) | P7 ("the unit of work is a task in a DAG, not an agent"), ADR-0002 and ADR-0008 reject exactly this. We have one agent runtime (module 13) + N15 registry | **Decline the fleet. Take the presentation** — every agent card (19, 21, 23, 25, 26) shows a numbered TASK PLAN naming inputs, scoring step and routing step. That is our evidence model, rendered | 1 wk (UI) | `20-modules/13` §6 → `N19` UI track |
| F7 | **Nexus — confidence-scored term↔column linking** (23) | Maps `net_revenue_usd` → "Net Revenue" at 98%; task plan step 3 "score confidence on each mapping", step 4 "surface low-confidence mappings for review" | GL-8 **DONE**: reviewed inferred term links from approved annotations. Module 08 §11 states inference is "deterministic exact matching… not fuzzy or model-generated". Module 08 §12 lists fuzzy/model ranking as remaining work | **Already covered in shape; the remaining delta is calibration, not design** | — | Existing module 08 §12 row |
| F8 | **Doc — README generation with inline provenance** (22) | Generates a dataset README (Overview / Source tables / Key columns), tagged `auto-generated`, with **"Lineage confirmed: upstream from billing.subscriptions, downstream to exec.revenue_dashboard"** written into the prose | N10 knowledge compilation (10–12 wks) is the equivalent and is larger and better specified. GL-9 covers the description half | **Already covered — but steal the inline citation form**: a claim that names its evidence in the sentence, not in a sidebar | 0 (fold into N10) | `gap/02` N10 acceptance criteria |
| F9 | **Atlas — domain tagging at scale** (25) | Reads Scribe descriptions + usage signals, matches against domain patterns, scores domain fit, "apply domain tag **or route to steward**" | N9 business graph **SHIPPED**: `business_assignment` carries `assignment_kind ∈ {MANUAL, RULE, INFERRED}`, `confidence`, `confirmed_by` (`target/01` §6). The routing fork is already modelled | **Already covered.** Only the *producer* of the inferred assignment is missing, and F1 is its best input | — | — |
| F10 | **Vera — composite quality scoring, auto-apply** (26) | Identifies critical assets by downstream dependency, runs completeness/accuracy/freshness, computes a composite score, "auto-apply **or** route to steward" | Module 05 profiling, `trust_scoring.py` (composite, explainable, factor-weighted), W1 quality-gates-runtime whitespace, C5 folding module 11 into profiling + policy | **Already covered, and our version is better** (factors are inspectable, and the score gates *runtime* not just a badge). Decline auto-applying a score as a **certification** — certification is a governed object under INV-8 | — | `gap/02` C5 unchanged |
| F11 | **"87% of customers say Context Agents write higher quality content than humans"** (18) | Vendor survey, no denominator, no rubric | — | **Ignore.** Not evidence. The 80%-accuracy quote (L94) is weak but at least names a gatekeeper population | — | — |

---

## 2. The value-free question — answered, and it is good news

This was the sharpest question put to this review: *is Atlan's quality coming from
something we have structurally forbidden ourselves?*

**No.** Read the task plans off the screenshots rather than the copy:

| Agent | Declared inputs (image) | Values? |
|---|---|---|
| Scout | "Scan SQL query history", "identify top-queried assets by team, frequency, and function", "rank by usage score" (19) | Counts only |
| Scribe | "SQL patterns, column names, and lineage" (20) — the generated description cites `stripe.charges` as UPSTREAM | Metadata + lineage |
| Lexis | "Scan column naming patterns", "read existing definitions and domain conventions" (21) | Identifiers + prior prose |
| Doc | Descriptions, usage signals, lineage (22) | Derived text |
| Nexus | "Read technical column names from the schema", "match against Lexis glossary terms" (23) | Identifiers |
| Sage | Two metric SQL formulas (24) | SQL text |
| Atlas | "Read Scribe descriptions and usage signals", "match asset metadata against domain patterns" (25) | Derived text + metadata |
| Vera | Completeness / accuracy / freshness checks (26) | **Touches rows, emits 94% / 87% / 100%** |
| Orion | Terms, domains, assets (27) | Graph only |

Eight of nine run on exactly the input set ADR-0014 already permits us: identifiers,
types, constraints, lineage, SQL text, dbt logic, and prior human prose. The ninth
(Vera) is a data-quality profiler, and *our own module 05 already does that class of
work* — it reads rows inside the source and emits statistics. INV-6 says source values
do not enter platform tables, logs, traces, events, profiles or model context. Vera's
output is a percentage. It does not breach INV-6 and neither does ours.

The customer quotes corroborate this rather than contradict it — every one that names
a mechanism names a metadata mechanism: *"from lineage, SQL logic, dbt logic"* (L85),
*"just from column names and lineage"* (L63), *"parsed our query behavior"* (L72),
*"reverse-engineering raw query footprints"* (L100), *"defining audit columns and
system attributes"* (L69). Not one says "read the data".

**Recommended text change.** `Docs/10-architecture/01-principles-and-invariants.md`,
INV-6, after the "Enforcement" paragraph, add:

> **What the constraint does not cost.** A 2026 review of Atlan's Context Agents
> (`review-2026-08/atlan-context/02-context-agents.md`) found that eight of its nine
> published context agents consume only identifiers, lineage, SQL text, dbt logic and
> prior human prose — the same input set ADR-0014 permits. The ninth is a data-quality
> profiler that emits statistics, which module 05 also does. The market's best
> published results in this category are therefore **not** evidence that value-freedom
> costs quality. The measured cost of INV-6 is narrower and is stated where it bites:
> literal redaction removes filter predicates from stored SQL (`src/aida/sql_redaction.py`
> module docstring).

That is worth writing down because the opposite belief — that we are handicapped —
is the kind of thing that gets an invariant quietly relaxed.

---

## 3. Query-footprint mining (F1) — the item that should change the plan

### 3.1 What they actually do

Scout (19) scans warehouse query history, counts queries per asset (`947 queries`,
`883`, `561`…), tags a Gold Layer, and **ranks enrichment priority by usage**. Panel 1
of image 28 states the strategy plainly: *"Most of your catalog nobody touches. Context
Agents identify your Gold Layer, Popular BI, Popular SQL, and upstream dependencies
first."* The customers who describe the highest-value inference describe this same
substrate: *"decodes the inner logic of our data through actual usage footprints"*
(L97), *"by reverse-engineering raw query footprints, the system automatically captured
the exact business questions our users naturally ask"* (L100), *"standard dictionaries
completely miss [our jargon]. The system parsed our query behavior and accurately
translated that unique jargon"* (L72).

Note what the last one claims: production SQL is a **better dictionary than the
dictionary**, because a query proves which columns co-occur, which are filtered
together, which are grouped by, and what an analyst named the result.

### 3.2 What we have, precisely

Two different corpora, and conflating them is the trap:

**(a) Our own gateway traffic.** `query_gateway.py:350` redacts literals and
`:416`/`:548` persist `redacted_sql` on every `QueryExecution`. We already hold a
literal-free, tenant-scoped, audited corpus of every query the platform ran. This is
excellent — and at cold start it is **empty**, because it only contains queries we
originated. Mining it solves the steady-state problem and not the cold-start problem.

**(b) The warehouse's own query history.** This is what Scout mines and what the
customers are describing: every analyst, every BI extract, every dbt run, going back
months, present on day one. We **advertise** access to it and do not have it:

```
src/aida/connectors/base.py:14      query_history: bool = False
src/aida/connectors/snowflake.py:749  query_history=True
src/aida/connectors/bigquery.py:610   query_history=False
```

`grep -rn query_history src/aida --include=*.py` returns those three lines and nothing
else. There is no `get_query_history()` on `Connector` — module 02 §49 lists it in the
interface, and `Docs/20-modules/00-module-index.md` row 09 already concedes *"No
view-DDL, procedure or query-log parser exists"*.

**This is also a live INV-9 defect.** `gap/06` §7.1 states that
`ingestion.default_capabilities` returns the hand-written dict verbatim and that
`query_history` is among the flags *"never certified"*. So we advertise a capability we
have not built, against the one invariant whose whole point is not doing that.

### 3.3 Does mining it break INV-6?

No, and the reasoning is already written in our own codebase.
`src/aida/sql_redaction.py`'s module docstring says it plainly:

> *"Lineage does not depend on literal values, so the main consumer loses nothing:
> `SELECT a FROM t WHERE x = :redacted` parses to the same column graph as the
> original. What is lost is the ability to read a filter predicate later — 'this view
> excludes test accounts' is visible in the raw text and not in the redacted one."*

Everything Scout needs survives redaction: the referenced tables, the projected
columns, the join keys, the `GROUP BY` columns, the filtered columns, the aggregate
functions, the result aliases, the distinct-user count, the frequency. What is lost is
the *value* in `WHERE contract_type = 'new'` — the column is kept, the string is not.
Popularity ranking loses nothing. Business-question reconstruction loses a modifier:
we can say *"analysts group revenue by segment and filter on contract_type"* and not
*"…for new contracts"*.

That is the honest cost and it is small relative to the win. Redaction must happen
**at ingestion, before the statement is persisted**, on the same path as
`d5f8b21c4a03` took for view DDL and routine bodies — the leak that migration fixed is
exactly the leak a raw query-history table would reintroduce, at far greater volume.

### 3.4 Recommended change

Add to `Docs/review-2026-08/gap/02-gap-diff-and-plan.md` §4, after N19:

> | N20 | **Query-history ingestion + usage scoring** — `Connector.get_query_history()` (Snowflake `ACCOUNT_USAGE.QUERY_HISTORY`/`ACCESS_HISTORY`, BigQuery `INFORMATION_SCHEMA.JOBS`), redacted at ingestion via `sql_redaction` before persistence, parsed with the existing `sqlglot` path into per-asset read counts, distinct-consumer counts, co-occurring columns, filter/group-by column frequency, and an `enrichment_priority = popularity × downstream_impact × documentation_deficit` (`target/01` §5) | 3 | Low | One ingestion capability with four consumers: RT-6 usage ranking, GL-9 review prioritisation, ST-8 eval corpus, N9 domain-assignment evidence. It is also the only asset that is rich on day one, which makes it the cold-start answer |

And in the §7 sequence, N20 belongs in **Phase 2**, before N10 — knowledge
compilation without a priority order compiles 40,000 pages nobody reads.

Two enforcement clauses to add with it:

1. `Docs/20-modules/02-connectivity.md` — until `get_query_history()` exists and is
   certified, flip `query_history=True` to `False` on Snowflake. INV-9 is not a
   principle we get to hold for connectors and break for ourselves.
2. `Docs/20-modules/05-profiling-and-classification.md` §"Not computed by default" —
   add a row: *query text is stored redacted-only; a raw query-history table is a
   value-plane object and is forbidden*.

---

## 4. The 80% bar and the cold-start argument (F3)

### 4.1 The question, restated fairly

Our design forbids auto-publish for anything a model inferred alone: 0.70 cap against
a 0.95 gate. A description is *definitionally* model-only — no deterministic rule
writes prose — so **no drafted description can ever auto-publish under the current
tiering**. Atlan ships drafts at scale and lets stewards correct (28 panel 2). One
customer put a batch in front of *"our most rigorous enterprise data gatekeepers"* and
it *"cleared an 80% accuracy baseline immediately"* (L94). Another says the point
directly: *"significantly faster for our producers to review and fix an existing draft
than to write one from scratch"* (L50).

Is the cap the reason we would never reach their speed?

### 4.2 No. Read panel 3 of image 28 again

> *"Stewards shift from documentation to certification — **sampling**, validating, and
> resolving the cases that require judgment. **One click. Not 847 manual reviews.**"*

Their speed comes from three things, in order of contribution:

1. **Targeting.** They enrich the Gold Layer and Popular SQL first (28 panel 1) — the
   2–5% of assets anyone queries. The estate is not 40,000 tables; it is 900.
   This is F1, and it is a *prioritisation* win with no authority implications at all.
2. **Batch as the unit of review.** 847 drafts, one decision. Not 847 decisions
   skipped — 847 decisions *aggregated*.
3. **Sampling as the acceptance discipline.** The steward inspects a sample, judges the
   batch, accepts or rejects the batch.

None of those three requires a model's output to become authoritative without a human.
The cap is not the bottleneck. **The bottleneck is that our review queue's unit of work
is one item.**

### 4.3 What we already have, and the one piece missing

Module 08 §7 already ships a bulk maker-checker contract: up to 500 subjects, one
independent review, applied atomically, *"partial success is not treated as approval"*.
RL-6 does the same for relationship candidates. So the primitive exists — it just does
not cover drafted prose, and it has **no sampling discipline**: the reviewer either
eyeballs 500 rows or rubber-stamps them, and module 06 §60 already warns about exactly
that failure mode (*"a reviewer who cannot see why will either rubber-stamp or reject
everything"*).

### 4.4 Recommended change

Amend `Docs/20-modules/08-glossary-and-stewardship.md` §13, GL-9, replacing the final
sentence (*"GL-9 is scoped as: draft + confidence score…"*) with:

> GL-9 is scoped as: draft + composite confidence score (accuracy, clarity, style,
> completeness), always routed through the existing maker-checker path (§6/§7), with
> the score used to set review **priority and batch membership** — never to skip review.
> Model-only drafts remain capped at 0.70 (`90-reference/04` §4) and therefore can never
> auto-publish; this is deliberate and is not the throughput constraint. Throughput
> comes from GL-11.

And add a new row:

> | GL-11 | **Acceptance-sampling batch review for drafted content.** A drafting run
> produces a *batch* (scoped by source, schema, domain, or enrichment-priority band).
> The reviewer is shown (a) the batch's size, score distribution and provenance mix,
> (b) a **randomly drawn, seeded, reproducible sample** sized by the batch's confidence
> band, and (c) the full list on demand. One decision applies to the batch atomically,
> under the existing §7 bulk contract and INV-8. The audit record stores the sample
> seed, the drawn member IDs, the per-item verdicts, and the resulting batch verdict —
> so an auditor can re-draw the identical sample and ask why it was accepted. Rejecting
> any sampled item rejects the batch and returns it for redrafting; a batch may not be
> re-submitted with the same seed. | TODO | P1 |

This is worth arguing for on its own terms rather than because Atlan does it.
Acceptance sampling is how regulated manufacturing has certified batches for seventy
years, it is a discipline a bank's audit function already recognises, and it gives a
*stronger* evidence record than 847 individual approvals — because 847 individual
approvals under time pressure are 847 unexamined clicks, and the audit trail cannot
tell the difference. A documented sample plan can.

Two boundaries that must not move:
- Batching is for **language fields only** — descriptions, business names, synonyms,
  READMEs, analytical questions. Classifications, tools, policies, model routes and
  metric definitions keep per-item review at any confidence (`30-contracts/09` §128).
- The maker of a batch is the drafting run's initiator, and INV-8 applies to the batch
  as it does to an item.

**Cost: 3 weeks.** Roughly two of them are the audit/replay record, which is the part
that makes it acceptable rather than the part that makes it fast.

---

## 5. Context-scoped terms (F4) — the one place our model is simply wrong for a bank

Orion (27) renders a graph in which `Revenue`, `ARR`, `MRR`, `LTV`, `CAC`, `Churn`,
`NPS`, `Sessions` and `Deploys` connect to Finance, Product, Marketing and Engineering,
and the stated job is: *"when an agent asks what 'revenue' means, it gets the right
answer for the right context."*

We cannot represent that:

```
src/aida/models.py:2322-2324
class GlossaryTerm(Base, TimestampMixin):
    __tablename__ = "glossary_term"
    __table_args__ = (UniqueConstraint("organization_id", "term_key"),)
```

One `term_key` per organization. `GlossaryTermVersion` versions a definition *through
time*, not *across context*. Module 08 §6 retains both sides of a conflict as durable
evidence — which is right, and better than last-write-wins — but resolution collapses
to a single definition.

**Why this is worse for us than for Atlan.** A CPG company can probably agree on one
definition of "revenue". A bank cannot agree on one definition of *exposure*,
*balance*, *default*, *customer*, or *limit* — Retail, Markets, Treasury and Risk mean
genuinely different, individually correct things, and several of those differences are
mandated by regulation rather than by sloppiness. Forcing one definition per
organization does not resolve the disagreement; it hides it, and then an agent answers
a Markets question with a Retail definition and nobody can see that it did.

Note also that Atlan's own product is internally inconsistent here: Sage (24) *"locks
in one answer"* while Orion (27) maps meaning per context. Ours should not copy the
tension — it should pick per-context resolution and treat single-answer resolution as
the special case where the contexts agree.

**Recommended change.** Add to `Docs/20-modules/08-glossary-and-stewardship.md` §13:

> | GL-10 | **Context-scoped term definitions.** A `glossary_term` becomes unique on
> `(organization_id, term_key, business_node_id)` where `business_node_id` is nullable
> — `NULL` meaning the enterprise-default definition. A term may carry an enterprise
> definition plus per-LOB or per-domain overrides, each independently versioned, owned
> and approved. Resolution is **most-specific-wins along the business-graph ancestry**
> (N9's recursive CTE traversal already provides the walk), and every resolution
> returns the node it resolved at, so an answer can say *which* definition it used.
> A conflict (§6) between two definitions at the **same** node stays a conflict; two
> definitions at **different** nodes are not a conflict and must stop being detected as
> one. | TODO | P0 |

Also correct `Docs/20-modules/07-semantic-layer.md` §12 SM-2 (*"Glossary term binding
to semantic objects"*) to say the binding resolves through the business graph, not to a
single global term.

**Cost: 4 weeks** — one for the migration and uniqueness change, one for resolution and
the "resolved at" field on every read path, one for the conflict-detector correction,
one for the review UI. It depends on N9, which has shipped, so it is unblocked now.

---

## 6. Metric conflict detection (F5)

Sage (24) shows two `MRR` definitions side by side — Finance's
`SUM(net_arr_usd) WHERE contract_type = 'new'` against Sales'
`SUM(new_bookings_arr) + SUM(committed_arr_usd)` — states the consequence in the
agent's terms (*"AI agents querying MRR return different numbers depending on which
team's definition they hit"*), and routes it to two named stewards, both `pending`.

We have `SemanticMetric` / `SemanticMetricVersion`, versioned with grain and physical
mappings and maker-checker (module 07 §11). We detect **glossary synonym collisions**
(module 08 §6). We do not detect **two metrics that compute the same thing
differently**, which is the collision that actually corrupts an answer.

Verdict: **gap worth closing, small.** The substrate exists; what is missing is a
bounded scan.

**Recommended change.** Add to `Docs/20-modules/07-semantic-layer.md` §12:

> | SM-8 | **Metric collision detection.** A bounded scan over approved metric versions
> flags pairs that (a) share a normalised display name or synonym, or (b) project the
> same measure column set at the same grain via different expressions. Each flagged
> pair opens a conflict through module 08 §6, retaining both positions and routing to
> both owning stewards. The comparison runs on the **parsed expression shape**
> (`sqlglot`), not on raw text, so formatting differences do not generate noise — and
> literals are compared as `:redacted` placeholders, so two formulas differing *only*
> in a filter value are surfaced as `PREDICATE_DIFFERS_VALUE_UNKNOWN` rather than as
> equivalent. Bounded to 5,000 metric versions and 100 conflicts per request, matching
> module 08 §11. | P1 |

Note the redaction interaction and be honest about it: image 24's conflict is legible
*because* the literal `'new'` is visible. Under INV-6 we would see
`contract_type = :redacted` and could still tell the two formulas apart — the measure
columns and the aggregate structure differ — but where two metrics differ *only* by a
filter value, we can flag that they differ and not say how. That is a genuine, bounded
degradation. It should be stated in the row rather than discovered later, which is why
it is in the row.

**Cost: 2 weeks.**

---

## 7. Is the agent fleet an architecture? (F6)

**No. It is a decomposition of one pipeline into twelve named cards, and the naming is
doing real work — for their users, and it could for ours.**

Image 18 gives it away: the agents are arranged as a "team" with a "works best with"
pairing (*"Vera works best with Orion to ensure only trusted data reaches AI"*), and
images 19–27 stage them **Foundational → Derived → Compounded** — which is a
*dependency order*, i.e. a DAG. Scout must run before Scribe (Scribe consumes usage);
Lexis before Nexus (Nexus matches against Lexis terms, stated in its step 2, image 23);
Scribe before Atlas (Atlas reads Scribe descriptions, step 1, image 25). That is
precisely P7: *"the unit of work is a task in a DAG, not an agent."* We already
concluded this in ADR-0002 and ADR-0008, and nothing in these screenshots is a reason
to reopen it. **Decline.**

But there is something real here and it is not marketing. Every agent card shows a
numbered **TASK PLAN** stating what the stage reads, that it scores, and where
low-confidence output goes:

- Scout (19): scan → identify → rank and assign priority
- Lexis (21): scan naming patterns → read existing definitions → extract and draft, *grouped by signal source* → organise by domain
- Nexus (23): read column names → match against terms → **score confidence** → **surface low-confidence for review**
- Atlas (25): read descriptions and usage → match against domain patterns → score fit → **apply or route to steward**
- Vera (26): identify critical assets by downstream dependency → run checks → compute composite → **auto-apply or route to steward**

Every one of those is information our evidence model already captures (module 13 §6:
retrieval selections with ranking reasons, tool selection or generation path, refusals
with the control that fired) and **never shows to anyone**. A steward looking at a
proposal in our review queue sees a confidence number; module 06 §60 is explicit that
this is not enough (*"a confidence number without its reasoning is not reviewable"*).

Lexis's *"grouped by signal source"* is the sharpest single detail on these eleven
screenshots. It is provenance rendered as the organising principle of the output rather
than as a footnote — and it is the thing that would make a batch review (GL-11)
tractable, because a steward can accept "the 340 terms derived from declared FK
constraints" and scrutinise "the 22 derived from name similarity alone" separately.

**Recommended change.** In `Docs/20-modules/13-agent-runtime.md` §6, after the evidence
table, add:

> **Evidence must be rendered, not merely retained.** Every enrichment run presents a
> named, ordered plan — what it read, what it scored, what it emitted, and what it
> routed to a human — and groups its proposals **by signal source** rather than by
> asset. A steward reviewing 400 proposals derived from declared constraints and 20
> derived from name similarity is doing two different jobs; presenting them as one list
> of 420 guarantees that both are done badly. This is a presentation requirement on the
> review surfaces (N19), not a new subsystem: the data is already in the evidence record.

**Cost: 1 week**, on the N19 UI track. This is the cheapest item in this review and
probably the highest ratio of steward-trust gained to engineering spent.

---

## 8. What we should deliberately decline

| Decline | Why |
|---|---|
| **A fleet of named specialist agents** (18) | P7, ADR-0002, ADR-0008. It is a DAG with mascots. Twelve agents means twelve permission surfaces, twelve failure modes and twelve things to certify under N15, to buy a decomposition we already have as stages |
| **Auto-apply of high-confidence drafted prose** (28 panel 2) | The 0.70 cap is the cleanest control in our design (`gap/02` K3) and it is not what costs us speed — §4 shows the speed comes from targeting and batching. Giving it up would buy weeks and cost the architectural (rather than statistical) answer to a model-risk reviewer |
| **Vera-style auto-applied certification** (26) | A quality score may gate runtime (W1) and may rank retrieval. It may not *certify* an asset — certification is a governed object with an owner, a rationale and an expiry (module 08 §7), and INV-8 applies |
| **Sage's "locks in one answer"** (24) | Correct for a startup, wrong for a bank. §5: per-context definitions with recorded resolution, not a forced merge |
| **Storing raw source query history** | The volume version of the leak `d5f8b21c4a03` already fixed. Redact at ingestion or do not ingest |
| **The 87% / "higher quality than humans" framing** (18) | Vendor survey with no denominator or rubric. If we ever publish a comparable number it must name the corpus, the rubric and the reviewer population — the 80% quote (L94) is weak evidence but at least names a population |

---

## 9. Cost summary

| Item | Weeks | Phase | Blocked by |
|---|---|---|---|
| N20 query-history ingestion + usage scoring | 3 | 2 (before N10) | Nothing |
| GL-11 acceptance-sampling batch review | 3 | 2 | Module 08 §7 bulk contract (exists) |
| GL-10 context-scoped term definitions | 4 | 2 | N9 (shipped) |
| SM-8 metric collision detection | 2 | 2 | Nothing |
| Evidence rendering / signal-source grouping | 1 | N19 track | Nothing |
| INV-9 correction: `query_history=False` until certified | 0.1 | Now | Nothing |
| INV-6 commentary (§2) + module 05 forbidden-row | 0.1 | Now | Nothing |
| **Total** | **13.2** | | |

Nothing here proposes to relax an invariant. N20 and GL-10 are additive; GL-11 changes
the *unit* of maker-checker without changing who may approve or what a model may do;
SM-8 and the evidence-rendering item are presentation and detection over data we hold.

---

## 10. Sources

- Screenshots `image18`–`image28`, captured from atlan.com/context-agents into the
  review document; images cited inline by number throughout.
- Customer-quote block, `atlan-context.txt` lines 29, 50, 63, 69, 72, 85, 94, 97, 100,
  103 — cited by line number, used only where they name a mechanism or a number.
- `review-2026-08/research/02-atlan.md` — prior Atlan teardown (Atlan AI and MCP; does
  not cover the named Context Agents).
- Repository evidence: `src/aida/models.py:2322-2324`, `src/aida/connectors/base.py:14`,
  `src/aida/connectors/snowflake.py:749`, `src/aida/connectors/bigquery.py:610`,
  `src/aida/query_gateway.py:350,416,548`, `src/aida/sql_redaction.py` (module
  docstring), `src/aida/trust_scoring.py`, `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` §7.1.
