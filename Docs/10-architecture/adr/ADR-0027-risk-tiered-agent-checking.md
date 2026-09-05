# ADR-0027 — Risk-Tiered Agent Checking

**Status:** Proposed | **Date:** 2026-09-04 | **Owner:** Architecture + Data Governance

## Context

INV-8 says **maker ≠ checker**: no actor approves its own work. It is one of the nine invariants, it is enforced platform-wide rather than per-feature, and it is a large part of why Atlas can claim a governance story a bank's model-risk function will accept.

Atlas now drafts a great deal of the work that arrives in the review queue. Asset descriptions, business annotations, glossary link proposals, classification promotions, metric candidates mined from query history — all are produced by deterministic services or by an approved model route, all land as proposals, and all wait for a human. That is correct and it is the design. It is also, at estate scale, the bottleneck: the 2026 market research in `00-product/08` records vendors generating hundreds of thousands of descriptions in weeks, and a queue that only humans can clear does not clear.

The tempting fix — let the drafting agent auto-apply its own high-confidence output — is exactly what INV-8 forbids, and rightly: it is one actor approving its own work with an extra step. ADR-0025's auto-approve escape hatch approaches the same problem from the confidence side and remains Proposed for the same reason.

The question this ADR answers: **is there a form of automated checking that satisfies INV-8's intent rather than eroding it?**

## Decision

**An independent reviewer agent may act as checker for risk-tier T0 and T1 proposal classes only, under three hard conditions.**

The tiers are defined in `src/aida/review_risk_tiers.py`, are a pure function of the object type, and default an unknown type to T3 (the tier no agent may ever decide):

| Tier | Question it answers | Examples |
|---|---|---|
| **T0** | "if this is wrong, someone reads a slightly worse sentence" | asset descriptions, business annotations, enrichment proposals |
| **T1** | "if this is wrong, a link or a label is wrong" | glossary links, term-to-semantic bindings, classification promotions, mined metric candidates, small bulk stewardship operations |
| **T2** | "if this is wrong, a published definition or an executable capability is wrong" | semantic model versions, metrics, glossary terms, governed tools, context products, data contracts |
| **T3** | "if this is wrong, the platform's trust boundary moved" | model routes, AI asset registrations, agent contracts, cross-boundary grants, access policies, source bindings |

### The three conditions

**(a) The reviewer agent can only approve or reject a review item.** It can never publish a semantic version, activate a model route, change a policy, grant access, or bind a tool. This is enforced twice: by the tier table (every one of those objects is T2 or T3), and by an allowlist derived *from the tier table* rather than from configuration, so a misconfigured ceiling can narrow what the agent may touch but can never widen it.

**(b) Every agent-approved item is sampled to a human, at a configurable rate with a hard floor of 5%.** The floor is a CHECK constraint on `agent_contract.sampling_rate` and is re-applied at the point of use, not only at validation time. Sampling is deterministic — a pure function of the item's fingerprint and the rate — so it replays, and so an auditor can verify that the sample was not chosen to flatter the agent.

**(c) A single human action suspends the tier for an organization.** `POST /reviewer-agent/suspend` stops every agent decision immediately; it is audited, and it requires no deployment.

### Why this satisfies INV-8 rather than eroding it

INV-8's intent is that a *second, independent judgement* stands between a proposal and its becoming authoritative. The reviewer agent is independent of the maker in every sense the invariant cares about:

* **A different principal.** Its own workload identity (`agent:` prefix, refused if it equals any human principal), so `maker != checker` is enforced by the same platform check that enforces it for humans, with no special case.
* **A different model route and prompt.** It never shares context with the drafting agent; it reads the proposal as data, exactly as a human reviewer would.
* **A different input set.** It decides on blast radius, negative knowledge, quality state and confidence — evidence *about* the proposal — not on the reasoning that produced it.
* **Its own eval gate.** A new reviewer-agent version must clear its exemplar corpus before it can be activated, which is a stronger precondition than any human checker faces.

