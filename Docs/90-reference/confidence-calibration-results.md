# Confidence calibration results (SM-3)

Generated 2026-09-01T07:53:24.615420+00:00 by `scripts/confidence_calibration_benchmark.py`. Reproduce with `uv run python scripts/confidence_calibration_benchmark.py` (requires `AIDA_ENVIRONMENT` set, e.g. `development`). Every number below comes from a real run of the real, unmodified `aida.metric_suggestion_service.score_evidence` (SM-4) against the deterministic corpus at `tests/fixtures/confidence_calibration_corpus/bank_domain_metric_corpus.json` -- not hand-typed.

Scope: this calibrates the one module-07 inference this branch has made real and gradated -- SM-4's metric-suggestion evidence score. `aida.semantic_inference.infer_table_semantics` also reports a `confidence` field, but it is a coarse binary choice (0.82 or 0.66) with no gradation across scores to calibrate against a reliability diagram, and SM-1 (dimension authoring) is not yet built; both are out of scope here, not silently skipped.

Unlike AG-8's model-generation half, this benchmark needs **no live infrastructure**: `score_evidence` is a pure function of a `MetricEvidence` value -- no DB session, no embedding provider, no model route. Every number below is a full, real result.

## Corpus

28 labelled bank-domain (table, column) cases -- 14 true positives / 14 false positives -- every case a numeric column with an EXACT or SUFFIX `MEASURE_KEYWORDS` match (`aida.metric_suggestion_service.match_measure_keyword`), the same gate the real production generation path applies before it ever calls `score_evidence`. False positives are drawn from real banking column-naming ambiguity the algorithm has no signal for: pre-aggregated/cumulative balances (`avg_daily_balance`, `running_balance`, `closing_balance`), per-unit rates (`unit_cost`, `weighted_avg_cost`), policy thresholds (`minimum_balance`, `balance_limit`), and precomputed `*_count` columns where the keyword's fixed COUNT aggregation is systematically wrong (the column already holds a per-row tally; the correct rollup is SUM of the stored counts, not COUNT of rows).

## Calibration curve (predicted confidence vs. observed accuracy)

| Confidence bucket | n | Mean predicted confidence | Observed accuracy | Gap |
|---|---|---|---|---|
| [0.0, 0.1) | 0 | — | — | — |
| [0.1, 0.2) | 0 | — | — | — |
| [0.2, 0.3) | 0 | — | — | — |
| [0.3, 0.4) | 2 | 0.3917 | 0.0000 | 0.3917 |
| [0.4, 0.5) | 4 | 0.4802 | 0.0000 | 0.4802 |
| [0.5, 0.6) | 1 | 0.5625 | 1.0000 | 0.4375 |
| [0.6, 0.7) | 10 | 0.6459 | 0.6000 | 0.0459 |
| [0.7, 0.8) | 4 | 0.7573 | 0.0000 | 0.7573 |
| [0.8, 0.9) | 7 | 0.8583 | 1.0000 | 0.1417 |
| [0.9, 1.0) | 0 | — | — | — |

**Expected Calibration Error (ECE): 0.2722**

**Brier score: 0.2184** (0 = perfect, 0.25 = an uninformative constant-0.5 predictor on this balanced 14/14 corpus)

## What the numbers say

score_evidence is measurably **not** well calibrated as a probability of correctness (ECE 0.2722 against a well-calibrated target of 0; Brier 0.2184, worse than the 0.2500 an uninformative predictor would score on this balanced corpus). The miscalibration is not random noise: it is concentrated exactly where the score has no signal at all -- `score_evidence`'s four dimensions (match strength, fact-shaped table role, monetary type, clarity/completeness of annotation evidence) say nothing about whether the *proposed aggregation* is semantically correct for the column's actual grain. A `_count`-suffixed numeric column and a plain `_amount`-suffixed one can score identically high on identical evidence richness, even though the `_count` case's suggested `COUNT` aggregation is wrong every time in this corpus (it should be `SUM` of the stored per-row tally). Concretely: `txn_count`, `daily_fraud_alert_count`, and `daily_active_user_count` -- each with a bound glossary term *and* a description mention, the two richest evidence signals `score_evidence` has -- score 0.7542, ahead of `deposit_amount_true` (0.5625, correct, but with neither a bound term nor a description mention) and `acct_balance_true` (0.6917, correct). A false positive with rich corroborating evidence outscores a true positive with sparse evidence, because bound-term/description-mention evidence corroborates that a human steward believes the *column* is meaningful, never that the *aggregation* is right for it.

