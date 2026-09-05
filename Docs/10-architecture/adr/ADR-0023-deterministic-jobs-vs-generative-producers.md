# ADR-0023 — Deterministic Jobs vs. Confidence-Gated Generative Producers for Catalog Enrichment

**Status:** Proposed | **Date:** 2026-09-03 | **Owner:** Architecture

## Context

The 2026-08-30 review of Atlan's Context Agents (`review-2026-08/atlan-context/02-context-agents.md`)
found real capability gaps worth closing — usage-based enrichment priority (N20),
metric-conflict detection (SM-8), context-scoped term definitions (GL-10), and
batch/sampling review (GL-11) — and one thing worth declining: packaging a pipeline
as a fleet of named agents (F6). The decline was correct and is already grounded in
ADR-0002 and ADR-0008 (the unit of work is a task in a DAG, not an agent).

What that review did not leave behind is a reusable rule. Each of N20/SM-8/GL-10/
GL-11, and every future feature that looks like "let something propose a metadata
change automatically," restates the same question: is this a deterministic
computation we haven't written yet, or does it need a model? Left undecided, that
question gets re-litigated per feature, and the answer will drift — one team routes
around the confidence cap because "this one case is obviously safe," and the
model-risk architecture ADR-0001 already committed to erodes one exception at a time.

ADR-0001 settled this for the interactive query path: deterministic services do
discovery, profiling, authorization, execution; models are bounded to semantic
interpretation and generation, and model output is untrusted input that can never
itself execute, publish, or approve. This ADR applies that same reasoning to the
other surface the Atlan review put in scope — background catalog-enrichment
producers — and gives it a concrete, per-capability test instead of a per-feature
debate.

## Decision

**Apply this test at design time, per capability, before building anything shaped
like "propose or change metadata automatically":**

Can the output be derived from structured input by a function whose result is
provable before it runs — parsing, counting, graph traversal, structural
comparison? Then it is a **deterministic job**, not an agent. No model sits in this
path. It still needs guardrails, just not model guardrails: a bounded scope (row
and batch caps, matching the existing 500-subject bulk pattern in module 08 §7),
an evidence record so its output is auditable the same way an `AgentRun` is,
honest capability reporting (INV-9 — a capability is advertised only once it is
built and certified, e.g. `Connector.get_query_history()` must exist and pass
certification before `query_history=True` ships on any connector), and, wherever
it touches raw source query text, redaction at ingestion before persistence
(the same discipline `sql_redaction.py` already applies, per INV-6).

If the task is irreducibly linguistic or a fuzzy judgment call — writing a
description, naming something, judging a synonym — it may use a model, but only as
a **confidence-gated generative producer**: registered the same way the existing
interactive agent is (an `AiAsset`/`AgentRun`-shaped evidence trail), capped at the
existing model-only confidence ceiling (0.70; never reaches the 0.95 auto-publish
line — ADR-0001, `90-reference/04-analysis-algorithms.md` §4), and routed through
the single `GovernanceReview` maker-checker queue (INV-8) before anything becomes
authoritative. A generative producer never gets its own bespoke approval path.

This is decided **per capability, not per bundle.** Do not decide once for "the
enrichment agents" as a group. Concretely, for the four items already scoped:

- **N20** (query-history/usage scoring) — deterministic job.
- **SM-8** (metric collision detection) — deterministic job.
- **GL-10** (context-scoped term resolution) — deterministic job (schema change +
  graph walk).
- **GL-11** (batch/sampling review) — a deterministic guardrail that wraps
  generative output; not itself generative.
- Description/README drafting (the N10/GL-9 lineage) — confidence-gated generative
  producer, already under the existing cap.

Every generative producer also gets a **per-producer kill switch**, in addition to
the platform-wide one, consistent with the agent-registry sketch already in
`review-2026-08/target/03-context-tools-agents-mcp.md` §4.

## Consequences

### Positive

- One reusable test replaces a per-feature debate about whether something is "an
  AI agent."
- No new agent framework and no bespoke approval path gets created — every new
  capability plugs into INV-6/INV-8/INV-9 and the existing confidence-tier table,
  so a model-risk reviewer sees the same architecture no matter how many
  enrichment capabilities ship after this one.
- Keeps the "decline the fleet of named agents" decision from being reopened
  feature by feature, which is how architectural boundaries actually erode.

### Negative — costs accepted

- Slower to ship something that "feels like an agent" than wrapping an LLM around
  it directly, since classification and evidence wiring are mandatory first steps,
  not an afterthought.
- Some deterministic jobs will miss cases a model might catch heuristically. This
  is already accepted explicitly for SM-8 in the source review (§6): two metrics
  differing only in a redacted filter literal are flagged as differing, not
  explained further, and that is the honest cost of INV-6 rather than a defect to
  fix later.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Build named agents per Atlan's model (Scout, Scribe, etc.) | Already rejected by the 2026-08-30 review and by ADR-0002/ADR-0008: a DAG of pipeline stages given agent identities, multiplying permission and certification surfaces without adding capability |
| Let each new feature decide deterministic-vs-model ad hoc | No shared test means the same debate repeats per feature, and the answer drifts inconsistently over time |
| Default every new enrichment capability to a model for speed | Violates ADR-0001's authority boundary — a model producing metadata that could reach a low-friction review path is exactly what INV-8's unconditional maker-checker requirement exists to block |

## Revisit trigger

A capability exists where the deterministic/generative line is genuinely ambiguous
at design time — not merely that the deterministic version is more work. Escalate
to Architecture rather than defaulting either way.

## Related

- ADR-0001 (hybrid deterministic/LLM architecture)
- ADR-0002 (workflow and agent orchestration)
- ADR-0008 (no agent framework in core)
- `10-architecture/01-principles-and-invariants.md` — INV-6, INV-8, INV-9
- `review-2026-08/atlan-context/02-context-agents.md`
- `review-2026-08/target/03-context-tools-agents-mcp.md` §4 (agent registry, kill switch)
