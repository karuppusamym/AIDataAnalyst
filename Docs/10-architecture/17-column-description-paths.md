# Column descriptions: where they come from, and where a user finds them

First written 2026-09-10 as a call-graph trace, not a module inventory — the
distinction AU-1 exists to enforce. Revised the same day, after column
description drafting and Excel save-back were built.

## Drafting now exists — and on today's dev catalog it drafts nothing submittable

Column descriptions can now be drafted automatically, on the same contract as
table drafts (GL-9): `aida.column_description_service` composes a draft for
each undescribed column **from catalog evidence alone**. That means dbt column
docs, the source system's comment, primary/foreign-key membership and approved
same-source relationships, with **no model call**. It scores the evidence on the
four dimensions table drafts use, and publishes only through an independent
decision on the draft's `GovernanceReview` (`COLUMN_DESCRIPTION_DRAFT`, risk
tier T0). A draft below `MINIMUM_EVIDENCE_FOR_REVIEW` (0.4, shared with tables)
cannot be submitted. Nothing is read into a column's *name*: `amt_ccy` with no
documentation is drafted as exactly what the catalog says, and scores below the
bar, rather than as a guess about ISO 4217.

Where a steward meets it: **Catalog → an asset → Columns → Column description
drafts**. Generate, fix the wording, submit one or all reviewable drafts, then
decide in the review queue. There the proposed text now leads the evidence list
for both column *and* table drafts — before this pass, a reviewer working from
the queue approved table-draft text the queue never showed them.

**Measured against the dev catalog on 2026-09-10 (read-only):** 27 active
tables, 196 columns, **0 reviewable drafts**. The catalog carries no dbt column
docs, no source comments and no approved relationships, so every draft is
structure-only and scores 0.15–0.31. This is the rule working as designed, not a
defect. A primary key and a foreign key say what a column *joins to*, not what
it *means*, and the contract refuses to guess the rest. The practical
consequence, though, is that on this estate deterministic drafting alone does
not meet the expectation that column descriptions get generated. Closing that
gap is a choice, and it is still open:

1. **Feed it evidence.** Import dbt artifacts with column docs, add
   `COMMENT ON COLUMN` in the source and rediscover, or approve relationship
   candidates. Each moves real columns over the bar with no change to the rules.
2. **Model-assisted drafting for the thin columns only.** Labelled
   model-inferred, capped below agent auto-approval, under the agent budget and
   ingress screening (AR-05, AR-10). This is the only option that produces text
   for `amt_ccy`, and the only one that can produce *confidently wrong* text.

## The write paths, and which ones a user can reach

`column_documentation.publish_column_description` is the only function that
creates a `ColumnDocumentationVersion`. Its callers, all behind a governance
review:

| Caller | Trigger | Reachable in ui-next? |
|---|---|---|
| `column_description_service.apply_column_description_draft` | A column draft is approved | Yes: Catalog → column panel |
| `model_import.py` (`_apply_column_changes`) | An uploaded or saved-back workbook batch is approved | Yes: Sources → Model workbook, or Excel → Save to Atlas |
| `document_ingestion.py` | A `DocumentClaim` with `subject_type == "COLUMN"` is approved | **No** |
| `description_withdrawal.py` | A withdrawn description is reinstated | Yes: Catalog → column panel |

Document ingestion is still unreachable. `document_ingestion_api.py` exposes
seven routes and ui-next calls none of them. It is the same shape as AR-5/LN-3:
a module with passing tests and no caller.

Two rules keep the paths from overwriting one another:

- **A draft cannot overwrite what it did not see.** Each draft records the
  column's description version when it was composed, and approval refuses if it
  has moved. Retirement counts as a move. It is the workbook's `*_version` rule,
  applied to drafts.
- **A workbook edit supersedes the draft it replaces.** When a batch publishes a
  description, open DRAFT-status drafts for that column close as `SUPERSEDED`.
  A draft already in review keeps its review; approving it is then refused on
  the version check.

One open draft per column is enforced by the database
(`uq_column_description_draft_open`, a partial unique index), not by a
read-then-insert two concurrent requests could both pass.

## Excel save-back

Save-back is the existing workbook import, reached from inside Excel by the
**Atlas for Excel** add-in (`ui-next/excel-addin/`, setup in its `README.md`).
The four things a save-back needs:

- **Editing integration:** the add-in. *Open a model from Atlas* creates the
  export as a new workbook; *Save to Atlas* sends the open workbook to the import
  endpoint.
- **Identity binding:** the export's README sheet names its datasource (and
  organization). The add-in addresses the save from it, and the server now
  **refuses** a workbook whose README names a different datasource
  (`model_import._check_workbook_datasource`). The uploader is the signed-in
  principal, recorded by the server — the development principal, or the OIDC
  user from the add-in's sign-in dialog.
- **Conflict handling:** unchanged. Every editable cell carries the version it
  was exported against, and an edit to anything published since is
  `SKIPPED_STALE` at approval.
- **Approval gate:** unchanged. Saving creates a DRAFT batch; *Submit for
  review* queues it; maker ≠ checker decides.

The workbook's Columns sheet now carries the open draft beside
`business_description` (`drafted_description`, `draft_score`, `draft_status`,
`draft_id`, all read-only). Adopting a draft in Excel means copying it into
`business_description`, which is an ordinary reviewed edit that keeps human
authorship explicit.

**Not yet verified:** the add-in has not been loaded in a real Excel, and no
sign-in has gone through a real identity provider from its dialog. Its logic is
covered against a fake Office runtime, and the manifest is checked against the
build. Loading it needs a trusted localhost certificate and a sideload, which
are machine and tenant changes for the owner to make.

## Analyst versus source-level placement

Unchanged. The workbook is source-level because it exports and imports every
table and column under one datasource, and its upload is role-gated. Drafting
is table-level, in the column panel, because that is where a steward reads
columns. The column panel links to the workbook, and the workbook shows the
drafts.
