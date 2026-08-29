# Round 6: Neo4j Graph-Summary Reconciliation Closed

This was the last substantive item from the original audit — `get_graph_summary` in `api.py`, the endpoint that reconciles the Neo4j metadata-graph projection against the authoritative Postgres counts. It had zero test coverage of any kind before this round.

## Why it needed a different approach than every other gap this session

Every other test written this session called a handler against a fake SQLAlchemy session — this endpoint additionally opens a real `neo4j.AsyncGraphDatabase` driver and runs a Cypher query. To test it without a live Neo4j instance, I faked the driver itself: `monkeypatch.setattr(api_module, "AsyncGraphDatabase", ...)`, using the same `monkeypatch` pattern already established elsewhere in this suite (`test_high_stakes_behaviors.py`, `test_operational_behaviors.py`) — no new testing infrastructure, just applied to a new target.

## What the five new tests in `tests/test_graph_summary.py` actually prove

- **CURRENT**: when the graph's projected counts match Postgres exactly, `projection_status` is `CURRENT` and every lag value is zero.
- **LAGGING**: when the graph is behind on one dimension (e.g., 2 tables not yet ingested), only that dimension shows nonzero lag and the overall status flips to `LAGGING`.
- **NOT_PROJECTED**: when the graph has no catalog node at all, the status is `NOT_PROJECTED` — and this takes priority over the lag computation even when Postgres has real data, since an unprojected graph is a distinct state from a merely-lagging one.
- **Lag clamps to zero, never negative**: when the graph has *more* rows than Postgres for a dimension (e.g., a stale table Postgres already deleted but the graph hasn't caught up on tombstoning), the lag for that dimension is 0, not negative — and this alone doesn't count as "lagging." This is a real edge case in the `max(authoritative - projected, 0)` formula that was previously unverified.
- **Unreachable graph**: when the Neo4j query raises, the endpoint returns 503 rather than propagating the exception — and, separately, the driver's `close()` is confirmed to run even on that failure path (the `finally` block), so a broken Neo4j connection can't leak driver handles.

No source changes were needed — the reconciliation logic was already a plain function; it just needed the driver faked.

## Verification (fresh clean-room build, current device state)

Bundled the repo exactly as it sits on your machine right now into a brand-new `python3.13` venv from scratch:

| Check | Result |
|---|---|
| `pytest` | **284 passed, 0 failed** (279 → 284, +5 new tests) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## Files touched this round

- `tests/test_graph_summary.py` (new) — 5 tests

## Final tally against the original 25-claim audit

Every thread identified in the original proof-gap audit — including the one I'd mis-scoped as "ranking" in an earlier report and had to correct — now has real behavioral test coverage: catalog tombstoning + reactivation, the full glossary module (GL-1/2/3/5/8), Temporal heartbeat/resume, HMAC audit evidence, MCP anti-enumeration, audit dead-letter/requeue, impact analysis, fleet scheduling (all six sub-claims), governed-tool version lifecycle, retrieval ranking, and now the Neo4j graph-summary reconciliation.

The only thing left unaddressed from the whole engagement is the Phase-0-before-Phase-A sequencing question — that was offered as an alternative track at the very start of this "close the proof gaps" work and you chose the test-gap track instead, so it was never touched. I'd treat the gap-closing work as essentially done at this point; that sequencing question is the one open thread if you want to pick it up next.

Across six rounds this session: 6 tests → 284 tests, 6 small behavior-preserving extractions in production code (all to make previously-untestable logic directly callable, no behavior changes), 8 new/extended test files, and every round independently verified from a fresh clean-room build. Nothing has been committed at any point — say the word when you want this committed, and whether as one commit or split by round/module.
