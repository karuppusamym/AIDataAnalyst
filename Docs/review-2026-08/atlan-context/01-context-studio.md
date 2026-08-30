# Atlan Context Engineering Studio and Context Bootstrapping — cross-validation

> Status: review input, 2026-08-30. Source: `Docs/Atlan-context.docx`, screenshots
> `image1`–`image17` (Context Engineering Studio; Context Bootstrapping) plus the
> explanatory prose at the head of the same document.
> Scope: the object model visible in their UI, checked against our design and our code.
> Every screenshot claim below names its image number.
>
> **Read with `60-delivery/03-tracker.md` §L and §M.** A concurrent session reviewed the
> same 43 screens for *UI patterns* and landed UX-17…UX-21, AT-1…AT-5, ST-8, GL-9 and the
> Trace Explorer correction. This document deliberately does not re-report those. It is
> about the **object model and lifecycle** underneath them, which that pass did not cover.

---

## 1. Findings

| # | What Atlan ships (evidence) | What we have | Verdict | Cost / plan slot |
|---|---|---|---|---|
| F1 | A **Deploy stage**: named consuming agents, each with its protocol and its currently synced version, a rollout progress bar at 68% "Rolling…", "8 synced", "ALL AGENTS AUTO-UPDATED ON DEPLOY" (image 3); "Consuming this repo — Snowflake Cortex, LangGraph, Databricks Genie, Claude (MCP) — ✓ All on v3.1.2" (image 8); `consumed_by: 6 agents` inline on the artifact (image 1) | Consumption is recorded **after the fact** (`ContextProductConsumptionEdge`, CX-4). There is no forward registry of who is bound to what. Worse: publishing v(n+1) sets v(n) to `SUPERSEDED` in the same transaction (`semantic_api.py` ≈ L1000-1012) and every read path filters `status == "PUBLISHED"` (`mcp_server.py` `_read_context_product_resource`), so a consumer pinned to `atlas://context-products/{key}/versions/{n}` gets the anti-enumeration "Resource not found or not accessible" the instant a steward approves the next version | **Gap worth closing — and a live defect.** Version-pinned URIs without a support window are a hard cutover disguised as pinning, and the failure is indistinguishable from a permission denial, so the consuming agent cannot self-diagnose | New **N20**. 0.5 wk for the support window alone (do immediately); 3 wks for the full consumer-binding registry. **Phase 2**, not 3 |
| F2 | A **Simulate stage** that gates Deploy: 47 tests auto-generated from named dashboards and `.sql` files, 41 pass / 6 fail, suite pass-rate 87% (images 2, 12); tests grouped by persona (image 6); a failure names the missing context key (`churn.tier_filter — missing`, image 13) | ST-8 (module 18) and N17 (exemplar store + benchmark suites) describe this. `agent_evals.py` is 121 lines of **fixed control scenarios** (SQL guard denials, tool-first planning) — the matrix is right to score us `◐ control evals only` (`00-product/04-competitive-feature-matrix.md` §6). Studio's harness validates that an object *compiles* (`studio_test_harness.py`), not that an *answer* survives a change | **Gap worth closing; Phase 3 is too late for the value-free half.** The cheap, correct slice is a *context-path* regression, and its inputs already exist | Split N17. **~2 wks into Phase 2** (context-path replay, attached to ST-A2); value comparison stays Phase 3 |
| F3 | The stored expected answer of a test is a **business value**: `Expected $45.2M net external (excl. intercompany)` vs `Got $48.9M` (image 2); `Expected 14 accounts, $840K ARR lost` (image 12); `EXPECTED 14 accounts · $840K ARR` (image 13) | INV-6 / ADR-0014: result rows are bounded and retention-governed, question text is an HMAC fingerprint, and Studio §7 already says **"Fixture datasets are synthetic, never production data (ADR-0014)"** | **They are wrong for our constraints, and ST-8 as currently worded walks into it.** A regression suite whose expected answers are real revenue figures is a business-data store in the control plane, retained for as long as the benchmark lives. It also goes stale the moment a figure restates after close | Design constraint, ~0 wks, but it must be decided **before** N17 is scoped or the item gets built wrong |
| F4 | The model writes the fix and can ship it: `+ tier_filter: enterprise_only` with `AI confidence 92%` behind a single **"✓ Apply fix to churn.yml"** button (image 13); a review queue reporting **"44 tests passed · 8 fixes auto-applied · 3 need your review"**, the reviewed one at 91% confidence (image 14) | K3: model-only inference is capped at 0.70 against a 0.95 auto-publish gate, so auto-publish is structurally impossible. INV-3: model output is an inert typed proposal | **Deliberately decline.** Not because auto-apply is fast and we are slow, but because the loop is self-certifying: the model generates the test, the test fails, the model writes the change that makes its own test pass, and the change ships. Adopting the flow breaks INV-3 in the one place it matters most — the definition that governs what every downstream agent answers | 0 wks. State it explicitly in ST-8 so nobody re-derives it |
| F5 | One artifact carries `last_edited_by: "ai + @jsmith"` **and** `approved_by: "@jsmith"` (image 1) | INV-8 (maker ≠ checker, tested per governed object type), plus a structural split between `created_by` and `approved_by` on every version row | **They are wrong and we should say why.** Their own object model shows the editor approving their own edit, and fuses machine and human authorship into a single unparseable string. Under SR 11-7 "who wrote this" must separate the model — route, version, prompt — from the human who accepted it | 0 wks. Positioning line, not a build |
| F6 | **Bounded Context Spaces**: `repos/finance/revenue.yml` ("Net sales after returns, post-tax", fiscal_quarter) and `repos/sales/revenue.yml` ("Gross bookings, pre-discount", calendar_quarter) coexist as published truth, and the agent query "What is revenue?" is **routed by domain** to the right one (image 9); the prose calls the boundary the thing that ensures conflicts are "resolved deliberately, not silently overwritten" | ADR-0018 axis 1 (workspace = access) and axis 2 (`business_node` + `business_assignment`, which already reaches glossary terms and metrics). But `glossary_term` is `UniqueConstraint(organization_id, term_key)` — one org-wide namespace — and module 08 §6 models two definitions as a **conflict to be resolved**, with one position approved and both retained | **Gap worth closing, and cheap.** A Bounded Context Space is neither our workspace (access) nor our business node (classification): it is a **resolution scope**. In a bank, Finance revenue and Sales revenue are both permanently correct; forcing resolution produces either a fake consensus or a naming fudge (`finance_revenue`). The primitive to hang it on already exists | ~2–3 wks. Extends N9 / ADR-0018 axis 2. **Phase 2**. Distinct from UX-21, which is the screen for resolving a genuine conflict |
| F7 | The **Context Repo** as a file tree with git semantics: `definitions/`, `tests/`, `README.md`, per-file semver (`revenue.yml v3.1.2`, `arr.yml v2.4.1`, `region_filter.yml v1.9.0`) (image 1); commit history with `experiment/q4-window-update` branched from v3.1.2, an A/B at "20% agents", a merge to main and a `rollback:` commit, with a unified diff (image 11) | Studio change sets with base-version conflict detection, test gate and single-proposal submission (`studio.py`, ST-A1/A2/A3/A5 DONE); the context compiler emits MCP / REST / YAML / OSI / ODCS / Snowflake Semantic View / Databricks Metric View from one approved definition with a stable artifact hash and a drift report (CP-5, `context_compiler.py`) | **Decline the file-shaped repo as the authoritative primitive; take three of its properties (F1, F8, F10).** A git repo makes a merge a publish, and Studio §8 already names that as the thing that must not happen; it also creates a second authoritative store against INV-1, and a file is not a row, so per-read ABAC has nothing to evaluate. The multi-consumer, model-agnostic claim we **already meet and meet better** — a deterministic compile with a hash beats "any framework can parse the YAML" | 0 wks. Reaffirm Studio §8; say the compiler answer out loud in `00-product/05` |
| F8 | Versions sit on **definitions**, not on the bundle: `revenue.yml` moves 3.1.2 → 3.1.4 while `arr.yml` stays at 2.4.1 (images 1, 11), so a revenue change has a blast radius bounded to revenue's consumers | `context_product_version` versions the **whole bundle**. Any change to any member bumps the product and supersedes it for every consumer at once — maximum blast radius by construction. (Our metrics and glossary terms *are* individually versioned; the bundle is where granularity is lost) | **Gap worth closing.** It is nearly free: the version row already stores `semantic_model_version_ids`, `glossary_term_version_ids`, `eligible_tool_version_ids` and a fingerprint, so diffing two versions' id-sets yields the changed-member list and hence the genuinely affected consumers | 1 wk, folds into N20 |
| F9 | Definition changes are **A/B tested on live agents**: "branch: experiment/q4-window-update … A/B: 20% agents", later merged to main (image 11); staged rollout at 68% with per-agent synced versions (image 3) | We have no rollout mechanism at all, and the answer contract already pins and returns `semantic_version` / `policy_version` (`schemas.py`, target/03 §6) | **Split verdict. Decline blind A/B on definitions; accept staged rollout.** A percentage split means two populations get different numbers for "revenue" simultaneously and neither is told — a BCBS 239 consistency failure, not a product decision. A staged rollout is acceptable *because* every answer already states the version that produced it, so divergence is visible and attributable. The legitimate form of A/B is shadow evaluation: compute both, serve one, compare offline | Constraint on N20, 0 extra wks |
| F10 | "When they confirm a response is right, **it becomes a new regression test**" (images 15, 17) | N17 says exemplars accumulate from promoted analyses and review-confirmed runs (target/03 §3). Nothing implements it; the confirmed-correct run is already an evidence record | **Gap worth closing, nearly free.** This is the promotion step of the exemplar store and it is the cheapest half of F2 | Folded into the Phase-2 N17 slice |
| F11 | Two-pane authoring: **HUMAN LAYER — PLAIN LANGUAGE** with `+ AI` per field, beside **MACHINE LAYER — AUTO-GENERATED YAML** with `# + AI inferred` markers and a "PARSEABLE BY" footer (image 10) | — | **Already covered.** `40-engineering/08-experience-shell-rebuild-plan.md` §8.6 adopted exactly this for ST-A4 | None |
| F12 | The **Observe stage**: live traces with the retrieved context path and the version each object resolved at, correct/flagged status, 1,842 interactions / 1,796 correct / 46 flagged (images 4, 16) | LN-3 AI decision lineage | **Already covered**, including the correction that our differentiator is narrower than claimed — they trace what was used; we also record what was refused (tracker §L, final paragraph) | None |
| F13 | **Context Bootstrapping**: agents read Snowflake, BigQuery, Tableau, Notion, dbt, Confluence and draft a semantic layer (`product-analytics v1.8.2 · AI-GENERATED`) to solve the cold start (image 5) | Deterministic + approved-model semantic inference with strict validation and maker-checker (module 07 §6); GL-9 adds AI-drafted descriptions with a composite confidence score, routed to review | **Already covered.** The one honest gap — that we draft nothing prose-shaped today — is GL-9, opened by the concurrent session | None |

