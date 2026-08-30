# ADR-0019 — Semantic Retrieval Without Assuming `pgvector`

**Status:** Accepted | **Date:** 2026-08-30 | **Owner:** Architecture

## Context

The retrieval design named `pgvector`. That was stated as a fact and it is not one.

The target PostgreSQL estate does not have the `vector` extension and is not expected
to get it. This is normal rather than unlucky: `CREATE EXTENSION` requires a privilege
a bank DBA will not grant a new platform, extensions expand the audited surface of a
shared database, and a database team that supports hundreds of schemas has a strong
institutional reason to say no. There is, separately, an approved in-network vector
service with its own URL.

An architecture that requires an extension the operator cannot install is not a
constraint to work around later — it is a design defect now.

## Decision

**Treat nearest-neighbour search as a port with adapters, and make the adapter that
needs nothing the default.**

`aida/vector_store.py` defines the port. Four backends, chosen by
`vector_index_backend`:

| Backend | When |
|---|---|
| **`postgres_bruteforce`** (default) | Always available. Vectors in an ordinary `bytea` column; exact cosine over a policy-narrowed candidate set |
| `external` | The bank's in-network vector service over HTTP |
| `pgvector` | Only where the extension genuinely exists — probed, not configured |
| `disabled` | Lexical only, reported honestly |

Embeddings live in `embedding` (`bytea` vector, stored norm, `index_signature`) in the
same PostgreSQL as everything else. That table is authoritative; any external index is a
projection rebuilt from it (INV-1), exactly as the graph projection is.

### Why exact search is viable, and where it stops

Nobody searches the whole estate. Retrieval filters by workspace binding and policy
**before** ranking — that ordering removes an information-leak class and is not
negotiable — so the candidate set reaching the scorer is what one principal may see.

Measured on PostgreSQL 16 against 200,000 stored 768-dimension embeddings, end to end
(fetch, unpack, score, top-25):

| Candidates | p50 |
|---:|---:|
| 200 | 45 ms |
| 1,000 | 100 ms |
| 5,000 | 427 ms |
| 20,000 | 1,697 ms |

So exact search is comfortable to roughly a thousand candidates and stops being
interactive well before ten thousand. The default candidate cap is 5,000 and the cap is
a **refusal with a reason code, not a truncation** — scoring an arbitrary slice of a
larger set returns plausible answers that are wrong, and nobody notices.

The consequence for the retrieval design: two stages, always. Lexical and policy
filtering narrow to order 1,000; exact cosine re-ranks. An approximate index earns its
place when candidate sets are *routinely* larger than that, which is a measurement to
take later rather than an assumption to make now.

### Embeddings are not anonymous

The tempting conclusion is that a vector is a safe numeric derivative, so shipping
embeddings outside PostgreSQL sits outside INV-6. It does not.
Embedding-inversion work recovers substantial portions of source text from vectors
alone, so a vector of a document chunk carries the sensitivity of that chunk.

Therefore:

- Only metadata and the customer's own uploaded documentation are embedded. Source
  business values are not, and no code path would allow it.
- An external index **must be inside the bank's network**. There is no hosted-vector-API
  mode, and adding one requires an ADR.
- The `embedding` table and any external index inherit the classification of what was
  embedded, and carry the same retention and deletion obligations as the control plane.

### Index identity

A vector is comparable only to vectors from the same model, model version, dimension
count and chunking. All four are pinned into `index_signature`, matched on every read,
and a mismatch is a rebuild trigger. This matters because the failure mode of mixing
vector spaces is not an error — it is quietly worse search results, which is the hardest
kind of defect to notice.

## Consequences

### Positive

- The platform has semantic retrieval on day one in an estate that will never install an
  extension, and does not need a second datastore to get it.
- Exact search has no recall to tune or measure, so there is one less thing to certify.
- Swapping to the bank's vector service, or to `pgvector` if it is ever approved, is a
  configuration change plus a rebuild, not a redesign.
- Capability is probed rather than declared, so the platform cannot advertise a backend
  it does not have (INV-9).

### Negative — costs accepted

- **Exact search does not scale to large candidate sets.** The measured ceiling is real:
  past a few thousand candidates this backend is the wrong tool, and the design leans on
  the pre-filter being effective. If a query pattern emerges that cannot be narrowed,
  the external backend is the answer and this is the trigger to switch.
