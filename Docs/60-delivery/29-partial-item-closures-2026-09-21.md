# Partial-item closure addendum — 2026-09-21

This extends the [complete demo validation report](28-demo-readiness-validation-2026-09-21.md), rather than creating another review queue. [Tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) remains the status authority.

## Completed in this continuation

| Item | Closure |
|---|---|
| R11-FP10 | Marked DONE: claim corrections and rejection suppression are implemented and the row explicitly named no unfinished acceptance item. |
| R11-FP11 | Marked DONE: routine/view retrieval and MCP support are implemented and the row explicitly named no unfinished acceptance item. Connector/parser limitations remain in their own footprint rows. |
| R11-GQL02 | Marked DONE: governed execution, SDK examples and the integrated explorer have landed. Confirmation/error fixes are in `dbe8078`; GraphQL telemetry and deployment-policy work remain GQL01. |
| R11-OKF01 | Marked DONE for deterministic export. Storage/incremental work belongs to OKF02; import/wiki editing belongs to OKF03. No claim of upstream certification was added. |
| R11-OKF02 security gap | Fixed product MCP context egress: selected stored sections are screened live before either Markdown or structured output reaches an agent. Refused sections are also excluded from consumption receipts. The quarantine audit contains identifiers/counts and screening version, never the unsafe text. The broader row stays PARTIAL. |

The context-screening regression plants unsafe text in a stored publication, simulating content retained from before the current screening rules. It proves the section reaches neither output representation nor read receipts, while the quarantine event identifies the withheld section. Existing tests still prove normal product/source reads, publication parity and authorization behavior.

## Validation evidence

The earlier morning validation in the main report was performed by this Codex session: 14,858 backend tests passed with 184 skipped, 1,271 UI tests passed, 47 real PostgreSQL/SQL Server journey tests passed without skips, and the read-only role sweep made 2,412 requests with no server errors. The main report records a later independent clean-worktree run (14,889 backend and 1,286 UI tests), the browser journey, deployment rebuild and demo rehearsal; those later measurements are attributed there and were not rerun as part of this small continuation.

After the MCP change:

- Product/source OKF context suites: **53 passed**.
- Final closure group: **96 passed** (`test_okf_context`, `test_mcp_source_knowledge`, `test_graphql_explorer_examples`, `test_access_change_review`, `test_review_queue_query_budget`, `test_migration_command_parity`).
- Targeted Ruff and MCP mypy: passed.
- Latest production UI build, including concurrently added screens: passed.
- Documentation links: passed before this addendum; final check recorded in the local log.

Initial sandbox restrictions, interrupted runs, frontend timing failures, the in-flight UTC-shape failure and successful reruns are retained in `artifacts/validation/2026-09-21/`. These local generated artifacts are ignored by Git. An unsuccessful or interrupted attempt is not a pass. The full suite has not been repeated after every concurrent edit; the 96-test closure group is the evidence for this continuation.

## What is still pending

The recorded export and execution scopes are complete; the whole product is not unconditionally certified. Section P retains complex routine/package lineage gaps, source-object knowledge behavior, freshness/rename handling, live model evaluation, batch-review acceptance and configured import acceptance. Production/customer identity, connectors, capacity/restore, external destinations, provider guarantees and human accessibility have their own acceptance requirements. Intentionally deferred native CALL/EXEC and sample-egress policy decisions are not bypassed to close a status row.

Other sessions are actively changing the shared branch. The MCP fix and latest UI changes need the final chosen commit deployed and the demo rehearsal repeated before they can inherit live-deployment claims. Keep the demo scoped to verified capabilities, and check the current tracker for later closures. The release secret-scan finding (R11-VAL01) and rotation of the previously exposed provider key also require explicit resolution; no secret is copied into this report.