---

## 2. The arguments

### 2.1 Their Deploy stage is the real finding, and it exposes a defect in our code (F1, F8)

Their four-stage flow is Bootstrap → Simulate → Deploy → Observe (image 1 header, repeated
in 2, 3, 4). We have credible answers to Bootstrap (module 07 inference, GL-9), a weak one
to Simulate (F2) and a good one to Observe (LN-3). **We have nothing for Deploy**, and
that is the stage where their object model is most concrete: a named consumer list, each
with the protocol it speaks and the version it is currently on, a rollout percentage, and
a rollback commit (images 3, 8, 11).

Ours is the mirror image. `ContextProductConsumptionEdge` records, immutably, that
principal P read version V at time T. That is excellent evidence and useless for the
question a steward actually asks before editing: *if I publish this, what breaks?* CX-4
answers it backwards. UX-18 (consumer footer) surfaces the backwards answer nicely, which
is worth doing, but it is a report over past reads, not a binding.

The defect underneath is worse than the missing feature. `semantic_api.py`'s
`CONTEXT_PRODUCT_VERSION` approval branch supersedes the prior published version in the
same transaction as the new publish, and both the REST list path
(`context_product_api.py`) and the MCP resource read
(`mcp_server.py::_read_context_product_resource`) require `status == "PUBLISHED"`. So the
version-pinned URI in the resource contract —
`atlas://context-products/{product_key}/versions/{version}` — is pinning in name only.
The moment a steward approves v3, every agent holding the v2 URI receives
`"Resource not found or not accessible."` Because that string is deliberately
anti-enumerating, the agent cannot distinguish "your version retired" from "you were never
allowed this", so it cannot even log a useful failure. A bank's change-control process
does not accept a governed interface that cuts over globally at approval time with no
notice period and no distinguishable error.

