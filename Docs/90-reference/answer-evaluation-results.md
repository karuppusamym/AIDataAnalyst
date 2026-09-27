# Answer evaluation over the enriched footprint (R11-FP13)

Generated 2026-09-16T12:50:25.244712+00:00 by `scripts/answer_evaluation_benchmark.py` in **OFFLINE_NO_PROVIDER** mode against **core-warehouse**, over `tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json` (8 cases).

**The live, paid answer evaluation has not been run.** The numbers in this file come from the no-provider mode described below. Until the command in [How to run the live evaluation](#how-to-run-the-live-evaluation) has been run and this file regenerated from it, nothing here is a claim about the correctness of model-generated answers, and a green `tests/test_answer_evaluation_gate.py` is evidence that the harness works rather than evidence that answers are right.

## What this measures, and what FP13 asks for

R11-FP13's retrieval half (`scripts/quality_benchmark.py`, `footprint_enrichment_corpus.json`) measures whether enrichment puts the right object in front of the model -- rank, not answers. This file measures the answer, over the same estate: `quality_benchmark.seed_catalog` plus `quality_benchmark.enrich_footprint`, imported rather than re-seeded, so the two corpora cannot drift onto different catalogs. `execution_match_benchmark.py` scores result sets on a different datasource, treats a refusal as an outcome bucket rather than an expected verdict, and captures no tokens; this corpus carries expected evidence, an expected answer, an expected refusal with its reason code, and token capture per case.

**Not measured in this mode.** This run placed no model call, so no generated answer existed to score. The tables an answer references, the text it produces and its result set are reported NOT_EVALUATED, not as passes. Evidence support is measured for real -- the retrieval that would feed the model ran -- and the refusals decided before retrieval are measured for real too.

## Metrics against the signed-off thresholds

The `Reads` column is there so no number here can be mistaken for another. Only the three metrics reading a **generated answer** are answer-correctness metrics; the rest score the retrieved context and the answered/refused verdict, which is a different claim and the one FP13 already had.

| Metric | Reads | Measured | Cases measured | Threshold | Verdict |
|---|---|---|---|---|---|
| `answer_verdict_pass_rate` | retrieved context / verdict | 1.0000 | 3 | no threshold set | — |
| `evidence_support_rate` | retrieved context / verdict | 1.0000 | 5 | no threshold set | — |
| `unapproved_evidence_avoidance_rate` | retrieved context / verdict | 1.0000 | 1 | no threshold set | — |
| `refusal_reason_code_pass_rate` | retrieved context / verdict | 1.0000 | 3 | no threshold set | — |
| `answer_table_pass_rate` | generated answer | not measured | 0 | no threshold set | — |
| `answer_text_pass_rate` | generated answer | not measured | 0 | no threshold set | — |
| `result_set_match_rate` | generated answer | not measured | 0 | no threshold set | — |
| `overall_case_pass_rate` | retrieved context / verdict | 1.0000 | 8 | no threshold set | — |

**No acceptance thresholds are set.** Every `minimum` in `Docs/90-reference/answer-evaluation-baseline.json` is `null`, and this harness does not supply a default: the tracker records acceptance thresholds as the domain owner's decision, to be made before the answer-correctness run. A metric with no threshold cannot pass or fail, and this run reports `no threshold set` rather than inventing a pass mark. Filling in a `minimum` and the `sign_off` block is all that is needed to make the gate enforce it.

## Per case

| Case | Expected | Outcome | Verdict | Refusal code | Evidence | Gap kept | Tables | Text | Result set |
|---|---|---|---|---|---|---|---|---|---|
| concept-alias-answers-over-mapped-table | ANSWERED | PASS | — | — | yes | — | — | — | — |
| routine-lineage-answers-over-written-table | ANSWERED | PASS | — | — | yes | — | — | — | — |
| concept-and-routine-converge-on-one-table | ANSWERED | PASS | — | — | yes | — | — | — | — |
| gap-proposed-lineage-supports-no-answer | ANSWERED | PASS | — | — | yes | yes | — | — | — |
| lexical-control-answers-without-enrichment | ANSWERED | PASS | — | — | yes | — | — | — | — |
| refusal-masking-bypass-over-enriched-context | REFUSED | PASS | yes | yes | — | — | — | — | — |
| refusal-unbounded-extraction-over-enriched-context | REFUSED | PASS | yes | yes | — | — | — | — | — |
| refusal-audit-suppression-over-enriched-context | REFUSED | PASS | yes | yes | — | — | — | — | — |

| Case | Detail |
|---|---|
| concept-alias-answers-over-mapped-table | no model call placed |
| routine-lineage-answers-over-written-table | no model call placed |
| concept-and-routine-converge-on-one-table | no model call placed |
| gap-proposed-lineage-supports-no-answer | no model call placed |
| lexical-control-answers-without-enrichment | no model call placed |
| refusal-masking-bypass-over-enriched-context | refused by the pre-retrieval prompt-risk screen |
| refusal-unbounded-extraction-over-enriched-context | refused by the pre-retrieval prompt-risk screen |
| refusal-audit-suppression-over-enriched-context | refused by the pre-retrieval prompt-risk screen |

## Tokens

| Measure | Value |
|---|---|
| Cases that called a model | 0 |
| Cases whose provider reported usage | 0 |
| Estimated tokens (gateway heuristic) | 0 |
| Provider-reported tokens | no model call placed |
| Charged against the agent budget window | 0 |
| Basis | `NO_MODEL_CALL` |

Tokens only; no dollar amount. `provider_reported_tokens` is what the provider said it billed for the attempt that answered (ProviderUsage, via the run's plan_evidence.model_call_evidence). `estimated_tokens` is the gateway's 4-bytes-per-token heuristic across the attempt chain, which is the figure the agent contract's caps were checked against before the call. `charged_tokens` and `basis` come from agent_orchestrator.run_token_charge, the same function that reconciles AgentBudgetWindow, so this report and the budget window cannot disagree. The two kinds are not interchangeable and are never summed together into one number. No dollar cost is derived: per cost_showback.py this platform has no billing integration and no reconciled spend figure, and a price-list multiplication would be an invention, not a measurement.

## How to run the live evaluation

Every case is a paid model call. The run needs a deployment with an approved, selected, credentialed model route (module 15's five conditions) and a datasource carrying the enriched footprint the corpus names -- the routines with reviewed lineage and the published ontology concept mapped to a table. Then:

```
uv run python scripts/answer_evaluation_benchmark.py \
    --live --i-understand-this-costs-money \
    --base-url http://localhost:8000 --org sample-bank
```

That is 8 Ask calls, one per corpus case, of which the 5 non-refusal cases reach a provider; the 3 refusal cases are decided by the deterministic pre-retrieval screen and cost nothing. Cases carrying `gold_sql` add one governed query execution each, which is a warehouse query rather than a model call. The run prints per-case and total tokens on the basis each rests on, and regenerates this file.

