# Quality benchmark results (AG-8)

Generated 2026-09-01T01:02:31.719031+00:00 by `scripts/quality_benchmark.py`. Reproduce with `uv run python scripts/quality_benchmark.py` (requires `AIDA_ENVIRONMENT` set, e.g. `development`). Every number below comes from a real run of the live retrieval/planning code against the deterministic seeded catalog in that script's `seed_catalog` -- not hand-typed.

Scope: this is the quality/accuracy counterpart to PF-3's latency ratchet (`Docs/90-reference/perf-baseline.json`), not the bank-scale 1M-object benchmark tracked separately as RT-8/PF-1, which this sandbox has no infrastructure for.

## Retrieval quality

`GovernedRetriever.retrieve` (-> `hybrid_retrieve_enhanced`) over `tests/fixtures/quality_benchmark_corpus/retrieval_quality_corpus.json` (12 cases).

| Metric | Value | Baseline | Change |
|---|---|---|---|
| `retrieval_hit_at_1_rate` | 0.8333 | 0.8333 | +0.00 pts |
| `retrieval_recall_within_bound_rate` | 1.0000 | 1.0000 | +0.00 pts |
| `retrieval_mrr` | 0.9028 | 0.9028 | -0.00 pts |

| Case | Question | Expected | Rank | Hit@1 | Within bound |
|---|---|---|---|---|---|
| orders-lexical-top1 | show me customer orders | TABLE:fact_orders | 1 | yes | yes |
| customer-lookup-tool-outranks | who is the customer | TABLE:dim_customer | 2 | no | yes |
| product-catalog-top1 | product catalog details | TABLE:dim_product | 1 | yes | yes |
| payments-top1 | payment transactions history | TABLE:fact_payments | 1 | yes | yes |
| branch-top1 | bank branch information | TABLE:dim_branch | 1 | yes | yes |
| loan-application-top1 | loan application status | TABLE:fact_loan_applications | 1 | yes | yes |
| employee-roster-top1 | employee roster | TABLE:dim_employee | 1 | yes | yes |
| account-balance-top1 | account balance snapshot | TABLE:fact_account_balances | 1 | yes | yes |
| merchant-top1 | merchant details for card transactions | TABLE:dim_merchant | 1 | yes | yes |
| fraud-alerts-top1 | fraud alert events | TABLE:fact_fraud_alerts | 1 | yes | yes |
| orders-related-customer-recall | orders and their related customer information | TABLE:dim_customer | 3 | no | yes |
| governed-tool-top1 | customer account summary | GOVERNED_TOOL:customer-account-summary | 1 | yes | yes |

Vector-similarity signal: skipped this run — `EMBEDDING_PROVIDER_NOT_CONFIGURED`. The numbers above are the real fused result of lexical + graph + fusion with the vector signal absent, not a partial run presented as complete.

## Tool / generation-path selection quality

`GovernedPlanner.plan` over `tests/fixtures/quality_benchmark_corpus/tool_selection_corpus.json` (5 cases) -- no live model route needed, since tool-first selection is a PLANNED-state decision upstream of GENERATED.

| Metric | Value | Baseline | Change |
|---|---|---|---|
| `tool_selection_pass_rate` | 1.0000 | 1.0000 | +0.00 pts |

| Case | Question | Roles | Expected strategy | Actual strategy | Passed |
|---|---|---|---|---|---|
| approved-tool-analyst-selected | customer account summary | Analyst | GOVERNED_TOOL | GOVERNED_TOOL | yes |
| approved-tool-role-denied-falls-to-development-sql | customer account summary | Viewer | DEVELOPMENT_SQL | DEVELOPMENT_SQL | yes |
| approved-tool-role-denied-requires-generation | customer account summary | Viewer | MODEL_GENERATION | MODEL_GENERATION | yes |
| no-eligible-tool-falls-to-development-sql | fraud alert events | Analyst | DEVELOPMENT_SQL | DEVELOPMENT_SQL | yes |
| no-eligible-tool-requires-generation | fraud alert events | Analyst | MODEL_GENERATION | MODEL_GENERATION | yes |

## Model generation quality (framework only in this sandbox)

| Activation prerequisite | Status |
|---|---|
| `model_generation_enabled` | False |
| `model_route` configured | False |
| OpenAI credential present | False |
| Gemini credential present | False |
| **Activatable in this environment** | **False** |

No usable model route in this sandbox: `model_generation_enabled` is False and neither `OPENAI_API_KEY` nor `GEMINI_API_KEY` is configured. This section is honestly framework-only — the harness above (posture check + the real, model-free tool/generation-path selection benchmark) is real and running; actual generated-text quality numbers require a configured, approved model route and are not fabricated here.

