# ADR-0013 — Prompt-Risk Screening Precedes Retrieval

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture + Security

## Context

Prompt injection is usually treated as an output problem: check what the model produced. By then the malicious instruction has already influenced which metadata was retrieved, what entered the model context, and which tool was selected. Screening after retrieval closes the smallest part of the gap.

## Decision

**A deterministic, versioned prompt-risk classifier runs as an explicit `SCREENED` state before any retrieval, model context construction, tool selection, or SQL work.**

It blocks signals for instruction override, system-prompt or credential extraction, policy or masking bypass, privilege escalation, and unbounded data extraction.

Only **value-free evidence** is retained: classifier version, score, and reason codes, alongside the existing question HMAC fingerprint. The raw question is never stored.

A denial stops the run before retrieval. The refusal names the classifier version and reason codes so the user understands what fired.

## Consequences

### Positive

- A malicious instruction never reaches retrieval ranking, model context, or tool selection.
- The screening decision is deterministic, versioned, and replayable — it can be audited and re-derived.
- Refusals are explainable rather than opaque.
- Evidence is value-free, so screening records are safe to retain for seven years.

### Negative — costs accepted

- False positives block legitimate questions; the classifier needs a tuning and appeal path.
- A deterministic rule set is evadable by paraphrase, obfuscation, and other languages — this is defence in depth, not a complete solution.
- Adds a stage to the latency budget (20 ms p95).
- Rule-set maintenance is ongoing security work.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Screen model output only | Injection has already influenced retrieval and tool selection |
| ML classifier as the primary gate | Non-deterministic; cannot be replayed for audit; may be added as defence in depth |
| No screening; rely on the execution gateway | The gateway stops unsafe *SQL*; it does not stop unsafe *retrieval* or context poisoning |

## Revisit trigger

Approved semantic or ML classifiers may be added **as defence in depth**. Deterministic deny rules and downstream execution gates remain authoritative.

## Open work

Indirect injection through *retrieved metadata* (a malicious column description) is a known gap, tracked as P0 in `60-delivery/03-tracker.md`.

## Related

- `50-security/03-ai-safety-controls.md`
- `20-modules/13-agent-runtime.md`