And the residual risk is bounded by construction: the worst case is a wrong T0 or T1 fact — a poor sentence, a wrong link — that is reversible by editing, visible in the audit ledger, and sampled to a human at a known rate. Regulators already accept sampled human oversight of automated controls; what they do not accept is its absence.

## Consequences

### Positive

* The review queue clears at the rate the estate generates proposals rather than the rate humans read them, which is the difference between a governed catalog and shelfware.
* Human reviewer attention moves to T2 and T3, where the judgement actually is.
* The disagreement rate on sampled items becomes a measured, published quality signal for the drafting agents — the calibration loop `00-product/08` §4.3 describes, with real labels.
* Refusing to let an agent decide its own work, while letting an independent one decide, is a defensible position to put in front of a model-risk officer. "The agent approves itself above 0.95 confidence" is not.

### Negative

* A second model route's cost and latency on every T0/T1 item.
* A new failure mode: a *systematically* wrong reviewer agent approving a class of wrong proposals. Mitigated by the sampling floor and by the eval gate, bounded by the tier ceiling, but real. The sampled disagreement rate is the metric to watch, and a rise in it should trip suspension.
* One more thing to explain in an audit. The tier table has to be defensible object type by object type.

### Neutral

* Default off. `AIDA_REVIEWER_AGENT_ENABLED=false` ships, and an organization that never enables it sees exactly today's behaviour.

## Alternatives considered

**Let the drafting agent auto-apply above a confidence threshold** (ADR-0025's shape). Rejected as the checker mechanism: it is one actor approving its own work, which is the thing INV-8 exists to prevent, and confidence is self-reported by the same model. ADR-0025 remains a separate, narrower question about deterministic high-confidence output.

**Raise the tier ceiling to T2.** Rejected. A published semantic version changes what every downstream answer means, and a governed tool is executable capability. Both deserve a human, and neither is a bottleneck at the volumes T0/T1 reach.

**Human review of a sample only, with the rest auto-applied and no agent.** Rejected: it removes the second judgement entirely rather than automating it, and the sample would be the *only* check rather than a check on a check.

**No automation; hire reviewers.** This is the honest baseline and it is what an organization that declines this ADR gets. It is viable at small estate sizes and does not scale to the volumes the market research documents.

## Revisit trigger

Revisit when **the sampled disagreement rate exceeds 5% for any object type over a full month**, or when a T0/T1 misclassification causes an incident. The rate is computed by `reviewer_agent_metrics.disagreement_rates` and published at `GET /v1/organizations/{org}/reviewer-agent/disagreement-rates`, which reports `breaching_object_types` directly — so this trigger is checkable rather than merely stated. It reports and never suspends: a metric that could stop the agent by itself would be a second automated authority arriving through an observability endpoint. Either says the tier table or the agent is wrong. Also revisit if a regulator or internal audit rejects sampled automated checking as a control — in which case the ceiling drops to T0 or the feature is disabled, both of which are configuration changes rather than code changes, by design.

## Implementation status (2026-09-04, updated later the same day)

The tier table (`review_risk_tiers.py`), the reviewer agent (`reviewer_agent.py`), its API, the sampling floor as a CHECK constraint on `agent_contract`, and the suspend/resume path are implemented. **No reviewer-agent model route is approved in any environment**, and the feature is off by default, so nothing decides anything today.

The disagreement-rate metric is now implemented (`reviewer_agent_metrics.py`) and **still returns `measured: false` in every environment**, which is the honest report while nothing is being sampled. Two properties of it are worth stating here because the revisit trigger's credibility rests on them: a rate is never reported as a signal below 20 resolved samples (at a 5% threshold, that is the smallest sample in which one disagreement is *at* the threshold rather than four times over it), and an unresolved sample is never counted as agreement — otherwise the rate would fall every time the audit queue fell further behind, which is precisely when it should rise. A large `pending` count beside a small `resolved` count is itself a finding: it means condition (b) of this ADR is not actually being met.

The kill switch this ADR's condition (c) depends on has an executable drill (`scripts/agent_kill_switch_drill.py`, 10/10 steps). It is local and single-process — evidence that the control's logic holds end to end, not bank-scale evidence.
