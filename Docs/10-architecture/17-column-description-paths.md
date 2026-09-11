# Column descriptions: where they come from, and where a user finds them

First written 2026-09-10 as a call-graph trace, not a module inventory — the
distinction AU-1 exists to enforce. Revised the same day twice: after evidence
drafting and Excel save-back were built, and after model-assisted drafting was
added for the columns evidence cannot describe.

## Two ways a draft is written

### From catalog evidence

`aida.column_description_service` drafts a description for each undescribed
column from **catalog evidence alone**: dbt column docs, the source system's
comment, primary- and foreign-key membership, and approved same-source
relationships. There is no model call. It scores the evidence on the four
dimensions table drafts use and shares their 0.4 submission bar. Nothing is read
into a column's *name*. `amt_ccy` with no documentation is drafted as exactly
what the catalog says, and scores below the bar, rather than as a guess about
ISO 4217.

**Measured against the dev catalog on 2026-09-10 (read-only): 27 active tables,
196 columns, 0 reviewable drafts.** The catalog has no dbt column docs, no
source comments and no approved relationships, so every evidence draft is
structure-only and scores 0.15–0.31. The rule was working. It just could not meet
the expectation that column descriptions get generated, on this estate.

### From the model, for thin columns only

That measurement decided the second path. `aida.column_description_model`
drafts **only** the columns whose evidence is too thin to clear the bar, and only
when a steward asks for it (`model_assist`, from *Use the model for thin columns*
in the column panel). Its constraints are the design:

- **Thin columns only.** A column with enough evidence is still drafted from it,
  and the model is never asked about it. An answer about a column it was not
  asked about is ignored. The model may replace a thin evidence draft nobody has
  touched. It never replaces one a person edited, one already in review, or
  another model draft.
- **Metadata only, screened both ways.** The payload is names, types,
  nullability, keys, references, classification and the table's approved
  description. It never contains row values or source comments (a column with a
  comment is not thin). Every name and the table description go through
  `ingest_screening.screen_text` first, and quarantined text is withheld, not
  sent. The model's answers are screened the same way; a quarantined answer is
  dropped and the column falls back to its evidence draft.
- **Labelled and capped.** A model draft records `origin = MODEL_INFERRED`, the
  basis the model gave (name, type, key, …), and the call's route, model and
  input/output fingerprints. Its confidence is capped at 0.70, the platform's
  bound for model judgements, and at 0.5 when the model says it inferred from the
  name alone. The draft keeps its evidence score alongside, so a reviewer can see
  the model was filling a gap rather than confirming a finding. An edit keeps the
  label (`MODEL_INFERRED_WITH_HUMAN_EDITS`).
- **A person always decides.** The reviewer agent **abstains** on every
  model-inferred draft, edited or not. That doesn't rely on its approve threshold
  (0.8 by default), which already sits above the cap. The review queue row and
  the column panel both say "model-inferred" on the draft itself.
- **The governed gateway, nothing else.** Calls go through
  `ProviderNeutralModelGateway`: kill switch, approved route, credential,
  input-token cap, timeout and output schema. The route must also be approved for
  `CLASSIFICATION`, the capability semantic inference requires before catalog
  metadata may go to a model. These are gateway controls. Per-agent contract
  budgets (AR-05) bind agent runs, not this call.

When the model can't run, the request is refused with the reason and nothing is
written. That covers model calls switched off, no route configured, a route not
approved for this organization, a route without `CLASSIFICATION`, and a route
with no credential. A call that fails part way falls back to evidence drafts for
the columns it would have covered, and says why.

**What this cannot do is make the text true.** A description inferred from
`amt_ccy` can read as exactly right and be wrong. The label, the cap, the hedging
the instruction asks for and the human decision are the controls.

**Not yet exercised against a real model.** On 2026-09-10 the dev database had
no route approved for `CLASSIFICATION`. `AIDA_MODEL_ROUTE` names
`openai-bank-sql`, which does not exist there, and the three routes that do exist
use a provider with no adapter. The tests drive the full path with a
deterministic provider. The first real call needs a route approved through the
maker-checker flow.

## The write paths, and which ones a user can reach

`column_documentation.publish_column_description` is the only function that
creates a `ColumnDocumentationVersion`. Its callers, all behind a governance
review:

| Caller | Trigger | Reachable in ui-next? |
|---|---|---|
| `column_description_service.apply_column_description_draft` | A column draft is approved (evidence or model) | Yes: Catalog → column panel |
| `model_import.py` (`_apply_column_changes`) | An uploaded or saved-back workbook batch is approved | Yes: Sources → Model workbook, or Excel → Save to Atlas |
| `document_ingestion.py` | A `DocumentClaim` with `subject_type == "COLUMN"` is approved | Yes: Steward → Data dictionaries, then the review queue |
| `description_withdrawal.py` | A withdrawn description is reinstated | Yes: Catalog → column panel |

Document ingestion was the one unreachable path until 2026-09-11, when Steward →
Data dictionaries gave it a caller. A steward uploads a CSV data dictionary,
matches its rows to the project's catalog by exact name (a row that matches
nothing, or more than one table, stays unmatched), and proposes the matched
rows, each as its own review. Proposing a document a second time is refused
with 409 under a row lock on the document, so a double click or a second
steward cannot raise every review again. `tests/test_document_claims_postgres_concurrency.py`
shows the lock holding on a real PostgreSQL.

Two rules keep the paths from overwriting one another:

- **A draft cannot overwrite what it did not see.** Each draft records the
  column's description version when it was composed, and approval refuses if it
  has moved. Retirement counts as a move.
- **A workbook edit supersedes the draft it replaces.** When a batch publishes a
  description, open DRAFT-status drafts for that column close as `SUPERSEDED`.

One open draft per column is enforced by the database
(`uq_column_description_draft_open`, a partial unique index).

## Excel save-back

Save-back is the existing workbook import, reached from inside Excel by the
**Atlas for Excel** add-in (`ui-next/excel-addin/`, setup in its `README.md`).
The four things a save-back needs:

- **Editing integration:** the add-in. *Open a model from Atlas* creates the
  export as a new workbook; *Save to Atlas* sends the open workbook to the
  import endpoint.
- **Identity binding:** the export's README sheet names its datasource and
  organization. The add-in addresses the save from it, and the server refuses a
  workbook whose README names a different datasource. The uploader is the
  signed-in principal, recorded by the server.
- **Conflict handling:** unchanged. An edit to anything published after the
  export is `SKIPPED_STALE` at approval.
- **Approval gate:** unchanged. Saving creates a DRAFT batch, and maker ≠
  checker decides.

The workbook's Columns sheet carries the open draft beside
`business_description` (`drafted_description`, `draft_score`, `draft_status`,
`draft_origin`, `draft_id`), all read-only. `draft_origin` says whether a model
wrote it. Adopting a draft means copying it into `business_description`, an
ordinary reviewed edit that keeps authorship explicit.

**Not yet verified:** the add-in has not been loaded in a real Excel, and no
sign-in has gone through a real identity provider from its dialog.

## Analyst versus source-level placement

Unchanged. The workbook is source-level because it exports and imports every
table and column under one datasource, and its upload is role-gated. Drafting
is table-level, in the column panel, because that is where a steward reads
columns.
