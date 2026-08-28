# ADR-0016 — Quality Baselines Are Value-Free; Source Freshness Fails Closed

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture + Data Governance

## Context

Two distinct temptations in data quality. The first is to retain rows for comparison, which conflicts with ADR-0014. The second is subtler and more dangerous: reporting **metadata scan time** as data freshness. They are not the same thing. A scan that ran ten minutes ago says nothing about when the business data last changed. A user who reads "fresh: 10 minutes ago" and acts on stale data has been misled by the platform.

## Decision

**Quality baselines are value-free.** Volume, null-rate, and schema-fingerprint comparisons are computed from counts, rates, and hashes. Observations are immutable and retain no source values. Incidents are fingerprinted so re-detection reopens rather than duplicates.

**Source-row freshness fails closed.** Freshness remains `NOT_CONFIGURED` until a connector receives an **approved watermark contract** naming the column, its classification, and its retention treatment. Metadata scan age is reported **separately and explicitly labelled** as scan age, never as data freshness.

## Consequences

### Positive

- Reproducible quality controls without replicating regulated rows.
- The platform never misrepresents scan time as business-data freshness — a correctness property users can rely on.
- Incident deduplication is stable across scans.
- Quality evidence can be retained long-term without becoming a data liability.

### Negative — costs accepted

- Freshness shows as `NOT_CONFIGURED` until watermarks are approved per connector, which looks like a missing feature and will generate support questions.
- Statistical detection is weaker than value-distribution comparison.
- No distribution-drift detection without an approved value-access exception.
- Competitors will show a freshness number where Atlas shows "not configured."

## Alternatives considered

| Option | Why rejected |
|---|---|
| Report scan age as freshness | Actively misleading; users would act on it |
| Retain samples for distribution comparison | Violates ADR-0014 |
| Infer freshness from row-count change | Unreliable — an update that does not change count is invisible |
| Default a watermark column by naming heuristic | A guessed watermark produces confident wrong freshness |

## Revisit trigger

An approved connector watermark, classification, and retention contract exists — at which point freshness activates for that connector.

## Related

- `20-modules/11-data-quality.md`
- ADR-0014
