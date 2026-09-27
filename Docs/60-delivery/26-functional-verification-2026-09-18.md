# Functional verification: 2026-09-18

Baseline: `c295a3f`, including GraphQL metadata reads, OKF storage/context grounding, and trigger parse coverage. This review also fixes Business Meaning component identity and reconciles stale tracker statements. It does not mark the whole product complete.

Compared against the [22-item assessment](../10-architecture/22-context-enrichment-and-review-workspace.md), [GraphQL/OKF/workspace design](../10-architecture/21-graphql-okf-and-workspace-design.md), [prior review reconciliation](25-review-reconciliation-2026-09-16.md), and current tracker section P. Historical observations are preserved; the tracker corrections below identify the claims superseded by later implementation.

## Findings and disposition

| Finding | Disposition | Evidence |
|---|---|---|
| Opening ontology authoring without a datasource gives the ontology dialog and business-generation component the same React key. React warns that sibling identity is unsupported. | Fixed: namespace each component's scope key. Scope changes still remount the correct component. | New assertion failed before the fix; Business Meaning/accessibility tests pass after it. |
| OKF tracker still describes character budgets, Ask receipts, ambiguity clarification and immutable publication selection as missing. | Corrected the existing OKF02 row using current source and tests; task remains PARTIAL. | Config fields, Ask receipt rendering tests, competing-table clarification test, publication-pinned read API and store tests. |
| Running API image predates the reviewed source. | Open deployment action. Do not treat source tests as evidence that these features are live. | Read-only parity check: migration matches, but live OpenAPI lacks trigger parse coverage and image lacks two OKF budget settings. |
| Default-parallel UI suite times out waiting for Relationships to finish loading during accessibility scanning. | Reported as test-run contention, not silently discarded. Focused rerun and complete suite with two workers pass with assertions unchanged. | Initial run: 942 passed, one timeout. Final complete run: 943 passed. |

## Verification results

| Check | Result and scope |
|---|---|
| Recent backend features | 257 passed: GraphQL API, OKF export/store/context, trigger coverage/review/reconciliation, and playbook input validation. |
| Real source journeys | Six passed against local PostgreSQL and SQL Server: footprint discovery/context/tool journey, trigger write lineage, and review-to-active-graph journey. Eight SQL Server driver deprecation warnings; no skips in this run. |
| Frontend suite | All 943 tests in 101 files passed with `--maxWorkers=2`. |
| Focused UI regression/accessibility | 55 passed in Business Meaning and accessibility sweep. |
| Production frontend build | TypeScript and Vite build passed after the UI correction. |
| Browser journeys | 14 Chromium tests passed against a separately built production nginx UI and the repository's journey stub API. Includes source connect/scan, description submission/review, Ask, context-product refusal, audit and denied access. This is production UI/proxy proof with a test backend, not a live-model or live-source browser proof. Temporary containers/network were removed afterward. |
| Backend lint and typing | Ruff passed; mypy passed for 398 source files. |
| Contract checks | No breaking OpenAPI changes; generated UI types match 534 schemas; 12 import contracts kept; one Alembic head (`e3a9c7d51f02`). |
| Documentation links | Existing 258 Markdown files passed before this report; updated documents checked again at completion. |
| Deployment parity | Five comparisons matched, three drifted. See exact scope below. |

Full-backend-suite result will be recorded when the run completes; targeted passes above are not a substitute for it.

## Deployment gap

The read-only parity check against `http://localhost:8000` found:

- Database migration is at source head `e3a9c7d51f02`.
- Source baseline has 421 paths / 534 schemas; running API has 420 / 533.
- Missing route: `/v1/datasources/{datasource_id}/triggers/{trigger_id}/parse-coverage`.
- Missing schema: `TriggerParseCoverageRead`.
- Missing deployed settings: `okf_context_ask_max_chars`, `okf_context_default_max_chars`.
- Required PostgreSQL readiness is UP. Workspace authorization reports OBSERVING, with unresolved scope proceeding undecided. That is observed configuration, not an enforcement certification.

Deploy the tested source and corresponding UI, then rerun `scripts/check_deployment_parity.py` and live acceptance. No shared deployment was rebuilt/restarted by this review. No production authorization posture was changed.

## Planned work that is still incomplete

The [tracker](03-tracker.md) remains authoritative; use these existing tasks, not a second backlog:

- **R11-SQL01:** user-facing generated/pasted SQL draft, validate, confirm and run workflow. Existing validation/execution endpoints do not complete that workflow.
- **R11-REV01:** scalable, evidence-based review and playbook previews for large estates.
- **R11-GQL01:** metadata reads exist; remaining lineage/context reads, explorer and rate-budget work are not implied by their tests. **GQL02:** governed execution mutation remains TODO.
- **R11-OKF01/02:** exporter and durable, scoped context consumption exist. Retain unclosed rendering/resource, source-scoped bundle, GraphQL consumption and answer-quality evaluation criteria. Tests do not establish a measured improvement in real-model answers.
- **R11-OKF03:** import/wiki-edit proposals remain TODO; no automatic publication is enabled.
- **R11-UX16/S13:** further result-workspace and stewardship consolidation, including remaining visual/reflow acceptance.
- **R11-FP18:** native routine invocation remains deliberately deferred. Extracted read-only SQL tools are not native procedure execution.
- **INV-9 certification-derived capabilities:** the existing strict expected failure in `tests/test_inv9_capability_honesty.py::test_capability_flags_are_derived_from_certification` remains meaningful. Capability flags are still hand-declared; certification does not yet establish every flag such as explain, constraints, indexes, partitions, query history, delegated identity and approximate statistics. Existing ST-03/E12 evidence records this gap. Do not turn the expected failure into a pass or describe all advertised engine capabilities as certified.

## Conclusion

The recent functionality has meaningful API, UI, security-boundary and real-engine test coverage. The product must not be described as fully implemented or fully deployed. Close the source/deployment mismatch and finish the named acceptance criteria before making that claim. This review adds no unrequested database families, provider integrations or unattended approvals.
