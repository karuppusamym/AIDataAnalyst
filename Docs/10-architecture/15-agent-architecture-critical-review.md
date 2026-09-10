# Agent architecture: implementation audit and adversarial review

> Reviewed: 2026-09-09. Baseline: `15f29cd`, clean working tree before this documentation pass.
> Status: code-backed review and proposed remediation, not production certification or approval to enable automation.
> Scope: agent inventory, authority boundaries, economics, recovery, observability, and selected vendor documentation. Live registrations, enabled routes and production capacity were not measured.
>
> **Remediation pass: 2026-09-09, same day.** Application code *was* subsequently changed against these findings; section 2 records what each finding's status now is and what remains. The rule this document holds to: a finding is CLOSED only when the property it names is enforced in code *and* asserted by a test that fails against `15f29cd`. A finding whose closure criterion includes measurement stays open until the measurement exists, however much code was written for it — no amount of implementation closes AR-09.

## 1. Decision and counting convention

Retain deterministic authorization, validation, transactions and durable workflows. Expand agent reasoning where it improves evidence interpretation, drafting and investigation. Deterministic automation need not involve a human; agent reasoning need not involve unrestricted authority. A framework adapter is permissible under ADR-0008 when justified by a concrete workflow.

Five core capabilities were identified, not five autonomous LLM agents or five deployed instances:

| Capability | Implementation | LLM involvement |
|---|---|---|
| Governed analytical runtime | `src/aida/agent_orchestrator.py`, `agent_runtime.py` | Conditional generation; tool selection/reuse can avoid generation |
| Semantic enrichment | `src/aida/semantic_inference.py::model_enrich_batch` | Optional; deterministic baseline and fallback |
| Marketplace discovery | `src/aida/marketplace_discovery.py::resolve_marketplace_filters` | Structured question-to-filter resolution |
| Reviewer | `src/aida/reviewer_agent.py` | None; deterministic recommendations and optional decisions |
| Quality triage | `src/aida/dq_triage_agent.py` | None; evidence-to-hint rules |

Description drafting (`asset_description_service.py`) and metric suggestions (`metric_suggestion_service.py`) are additional deterministic automations. This is a capability inventory, not an exhaustive count of every helper, task, persona, connector or registry entry. An LLM call is not by itself an autonomous planning loop.

Report separately: implemented capabilities; registered agent versions; enabled deployments; active agents with attributable runs; successful outcomes. A registry row may describe an external agent Atlas does not execute. Default configuration is not evidence of the current deployment configuration.

## 2. Findings and acceptance criteria

Priorities are recommendations. **P0** blocks expansion of unattended decisions; **P1** blocks enterprise readiness claims or materially weakens operations; **P2** improves product quality. Owners below are proposed functional owners, not assigned individuals.

Status vocabulary, used strictly:

* **CLOSED** — the property is enforced in code and asserted by a test that fails against `15f29cd`.
* **PARTIAL** — the code half is done and tested; the finding's closure criterion also required measurement, an audit sweep, or an evaluation that does not yet exist. What is missing is named per row.
* **OPEN** — not addressed. Writing code would not close it.

A PARTIAL row is not a half-fixed defect. In every case below the specific defect the row describes is fixed; what remains is the *evidence* the closure criterion demanded, which is a different kind of work and must not be quietly absorbed into "done".

### Status summary

| Status | Findings |
|---|---|
| CLOSED | AR-01, AR-02, AR-05, AR-07, AR-08 |
| PARTIAL | AR-03, AR-04, AR-06, AR-10, AR-11, AR-12 |
| OPEN | AR-09 |

AR-05 moved from PARTIAL to CLOSED on 2026-09-10 when its PostgreSQL reproduction was run — see its row.

### What the remediation pass found that this review had not

Three things surfaced during the fix that are worse, or different, than the finding that led to them. They are recorded here rather than folded into the rows, because a review that only ever confirms itself is not being read carefully.

