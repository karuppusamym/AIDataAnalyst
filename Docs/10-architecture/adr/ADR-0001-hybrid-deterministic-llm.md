# ADR-0001 — Hybrid Deterministic and LLM Architecture

**Status:** Accepted | **Date:** 2026-08-28 (originally recorded 2026-08) | **Owner:** Architecture

## Context

The platform must answer natural-language analytical questions over regulated bank data. Two architectures are available: let a model interpret, plan, generate, and execute; or use models only where determinism cannot reach.

A model-risk review asks one question that decides this: *can you prove the model was unable to take an unapproved action?* An architecture in which model output becomes an executed command cannot answer it, because the proof would have to be statistical.

## Decision

Deterministic services perform discovery, profiling, classification rules, key and relationship evidence, authorization, SQL parsing, cost control, execution, audit, and workflow state.

LLMs are used **only** for bounded semantic interpretation, ambiguity handling, logical-plan suggestion, description generation, and result explanation.

**LLM output is untrusted input.** It is schema-validated on receipt and may never directly:

- execute a query or call a source,
- change a policy,
- publish a semantic version,
- approve a governed object,
- widen a permission or an allowlist.

For business-semantic inference specifically, the model receives bounded identifiers, types, classifications, constraints, and deterministic baselines only. It may propose domains, entities, descriptions, roles, grain, synonyms, questions, and a column-only tool blueprint. **It cannot author executable SQL** — tool SQL is rendered deterministically from an approved blueprint and begins as a draft. An independent checker must approve before anything becomes authoritative.

## Consequences

### Positive

- The model-risk answer is architectural, not statistical.
- A model change (provider, version, prompt) cannot alter what the system is *able* to do.
- Failures are attributable: a wrong answer is a retrieval, semantic, or generation problem, and the evidence record says which.
- Model spend is bounded because deterministic work is not delegated to a model.

### Negative — costs accepted

- More engineering. Deterministic classification, key inference, and relationship scoring are real work that a model could approximate cheaply.
- Lower ceiling on "magic." Atlas will not answer a question that requires reasoning outside the approved envelope, where a less constrained competitor might.
- Latency from validation stages that a direct-execution design skips.

### Neutral

- Model neutrality follows: no provider is architecturally privileged.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Model plans and executes directly | Fails model-risk review; unbounded blast radius |
| Model generates, human approves each query | Unusable at analyst volume |
| No models at all | Loses the semantic interpretation that makes the product valuable |
| Model with tool-calling and a permission list | The permission list becomes the trust boundary and is only as good as the prompt |

## Revisit trigger

**Never for the authority boundary.** Revisit only to expand the set of approved reasoning routes, each of which requires its own evaluation evidence.

## Enforcement

- INV-3 in `10-architecture/01-principles-and-invariants.md`
- Test: `test_model_output_types_are_inert`
