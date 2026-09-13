# ADR-0014 — Source Values Are Not Platform Memory

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture + Data Governance

## Context

Many features are easier with sample values: better classification, better semantic inference, richer query memory, more helpful error messages. Every one of those copies regulated bank data into a second system with its own retention, backup, access, and breach surface.

## Decision

**Raw source business values, user question text, and feedback comments are not platform memory.**

| Data | Treatment |
|---|---|
| Sample row values | Never persisted, logged, evented, or sent to a model |
| Result rows | Bounded, retention-governed, not in logs or model context by default |
| User question text | Keyed HMAC fingerprint only |
| Persisted SQL | Literals redacted |
| Profiles | Statistics only — counts, null rates, distinct estimates, length, fingerprints |
| Credentials | References only |

A policy-approved masked-value mode may be enabled **per classification and per model route**. It is never a default.

## Consequences

### Positive

- The blast radius of a platform breach is metadata, not customer data.
- Retention and residency obligations are dramatically simpler.
- Evidence records can be retained for seven years without becoming a liability.
- Model-context leakage of regulated data is architecturally prevented, not policed.

### Negative — costs accepted

- Classification accuracy is lower than value-based classification would achieve.
- Semantic inference works from structure alone, which is harder.
- Query memory cannot match on values, only on structure and semantics.
- Debugging is harder: an engineer cannot see the data that caused a failure.
- Some competitor features that depend on value inspection are simply unavailable.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Store samples with encryption | Encryption at rest does not remove the retention, access, and breach obligations |
| Store samples with short TTL | Still a copy; still discoverable; still a breach surface |
| Tokenize values | Tokenization mapping becomes the sensitive asset |
| Value access for classification only | Every exception becomes a precedent |

## Revisit trigger

Classification-specific retention approval could permit bounded, approved value access for a specific purpose — with its own residency, retention, and audit contract.

## Addendum — 2026-09-12: freshness watermarks (R11-B8)

**Decided 2026-09-12 on the product owner's delegation, under this ADR's own revisit trigger.** A freshness contract (`FreshnessWatermarkConfig`) is the shape that trigger describes: approved maker-checker, for one purpose, on one column, with its own classification and `retention_days`.

Freshness cannot be measured without one value from the source -- when the data last changed -- and no statistic in the Profiles row carries it. Without it every freshness contract evaluates STALE forever, correctly, and the control is decorative.

**Permitted:** the maximum of one column, and nothing else about it, only when all of these hold:

| Condition | Enforced by |
|---|---|
| The column is named by an ACTIVE (approved) freshness contract. An edit returns the contract to PENDING_APPROVAL, and reading stops | `freshness_observation.observe_freshness_for_datasource` reads ACTIVE contracts only |
| The catalog types the column as a date or a time | Refused before any statement is built (`COLUMN_NOT_TEMPORAL`), so a contract cannot be used to read `MAX(balance)` |
| The read is an ordinary governed query through the Query Execution Gateway, as the scheduler's identity (ADR-0004) | A masked or tokenized result yields no observation; a refusal is counted, never worked around |
| The value is stored only as `freshness_observation.watermark_value` -- and, as before this addendum, the latest one in a freshness incident's evidence | The observation sweep's audit record and log lines carry counts and reason codes only |
| Observations older than the contract's `retention_days` are deleted | Each observation pass |

Everything else in this ADR is unchanged. This is not a precedent for other statistics: a second one needs its own addendum with its own purpose, approval and retention, which is what the table above is for.

## Enforcement

- INV-6 in `10-architecture/01-principles-and-invariants.md`
- Test: `test_no_source_values_in_control_plane` (`tests/test_inv6_value_freedom.py`; sentinel scan across tables, logs, events, traces)
- Test: `tests/test_r11b8_freshness_observation.py` (the addendum above: only ACTIVE contracts on temporal columns are read, a masked read stores nothing, and the value never reaches the audit record)