1. **AR-03 was understated.** The rule was described as approving when confidence was absent. In practice confidence was *always* absent: `_proposal_confidence` read `proposal_confidence` out of `GovernanceReview.pre_review_evidence`, which is written by pre-review and by nothing else — and pre-review skips already-pre-reviewed rows, so the only pass that read it always saw `None`. Every tier-eligible item with no prior rejection and no open incident was recommended for approval, and `reviewer_agent_approve_confidence` was unreachable dead configuration. The fix reads each proposal's own row instead.
2. **`review_risk_tiers.agent_decidable_object_types` contradicted its own docstring.** It stated that a misconfigured ceiling "can never widen it past what this module classifies" while passing the configured ceiling straight into the comparison. The docstring was the specification; the code did not implement it.
3. **The reviewer agent's happy path had no test at all.** Every auto-decision test in `tests/test_reviewer_agent.py` asserted a refusal, and none imported `aida.semantic_api` — so no target adapter was registered and `decide_review` would have refused every object type with `unsupported governance object type`. A successful agent decision had never been exercised. This is why AR-01 through AR-04 could all be true simultaneously without a test going red.

| ID / priority | Evidence and consequence, as found at `15f29cd` | Status after the remediation pass, and what remains |
|---|---|---|
| AR-01 / P0 | `config.py::Settings.reviewer_agent_max_tier` accepts T0 through T3. `review_risk_tiers.py::agent_decidable_object_types` filters by that configurable ceiling without an independent T1 maximum. `auto_decide_tier0_tier1` uses the result. Consequently, the documented hard T0/T1 boundary is not enforced by these guards under elevated configuration. | **CLOSED.** `review_risk_tiers.HARD_MAX_AGENT_TIER` and `effective_agent_ceiling` clamp the configured ceiling before anything derives from it; `agent_decidable_object_types` and `auto_decide_tier0_tier1` both use the clamped value, and the clamp is recorded in every pre-review's evidence (`max_tier_clamped`) and reported by the reviewer-agent state endpoint, so a refused ceiling is visible rather than merely ineffective. Tests: `test_ar01_*`, including a T3-configured end-to-end refusal of a `CONTEXT_PRODUCT_VERSION`. |
| AR-02 / P0 | `pre_review_pending` calls `risk_tier_for(review.object_type)` without payload. Size-based escalation in `risk_tier_for` requires payload for bulk stewardship and workbook imports. Auto-decision can reuse that stored tier. Large batches can therefore remain classified T1 by this path. | **CLOSED.** `_sized_risk_tier` resolves the authoritative count before tiering -- `ModelImportBatch.change_count` for workbooks, `len(BulkStewardshipOperation.subject_ids)` for bulk stewardship, both through the review's own back-link -- and escalates to T2 when the count cannot be established. The tier is recomputed at decision time, so a batch that grows between passes escalates instead of riding its stored T1. Tests: `test_ar02_*`, including the review's 900-change example on both bulk paths. |
| AR-03 / P0 | `_recommendation` returns APPROVE for eligible items with no prior rejection, no open incident and `confidence is None`. Absence of contrary evidence does not demonstrate correctness. It is not an independent semantic assessment. | **PARTIAL.** The rule now requires positive, object-specific evidence: a per-type resolver registry reads the proposal's own confidence (`AssetDescriptionDraft.overall_score`, `GlossaryLinkProposal.confidence`, `MetadataEnrichmentProposal.confidence`, `QueryHistoryMetricCandidate.confidence`, `DocumentClaim.confidence`, verified size for the two bulk types), and a type with no resolver abstains rather than approving. A missing confidence is now the most restrictive input, not the most permissive. **Remaining:** this is still the proposal's own self-reported score, not an independent semantic assessment. The evaluation half -- misleading metadata, deliberately wrong proposals, false-approval rates measured by object type -- does not exist. Tests: `test_ar03_*`. |
| AR-04 / P0 | Pre-review skips already-reviewed items; auto-decision consumes stored recommendations. Organization suspension is checked at batch entry, before iterating. This does not establish fresh evidence or an immediate stop for an already-running batch. | **PARTIAL.** Evidence is re-derived at decision time from live state rather than read back from the row, so a stored recommendation now only makes an item *eligible*; a fresh verdict contradicting the stored one abstains. `reviewer_agent_evidence_max_age_minutes` bounds how stale a selected recommendation may be. Suspension is re-read before each commit, bounding the stop at one item. **Remaining:** that bound holds only at READ COMMITTED, and the multi-worker concurrent-suspension experiment measuring the real maximum stop delay has not been run. Tests: `test_ar04_*`. |
| AR-05 / P1 | Contract daily/per-run token and wall-clock caps are validated and stored. The inspected runtime checks contract existence, kill switches and selected tool slugs, but no runtime consumer of these contract cap fields was found. Model-route caps are a separate control. Token accounting is estimated. | **CLOSED 2026-09-10.** The caps have a runtime consumer. `aida.agent_budget` reserves against an `agent_budget_window` row with a conditional UPDATE carrying the cap in its own `WHERE`, reconciles down to actual spend, releases in full on failure, and enforces the per-run cap twice (input estimate before the call, total after) and the wall-clock cap from the run's start. The gateway's estimator is a named shared function, so the number a run is refused on and the number recorded against it cannot drift. Migration `a7c41e93d2b0` applied against the running PostgreSQL 17 and the table verified column-for-column. **Raced on real PostgreSQL** (`tests/test_agent_budget_postgres_concurrency.py`), the reproduction this row asked for: ten racers on ten connections released from one `asyncio.Barrier`, at READ COMMITTED and REPEATABLE READ. Falsifiable and falsified — removing the cap clause turns three READ COMMITTED tests red with a 1000-token day holding 2000. **Two residuals, named in that file rather than hidden and neither reopening this finding:** at REPEATABLE READ the isolation level bounds the day by itself, so those cases confirm the invariant without testing the guard, and a loser there is refused SQLSTATE 40001 rather than `AgentBudgetExceeded`, which needs a retry loop the application does not supply. Every figure remains estimated — no provider adapter reports billable usage, and `reconcile_run_budget` is the single place a real one would enter. |
| AR-06 / P1 | Contract `context_product_ids` and `write_lanes` are parsed/stored; `envelope_violation` checks tool slugs. Their declaration alone is not evidence of complete runtime enforcement. | **PARTIAL.** `context_product_ids` is enforced at the MCP boundary where a contracted agent actually consumes a context product (`load_contract_for_principal` + `context_product_violation`), with the same fail-closed semantics `tool_slugs` has: an empty allowlist allows nothing, an unparseable envelope allows nothing. `write_lanes` had no consumer anywhere, so declaring one is now *refused* at validation -- an unenforced control quoted in a governance dossier is the failure this finding names, and refusing it forces whoever builds the first lane-bearing write path to build the check in the same change. **Remaining:** the full endpoint/path enforcement matrix has not been produced; this pass enforced the field it could trace to a real consumer and fail-closed the one it could not. Tests: `test_ar06_*`. |
| AR-07 / P1 | `AgentRun.ai_asset_version_id` exists and is populated for linked runs; `agent_roster.py` still states the identity link does not exist and summarizes activity organization-wide. It also emits blanket no-auto-apply text. | **CLOSED.** Each roster entry is scoped to its own version's runs through `AgentRun.ai_asset_version_id`; runs with no agent identity are reported once at the top level as `unattributed` and credited to nobody; a registered agent the platform never executes reports zero rather than borrowing the organization's totals. The stale docstring asserting no link exists is gone, and the blanket no-auto-apply text is replaced by a per-agent answer read from the version's own contract, so the ADR-0027 reviewer agent reports its real branch, threshold and enabled state. Tests: `test_ar07_*`. |
| AR-08 / P1 | `semantic_inference.py` batches at most 25 tables per model invocation, sequentially in the inspected enrichment loop. Old documentation claims approximately 50 calls for 100,000 tables. | **CLOSED** as a documentation correction. The `ceil(N / 25)` arithmetic replaced the ~50-call claim in `08-workers-and-workflows.md` and in section 4 below. Capacity claims still require measurement, which is AR-09, not this row. |
| AR-09 / P1 | Discovery has bounded fan-out, retries, heartbeats and continue-as-new. `workflows/worker.py` registers discovery and ingestion on one configured Temporal task queue. This does not prove independent queues per worker class, deployment failover or source/tenant fairness at scale. | **OPEN.** Unchanged, deliberately. `workflows/worker.py` still registers discovery and ingestion on one configured task queue and `workflows/scheduler.py` starts both on the same one. Separating queues is a deployment-topology change whose value cannot be assessed without the load and recovery experiments this row asks for, and running those is what closes it -- so no code was written against it rather than code that would look like progress. Nothing in the remediation pass licenses an enterprise-scale claim. |
| AR-10 / P1 | `injection_defense.py` and `ingest_screening.py` exist and are used at ingestion/MCP paths. The runtime document's blanket assertion that indirect screening does not exist is stale. Coverage across legacy/imported/retrieved content was not established here. | **PARTIAL.** Tracing the model-context ingresses found one genuinely unscreened: `retrieval_evidence` carries a business annotation's `business_name` and its domain/entity display names straight into the model payload, with no stored verdict to consult. Those fields are now screened on the way through and quarantined text is withheld behind a fixed marker; the persisted audit record keeps every hit verbatim and the withheld count is recorded in plan evidence. `_model_context` itself was found to carry only identifiers, types and constraint shapes -- no free text. **Remaining:** one ingress is not the path-level audit of all of them, and no adversarial evaluation of the classifier was run. It remains evadable by paraphrase; INV-3 remains the load-bearing control. Tests: `tests/test_agent_context_ingress_screening.py`. |
| AR-11 / P1 | A deterministic reviewer can propagate systematic errors at high volume. Different principal IDs and 5% sampling alone do not demonstrate independent evidence, semantic correctness or timely human oversight. | **PARTIAL.** The half code can enforce is enforced: `reviewer_agent_max_unresolved_samples` makes the agent's licence to decide contingent on humans resolving the sample it already produced, refusing with `reviewer_agent_audit_backlog_exceeded` once the unread queue reaches the bound. Condition (b)'s safety argument is about humans *reading* a 5% sample, and nothing previously checked that they were -- the argument could degrade to nothing silently. **Remaining:** everything measured. Disagreement rates and false approvals by risk class, audit completion time, downstream harm, and the withdrawal/compensation and notification procedures are all still absent. Tests: `test_ar11_*`. |
| AR-12 / P2 | Product/architecture documents contain unqualified competitor absence claims, cost estimates and shipped-state statements that are not supported by this review. | **PARTIAL.** The documentation pass marked the historical claims in `00-product/08` and replaced the competitor comparison in section 5 below with dated primary sources. **Remaining:** the rest of `00-product/` was not swept for the same pattern, and no source/expiry discipline is enforced on new competitive claims. |

