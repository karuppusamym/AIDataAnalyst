# Five-feature implementation and validation

> **Scope reconciled 2026-09-11.** [Tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) owns current execution status; [reconciliation decisions](23-review-reconciliation-2026-09-11.md) map the remaining work. This document retains design and dated evidence. Older priorities, partials and recommendations do not form a separate queue.

Updated 2026-09-11. This supersedes the feature-gap entries in the earlier
[follow-up ledger](17-follow-up-validation.md). Code implementation, automated
verification, database deployment and live visual acceptance are distinct.

| Recommendation | Implemented entry point | Boundaries |
|---|---|---|
| Complete review details | Select a workbook-import, context-product-version or ontology-version proposal in Review queue | On-demand structured snapshots, paged field deltas, full saved content, excluded/rejected rows and version expectations. Decision controls wait for a successful matching preview. Context versions compare with their declared base. Loading every large diff into the queue list is deliberately avoided. |
| Browser column worksheet | Catalog → select table → Columns → **Open column worksheet** | Edit authored descriptions, filter/paginate columns, save to a draft and inspect/submit its preview. Source fields remain read-only. Saving is not publication. Existing maker-checker, version checks and source/table authorization remain in force. |
| Column-description generation | Catalog → select table → column-description drafts | The existing column draft API/UI is now present and retested: evidence-grounded deterministic generation, editing, provenance, evidence threshold, duplicate/stale handling, independent approval. This is not a claim that generated business meaning is always correct or that every draft comes from an LLM. |
| Broader natural-language graph questions | Unified lineage → Ask the graph → Preview → Run impact query | Supports upstream/downstream, dependencies, impact, inputs, “what feeds”, “what depends on”, “which assets use”, “where does … come from”, and “lineage of”. The last returns both directions. Exact loaded-asset resolution, ambiguity refusal, 1–5 hops and the existing 200-node impact bound remain. No arbitrary Cypher/SQL or unconstrained model plan execution. |
| Governed ontology management | Unified lineage → **Manage ontology** | Versioned JSON editor and history for concepts, aliases, typed relations, declared cardinalities, provenance, owners and TABLE/COLUMN mappings. Draft → review → independent publication. Published keys must be deprecated rather than silently removed. Concurrent stale proposals cannot replace a newer publication. |

## Workflow and safety details

The browser worksheet pins the description versions from the start of its edit
session. `POST /v1/tables/{table_id}/column-worksheet` validates the active table,
source access, column membership, duplicate IDs, text and request size, then
reuses the workbook diff/persistence service. The browser automatically loads the
saved batch preview, but the user must explicitly submit it. Failed saves retain
the editor; closing unsaved edits requires a discard choice. Blank cells cannot
silently withdraw approved descriptions.

Worksheet saves accept up to 1,000 edited columns per request. Existing workbook
limits still apply. Column loading now paginates completely up to a 10,000-column
browser limit and refuses incomplete, inconsistent or wrong-table responses.
This is an in-browser alternative to the manual Excel round
trip, not a claim that an arbitrary local Excel file is monitored for changes.
There is also separate Excel-add-in work in the repository; live deployment and
verification of that integration are not established by this pass.

Ontology APIs are organization-scoped and restricted to existing stewardship,
administration and review roles. Mappings must resolve to authorized active
tables/columns. Ontologies do not grant permissions. Publication is T2, through
the shared governance decision service, not unattended reviewer-agent approval.
The published-head update is conditional on the version's original base; an
outdated proposal is refused. Versions remain available as history. Each edit is
a new draft; there is no silent in-place mutation of a submitted definition.

The ontology editor is a validated JSON editor, not a visual drag-and-drop
designer. Relation cardinalities describe ontology semantics; they do not prove
that source records conform. No OWL/RDF import, SHACL validation, instance
reasoning, ontology-to-Neo4j projection or real Neo4j adapter certification is
claimed. PostgreSQL remains authoritative and the existing Neo4j enablement guard
is unchanged. Those advanced capabilities are beyond this first implementation.

## Automated validation

- Full UI suite: **624 tests passed in 84 files**; production UI build passed.
- Subsequent focused validation: **47 tests passed in 4 files**, including the
  new complete-column pagination checks. A build caught the concurrently added
  Data Dictionaries screen mid-edit; the repeat production build then passed,
  including that screen.
- New UI tests cover worksheet save/retry/discard/version binding, review-preview
  failures and wrong-review responses, ontology draft/submit/JSON errors/history
  baselines, and natural-language aliases with ambiguity/depth/statement refusal.
- Backend suites passed: `test_model_import.py`, `test_column_description.py`,
  `test_review_queue_read_model.py`, `test_review_queue_query_budget.py`,
  `test_ontology.py`. New cases cover actual worksheet draft→review→publication,
  maker-checker refusal, wrong-table/duplicate columns, context-product baselines,
  ontology approval, stale proposals, tenant boundaries and invalid definitions.
- Targeted backend lint and typing passed before the final integration check;
  the final results are recorded below when available.

These tests use mocked UI transport and isolated database fixtures. They do not
certify live browser layout, provider semantics, production throughput or recovery.

## Database deployment checkpoint

> **September 12 read-only confirmation:** configured PostgreSQL and repository both report
> `090b3be72b67 (head)`; `ontology_head` and `ontology_version` exist. The unapplied-migration
> blocker described below is historical and resolved. R11-C1 is PARTIAL pending live
> create/review/publication evidence; do not act on the old lock/session IDs. See the
> [completion confirmation](23-review-reconciliation-2026-09-11.md#september-12-completion-confirmation).

The following records the earlier checkpoint:


Concurrent work created migration `7c2d94e1b8a3` for the same ontology tables while
this pass was preparing its migration. The duplicate `f2a6b9c1d4e8` attempt was
cancelled before commit and its local migration file removed. Read-only checks
confirmed no ontology tables remained and the live local database was still at
`d5e8a2c7f9b1`. No existing data was removed.

The retained migration chain has one head, `7c2d94e1b8a3`; it also includes the
separately developed quality-rule proposal migration. An active database lock
was observed. Do not stamp past unapplied migrations or terminate another
session's transaction merely to make the version number current. Coordinate the
other coding/database session, apply the retained chain, restart/reload the app,
then verify ontology create/review/publication in the running environment.

Until that is verified, ontology deployment is **pending**, even though its code
and automated lifecycle tests are implemented. Live 100% zoom/multi-screen visual
acceptance also remains unverified. This document does not label either complete.

Rechecked on the user's request: the database remains at `d5e8a2c7f9b1`, and
neither `ontology_head` nor `ontology_version` is visible as a committed table.
Database session `54640` is waiting to create the ontology version table and is
blocked by session `10760`, running a metadata-table SELECT. No local Python
Alembic upgrade/downgrade process was found at that check. Repository edits are
still arriving from another session. No other session was cancelled or terminated.
