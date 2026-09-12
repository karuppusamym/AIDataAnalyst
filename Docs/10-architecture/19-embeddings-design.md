# Embeddings: what exists, what it can and cannot do, and what to build next

> Written 2026-09-12, after configuring a real embedding provider for the first
> time and measuring the result. A dated snapshot; status lives in
> [tracker section P](../60-delivery/03-tracker.md).

## 1. Nothing was removed. One dead table was.

Two differently-named things existed, and only one of them was ever wired:

| | `vector_embedding` (**dropped**, migration `c93a5f1d47e8`) | `embedding` (**live**) |
|---|---|---|
| Readers / writers in any revision | none | the retrieval path and the index |
| Chunk dimension | absent — one row per object, so a long document could not be split | present |
| Vector storage | JSON floats | packed, with a norm |
| Index signature | absent — vectors from two different models were indistinguishable | `(model_id, model_version, dimensions, chunking_version)` |

`vector_embedding` could not have served document embeddings even if something
had written to it: without a chunk dimension there is nowhere to put chunk two
of a PDF, and without a signature a re-embedding under a new model silently
mixes incomparable vectors. It was removed as dead configuration, and the
capability was never in it.

The live stack is complete and, as of 2026-09-12, running:

- [`src/aida/embedding_provider.py`](../../src/aida/embedding_provider.py) —
  OpenAI and Gemini, with the credential arriving as a reference and never as a
  value. `unset` is treated as an unmade decision and **refuses**, rather than
  falling back to a hash double: a cosine score computed from a SHA-256 digest
  is noise wearing the name of a signal.
- [`src/aida/vector_store.py`](../../src/aida/vector_store.py) and
  [`src/aida/vector_index_service.py`](../../src/aida/vector_index_service.py) —
  the persisted index, its freshness rule and its rebuild.
- [`src/aida/retrieval_stages.py`](../../src/aida/retrieval_stages.py) — the
  vector channel inside the hybrid retrieval pipeline.

## 2. The one thing to understand: embeddings here **re-rank**, they do not discover

The vector channel scores *the candidate set policy already authorized*. It
cannot introduce a candidate no earlier channel found, because the candidate
list handed to the index **is** the authorized list. That is a deliberate
safety property — the index can only reorder what the caller was already
entitled to — and it has a direct consequence that configuration alone cannot
change:

> A table that lexical matching never surfaced can never be reached by
> similarity, however good the embeddings are.

This is measured, not inferred. Five cases were added to the retrieval corpus
whose wording shares no word stem with the target table's name or description
("what funds does each depositor have at close of business" against
`fact_account_balances`). **All five miss.** Before those cases existed, every
case in the corpus was solvable lexically, so the corpus could not fail on the
signal it was fusing, and the vector channel's numbers were fusion over hits
the lexical stage had already found.

So "we have embeddings now" and "we have semantic search now" are different
claims, and only the first is true today.

### What semantic discovery would take

Invert the order for one stage: search the index across the datasource's
catalog **first**, then apply the same policy filter to the results. Output
safety is unchanged — nothing the caller may not see is returned, because the
filter still runs before anything leaves the pipeline. What changes is that the
search *touches* vectors belonging to objects the caller cannot see, which
leaves a timing signal rather than a disclosure. That is a real but much weaker
exposure than returning the object, and it is the trade every vector database
with row-level security makes.

This is a decision to take deliberately, not a refactor to slip in, because the
current order is load-bearing for how this platform explains itself. It belongs
to [R11-S3](../60-delivery/03-tracker.md) (simplify retrieval) with a named
owner, and it should be an **additional** stage rather than a change to the
existing one, so the safe ordering remains available per query.

## 3. The gap that will bite a deployment first

`rebuild_vector_index` is reachable only from
`POST /v1/organizations/{organization_id}/retrieval/vector-index/rebuild`.
**Nothing schedules it**, and that endpoint is in the cluster R11-X5 records as
missing its UI. So on a real estate:

1. the index is built only if an operator knows to call an endpoint with no
   screen;
2. the estate then changes, the index goes stale, and the freshness check
   correctly stops trusting it;
3. the vector channel falls back to embedding **every candidate on every
   query** — a provider call per candidate per query, which is precisely the
   cost the persisted index exists to remove.