Note that Atlan's own answer to this is *also* wrong for us, in the opposite direction:
"ALL AGENTS AUTO-UPDATED ON DEPLOY" (image 3) means a definition change silently alters the
behaviour of eight production agents. The correct design for a bank is neither hard
supersession nor silent auto-update: it is **pinned consumers with a support window and an
explicit migration**.

F8 is the same problem at a different granularity. Their versions are on definitions, so
`arr.yml` sits at v2.4.1 while `revenue.yml` moves to v3.1.4 (image 1). Ours are on the
bundle, so editing one glossary term supersedes the product for every consumer of every
member of it. The fix costs almost nothing because the diff is already computable from the
member-id lists stored on the version row.

**Proposed change — `20-modules/19-context-products-and-mcp.md`, §15.3 "Context product
lifecycle".** Replace the lifecycle block and add a paragraph after it:

> ```text
> DRAFT -> REVIEW_REQUIRED -> PUBLISHED -> SUPPORTED -> SUPERSEDED
>   |             |
>   +-> REJECTED <-+
>
> PUBLISHED -> pending DEPRECATE review -> DEPRECATED
> ```
>
> Publishing version *n+1* moves version *n* to `SUPPORTED`, not `SUPERSEDED`. A
> `SUPPORTED` version remains readable by a consumer that pins it, for the product's
> configured support window (default 30 days), and every read of one returns a
> `version_status: SUPPORTED` field and the successor's URI so the consumer can migrate
> itself. Only at the end of the window does it become `SUPERSEDED` and unreadable. A
> read of a `SUPERSEDED` version returns a distinguishable retirement response — not the
> anti-enumeration denial — because the pin proves the consumer was entitled to it while
> it was published; anti-enumeration protects products the caller was never entitled to,
> not versions that have retired.
>
> Publication computes the **changed-member set** by diffing the new version's
> `semantic_model_version_ids`, `glossary_term_version_ids` and `eligible_tool_version_ids`
> against the outgoing version's. A consumer is *affected* only if it consumed a changed
> member. The approval screen states the affected-consumer count before the checker
> decides, and it is the count of affected consumers, not of all consumers.

