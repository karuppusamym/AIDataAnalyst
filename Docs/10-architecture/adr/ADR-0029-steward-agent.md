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

## Amendment (2026-09-11): one runtime, two more agents

The six properties in the Decision were first written inside `steward_agent.py`. They are also what would make a lineage agent or a quality agent governable, and a second copy of them is how two agents end up governed differently. They now live in one runtime, `src/aida/task_agent.py`. Each agent contributes only a spec — its key, its capabilities and the object types they propose — and one coroutine per capability that finds work.

* Refusal and stop codes are agent-neutral: `agent_version_not_approved`, `agent_principal_reserved`, `agent_autonomy_withdrawn`, `agent_review_backlog_full`.
* Every setting ending in `_agent_principal_id` is reserved against every other agent. The runtime reads that list from the settings model itself, so an agent added later is kept off every existing identity, the reviewer agent's included, as soon as its setting exists.

Two agents were added on this runtime. Each passes the test the Decision sets: its identity enforces something.

**The lineage agent (`agent:lineage`).** Ingestion captures every view's definition, and nothing parsed it. View lineage existed only where a person pasted SQL into the parse endpoint. The agent parses eligible definitions — active, available, literal-redacted and screened clean — for views that have no parsed lineage in any review state. It writes the edges whose sources resolve, as PROPOSED. Two things differ from the steward agent:

1. **A second write path, deliberately narrow.** Parsed edges are decided in [ADR-0026](ADR-0026-per-edge-type-lineage-review.md)'s per-edge queue, not in `GovernanceReview`. The runtime lets an agent write into a dedicated queue only if that queue is on its human-only list: a queue no agent can decide from, whose own maker-checker compares an edge's author with its reviewer. `PARSED_LINEAGE_REVIEW` is the only entry.
2. **Auto-activation does not apply.** `lineage_parsed_edges_review_mode` and the high-confidence threshold govern what a person's parse may activate. An agent's edge is PROPOSED whatever they say.

The agent does not re-parse a view that has any edge, including one a reviewer rejected. A definition it cannot turn into lineage is recorded once and not examined again until it changes. Its backlog bound and its outcome measure count edges.

**The quality agent (`agent:quality`).** DQ-4 gave stewards threshold rules; nothing suggested one. The agent derives two value-free rule types from each table's most recent completed profiles:

* a row-count floor at half the smallest recent count;
* a null-rate ceiling just above the worst recent null rate of a column that is normally complete.

It needs at least three profiles. It never proposes a rule key — table, column and rule type — that already has a rule, enabled or not, or a proposal in any state. A rule a person disabled and a proposal a person rejected are answers already given.

A proposal is a new governed object, `QUALITY_RULE_PROPOSAL`, classified **T2**. A failing rule gates governed tools, demotes retrieval and attaches warnings to answers, which is the harm a data contract's quality clause can do. On approval, the decision adapter creates an enabled rule in the datasource's "Agent-proposed rules" pack, with the approver as the rule's creator.

**Proposing and deciding are now separate ceilings.** This is the one change to the runtime's rules, and the quality agent needed it:

* **Deciding** stays capped at `HARD_MAX_AGENT_TIER` (T1) for every agent, and nothing about the reviewer agent changes.
* **Proposing** also defaults to T1. A spec may declare a higher proposal ceiling, up to `HARD_MAX_PROPOSAL_TIER` (T2), and the quality agent is the only spec that does. A spec above T2 cannot be constructed, and the tier is checked again each time a review is opened.

So a person decides every T2 proposal, and no agent ever asks to move the trust boundary (T3).

### Consequences of the amendment

* The same code refuses, stops, bounds, ledgers and measures every task agent. An agent that wanted to behave differently would have to change the runtime, where the change is visible, rather than its own module.
* People get more to review: edges from every eligible view, and rule proposals no automation may approve. The default backlog bounds — 500 edges, 50 rule proposals — are what keep that from becoming a queue nobody reads.
* If the reviewer agent is enabled, it may still decide the steward agent's T0/T1 proposals. It can decide neither lineage edges (a different queue) nor quality rules (T2).

### Revisit trigger (added)

* Any spec other than the quality agent's is proposed with a proposal ceiling above T1.
* A second dedicated queue is proposed for the runtime's human-only list.

## Implementation status (2026-09-11)

**Implemented:**

* **The shared runtime:** `task_agent.py` and `task_agent_api.py`, with its response shapes.
* **The lineage agent:** `lineage_agent.py`, `lineage_agent_api.py` and `lineage_table_resolution.py`.
* **The quality agent:** `quality_agent.py`, `quality_agent_api.py`, `quality_rule_proposals.py`, `quality_rule_proposal_model.py` and migration `e3b8f14c6a92`.
* **For every agent:** four settings, and a console built on one shared screen component.
* **Scheduling:** `task_agent_registry.py` lists the agents for every surface that treats them as a class, and `task_agent_schedule.py` is a scheduler pass. A positive `<key>_agent_interval_minutes` starts that agent once per interval in every organization that registered it. A scheduled run is the governed run; only its trigger differs.
* **Counting:** a refusal that came after authority resolved records the version it stopped. The agent inbox counts task-agent runs, completed and refused, from their audit rows. The roster lists each task agent's completed runs from the same rows.
* **Column descriptions:** the steward agent's third capability, COLUMN_DESCRIPTION. It drafts the columns of its worklist tables that have no approved or retired description and no open draft. Drafts come from evidence only (`column_description_service`), and each is submitted as the agent's own request.
* **The ingest side-car under the contract:** where an organization has registered the steward agent, `newly_created_table_drafter` drafts as that agent. The agent's kill switch or a T0 contract stops it; a reviewable draft becomes the agent's request, and each draft gets a ledger row. An organization that never registered the agent sees no change. That removes the behaviour change the Alternatives table deferred on.
* **An evidence fix:** GL-9 names only same-source lineage that no reviewer rejected, which closes the ADR-0017 gap recorded in the 2026-09-10 status.

Tests: `tests/test_lineage_agent.py`, `tests/test_quality_agent.py`, `tests/test_task_agent_schedule.py`, `tests/test_steward_column_descriptions.py`, `tests/test_side_car_steward_contract.py` and `tests/test_gl9_lineage_same_source.py`, alongside the steward agent's.

**Not done, stated plainly:**

* Scheduled runs are off. Every `<key>_agent_interval_minutes` is 0 by default, so a person starts every run until an operator sets one.
* The roster lists a task agent's completed runs, not its refused ones; the agent inbox counts those.
* The lineage agent parses views only, by decision. The routine-aware procedure edge table (`deep_procedure_lineage_edge`) has no review state. The reviewable one (`procedure_lineage_edge`) carries no routine identity. An agent must not write lineage that no person reviews, so procedures wait until the routine-aware table has a review state. dbt models keep their own manifest path.
* The quality agent proposes floors and null-rate ceilings only. Profiles store no values, so it has nothing to derive a range or distribution rule from without breaking INV-6.
* Nothing has been measured on a real estate.