Nothing is wrong at that point, and nothing says anything either: retrieval
still returns good answers, at a bill that grows with the estate and the
traffic at the same time.

**Closed 2026-09-12.** `vector_index_service.run_vector_index_rebuild_pass`
now runs from `run_scheduler_iteration`, on the same cadence shape R11-D11 used
for the classification roll-up: daily by default, bounded per sweep, per-tenant
fault isolation, and a no-op when no embedding provider is configured — which
is the shipped state, so an unconfigured deployment must do nothing rather than
log an exception every tick. The skip stamps the clock as well, so it asks once
per interval instead of resolving a provider it does not have on every tick.

## 4. The four use cases that were asked about

### a. Generated and refined queries — the highest-value one, already half-built

[`QueryMemoryEvidence`](../../src/aida/models.py) already stores
`question_hash` and `sql_hash`: keyed HMAC fingerprints, no text. A hash matches
an **identical** question and nothing else, so "show me last quarter's
defaults" cannot reuse the SQL a human already approved for "defaults in Q4".
Embedding the question is exactly what closes that, and the reuse is of
*human-approved* SQL, which makes it a governance win rather than a shortcut
around one.

The obstacle is real and must be handled rather than waved at: the question is
user-authored text, and an embedding is a lossy but **partially invertible**
projection of it. Storing one is therefore closer to storing the question than
to storing a hash of it, and ADR-0014 and INV-6 apply. The controls that make
it acceptable:

- classify the vector as derived-sensitive, at the classification of the
  question's own context — not as metadata;
- scope it to the workspace, never the organization, and never across tenants;
- match its retention to the question's retention, so a deletion request
  reaches it;
- store the vector and the fingerprint, never the text — the text is already
  not stored and must stay that way.

**Recommendation: build it**, scoped to reuse of approved SQL, with those four
controls stated in the model's own docstring.

### b. Conversations — defer, and not for embedding reasons

There is no conversation model in this platform. Embedding conversations needs
a conversation store first, which is a new bounded context, its own retention
policy and its own governance surface. The embedding part is the easy half. The
sharper problem is that a conversation accumulates far more user text than a
single question, so every control in (a) gets harder, not easier. This is a
product decision with an ADR attached to it, not an embedding feature.

### c. Uploaded PDF and text — the best next investment, because it fits

[`src/aida/document_ingestion.py`](../../src/aida/document_ingestion.py) handles
exactly one document shape today: a CSV data dictionary of
schema/table/column/description rows, recognised and mapped directly rather
than chunked as prose. Its own docstring names general PDF, DOCX and XLSX
extraction, and semantic mapping, as separate builds.

Embeddings help the **mapping** step — matching a described column in a
document to a column in the catalog — and mapping is a *re-ranking* problem
over a candidate set the catalog already bounds. So this use case fits the
architecture as it stands, with no inversion of the policy order and no new
exposure: both sides of the comparison are metadata the caller may already see.
That makes it the cheapest genuine win of the four.

Extraction itself (getting text out of a PDF with its structure intact) is
ordinary engineering and unrelated to embeddings; it is the prerequisite.

### d. "Per model" chunking and re-embedding — already designed correctly

The index signature pins `(model_id, model_version, dimensions,
chunking_version)`, and the freshness check refuses an index built under a
different signature. So changing the embedding model **invalidates** the index
rather than silently mixing incomparable vectors, which is the failure mode
that makes vector search quietly bad rather than loudly broken. Nothing needs
designing here; it needs the scheduled rebuild from §3 so the invalidation is
followed by a rebuild instead of an indefinite fallback.

## 5. Order of work

1. **Schedule the index rebuild** (§3). Smallest change, prevents a silent
   cost blow-up, and makes the persisted path the one that actually serves.
2. **Document-to-catalog mapping by similarity** (§4c). Fits the current
   architecture; no new exposure.
3. **Decide on search-then-filter discovery** (§2). Needs an owner and an ADR,
   because it changes what the index is for.
4. **Semantic reuse of approved SQL** (§4a). Highest value, and the one with
   governance controls that have to be designed rather than assumed.

Conversations (§4b) sit behind a product decision that has not been taken.