**Proposed change — `20-modules/19-context-products-and-mcp.md` §14, new open-work rows:**

> | CX-9 | Version support window: `SUPPORTED` state, configurable window, `version_status` and successor URI on every read, distinguishable retirement response | P0 |
> | CX-10 | Consumer binding registry: an agent or workload identity binds to `(product, version)`; the registry holds the bound version, the protocol, the last read and the migration deadline; a steward sees who is on what *before* editing | P1 |
> | CX-11 | Staged rollout and rollback over bindings: move a named subset of bindings to the new version, observe, advance or revert. No percentage split (see `review-2026-08/atlan-context/01-context-studio.md` F9) | P1 |
> | CX-12 | Changed-member diff and affected-consumer count on the approval screen | P1 |

**Proposed change — `review-2026-08/gap/02-gap-diff-and-plan.md` §4, new row:**

> | N20 | **Consumer binding registry + version support window + staged rollout/rollback for context products** (CX-9…CX-12) | 3 | Low | Today a publish is a global hard cutover: the prior version becomes unreadable in the same transaction, and pinned consumers get an anti-enumeration denial they cannot diagnose. The 0.5-week support-window slice should ship ahead of the rest |

Sequence it into **Phase 2**, not Phase 3: N20 is a correctness fix on a shipped surface,
not new capability.

### 2.2 Evals: the right idea, the wrong assertion (F2, F3, F10)

Their Simulate stage is genuinely good and the criticism of us is fair. They mine 47 tests
out of a named Q4 Revenue Dashboard, a Churn Analysis Report and two `.sql` files
(image 12), group them by who asks (image 6), and refuse to advance to Deploy while the
suite is at 87% (image 2). ST-8 already says we should do this. N17 already says exemplars
and benchmarks. Both are right and both are under-specified in the one way that matters.

