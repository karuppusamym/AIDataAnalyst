# Round 5: Governed-Tool Lifecycle + Retrieval Ranking Closed

Both remaining threads from the original audit are now closed with real behavioral tests.

## Governed-tool version lifecycle

The publish/reject/deprecate lifecycle lives in one shared maker-checker endpoint, `decide_governance_review` (`semantic_api.py`) — before this round, nothing in the suite called it at all, for any object type. The only prior evidence was that the request-side routes in `tool_api.py` (submit-for-review, submit-deprecation) existed.

Five new tests in `tests/test_governed_tool_lifecycle.py` prove the actual state machine:

- Approving a publish request promotes the version to `PUBLISHED` and — this is the part that most needed proving — issues a real `UPDATE` that supersedes the tool's prior published version. I captured the actual SQLAlchemy statement the code executes and checked its compiled SQL (`SET status='SUPERSEDED' ... WHERE ... status = 'PUBLISHED' AND id != <this version>`), not just the end state, so the test would fail if that supersede logic were ever accidentally dropped.
- Rejecting a publish request marks the version `REJECTED` and confirms *no* supersede statement is issued — a rejection must never touch other versions.
- Approving a deprecation request on a currently-published version moves it to `DEPRECATED` (and, again, confirms this path never runs the supersede update — deprecation and publish are independent branches).
- A deprecation request against a version that's no longer published (e.g., already `DRAFT`) is rejected with 409, proving the guard is live.
- A reviewer can't decide a review they themselves requested — the maker-checker separation check — tested directly against this object type rather than assumed from the generic pattern.

## Retrieval ranking

`aida.retrieval.hybrid_retrieve` is the BM25 + weighted-boost engine that ranks tables, columns, governed tools, business annotations, and dbt resources for the agent orchestrator. It had zero direct coverage — the only retrieval-adjacent tests were hand-built `RetrievalHit` fixtures for unrelated planner tests, which never touched the scoring or ranking code itself.

Eight new tests in `tests/test_retrieval_ranking.py`:

- Five test the pure scoring functions directly (`_tokenise`, `_idf_weight`, `_bm25_score`, `_exact_phrase_bonus`) — stop-word stripping, snake_case/camelCase splitting, the IDF length-based weighting tiers, the matched-token-fraction formula, and the exact-phrase substring bonus.
- Three exercise `hybrid_retrieve` end-to-end against a fake session: a governed tool and a table with *identical* BM25 relevance are ranked with the tool first, isolating the tool-priority boost as the actual cause (I hand-computed the expected scores — 0.5 vs 0.25 — and the test asserts the exact values, not just the ordering). A caller's `preferred_tool_version_id` is shown to outrank an otherwise-identical tool version by exactly the documented +0.35. And the result list is shown to actually sort by score and truncate at `settings.agent_retrieval_limit`, not just at whatever order the DB returned rows in.

No source changes were needed for either gap — both were already directly testable; they just needed fake sessions built to their specific call sequences (governance-review: two `get()`s plus a captured `execute()`; retrieval: three `scalars()` and two `execute()` calls in a fixed order).

## Verification (fresh clean-room build, current device state)

Bundled the repo exactly as it sits on your machine right now into a brand-new `python3.13` venv from scratch:

| Check | Result |
|---|---|
| `pytest` | **279 passed, 0 failed** (266 → 279, +13 new tests) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## Files touched this round

- `tests/test_governed_tool_lifecycle.py` (new) — 5 tests
- `tests/test_retrieval_ranking.py` (new) — 8 tests

No production code changed — both gaps were closeable with tests alone.

## Final tally against the original 25-claim audit

Closed with real behavioral tests: catalog tombstoning + reactivation, glossary coverage scoring and the full glossary module (GL-1/2/3/5/8), Temporal heartbeat/resume, HMAC audit evidence, MCP anti-enumeration, audit dead-letter/requeue, impact analysis, fleet scheduling (all six sub-claims), governed-tool version lifecycle, and retrieval ranking.

Still open: the live-Neo4j `get_graph_summary` reconciliation endpoint (see correction below — needs driver mocking, a different test pattern than everything else closed this session). Also still standing from the original strategic read: the Phase-0-before-Phase-A sequencing question — that was offered as an alternative track early on and you chose to keep closing test gaps instead, so I never touched it.

One correction to my own earlier tracking: I'd been calling the remaining Neo4j thread "graph traversal/ranking," but on actually reading the code, the only live-Neo4j endpoint is `get_graph_summary` in `api.py` — it opens a real `AsyncGraphDatabase` driver and runs a Cypher query to reconcile Neo4j's projected catalog/schema/table/column counts against the authoritative Postgres counts. It's not a ranking function at all, and unlike everything closed so far, it can't be tested by calling the handler against a fake SQLAlchemy session — the driver construction and `execute_query` call would need to be mocked too, which is a different (and heavier) kind of test than the pattern used everywhere else in this session. Flagging it honestly as still open, rather than either rushing a shallow driver-mock test or quietly dropping it from the tally.

Nothing has been committed — nine files across five rounds (the five source extractions plus five new/extended test files) are sitting uncommitted on top of your last commit. Say the word when you want this committed, and let me know if you'd rather I stage it as one commit or split it by round/module.
