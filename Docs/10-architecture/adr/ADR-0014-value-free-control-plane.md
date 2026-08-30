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

## Enforcement

- INV-6 in `10-architecture/01-principles-and-invariants.md`
- Test: `test_no_source_values_in_control_plane` (`tests/test_inv6_value_freedom.py`; sentinel scan across tables, logs, events, traces)