### Operator-visible behaviour changes in the remediation pass

Four changes alter what an existing deployment does, and none is a pure tightening of an unused path. They are listed here because a status table saying CLOSED does not tell an operator what will start failing.

1. **The reviewer agent now approves far less.** Approval requires the proposal's own confidence at or above `reviewer_agent_approve_confidence` (default 0.8). `TERM_SEMANTIC_BINDING` and `ASSET_DOCUMENTATION_VERSION` became non-decidable entirely — both are unscored human assertions. Anyone who had the agent enabled will see the recommendation mix move sharply toward NONE, and that is the fix working: those approvals were being made on the absence of contrary evidence.
2. **A contracted agent with an empty `capability_envelope.context_product_ids` can no longer read any context product through MCP.** This matches how `tool_slugs` has always behaved — an envelope is an allowlist and an empty allowlist is empty — but every contract written before this pass has an empty list, because nothing read the field. Those contracts must be updated to name the products their agent needs. Human principals are unaffected.
3. **A contract declaring any `write_lanes` is now refused at validation** (`envelope_write_lane_unenforceable`). No contract in this repository declares one, so nothing breaks today; the refusal exists so the first lane-bearing write path cannot ship without its check.
4. **`reviewer_agent_max_unresolved_samples` (default 50) will stop the agent** in any organization whose sampled-decision queue is already past that. Resolving the backlog restores it; setting the value to 0 disables the check and, with it, the oversight claim.

