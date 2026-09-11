# Follow-up validation and remaining work

Later implementation: the [five-feature report](18-five-feature-implementation.md)
supersedes the feature-gap statuses below and records the separate deployment checkpoint.

Date: 2026-09-10. Scope: revalidate the user's layout, column documentation,
source workbook, reviewer, graph and architecture concerns. This is not a
statement that the whole request is complete.

## Defects fixed in this follow-up

| Defect | Change and evidence |
|---|---|
| An uploaded workbook could be submitted after its preview request failed | Submission and exclusion require a successfully loaded preview. Retry reloads the existing batch without uploading another file. Component regression test covers the failure and recovery. |
| Preview silently fetched only the first 1,000 changes | The client follows all pages and rejects missing pages, changing totals, duplicate rows or rows from another batch. API tests include a 1,001-row batch. |
| Large previews could render thousands of rows simultaneously | The preview renders 100 rows per page with navigation and an explicit total. The defensive client limit is 50,000 preview rows including rejected rows; the backend separately limits actual changes to 5,000. Oversized previews are refused, never silently truncated. |
| Source switching retained an unrelated import session | The import session is keyed by datasource. A regression test proves the previous batch cannot be submitted from the newly selected source. Reset is disabled while an operation is in flight. |
| Rejected/excluded-only workbooks were described as matching the model | The empty-submit message now explains that no included changes are ready and directs the user to rejected/excluded rows. |
| A late response could replace the selected table's column documentation | Column requests are cancelled and their results ignored after navigation or another load. A delayed-response regression test reproduces the ordering. |
| Column documentation was hard to scan or recover after a failure | Added column name/type search, a distinct no-match state, and retry. Source comments and approved descriptions remain separate. |
| A stale review detail could still offer decisions after a failed queue refresh | The detail remains readable but decisions are blocked until a successful refresh. Added a regression test. Workbook import batches are also available in the object-type filter. |

These are UI/client correctness fixes. They do not replace backend authorization,
maker-checker separation, stale-version checks or submission validation.

## User-request acceptance ledger

| Concern | Current status | Remaining acceptance work |
|---|---|---|
| Alignment at laptop 100% zoom and other screen sizes | Existing responsive shell/pane rules inspected; **not visually verified** this pass | Sources with expanded workbook, Catalog, Review queue and graph at representative laptop/desktop/mobile widths and zoom levels. No enabled browser was exposed by the computer-use connection. |
| Table and column definitions | Catalog columns, source comments and authored descriptions exist; search/retry/navigation safety tested | Very wide-table pagination and live visual acceptance remain. |
| Automatic column business-description generation | **Not implemented by this pass.** Existing draft generation applies to tables | A grounded column proposal API/workflow, provenance, editable preview, duplicate/stale handling, authorization and maker-checker tests. Do not label table-only drafting as column support. |
| Source versus analyst placement | Source-scoped workbook and Catalog cross-links exist; analyst downloads and steward/admin import requirements are explicit | An analyst-centered editing entry point must reuse the same authorization model, not grant import rights by navigation or persona choice. |
| Manual Excel round trip | Implemented with tested upload/preview/submit safeguards | Live Excel interoperability acceptance remains. |
| Open worksheet and automatic save-back | **Not implemented.** Saving an exported Excel file does not sync it | Choose an in-browser editor or an explicitly connected Excel integration. Define identity, file/session binding, concurrency, retries and conflict resolution; save must not bypass review. No local-folder watcher or permission expansion was installed. |
| Maker-checker error from screenshot | Existing decision-error isolation and own-proposal messaging retested; stale-refresh action gap fixed | Live reproduction remains. Structured diffs are still unavailable for several review types, including context-product versions and model-import batches in the generic queue. Filtering a type does not add its detailed diff. |
| Graph filtering and question execution | Prior asset/type/confidence filters and deterministic guided questions retested in full UI suite | General NLP planning/execution, ontology lifecycle and real Neo4j parity remain separate, uncompleted work. |
| Enterprise agent architecture | Deterministic authority boundaries remain; documentation distinguishes timeout estimates from measured usage | Production capacity, adversarial semantic accuracy, queue contention, audit recovery and provider usage evidence are not established by UI tests. |

## Validation evidence

- Final full UI suite: **566 tests passed in 74 files**, including all added regressions.
- Review-queue suite: **19 tests passed**, including the new stale-detail case.
- TypeScript typecheck passed after that change.
- Final production UI build passed with all changes in this follow-up.
- Backend workbook, column documentation, withdrawal and review-composition
  suites passed (`test_model_import.py`, `test_column_documentation.py`,
  `test_description_withdrawal.py`, `test_review_queue_read_model.py`).

No production approvals, credential changes, browser permission changes, automatic
Excel uploads or Neo4j enablement were performed. A passing mocked UI suite is not
a live-browser or production-scale certification.