This is a real, actionable finding, not a restatement of the obvious: it says `score_evidence`'s overall score should not be read as "probability this proposal is correct as-is" -- it is closer to "strength of evidence that this column is a measure worth a reviewer's attention", which the human-in-the-loop `governance_review` gate this row's evidence feeds already assumes (SM-4's own docstring: this generates a *draft* for review, never an auto-published metric). A concrete next step this row's numbers point to: add a dimension penalizing `*_count`/`*_balance` qualifier patterns (`avg_`, `running_`, `closing_`, `opening_`, `ending_`) the same way `MEASURE_KEYWORDS` already special-cases aggregation per keyword -- out of scope for this row (SM-3 measures the score's calibration; it does not re-tune SM-4's formula), but the corpus and this report are what such a change would be evaluated against.

## Below the review gate

2 of 28 cases score below `MINIMUM_EVIDENCE_FOR_METRIC_REVIEW` (0.4) and so would never even reach a human reviewer in production (`ensure_reviewable` refuses them with 422 before any `GovernanceReview` row is constructed): unit_cost_false, weighted_avg_cost_false. 0 of those 2 are ground-truth correct -- the gate is not free (it also silently drops some genuinely valid proposals), which this small sample cannot generalize from, but is worth naming plainly rather than omitting because it complicates the headline numbers.

## Per-case detail

| Case | Column | Table role | Confidence | Reviewable | Ground truth | Expected agg. |
|---|---|---|---|---|---|---|
| unit_cost_false | `unit_cost` | DIMENSION | 0.3917 | no | incorrect | AVG |
| weighted_avg_cost_false | `weighted_avg_cost` | DIMENSION | 0.3917 | no | incorrect | AVG |
| item_count_false | `item_count` | FACT | 0.4583 | yes | incorrect | SUM |
| minimum_balance_false | `minimum_balance` | DIMENSION | 0.4833 | yes | incorrect | N/A |
| balance_limit_false | `balance_limit` | DIMENSION | 0.4833 | yes | incorrect | N/A |
| monthly_login_count_false | `monthly_login_count` | SNAPSHOT | 0.4958 | yes | incorrect | SUM |
| deposit_amount_true | `deposit_amount` | TRANSACTION | 0.5625 | yes | correct | SUM |
| od_fee_true | `od_fee` | EVENT | 0.6000 | yes | correct | SUM |
| qty_true | `qty` | FACT | 0.6000 | yes | correct | SUM |
| avg_daily_balance_false | `avg_daily_balance` | SNAPSHOT | 0.6000 | yes | incorrect | AVG |
| opening_balance_false | `opening_balance` | SNAPSHOT | 0.6000 | yes | incorrect | LAST |
| ending_balance_false | `ending_balance` | SNAPSHOT | 0.6000 | yes | incorrect | LAST |
| acct_balance_true | `acct_balance` | SNAPSHOT | 0.6917 | yes | correct | SUM |
| late_fee_true | `late_fee` | TRANSACTION | 0.6917 | yes | correct | SUM |
| total_deposits_true | `total_deposits` | FACT | 0.6917 | yes | correct | SUM |
| card_txn_volume_true | `card_txn_volume` | FACT | 0.6917 | yes | correct | SUM |
| running_balance_false | `running_balance` | TRANSACTION | 0.6917 | yes | incorrect | LAST |
| txn_count_false | `txn_count` | FACT | 0.7542 | yes | incorrect | SUM |
| daily_fraud_alert_count_false | `daily_fraud_alert_count` | SNAPSHOT | 0.7542 | yes | incorrect | SUM |
| daily_active_user_count_false | `daily_active_user_count` | SNAPSHOT | 0.7542 | yes | incorrect | N/A |
| closing_balance_false | `closing_balance` | SNAPSHOT | 0.7667 | yes | incorrect | LAST |
| txn_amount_true | `txn_amount` | TRANSACTION | 0.8583 | yes | correct | SUM |
| withdrawal_amount_true | `withdrawal_amount` | TRANSACTION | 0.8583 | yes | correct | SUM |
| loan_balance_true | `loan_balance` | SNAPSHOT | 0.8583 | yes | correct | SUM |
| processing_fee_true | `processing_fee` | EVENT | 0.8583 | yes | correct | SUM |
| interest_revenue_true | `interest_revenue` | TRANSACTION | 0.8583 | yes | correct | SUM |
| merchant_fee_true | `merchant_fee` | EVENT | 0.8583 | yes | correct | SUM |
| fraud_loss_amount_true | `fraud_loss_amount` | EVENT | 0.8583 | yes | correct | SUM |

