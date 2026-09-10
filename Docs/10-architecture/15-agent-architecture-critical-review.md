# Agent architecture: implementation audit and adversarial review

> Reviewed: 2026-09-09. Baseline: `15f29cd`, clean working tree before this documentation pass.
> Status: code-backed review and proposed remediation, not production certification or approval to enable automation.
> Scope: agent inventory, authority boundaries, economics, recovery, observability, and selected vendor documentation. No runtime configuration or application code was changed. Live registrations, enabled routes and production capacity were not measured.

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

Priorities are recommendations. **P0** blocks expansion of unattended decisions; **P1** blocks enterprise readiness claims or materially weakens operations; **P2** improves product quality. Owners below are proposed functional owners, not assigned individuals. All findings remain open unless explicitly described as a documentation correction.

| ID / priority | Evidence and consequence | Required closure evidence / proposed owner |
|---|---|---|
| AR-01 / P0 | `config.py::Settings.reviewer_agent_max_tier` accepts T0 through T3. `review_risk_tiers.py::agent_decidable_object_types` filters by that configurable ceiling without an independent T1 maximum. `auto_decide_tier0_tier1` uses the result. Consequently, the documented hard T0/T1 boundary is not enforced by these guards under elevated configuration. | Clamp eligibility to an immutable T0/T1 maximum and enforce it at the decision boundary. Test T2/T3 configurations with actual higher-tier proposals; assert no authoritative mutation, success audit or outbox event. Governance + Security. |
| AR-02 / P0 | `pre_review_pending` calls `risk_tier_for(review.object_type)` without payload. Size-based escalation in `risk_tier_for` requires payload for bulk stewardship and workbook imports. Auto-decision can reuse that stored tier. Large batches can therefore remain classified T1 by this path. | Load authoritative change counts and relevant action details; missing/unverifiable size must abstain or escalate. Exercise a 900-change import through pre-review and decision, not just the tier helper. Governance. |
| AR-03 / P0 | `_recommendation` returns APPROVE for eligible items with no prior rejection, no open incident and `confidence is None`. Absence of contrary evidence does not demonstrate correctness. It is not an independent semantic assessment. | Require positive, object-specific evidence and explicit abstention criteria. Evaluate missing evidence, misleading metadata and deliberately wrong proposals. Measure false approvals by type. AI Platform + Stewardship. |
| AR-04 / P0 | Pre-review skips already-reviewed items; auto-decision consumes stored recommendations. Organization suspension is checked at batch entry, before iterating. This does not establish fresh evidence or an immediate stop for an already-running batch. | Define freshness and revocation semantics; revalidate evidence/version and authority before each commit. Concurrently suspend a multi-worker batch and introduce new adverse evidence. Measure the maximum stop delay and document handling of committed/in-flight work. Governance + Platform. |
| AR-05 / P1 | Contract daily/per-run token and wall-clock caps are validated and stored. The inspected runtime checks contract existence, kill switches and selected tool slugs, but no runtime consumer of these contract cap fields was found. Model-route caps are a separate control. Token accounting is estimated. | Demonstrate enforcement for every execution path, including fallback, retry and concurrent calls, with atomic budget reservation/reconciliation and provider usage where available. Distinguish estimated tokens from billable usage. AI Platform. |
| AR-06 / P1 | Contract `context_product_ids` and `write_lanes` are parsed/stored; `envelope_violation` checks tool slugs. Their declaration alone is not evidence of complete runtime enforcement. | Produce an endpoint/path enforcement matrix for tool execution, freeform generation, context consumption and writes; deny out-of-envelope requests at server boundaries. This is an audit gap, not proof that all paths bypass authorization. Security. |
| AR-07 / P1 | `AgentRun.ai_asset_version_id` exists and is populated for linked runs; `agent_roster.py` still states the identity link does not exist and summarizes activity organization-wide. It also emits blanket no-auto-apply text. | Attribute runs through the existing version relation; retain a separate unlinked/human category. Show actual capability, enablement, suspension and effective scope without claiming per-agent ownership of organization totals. AI Platform + UX. |
| AR-08 / P1 | `semantic_inference.py` batches at most 25 tables per model invocation, sequentially in the inspected enrichment loop. Old documentation claims approximately 50 calls for 100,000 tables. | Corrected in this pass. Before capacity claims, measure selected table count, payload size, provider limits, retries, fallbacks, spend and elapsed time. Platform. |
| AR-09 / P1 | Discovery has bounded fan-out, retries, heartbeats and continue-as-new. `workflows/worker.py` registers discovery and ingestion on one configured Temporal task queue. This does not prove independent queues per worker class, deployment failover or source/tenant fairness at scale. | Run multi-tenant load and recovery experiments; publish configuration, queue depths, saturation, fairness, recovery times, p95/p99 and provider-inclusive latency. Platform. |
| AR-10 / P1 | `injection_defense.py` and `ingest_screening.py` exist and are used at ingestion/MCP paths. The runtime document's blanket assertion that indirect screening does not exist is stale. Coverage across legacy/imported/retrieved content was not established here. | Trace every model-context ingress, including updates and legacy rows; test adversarial metadata and establish quarantine/provenance behavior. A regex classifier passing does not prove immunity to injection. Security. |
| AR-11 / P1 | A deterministic reviewer can propagate systematic errors at high volume. Different principal IDs and 5% sampling alone do not demonstrate independent evidence, semantic correctness or timely human oversight. | Evaluate by risk class; measure resolved/pending sample counts, disagreement and false approvals, audit completion time and downstream harm. Provide withdrawal/compensation and notification procedures. Governance + Operations. |
| AR-12 / P2 | Product/architecture documents contain unqualified competitor absence claims, cost estimates and shipped-state statements that are not supported by this review. | This pass marks historical claims and replaces selected comparisons with dated primary sources. Further competitive claims need a source, edition/release scope, measured comparison and expiry/review date. Product. |

