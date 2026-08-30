# Four More Proof Gaps Closed

Picked up where the reactivation fix left off. These are the four gaps you selected: HMAC evidence, MCP anti-enumeration, audit dead-letter/requeue, and the impact-analysis endpoint.

Also resolved along the way: confirmed you'd already committed the earlier round of work (`e011742`, "Refactor UI assets and expand behavior coverage", on `feature/snowflake-dbt-lineage-mcp`) — that's what "checked in" referred to. The stale `.git/index.lock` I flagged earlier only affects git commands run from *this* sandbox (it can't unlink the file due to a permission restriction); it never touched your real git on Windows, which is why the commit went through cleanly. As instructed, I left git alone this round — everything below is uncommitted on top of your commit.

## 1. HMAC evidence (Query Execution Gateway)

**Gap:** the only related test checked that a short `audit_hmac_key` is rejected at startup — nothing verified the HMAC digest itself is computed correctly or is actually tamper-evident.

**Fix:** extracted the inline `hmac.new(...)` call in `QueryExecutionGateway.execute()` into a named, testable function, `audit_sql_hash(key, sql)`, in `src/aida/query_gateway.py`. Same computation, now callable without a database.

**Test** (`test_high_stakes_behaviors.py`): `test_audit_sql_hash_is_deterministic_key_bound_and_tamper_evident` — proves three properties a real evidence trail needs: same input always produces the same digest (so a stored hash can be re-verified later), a different key produces a different digest (a caller without the server's key can't forge one), and altering the SQL after the fact changes the digest (tamper-evidence).

## 2. MCP anti-enumeration (CX-5)

**Gap:** `_tool_role_eligible` (the boolean check) was well tested, but nothing exercised `tools/call` end-to-end to confirm a nonexistent tool and a role-denied tool actually produce the *identical* response — the specific "no existence leak" claim.

**Test** (`test_mcp_server.py`, no source change needed — the logic was already correct):
- `test_tools_call_reports_identical_response_for_unknown_and_denied_tool_names` — calls `_handle_tools_call` twice with the same tool name: once where the tool doesn't exist, once where it exists but the caller's role isn't bound to it. Asserts the two response envelopes are **byte-for-byte identical** (`unknown_result == denied_result`). Separately confirms the denial *is* still recorded server-side as audit evidence, unlike the true-unknown case — so operators can still see it happened even though the caller can't.
- `test_tools_call_reaches_datasource_resolution_when_role_is_eligible` — a control test proving the collapsed response isn't a blanket bug; an eligible caller gets a genuinely different code path and message.

## 3. Audit dead-letter / requeue

**Gap:** retry/backoff math was tested; the actual dead-letter transition and the operator requeue action had zero coverage.

**Fix:** the dead-letter decision was inline inside `publish_batch`'s except-block, entangled with a live Kafka producer and a real DB session — not unit-testable as written. Extracted it into `record_publish_success` / `record_publish_failure` in `src/aida/projectors/outbox_publisher.py` (same logic, same behavior, now separately callable). `requeue_outbox_event` in `operational_api.py` needed no change — just no test.

**Tests** (`test_operational_behaviors.py`):
- `test_record_publish_failure_dead_letters_once_max_attempts_reached` — an event at `attempt_count == max_attempts - 1` moves to `DEAD_LETTER` on the next failure.
- `test_record_publish_failure_retries_with_backoff_below_max_attempts` — an event below the threshold stays `PENDING` with `next_attempt_at` advanced by the exponential-backoff formula.
- `test_requeue_outbox_event_resets_a_dead_letter_event_to_pending` — calls the real endpoint function directly; confirms `status`/`attempt_count`/`last_error` reset and that an audit record is written.
- `test_requeue_outbox_event_rejects_events_that_are_not_dead_lettered` — confirms the 409 guard (only dead-lettered events are requeueable).

## 4. Impact-analysis endpoint

**Gap:** `GET /metadata/tables/{table_id}/impact` existed in source with zero references anywhere in `tests/`.

**Test** (`test_high_stakes_behaviors.py`, no source change): calls `table_impact_analysis` directly against a fake session that returns fixture data for each of its five queries (table/schema/catalog join, semantic metrics, governed tools, approved relationship candidates, dbt resources).
- `test_impact_analysis_aggregates_downstream_objects_and_filters_tools_by_table_name` — the meaningful one: two governed-tool fixtures are supplied, only one of which actually references the target table by name; asserts the response includes only the matching tool and that `downstream_object_count` correctly sums all four evidence categories.
- `test_impact_analysis_returns_404_for_unknown_table` — the not-found path.

## Verification (clean environment, current device state)

Bundled the repo exactly as it sits now — your commit plus these five changed files, nothing else — into a fresh `python3.13` venv from scratch:

| Check | Result |
|---|---|
| `pytest` | **248 passed, 0 failed** (239 → 248, +9 new tests) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## Files touched

- `src/aida/query_gateway.py` — extracted `audit_sql_hash` (behavior-preserving)
- `src/aida/projectors/outbox_publisher.py` — extracted `record_publish_success`/`record_publish_failure` (behavior-preserving)
- `tests/test_high_stakes_behaviors.py` — +3 tests (HMAC, impact analysis ×2)
- `tests/test_operational_behaviors.py` — +4 tests (dead-letter ×2, requeue ×2)
- `tests/test_mcp_server.py` — +2 tests (anti-enumeration)

## What's left from the original audit

Of the 25 original claims, the proof gaps now closed with real behavioral tests: catalog tombstoning + reactivation, glossary coverage scoring (GL-4), Temporal heartbeat/resume, HMAC evidence, MCP anti-enumeration, audit dead-letter/requeue, and impact analysis. Still open and not yet picked up: fleet scheduling's priority/quota/backpressure sub-claims (only maintenance-windows is tested), governed-tool version lifecycle and retrieval ranking, the Neo4j FK-relationship half of catalog inventory (source-side FK extraction is tested; the graph projection itself now has one test from the earlier round, but ranking/traversal isn't), and most of the remaining glossary rows (GL-1/2/3/5/8 are still schema-validation only). Also still standing: the Phase-0-before-Phase-A sequencing question from the original strategic read.

Nothing committed — say the word when you want this batch checked in.