### Read-only reproduction of AR-01 and AR-02, before and after

Executed against the Python environment with no database mutations. At `15f29cd`:

```python
from aida.review_risk_tiers import agent_decidable_object_types, risk_tier_for

print({t: "CONTEXT_PRODUCT_VERSION" in agent_decidable_object_types(t)
       for t in ("T0", "T1", "T2", "T3")})
# {'T0': False, 'T1': False, 'T2': True, 'T3': True}
print(risk_tier_for("MODEL_IMPORT_BATCH"))  # T1  -- the tier pre-review used
```

After the remediation pass the first line answers `False` for every ceiling, which is the invariant ADR-0027 always stated. `risk_tier_for("MODEL_IMPORT_BATCH")` still answers `T1` on its own, deliberately: it is a pure classification of the *type*, and the queue-labelling callers want that. What changed is that the callers who gate an action no longer ask it that way — `reviewer_agent._sized_risk_tier` resolves the authoritative count first and treats an unresolvable one as T2, and `review_risk_tiers.requires_size_evidence` is the predicate that tells a caller it must.

These reproduce guard behaviour, not a successful exploit or an observed production publication. The tests that now assert the fixed behaviour are named in the status column above; each fails against `15f29cd`. Note what the *old* test suite did here: `test_the_allowlist_is_derived_from_the_tier_table_not_from_config` asserted that every type a T3 ceiling admitted was a *classified* type — which was true while the ceiling admitted every T2 and T3 one. A passing test asserting the wrong property is how AR-01 survived.