- Scoring happens in Python. `numpy` would cut the scoring half of the cost by roughly
  two orders of magnitude and has deliberately not been added yet, because the current
  numbers are adequate inside the two-stage envelope and a dependency should be paid for
  by a measurement rather than a preference.
- The `pgvector` adapter is declared and not implemented. It refuses with
  `PGVECTOR_ADAPTER_NOT_IMPLEMENTED` rather than existing untested, because shipping an
  unexercised adapter for a database nobody here has is precisely the overstated
  capability INV-9 exists to prevent.
- Two storage paths (local table, external service) means two rebuild stories. Both are
  projections, so both rebuild from the same place, but the drill has to cover both.

## Reversal condition

Reverse toward a single external index if measured candidate sets after policy filtering
are routinely above a few thousand — that is, if the pre-filter turns out not to narrow
in practice. Reverse toward `pgvector` if the estate's database standard ever adopts it,
at which point the local table remains authoritative and the extension becomes an index
over it rather than a replacement for it.


## Amendment, 2026-08-30 — the embedding model is chosen

The owner's decision: **embeddings come from OpenAI or Gemini** — the same two providers the
generation path already supports. `src/aida/embedding_provider.py` implements both, and
`Settings.embedding_provider` selects between them.

This closes the one thing this ADR deliberately left open. The port decided *where* vectors
are searched and needed no extension to do it; nothing produced a vector, because
`index_signature` pins `(provider, model_id, model_version, dimensions, chunking_version)`
and choosing wrong means reindexing rather than degrading.

**Reusing the generation providers was the point, not a shortcut.** A third embedding vendor
would have meant a second credential path, a second retry policy and a second failure mode
for one capability. Instead the embedding credential resolves through the same reference
mechanism as every model credential, so it inherits the same rotation, the same registry and
the same production refusal of `env://`.

### The property that matters most

**A stand-in never silently becomes the real thing.** `vector_retrieval.HashEmbeddingProvider`
derives a vector from a SHA-256 digest. It is a good test double and a terrible model: a hash
has no semantic structure, so a "vector similarity" computed from one is noise carrying the
name of a signal.

The fused retrieval path built that provider **unconditionally** and fed its output into
ranking as the `vector` signal. Nothing looked wrong from outside — a complete-looking result,
ranked partly on noise. Two changes fix it:

* `resolve_embedding_provider` fails closed (INV-4). With no provider configured it raises
  `EmbeddingUnavailable` carrying a reason code, and returns no fallback.
* The vector stage is **skipped and the reason logged** when that happens, rather than
  substituted. A smaller answer beats a confidently wrong one, and reporting a capability you
  do not have is precisely what INV-9 forbids.

### Defaults and shape

| | OpenAI | Gemini |
|---|---|---|
| Default model | `text-embedding-3-small` | `gemini-embedding-001` |
| Endpoint | `POST /embeddings` | `POST /models/{model}:batchEmbedContents` |
| Width control | `dimensions` | `outputDimensionality` |
| Credential | `Authorization: Bearer` | `x-goog-api-key` **header** |

Both support dimension reduction, so `embedding_dimensions` is honoured rather than dictated.
The Gemini key goes in a header rather than a query parameter deliberately: a credential in a
URL ends up in access logs, proxies and browser history.

One batched call embeds the question and every candidate together — the providers bill and
rate-limit per request, and N+1 round trips inside a retrieval path spends a latency budget on
nothing.

### Three refusals worth naming

A response is rejected rather than accepted when it returns the wrong number of vectors, a
vector of the wrong width, or a shape that does not parse. Each would otherwise misalign
vectors with the texts they describe, or store something incomparable with what is already
indexed — and **neither failure is detectable downstream**. They surface as quietly bad search
months later, which is the worst way for this to go wrong.

### Still open

The model is chosen; the corpus is not embedded. Nothing yet writes vectors for the catalogue,
and the evaluation described in `review-2026-08/decisions/02-embedding-model.md` — 200–500 real
steward questions, recall@10 measured *after* policy filtering — has not been run. Choosing the
provider was the blocking decision; proving the choice is the next piece of work.