**What they assert on is a business value.** `Expected $45.2M net external (excl.
intercompany)` (image 2), `Expected 14 accounts, $840K ARR lost` (image 12). Persist that
and the benchmark becomes a table of real financial figures living in the control plane
for the life of the suite, which INV-6 forbids, ADR-0014 forbids by default, and Studio §7
already explicitly forbids for fixtures. If N17 is scoped without settling this, it will be
built wrong and the INV-6 sentinel scan will catch it late — exactly the failure recorded
in INV-6's own "a leak this test did not cover" paragraph.

There is a better assertion available, and the constraint is what produces it. Their own
Observe stage shows it (images 4, 16): a trace is `Query parsed → Context repo:
sales-accounts v2.4.1 → enterprise_tier, churn_window loaded → Response generated`. The
value-free assertion is that structure, not the number:

> For question Q, the system must resolve context objects {X@v, Y@v}, select tool T@v or
> compile to logical plan P, and reach an ALLOW decision under policy version R.

That is fully replayable, needs no source access to evaluate, holds every property an
auditor wants, and — unlike a stored number — **does not go stale when a figure restates
after close**, which in a bank it routinely does. A stored `$45.2M` is a test that starts
failing for reasons that have nothing to do with the context.

Where a number genuinely must be compared (a semantic change that alters the arithmetic),
compare **two runs against the same source in the same window**, current context versus
proposed context, and persist only the delta's shape (equal / differs by more than
tolerance) plus a keyed fingerprint of each result under the mechanism already used for
question text. No value at rest.

The cheap half is F10 and it needs no new machinery: a run that a human confirmed correct
is already an evidence record with a pinned context path. Promoting it into the regression
corpus is a state transition, not a build.

**Proposed change — `20-modules/18-studio.md` §7, add two rows to the test-kind table:**

> | Context-path regression | For a corpus question, the resolved context objects, their versions, the selected tool or compiled plan, and the policy decision match the approved baseline |
> | Answer-delta regression | Where a change alters arithmetic, the current and proposed contexts are executed against the same source in the same window and the **delta** is compared to a tolerance. Neither result is persisted; only the comparison outcome and keyed fingerprints are |

**Proposed change — `20-modules/18-studio.md`, ST-8's explanatory paragraph.** Append:

> **Two constraints settle how ST-8 is built, and both come from screenshots of Atlan's
> own suite** (`review-2026-08/atlan-context/01-context-studio.md` F3, F4). First, the
> assertion is the **resolved context path**, never a stored business value: Atlan's tests
> persist `Expected $45.2M` and `Expected 14 accounts, $840K ARR` (images 2, 12), which for
> us would put regulated figures in the control plane against INV-6 and ADR-0014, and would
> go stale on the first restatement after close. Second, **an eval expectation is a
> governed object with an owner and maker-checker**, and a failing eval opens a *finding*,
> never a pre-filled fix. Atlan's studio drafts the fix and applies it: "8 fixes
> auto-applied" beside "3 need your review" (image 14), and a one-click "✓ Apply fix to
> churn.yml" at 92% confidence (image 13). A model that writes both the test and the change
> that satisfies it is a self-certifying loop; under K3 the 0.70 model-only confidence cap
> makes it structurally impossible here, and ST-8 must not introduce a path around it.

**Proposed change — `review-2026-08/gap/02-gap-diff-and-plan.md` §4, amend N17 and §7 Phase 2:**

> | N17 | **Exemplar store + benchmark suites.** Split: the *context-path regression* half (mine the corpus from confirmed-correct runs and BI/query history, assert on resolved objects + versions + selected tool + policy decision, gate change-set submission) is **2 wks and belongs in Phase 2**, because its inputs — `consumption_lineage.py`, `ai_decision_lineage.py`, the studio test gate — already exist. The *answer-delta* half stays Phase 3 | 2 + 2 | Low | The Genie lesson: accuracy is a curation loop |

and add `N17a` to the Phase 2 line in §7.

### 2.3 Bounded Context Spaces are a resolution scope, and we do not have one (F6)

This is the finding I expected to dismiss and could not.

Image 9 is unambiguous: `repos/finance/revenue.yml` says "Net sales after returns,
post-tax" on a `fiscal_quarter` window; `repos/sales/revenue.yml` says "Gross bookings,
pre-discount" on a `calendar_quarter` window; both are live; the agent query "What is
revenue?" is routed by domain, with a Finance Analyst and a Sales Analyst as the two
entry points. Neither definition is a draft, a conflict, or an error. Both are the answer,
for different askers.