## 3. Devil's advocate: challenge both architectural extremes

| Challenge | Assessment and design response |
|---|---|
| Are we calling ordinary functions agents to look competitive? | Some named agents are rules. Label their method accurately and measure task outcomes rather than agent count. |
| Is deterministic implementation automatically correct? | No. Incorrect rules, missing evidence and stale state can be repeatably wrong. AR-01 through AR-04 are concrete counterexamples. |
| Are we overusing templates where reasoning would help? | Possibly. Compare grounded LLM drafts with deterministic descriptions on domain-expert-rated correctness, usefulness, omissions and editing effort before choosing either globally. |
| Could agents run most user workflows? | Yes, as bounded clients of authorized APIs. Open-ended planning can help investigations and documentation. It does not replace transactions, idempotency or permission enforcement. |
| Does an agent framework threaten governance by definition? | No. ADR-0008 permits adapters. Compare the operational cost of maintaining custom orchestration with a framework for a specific workflow; retain application-owned authority checks either way. |
| Does screening before retrieval prevent hostile influence? | It blocks inputs the classifier detects. Undetected hostile input and malicious retrieved metadata remain possible. Typed boundaries, authorization and adversarial evaluation remain necessary. |
| Does read-only execution eliminate risk? | No. Wrong answers, sensitive disclosures and expensive scans are consequential. Validate semantic correctness, disclosure and resource use independently. |
| Is metadata always nonsensitive? | No. Descriptions and identifiers may contain secrets or personal information. Metadata-only is a minimization policy, not automatic permission to transmit it to any provider. |
| Does an audit trace guarantee replay? | No. Hashes and version IDs support attribution and integrity checks; exact regeneration needs preserved authorized inputs, retained versions, provider behavior and a clearly defined replay mode. Evidence replay differs from LLM reproduction and query re-execution. |
| Can the platform keep working when the model provider is unavailable? | Deterministic baselines and approved tools provide useful fallbacks. Test partial failures and disclose degraded output; never count fallback as successful AI enrichment. |
| Is automated sampling sufficient at estate scale? | Only if humans resolve samples promptly and recovery works. A growing unresolved audit queue undermines the claimed oversight. |

## 4. Economics and scalability criteria

For the current enrichment implementation, if N selected tables all reach model enrichment, the nominal batch-call count is `ceil(N / 25)`. For 100,000 selected tables that is 4,000 nominal batch attempts, before route failures, token-cap refusals or any provider/client retry behavior. This is arithmetic from code, not measured cost or throughput. Fewer selected tables, reuse and change detection can reduce work; large schemas can exhaust a token budget even within a 25-table batch.

Measure end-to-end task success and latency including model/provider time. Also report Atlas-only overhead, but do not present it as the user's total wait. Separate semantic accuracy from SQL validity and policy compliance. Report cost per accepted result, not just cost per generated draft.

Enterprise validation should cover sustained workload plus bursts, unfair/noisy tenants, provider rate limiting/outages, worker termination, duplicate delivery, stale metadata, concurrent decisions, revocation during execution and audit backlog. Agree estate size, concurrency, error/latency budgets and recovery objectives before testing. Historical single-process drills and individual traversal benchmarks do not establish system-wide capacity.

## 5. Vendor comparison: public evidence, not backend equivalence

Sources checked 2026-09-09. Published capabilities do not establish internal implementation, edition availability or performance equivalence with Atlas. Atlassian is included as the likely interpretation of the user's spelling; Alation is included as a closer catalog comparison.

