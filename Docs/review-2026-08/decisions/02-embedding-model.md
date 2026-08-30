# Embedding model — decision brief

Status: **Input for a decision, not a decision.** Written 2026-08-30.

This one is not mine to make. Which embedding model runs, and where, is a model-risk and
procurement question with the same governance weight as any other model route (ADR-0009):
it needs an approved route version, a residency answer, a retention answer and a named
owner. What follows is the input that decision needs, including the parts that are
irreversible.

## Why it is blocking

`vector_index_backend` defaults to `postgres_bruteforce` and works today, but
`embedding_model_id` defaults to `unset` and **nothing produces vectors**. Until a model is
chosen, retrieval stays lexical-only, which means the wiki search, document mapping,
semantic asset search and agent context retrieval described in the target design all remain
unbuilt regardless of the storage work being finished.

## The part that is hard to reverse

`index_signature` pins `(model_id, model_version, dimensions, chunking_version)`. Vectors
carrying different signatures are not comparable, and the system refuses to compare them —
deliberately, because mixing vector spaces does not raise an error, it quietly returns worse
results.

The consequence: **changing the model means re-embedding everything.** At the target estate
(1M tables, plus wiki blocks and document sections) that is a bulk job measured in hours of
model throughput and whatever the model costs per million tokens. Not catastrophic, but not
a decision to revisit quarterly. Choose once, deliberately.

## What the platform actually requires

| Requirement | Why | Consequence if unmet |
|---|---|---|
| **Runs inside the bank's network** | INV-6 and the embedding-inversion argument in ADR-0019: a vector of a document chunk carries the sensitivity of that chunk, so embedding text through an external API is a data egress, not a computation | Rules out hosted embedding APIs entirely, however convenient |
| **Deterministic for a given input and version** | The index is a projection that must be rebuildable to the same state (INV-1) | A model whose output drifts silently makes rebuild non-idempotent, and no drill can verify it |
| **Fixed, documented dimensionality** | It is pinned into the signature and into the `embedding.dimensions` column | A model that changes dimensions between minor versions forces a full re-embed on a patch release |
| **Version pinnable and archivable** | Decisions must be replayable a year later (the same reason semantic models and policies are versioned) | "Latest" as a version is not a version |
| **Throughput adequate for a bulk backfill** | The first run embeds the whole estate | An under-provisioned model turns onboarding into a multi-week job |
| **Handles short, name-like strings well** | Most of what gets embedded is `rtl_cust_mstr.acct_open_dt`, not prose | A model tuned only for paragraphs performs poorly on the dominant input shape |

That last row is the one most often missed. The corpus here is overwhelmingly identifiers
and short descriptions, not documents — so general benchmark performance on long-form
retrieval is weakly predictive of performance on this workload.

## What should be evaluated, and how

Do not choose on a public leaderboard. Build the evaluation set from this estate:

1. Take 200–500 real questions a steward or analyst would ask ("where is the customer's
   address held", "which table has the daily balance").
2. Have a steward mark the correct assets for each. This is the expensive step and there is
   no way around it.
3. Measure **recall@10 after policy filtering**, because that is the number the product
   depends on — the ranker only ever sees what a principal may see.
4. Re-measure the same set whenever the model or chunking changes. This is the benchmark
   suite that makes the model choice reversible-with-evidence rather than by opinion.

The same corpus doubles as the agent evaluation benchmark (N17), so the cost is shared.

## Open questions for whoever owns this

1. **Which model, and who approves it?** It needs a model-route version under ADR-0009 like
   any other, with residency, retention and budget approved. Route approval is not
   activation (that ADR's central point) and the same applies here.
2. **Where does it run?** An in-network inference service, a sidecar on the platform's own
   Kubernetes, or the bank's existing model-serving estate. This is an infrastructure
   decision with a latency and capacity consequence for the ingest path.
3. **What chunking?** `embedding_chunking_version` is pinned. Table and column metadata is
   short enough to embed whole; document sections need a chunk size and overlap decided.
4. **Is one model enough?** Short identifiers and long document prose have different optimal
   models. Two signatures can coexist in the same table by design — the question is whether
   the added operational complexity buys enough recall to be worth it. Default answer: one
   model until measurement says otherwise.
5. **Who owns re-embedding?** It is a scheduled bulk job with a cost. It needs an owner and
   a runbook before it is needed urgently, not after.

## Recommendation on process, not on product

Pick the model that satisfies the in-network requirement with the fewest moving parts, pin
it, and spend the effort on the evaluation set rather than on model selection. The evidence
from the market research is consistent on this: **accuracy in this category is a curation
problem, not a model-choice problem** — Alation's published case moved a SQL agent from 60%
to 100% on metadata corrections alone, with no model change. The evaluation corpus will
outlive whichever model is chosen first.