Our three candidate answers all miss:

- **Workspace (ADR-0018 axis 1)** is the unit of *grant*. It says who may read, not which
  of two correct definitions applies. Two teams sharing a workspace still need two
  revenues; one team spanning two domains still needs to be told which it is getting.
- **`business_node` / `business_assignment` (axis 2)** is exactly the right primitive and
  is already built and already reaches glossary terms and metrics — but nothing *resolves*
  through it. `glossary_term` is `UniqueConstraint(organization_id, term_key)`: one
  org-wide namespace.
- **Module 08 §6 conflict resolution** is the wrong shape for this case, and the module
  says so about itself in a way worth quoting: it retains both positions and routes a
  resolution through maker-checker. That is right when two teams *disagree* and one must
  win. It is wrong when two teams *legitimately differ*, which in a bank is the normal
  case — Retail's "customer" and Financial Crime's "customer" are both permanently
  correct. Forcing a resolution produces either a fake consensus or a naming fudge
  (`finance_revenue`, `sales_revenue`), and the fudge is worse than it looks: it moves the
  disambiguation out of the governed model and into whoever remembers which key to type.

So the missing concept is a **resolution scope**: a business node, plus a rule that a
label resolves within it, plus a defined behaviour when it cannot.

That last part is where we should be better than them rather than equal. Image 9 shows
routing that always succeeds — the query enters, a domain is picked, an answer comes out.
Nothing in any of the seventeen screens shows the system saying *"revenue" is defined
differently in Finance and in Sales, and nothing in your question tells me which you
mean.* We already refuse for other reasons and we already carry the refusal in the answer
contract with `control`, `reason_codes` and `remediation`. Ambiguous-term refusal is the
same machinery pointed at a new cause, and it is a strictly better answer than a confident
number computed under a definition the asker did not choose.

**Proposed change — `20-modules/08-glossary-and-stewardship.md`, new §6a after "Conflict
resolution":**

> ## 6a. Scoped definitions: legitimate difference is not conflict
>
> Two definitions of the same label can both be correct. Retail Banking's "customer" and
> Financial Crime's "customer" differ deliberately; Finance's "revenue" is net and
> post-tax where Sales' is gross and pre-discount. §6 handles *disagreement*, where one
> position must win. This section handles *difference*, where neither should.
>
> A term version may carry a **scope**: a `business_assignment` to a `business_node`
> (ADR-0018 axis 2). Two term versions of the same label, scoped to two different nodes,
> are both approved and both published; neither is a conflict and neither supersedes the
> other. `glossary_term`'s `(organization_id, term_key)` uniqueness is relaxed to
> `(organization_id, term_key, scope_node_id)`, with `scope_node_id NULL` reserved for the
> single enterprise-wide definition where one genuinely exists.
>
> **Resolution.** Term resolution takes the asking context — the caller's workspace, the
> context product being read, or an explicit business node — and resolves to the
> definition scoped to the nearest node on the path from that context to the root. If two
> sibling nodes both claim the label and the asking context does not select between them,
> the system **refuses and names both definitions and both owners**. It does not pick, and
> it does not fall back to the enterprise definition, because a silent fallback is exactly
> the failure this section exists to prevent.
>
> §6 conflict detection is narrowed accordingly: two definitions in different scopes are
> not a conflict. Two definitions in the *same* scope still are.

**Proposed change — `20-modules/07-semantic-layer.md` §8.** `resolve_entity` gains the
asking context and an ambiguity outcome:

> ```python
> def resolve_entity(scope, name_or_synonym: str, *, asking_context: BusinessScope) -> EntityRef | Ambiguity | None
> ```
> `Ambiguity` names every candidate definition, its scope node and its owner, and is
> rendered as a refusal with `reason_codes=["AMBIGUOUS_TERM_SCOPE"]` — not as a ranked
> guess.

**Proposed change — `60-delivery/03-tracker.md` §H (glossary rows), new rows:**

> | GL-10 | Scoped term definitions: `scope_node_id` on term versions, relaxed uniqueness, §6 conflict detection narrowed to same-scope collisions | 08 | B | P1 | TODO |
> | GL-11 | Context-aware term resolution with `AMBIGUOUS_TERM_SCOPE` refusal | 07/08/12 | B | P1 | TODO |