| Platform | Supported public observation | Implication for Atlas |
|---|---|---|
| Atlan | Description, README and SQL Intelligence context agents enrich missing metadata. [Context-agent documentation](https://docs.atlan.com/product/capabilities/governance/context-agents-studio/concepts/agents) | Benchmark documentation quality and review effort; avoid claiming Atlas's deterministic drafting is equivalent to these agents. |
| Collibra | AI Command Center centralizes agent/model/use-case governance and assessments. Its product page labels operational trust with Databricks Agent Bricks as preview. [Documentation](https://productresources.collibra.com/docs/collibra/latest/Content/AICommandCenter/co_aicc-about.htm), [product availability](https://www.collibra.com/products/ai-command-center) | Inventory, oversight and coherent administration matter alongside execution controls. Do not claim every integration is generally available or that Collibra cannot support actions. |
| Atlassian Rovo | Agent actions respect the user's permissions; automation knowledge access is scoped to the connecting user. [AI trust documentation](https://www.atlassian.com/trust/ai) | Make human, agent and delegated execution identity explicit at each action. This is a workflow comparison, not catalog parity. |
| Alation | Documents cataloging, quality, governance and data consumption together, with SDK/templates for metadata-grounded agents. [Product documentation](https://docs.alation.com/en/latest/welcome/About/WhereShouldIStart.html) | Compare complete user journeys and context quality, not the presence of an MCP endpoint alone. |

No source above supports replacing an entire enterprise backend with autonomous agents. They support investment in agents together with governed context, permissions and platform services. Conversely, public documentation cannot prove that a competitor lacks a feature. Retire unsupported claims such as "every competitor regenerates SQL" or "nobody else has this."

## 6. Delivery sequence and remaining questions

The guard-level defects (AR-01 through AR-04) and the unenforced-declaration defects (AR-05 through AR-07) have been fixed and tested; see the status column. What is left is almost entirely *evidence*, and it does not get easier by being deferred:

1. **Before enabling unattended review anywhere.** Establish the effective production configuration, which no amount of default-off code establishes. Run the adversarial proposal evaluation AR-03 asks for — misleading metadata, deliberately wrong proposals, false-approval rate by object type — because the agent now approves on a proposal's *self-reported* score and nothing has tested what that score is worth. Keep ADR-0027 proposed until that evaluation exists.
2. ~~**Before quoting a budget cap in a governance dossier.** Reproduce the AR-05 reservation on real PostgreSQL the way F05 was reproduced.~~ **Done 2026-09-10** — `tests/test_agent_budget_postgres_concurrency.py`. AR-04's remaining question is *not* answered by it: that needs a multi-worker batch with a suspension landing mid-run, which is a different experiment and is still outstanding.
3. **Before claiming enterprise scale.** AR-09. Nothing else on this list substitutes for it, and no code written since this review bears on it.
4. **Before claiming oversight.** AR-11's measurements, plus the withdrawal/compensation and notification procedures. The backlog control now stops the agent when humans fall behind; it does not tell anyone what to do about the decisions already made.
5. **Then** add grounded column documentation and investigation capabilities incrementally. Compare rules, a single LLM workflow and bounded multi-step reasoning against the same task corpus; deploy the simplest approach that meets the agreed outcome targets.

Still to establish: actual registered/enabled agent counts; production model approvals and data residency; the complete per-path workload identity binding matrix (AR-06's remaining half); provider-reported token usage as distinct from the estimate every figure currently uses; multi-worker revocation delay; restore/compensation behaviour; model and prompt version retention; provider-inclusive SLOs; connectors/editions needed for competitor comparisons. These are unresolved verification questions, not assertions that all corresponding features are missing.

## 7. Review validation

**The review itself (documentation-only).** Code inspection and the pure-function reproductions above were performed. Vendor sources were consulted. No production load test, model evaluation, live decision or configuration change was run.

**The remediation pass (2026-09-09).** Application code was changed. The Python test suite was run and passes (9,061 tests), including tests that fail against `15f29cd` for every finding marked CLOSED or PARTIAL. Alongside it: `ruff`, `mypy`, import-linter, the docs-link gate, the reachability gate, and regenerated OpenAPI/`ui-next` type baselines.

**The verification pass (2026-09-10).** Migration `a7c41e93d2b0` was applied against the running PostgreSQL 17 and the resulting table verified column-for-column, rather than assumed from a passing SQLite suite. AR-05's guard was then raced on that PostgreSQL and shown to be falsifiable by removing it. The running stack was inspected for the operator action AR-06 requires: it holds zero `AgentContract` rows and zero unresolved `ReviewAuditSample` rows, so no live deployment is affected by either behaviour change today.

**Still not done, and not substituted for.** No load test, no model evaluation, no adversarial proposal corpus, no live agent decision, and no production configuration change. A green suite is not the evidence AR-09 or AR-11 asked for, and this document does not treat it as such.

Related: [runtime](../20-modules/13-agent-runtime.md), [workers](08-workers-and-workflows.md), [risk-tier ADR](adr/ADR-0027-risk-tiered-agent-checking.md), [historical market proposal](../00-product/08-market-deep-dive-and-target-architecture-2026-09.md).
