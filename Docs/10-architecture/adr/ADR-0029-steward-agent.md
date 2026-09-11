# ADR-0029 — The Steward Agent: a Contracted Identity for Stewardship Proposals

**Status:** Proposed | **Date:** 2026-09-10 | **Owner:** Architecture + Data Governance

## Context

The [target architecture](../../00-product/08-market-deep-dive-and-target-architecture-2026-09.md) (§5.2) names a Steward agent. Two later decisions constrain what one may be:

* [ADR-0023](ADR-0023-deterministic-jobs-vs-generative-producers.md) declined "a fleet of named agents" — pipeline stages given agent identities, multiplying permission and certification surfaces without adding capability — and required every *generative* producer to be registered with an evidence trail and a per-producer kill switch.
* The [2026-09-09 architecture review](../15-agent-architecture-critical-review.md) asked that named agents which are rules say so, and that agents be measured by task outcomes rather than counted.

What existed before this decision:

* The stewardship producers — GL-9 table-description drafts, GL-8 glossary-link proposals — ran only in two ways. A steward clicked a button, which attributed the draft to that steward, who then could not approve it. Or the ingest side-car wrote drafts as the principal auto-enqueue-drafter, which has no registration, no contract, no kill switch and no ledger.
* The AG-10 machinery — contract, per-agent kill switch, task ledger, budget, inbox — had exactly one execution consumer, the analyst orchestrator. `AgentContract.autonomy_tier` had no runtime consumer at all.

The question: **would giving the stewardship producers an agent identity add a control, or only a name?**

## Decision

**One registered agent, `agent:steward`, whose every run is governed by its `AgentContract`.** It works the documentation backlog, drafts with the existing deterministic producers, and puts each draft in the shared review queue as its own request. It decides nothing.

The identity is justified by what it lets the platform enforce. Each item below is enforced in `src/aida/steward_agent.py` and asserted in `tests/test_steward_agent.py`:

1. **An authority boundary.** The agent runs only under exactly one contract that names its principal and is attached to an APPROVED `AGENT`-kind AI asset version in the organization. A missing contract, an unapproved version, an ambiguous pair of contracts, or a principal equal to the reviewer agent's is a refusal. The run is never unconstrained.
2. **A kill switch that works mid-run.** The switch — the agent's own, its tier's, the organization's, or the model gateway's — is checked before the run and re-read from the database before every item. If authority is withdrawn mid-run (a switch engaged, the tier lowered to T0, the approved version gone), the run is refused and rolled back whole. Nothing a run produced after its licence was questioned survives it.
3. **The autonomy tier as a ceiling.** T0 observes: the run reports what it would propose and opens nothing. T1 proposes. T2 and T3 propose exactly what T1 does, because the agent has no branch that applies its own output. The tier can narrow it; it cannot widen it.
4. **Maker ≠ checker with no special case.** Every proposal carries `requested_by = agent:steward`, so the platform's existing INV-8 check needs nothing new:
   * any human reviewer may decide the proposal, including the steward who started the run — they did not write the text;
   * the agent may decide none;
   * the ADR-0027 reviewer agent, a different identity, may decide the T0/T1 ones.
   The agent may open only T0/T1 object types; opening anything above `HARD_MAX_AGENT_TIER` refuses the run.
5. **Bounds.** A per-run proposal limit with a configuration hard cap (ADR-0023's bounded scope). A review-backlog bound: the agent stops proposing when its own undecided proposals reach `steward_agent_max_pending_proposals`. And the contract's wall-clock cap. A budget reached mid-run ends the run and keeps what it did.
6. **A ledger and an outcome measure.** One `AgentTask` per proposal, linked to its review and carrying ids, hashes and scores, never text. An acceptance rate per object type (approved ÷ decided) that reads "no measurement" rather than zero until a reviewer has decided something.

**Selection is the steward's own.** Descriptions come from the AT-5 documentation worklist in its default priority order, so the agent works the backlog in the order a human steward is shown it. Links come from GL-8's exact label matcher. Both rules were moved out of their router into `documentation_worklist_signals` and `glossary_link_candidates`, and an import contract keeps the agent from reaching into a router for the next one.

**Method, stated plainly.** Both capabilities pass ADR-0023's test as deterministic jobs; nothing here calls a model. The identity is justified by the authority boundary above, not by the method, and every surface that reports this agent says `DETERMINISTIC`. The contract's token caps have nothing to bound; its wall-clock cap does.

## Consequences

### Positive

* The one automated writer of stewardship proposals that the platform supervises now has an owner, a contract, a kill switch and a ledger. Before this decision the only such writer was an anonymous principal.
* `autonomy_tier` has its first runtime consumer, and the consumer is a narrowing one.
* The maker/checker chain ADR-0027 describes — a drafting agent checked by an independent reviewer agent, sampled to a human — now has a real maker, not a human standing in for one.
* Reviewer acceptance of the agent's work is a number, not a claim.

### Negative

* Another registered identity, another console, another thing to explain in an audit.
* Deterministic drafts are thin. On an estate with little authored metadata most tables fall below GL-9's evidence bar, and the agent correctly skips them — so it will look less productive than an LLM drafter, because it declines to guess.
* A proposal goes straight to review, so a steward cannot edit the agent's text before deciding. They reject it, which records negative knowledge, and write their own.

### Neutral

* Off until registered. There is no enable flag. An organization that registers nothing sees no change.

## Alternatives considered

| Option | Why not |
|---|---|
| Leave the producers anonymous (the status quo) | An automated writer the platform cannot stop, attribute or bound is the gap ADR-0023's per-producer kill switch exists to close. |
| Bring the ingest side-car (the auto-enqueue-drafter principal) under this contract now | Deferred, not rejected. Failing closed would stop auto-drafting on ingest in every organization that has not registered the agent — a behaviour change to an existing path that deserves its own decision. |
| LLM-written descriptions | Deferred to the evaluation the architecture review asks for (§6 item 5): compare rules, a single LLM workflow and bounded reasoning on one corpus, then deploy the simplest that meets the target. A generative drafter would be an ADR-0023 confidence-gated producer and could run under this same contract, where the token caps would then bound something. |
| Auto-apply high-scoring drafts | The maker approving its own work. Rejected for the reasons ADR-0027 gives. |
| One agent per capability ("scribe", "linker") | ADR-0023's F6 decline. The identity follows the supervisory relationship — one steward, one agent — not the pipeline stage. |

## Revisit trigger

Revisit when any one of these happens:

* Either object type's acceptance rate falls below 50% over at least 50 decided proposals in 30 days. The agent is then producing work reviewers mostly discard.
* A model route is approved for drafting.
* The ingest side-car is proposed for inclusion under this contract.

## Implementation status (2026-09-10)

**Implemented:** `steward_agent.py`, `steward_agent_api.py` (`GET /v1/organizations/{org}/steward-agent`, `POST …/steward-agent/run`), the two extracted rule modules, three settings, the import contract, and a console screen. Tests: `tests/test_steward_agent.py`.

**Not done, stated plainly:**

* There is no scheduler. A run is started by a person.
* Column descriptions are not a capability yet. They were being built in parallel on the same branch.
* Nothing has been measured on a real estate.
* The mid-run stop has been exercised on SQLite in one process, not across PostgreSQL workers.
* GL-9's evidence gathering names upstream and downstream tables from lineage without filtering them to the draft's own datasource. The column drafter does filter them (ADR-0017). That gap predates this agent, but the agent now reaches it without a human in the loop.