Estimate 2–3 weeks together, Phase 2. Distinct from **UX-21**, which is the screen for
resolving a genuine same-scope conflict and should stay as written.

### 2.4 The Context Repo is not a better primitive than what we designed (F7)

The brief asked whether a file-shaped, branchable repo beats our knowledge-compilation
design (N10) and our approval workflow. It does not, and the reason is visible in their
own screenshots rather than in ours.

What a repo buys them, honestly:

1. **Diffs, history and rollback that a human can read** (image 11). We have this: change
   sets with base-version conflict detection (ST-A1, DONE), semantic diff view (ST-A3,
   DONE), and clone-to-rollback on immutable versions (module 07 §7). Not a gap.
2. **Per-definition version granularity** (image 1). Real, and F8 closes it at the bundle
   level for a week of work. Our metrics and terms are already individually versioned.
3. **One definition consumed by many runtimes** — Snowflake Cortex, LangGraph, Databricks
   Genie, Claude over MCP, all on v3.1.2 (image 8), and again in image 3 with the protocol
   named per consumer. **We already do this and we do it better.** CP-5's context compiler
   emits MCP, REST, YAML, OSI, ODCS, Snowflake Semantic View and Databricks Metric View
   from one approved definition, with a stable artifact hash and a structural drift report.
   "Any framework can parse the YAML" is weaker than a deterministic compile with a hash,
   because a YAML file that four runtimes each interpret their own way is four
   interpretations of one text, not one definition. We should say this out loud; today the
   compiler is a delivery-slice row nobody outside the module doc knows exists.

What a repo costs us:

- **A merge is a publish.** Studio §8 already names this as the thing that must not
  happen. Git's model is that authority lives with whoever can push to `main`; ours is
  that authority lives with an approval service that no feature module can bypass (K6,
  INV-8). Making the repo authoritative rather than a projection inverts that, and every
  subsequent control has to be re-derived on top of a store designed to make merging easy.
- **A second authoritative store**, against INV-1 and ADR-0003.
- **A file has no rows to evaluate policy against.** Per-read ABAC (CX-3) evaluates
  lifecycle, roles, exact scope, purpose and quality on an object. A `.yml` blob is
  authorized as one thing or nothing — which is precisely W2's criticism of everyone
  else's MCP server, and we would be adopting it.

**Verdict: decline the repo as primitive, adopt F1, F8 and F10, keep ST-A6 (git binding)
scoped exactly as Studio §8 already words it — export/import as a projection, Atlas
authoritative, merge cannot publish.** No change needed to Studio §8; it was already
right. The change worth making is to stop underselling the compiler.

**Proposed change — `00-product/05-differentiation-and-whitespace.md` §2, add to D2 or as
a new short paragraph under W2:**

> **One definition, every runtime — deterministically.** Atlan's Context Repo is
> consumed simultaneously by Snowflake Cortex, LangGraph, Databricks Genie and Claude over
> MCP (images 3, 8), and its portability argument is that the underlying YAML is
> parseable by all of them. Atlas compiles one approved definition to MCP, REST, YAML,
> OSI, ODCS, Snowflake Semantic View and Databricks Metric View with a **stable artifact
> hash and a structural drift report** (CP-5). A shared file that four runtimes each parse
> their own way is four interpretations of one text; a deterministic compile with a hash
> is one definition with four renderings, and only the second can be shown to an auditor
> as evidence that two agents were reading the same thing.

### 2.5 Where their human-in-the-loop story and ours actually agree, and the one place it breaks (F4, F5)

