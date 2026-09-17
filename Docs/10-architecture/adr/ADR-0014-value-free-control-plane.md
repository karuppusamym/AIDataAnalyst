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

## Proposed addendum — governed sample rows (R11-FP04). **NOT ADOPTED**

**Status: PROPOSED. Nothing in this section is implemented, and no code behaves as if it were.**
It is written because R11-FP04's remaining half has been BLOCKED on "its own ADR-0014 addendum
covering masking, expiry and destinations" with no addendum drafted, so the block had no exit.
Drafting the exit is not taking it: this needs Architecture and Data Governance to accept it, as
the 2026-09-12 freshness addendum was accepted, before a line is built.

### Why this is a bigger decision than the addendum above it

The freshness addendum permits **one aggregate of one temporal column**, and says so in a table of
five conditions. This proposes something categorically larger: *whole rows of arbitrary columns*.
The 2026-09-12 addendum's closing sentence — "this is not a precedent for other statistics" — is
doing real work here, and the honest reading is that it is not a precedent for this at all. A
reviewer should treat the two as different in kind, not in degree.

What makes it arguable at all is the purpose. The [footprint design](../20-database-footprint-and-agent-context.md)
states that FP-04 "must remain useful when row sampling is disabled" and that "unknown meaning
should remain explicit rather than forcing broader data access" — and the value-free half now
shipped does exactly that. So this is not needed to make the product work. It is needed only for
the cases where structure and statistics genuinely cannot decide what a column means: a
free-text field carrying three different encodings, a status column whose codes are undocumented,
a numeric column that could be cents or dollars. A steward looking at counts cannot tell.

### What would be permitted

A **bounded, masked, non-persisted, expiring** read of rows from named columns, only when every one
of these holds. The right-hand column is what *would* enforce it — none of it exists yet.

| Condition | Would be enforced by |
|---|---|
| An approved policy names the datasource, the table and **the specific columns**. Editing it returns it to PENDING_APPROVAL and reading stops | The maker-checker lifecycle `ProfilingExceptionPolicy` already implements for value-bearing profiles, extended with a column list — not a second engine |
| A different principal approved it than requested it, and the request carries a stated purpose | The existing decision endpoint's maker-checker separation |
| The read is an ordinary governed query through the Query Execution Gateway, under the caller's own identity — never a worker's | INV-2's single execution boundary; a caller who cannot read the table cannot sample it |
| Masking applies per classification **before** the rows leave the gateway, and a masked or tokenized result is the only result | The gateway's existing masking path; add a refusal when a policy names a column whose classification has no masking rule |
| **Nothing is persisted.** The rows exist in one HTTP response and nowhere else — no table, no cache, no log, no event, no trace span, no audit detail, no export | This is the load-bearing difference from every prior addendum, and would need its own sentinel test extending `test_no_source_values_in_control_plane` |
| The response expires: a short-lived handle, single-use, with the row count and the expiry in the receipt | Add a handle whose lifetime is measured in minutes, not a stored result set |
| The rows never reach a model, an embedding, a description draft or a context product | The screening boundaries that already keep routine bodies out of model context |
| Every read writes an audit record naming the policy, the purpose, the caller, the columns and the row count — never a value | The existing audit path, which already records counts and reason codes only |

### What would remain forbidden

- Any persistence of a sampled value, including "temporarily" and including a hash keyed to a value.
- Any widening to columns the policy does not name, or rows beyond the stated bound.
- Reading through a worker or scheduler identity, which would detach the read from a human decision.
- Sampling as an input to classification, semantic inference or description drafting — the four
  "Alternatives considered" rejections above still stand, and "value access for classification only"
  is rejected there in terms that this proposal does not disturb.
- Any inference from an approved value-bearing **profile** policy. A profile exception permits
  `min_value`/`max_value`/`top_values` capture under retention; it is not sample access, and the
  footprint design says so explicitly. A test already asserts that non-inference and must keep
  passing whatever is decided here.

### What a reviewer should decide

1. Whether the purpose above justifies the category change at all, given the value-free half already
   ships and the design says the product must work without this.
2. Whether "nothing is persisted" is genuinely enforceable, or whether a bounded retention with
   deletion — as the freshness addendum chose — is the more honest contract. A rule that is easy to
   state and hard to verify is worse than a narrower rule that a sentinel test can prove.
3. Who owns the residency question. Rows in a response still cross whatever boundary the API
   crosses, and R11-C11 carries model residency as a separate blocked acceptance.

Until those are answered, R11-FP04's sample-row half stays BLOCKED, and that is the correct state
rather than a gap.

## Enforcement

- INV-6 in `10-architecture/01-principles-and-invariants.md`
- Test: `test_no_source_values_in_control_plane` (`tests/test_inv6_value_freedom.py`; sentinel scan across tables, logs, events, traces)
- Test: `tests/test_r11b8_freshness_observation.py` (the addendum above: only ACTIVE contracts on temporal columns are read, a masked read stores nothing, and the value never reaches the audit record)
