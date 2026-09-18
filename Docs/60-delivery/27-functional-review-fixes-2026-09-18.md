# Functional review follow-through: 2026-09-18

This completes the checks started in [review 26](26-functional-verification-2026-09-18.md) and responds to the instruction to proceed fixing. Baseline for this follow-through is `53c5ab1`; that commit added the SQL workspace while the earlier broad test run was in progress. Status remains in [tracker section P](03-tracker.md).

## Closed in this follow-through

1. **Late Business Meaning responses after a scope change.** Clearing the datasource while its first page or next page was pending could restore stale annotation counts/content. Every first-page load now invalidates prior requests, including the empty-selection case, clears old results, resets pagination, and passes cancellation to subsequent pages. Both response paths check cancellation and request generation before updating state. Two regressions cover late first/next pages; the first-page regression failed before the correction.
2. **Parity checker crashes during an API restart.** An early post-restart check raised `RemoteDisconnected` instead of reporting that the HTTP comparison could not be measured. Direct socket errors/timeouts now return the existing unknown-result contract. Both regression cases failed before the fix; the checker still never calls an unreachable deployment a match.
3. **Local deployment lag.** Built the current API/UI and already-running worker services, ran the migration service, and refreshed those local containers. Did not remove volumes, reseed the estate, change authorization posture, or enable additional profiles. Final parity: **eight matched, zero drifted, zero not measured**. Migration `f4b8d2a6c913`, 423 API paths, 539 schemas, 273 settings. API and UI report healthy.
4. **Missing live proof for the new reviewed-SQL path.** Extended the existing private-database footprint journey. On PostgreSQL and SQL Server, pasted SQL validates without adding a query execution, explicit Run returns the known sample revenue, and a repeated receipt is refused with the first execution ID and no second execution. Existing change/rebuild/quality-hold steps still pass afterward.
5. **Stale SQL01 status.** The workspace was committed during review; the TODO row is updated to PARTIAL with implementation, configuration, verification and remaining acceptance distinguished.

The earlier duplicate React key correction was absorbed by the intervening commits. Its regression remains; it is not represented as a second new implementation here.

## Test accounting

| Check | Result |
|---|---|
| Full backend run started at `c295a3f` | 12,139 passed; 176 skipped; one strict expected failure; one OpenAPI-baseline failure. Runtime 32m55s. The tree and baseline changed during the run, so this is not a clean full-suite pass on `53c5ab1`. |
| Current OpenAPI gate + new SQL workspace + OKF Markdown safety | 50 passed on the current tree. The baseline failure did not reproduce; no baseline was regenerated to hide it. |
| Deployment comparator + new transport regressions | 31 passed; targeted lint passed. |
| Migration/ORM drift | One passed against PostgreSQL. |
| Current complete frontend suite | 954 passed in 102 files with two workers; includes the new SQL workspace and both scope-race regressions. Production build passed. |
| Extended PostgreSQL/SQL Server footprint journeys | Two passed, zero skipped. SQL Server driver deprecation warnings remain. |
| Earlier recent-feature suite | 257 passed; see review 26 for its baseline and scope. |
| Earlier browser journeys | 14 passed against production nginx UI and a test API; this does not certify the newly added SQL workspace's complete browser journey. |
| Live deployment parity | Eight matched; zero drifted; zero not measured after refresh. |

A full backend rerun was not repeated after the concurrent SQL-workspace commit; the changed paths and failing gate were tested separately. The one expected failure remains INV-9: connector capability flags are not yet fully derived from certification evidence. Skipped tests are not passes.

## Still open

- SQL01's generated-SQL live-model acceptance, dedicated browser draft/edit/validate/run journey and remaining request/UX contracts.
- REV01 large-estate review, GQL01's remaining read scope, GQL02 execution mutations, OKF03 import/edit proposals, remaining OKF quality/resource acceptance and UX16/S13 consolidation.
- Native routine invocation remains explicitly deferred under FP18.
- Workspace authorization is still configured OBSERVING with unresolved scope proceeding undecided. Deployment parity proves alignment, not production enforcement approval.

These are existing tracker tasks. This follow-through does not claim the entire product or every advertised database capability is complete.