They agree with us more than the marketing implies. Every failure carries a
`Why:` line grounded in evidence ("9 of 12 churn-related queries in your BI history
reference enterprise accounts only", image 13), review is Approve/Reject with the diff
rendered (image 14), the human layer is plain language and the machine layer is marked
where AI inferred it (image 10), and their own copy is "Human **ON** the loop". Our
maker-checker produces the same artifacts; §8.1 and §8.6 of the shell rebuild plan already
adopt the presentation.

It breaks in exactly two places, both visible in the object model rather than the prose:

- **"8 fixes auto-applied"** (image 14) with the reviewed item at 91% confidence, and
  **"✓ Apply fix to churn.yml"** at 92% (image 13). The auto-apply threshold sits somewhere
  above 91% and below whatever the applied eight scored. K3 caps model-only inference at
  0.70 against a 0.95 gate precisely so that this number can never be tuned into an
  auto-publish. The deeper objection is not the threshold: it is that the model generated
  the test, the test failed, and the model wrote the change that makes its own test pass.
  A human approving the third of those has been handed a conclusion, not evidence.
- **`last_edited_by: "ai + @jsmith"` beside `approved_by: "@jsmith"`** on the same file
  (image 1). The editor approved their own edit — INV-8's exact prohibition — and machine
  and human authorship are fused into one string that no audit query can separate. When
  a regulator asks which changes to this definition were model-originated and who
  independently accepted each, that field cannot answer.

Both are worth stating in our own words rather than left implicit, because "AI drafts,
expert refines" is a claim we make too, and a buyer will ask what is different.

**Proposed change — `50-security/03-ai-safety-controls.md`, in the "what Atlas does not
claim" area, add:**

> **Two things we decline that the market ships.** A confidence score never authorizes a
> write: model-only inference is capped below the auto-publish gate by construction (K3),
> so there is no threshold an operator can raise to make a model-authored change
> self-applying. And an evaluation result never authors its own remedy: a failing eval
> opens a finding for a human, never a pre-filled change that one click publishes. Both
> patterns are shipping today — Atlan's Context Engineering Studio reports "8 fixes
> auto-applied" beside "3 need your review" and offers a one-click "Apply fix" at 92%
> confidence (`review-2026-08/atlan-context/01-context-studio.md` F4). The objection is
> structural, not statistical: a system that writes the test, fails it, and writes the fix
> is certifying itself.

---

## 3. What we should deliberately not do

| # | Decline | Because |
|---|---|---|
| 1 | A git-shaped Context Repo as the authoritative store | A merge becomes a publish, and INV-8 plus INV-1 both fall. Keep ST-A6 as a projection, exactly as Studio §8 words it |
| 2 | Auto-applying model-authored context fixes at any confidence | K3 makes it structurally impossible; the loop is self-certifying (F4) |
| 3 | Persisting business values as expected answers in the eval corpus | INV-6, ADR-0014, Studio §7 — and they go stale on restatement (F3) |
| 4 | Blind A/B splits of definitions across live agents | Two populations get different numbers with no way to know; a BCBS 239 consistency failure. Staged rollout over named bindings is fine because the answer already states its version (F9) |
| 5 | "All agents auto-updated on deploy" | The mirror-image error to our hard supersession. Pinned consumers with a support window and an explicit migration is the only version of this a bank change process accepts (F1) |
| 6 | Re-reporting the UI patterns | UX-17…UX-21, AT-1…AT-5, ST-8, GL-9 and the Trace Explorer correction already landed from the same screenshots. Tracker §L and §M |

---

## 4. Where we are simply fine

Stated plainly so nobody spends a sprint on it:

- **Bootstrapping from existing metadata.** Module 07 §6 plus GL-9 is the same idea with a
  stricter review path. Their testimonial wall is about the cold-start problem, which we
  have designed for.
- **Multi-runtime consumption.** CP-5 already exceeds it (§2.4).
- **Diffs, change sets, impact preview, test gate.** ST-A1/A2/A3/A5 are DONE.
- **Observability of agent runs.** LN-3, and we additionally record refusals (tracker §L).
- **Model-agnostic, no vendor lock.** Model gateway plus approved routes; the compiler's
  seven targets.

## Related documents

- `20-modules/18-studio.md` (ST-8, §7 test harness, §8 git)
- `20-modules/19-context-products-and-mcp.md` (§15.3 lifecycle, §14 open work)
- `20-modules/08-glossary-and-stewardship.md` (§6 conflicts)
- `10-architecture/adr/ADR-0018-three-axis-tenancy-and-classification.md` (axes 1 and 2)
- `10-architecture/01-principles-and-invariants.md` (INV-1, INV-3, INV-6, INV-8)
- `review-2026-08/gap/02-gap-diff-and-plan.md` (N10, N15, N17, K3, K6; proposed N20)
- `60-delivery/03-tracker.md` §L and §M (the concurrent pass over the same screenshots)
