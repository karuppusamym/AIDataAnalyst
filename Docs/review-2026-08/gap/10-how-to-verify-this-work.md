# How to verify this work without trusting it

Status: written 2026-08-30, in answer to "how can I trust your analysis?"

The right answer to that question is **don't** — check. Everything claimed in this
review is either a command you can run or a number you can reproduce. This document
lists both, and then lists what I got wrong, because a review that reports only its
successes is a sales document.

---

## 1. Claims you can check in under a minute

| Claim | Command |
|---|---|
| Every gate passes | `uv sync --frozen --extra dev && uv run ruff check . && uv run mypy src && uv run lint-imports && uv run pytest` |
| INV-2 is enforced, not asserted | `uv run lint-imports` — look for the contract named *"INV-2 connector SQL execution is reachable only from the query gateway"* |
| ...and that it actually bites | Add `from aida.connectors.execution_access import open_execution_session` to `src/aida/api.py`, re-run `lint-imports`. It must report `BROKEN`. Revert. |
| ...and that the type system catches the other route | Add `connector_registry.create("postgres", "x").execute_read_query("SELECT 1", timeout_seconds=1)` anywhere, run `uv run mypy src`. It must report `"Connector" has no attribute "execute_read_query"`. Revert. |
| Only one code path reaches a source | `grep -rn "execute_read_query" src/ --include=*.py \| grep -v connectors/` — one hit, in `query_gateway.py` |
| There is exactly one migration head | `uv run alembic heads` |
| `legal_entity` does not exist | `grep -rn "LegalEntity" src/` — no hits |
| Retrieval was lexical-only | `grep -rn "pgvector\|embedding" src/aida/retrieval.py` |

**The most important one is the third row.** A contract that has only ever passed is
not evidence of anything. Break it deliberately and watch it fail; that is the check
that distinguishes a working control from a decorative one.

---

## 2. Numbers you can reproduce

Every performance figure in this review came from PostgreSQL 16 with no `vector`
extension — deliberately, because that is the estate's configuration. None came from
estimation.

| Claim | How it was measured |
|---|---|
| Roll-up: 3,147 ms → 0.4 ms | 13,548-node taxonomy at depth 4, 5,000,000 assignments. Recursive CTE vs closure join vs materialised read |
| Authorization scope: 26 ms → 0.8 ms | Same dataset; two round trips collapsed into one query |
| Lineage: 12 hops in 10.8 ms | 12-layer column-level DAG, 40,000 columns/layer, real fan-in, 880,000 edges |
| Vector search: 45/100/427/1,697 ms | 200,000 stored 768-dimension embeddings at 200/1,000/5,000/20,000 candidates |
| Migration backfill correct | 6 LOBs, 24 domains incl. sub-domains, 24 projects, 48 datasources; 14 assertions on the result |

To re-run any of them: stand up a PostgreSQL 16, point `AIDA_DATABASE_URL` at it,
`alembic upgrade head`, and generate the same shapes. The benchmark scripts were
throwaway and deliberately not committed — a benchmark you cannot re-derive from its
description is a number you should not trust either, so the descriptions above carry
the dataset shape rather than a script to run blindly.

**Treat the lineage number with the most suspicion.** The first version of that
benchmark measured nothing: the synthetic graph collapsed to a linear chain with no
branching, so "12 hops" reached 12 nodes. It was rebuilt with real fan-in before any
conclusion was drawn. A benchmark that produces a convenient answer deserves a second
look at its generator.

---

## 3. What I got wrong

Found by deliberately attacking my own work after it had shipped inside a green suite
of 575 tests. All three are now regression tests in
`tests/test_regressions_from_adversarial_review.py`.

### 3.1 Fail-open tenant isolation — the serious one

`workspace_service.authorize` was written as:

```python
if context.organization_id is not None and workspace.organization_id != context.organization_id:
    return AuthorizationResult(allowed=False, reason_code="CROSS_ORGANIZATION_DENIED")
```

A caller claiming **no** organization skipped the check entirely. Development identity
makes `X-Organization-Id` optional, so `None` is reachable from outside. This is an
INV-5 violation, in the function whose entire job is INV-5, written by me, reviewed by
me, and passed by a test suite that included two tests specifically about cross-tenant
denial — both of which supplied an organization and so never exercised the branch.

Fixed: an absent tenant claim is now a denial (`NO_ORGANIZATION_CONTEXT`), and there is
deliberately no `PlatformAdmin` bypass, unlike the older `enforce_organization`.

### 3.2 An allowlist that matched on the wrong key

`PostgresBruteForceIndex.search` filtered candidates by `owner_id` alone. `owner_id` is
unique only *within* an `owner_type`, so an allowlist authorising `("TABLE", "x")` also
admitted `("COLUMN", "x")` — a different object, which the policy filter had not
authorised. Fixed by matching the full pair.

### 3.3 A constraint violation that only appears on one backend

Re-assigning the same target at the same instant set `effective_to == effective_from`
on the old row and then inserted a new row colliding on the unique key. The first fix
did not work, and the reason is worth recording: timestamps read back **aware** from
PostgreSQL and **naive** from SQLite, so `stored == supplied` was silently False on the
test backend and would have been True on the production one. Backend-dependent
comparison logic is the worst failure shape available, because no single test
environment reveals it. Fixed with explicit normalisation.

### 3.4 A test of mine that asserted nothing

```python
assert "tbl_1" not in rendered or decision.matched_policy_id is not None
```

The right-hand side is always true, so the assertion always passed. It was the test for
INV-6 — that a policy decision carries no resource values — which is to say the one
control I claimed was verified was the one I had not verified at all. Rewritten to
assert both halves separately, and the original left in a comment as a reminder that a
green test is not evidence until you have watched it fail.

### 3.5 A performance bug hiding behind a correct-looking fallback

`rollup` fell through to the expensive recompute whenever the materialised result was
empty — but "empty" is ambiguous between *nothing is assigned here* and *the projection
has not been built*, and the first is the common case early in an estate's life. So the
3-second query would have run constantly on exactly the nodes it was built to make
fast. Fixed with an existence probe.

---

## 4. What I have not verified, and you should not assume

- **Nothing is wired into the read and execution paths.** ABAC, the business graph and
  the vector index all work and all decide nothing in production traffic yet.
- **Hub-shaped lineage is unmeasured.** The graph benchmark used uniform fan-in 2. Real
  estates have hub columns feeding tens of thousands of downstream columns, and that is
  the likeliest place the "PostgreSQL, not Neo4j" decision hurts. The reversal threshold
  is written into ADR-0020.
- **No embedding model is configured.** `embedding_model_id` defaults to `unset` and
  nothing produces vectors.
- **CI has never run on a remote.** The recipe was verified in a clean local checkout,
  which is not the same as a green run on GitHub.
- **The competitor research is desk research.** Vendor documentation and release notes,
  not hands-on tenants. Where a vendor's own docs contradict their marketing I have said
  so, but I have not logged into any of these products. Screenshot coverage is 2 of ~45
  targets because the rest are blocked by browser site permissions.
- **Every operational drill except the migration rehearsal remains un-run.**

---

## 5. The standing rule

Where this review states a number, it was measured. Where it states a capability, there
is a command that proves it. Where it states an opinion — and §7 of the design brief,
which argues against a rebuild, is an opinion — it is labelled as one and carries its
reasoning so you can disagree with the reasoning rather than the conclusion.

If something here cannot be checked by one of the means above, treat it as unverified.