### Read-only reproduction of AR-01 and AR-02

Executed against the baseline Python environment, with no database mutations:

```python
from aida.review_risk_tiers import agent_decidable_object_types, risk_tier_for

print({t: "CONTEXT_PRODUCT_VERSION" in agent_decidable_object_types(t)
       for t in ("T0", "T1", "T2", "T3")})
# {'T0': False, 'T1': False, 'T2': True, 'T3': True}
print(risk_tier_for("MODEL_IMPORT_BATCH"))  # T1
print(risk_tier_for("MODEL_IMPORT_BATCH", payload={"change_count": 900}))  # T2
```

These reproduce guard behavior, not a successful exploit or an observed production publication. Existing tests cover the default ceiling; the elevated-ceiling test checks membership in known types rather than exclusion of T2/T3. Passing those tests does not establish the stronger invariant in ADR-0027.

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

1. Close AR-01 through AR-04 before expanding unattended review; establish the effective production configuration separately. Keep the ADR proposed until its stronger guarantees and approval process are resolved.
2. Complete the contract enforcement matrix, atomic budgets, revocation semantics and truthful per-agent reporting (AR-05 through AR-07).
3. Validate ingestion/context screening coverage and measure representative scale, recovery and human audit capacity (AR-08 through AR-11).
4. Add grounded column documentation and investigation capabilities incrementally. Compare rules, a single LLM workflow and bounded multi-step reasoning against the same task corpus; deploy the simplest approach that meets the agreed outcome targets.

Still to establish: actual registered/enabled agent counts; production model approvals and data residency; per-path workload identity binding; live cap enforcement; multi-worker revocation delay; restore/compensation behavior; model and prompt version retention; provider-inclusive SLOs; connectors/editions needed for competitor comparisons. These are unresolved verification questions, not assertions that all corresponding features are missing.

## 7. Review validation

Code inspection and the pure-function reproductions above were performed. Vendor sources were consulted. No production load test, model evaluation, live decision, configuration change or application test suite was run for this documentation-only review. Documentation changes do not close the underlying code findings.

Related: [runtime](../20-modules/13-agent-runtime.md), [workers](08-workers-and-workflows.md), [risk-tier ADR](adr/ADR-0027-risk-tiered-agent-checking.md), [historical market proposal](../00-product/08-market-deep-dive-and-target-architecture-2026-09.md).
