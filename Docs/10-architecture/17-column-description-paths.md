# Column descriptions: where they come from, and where a user finds them

Review date: 2026-09-10. Traced from the call graph, not from module inventory
— the distinction AU-1 exists to enforce.

## There is no column-description generation

Automatic description generation is **table-level only**.
`POST /v1/organizations/{org}/asset-description-drafts/generate` drafts a
description *of a table*, scoring evidence that includes its columns
(`asset_description_service.score_evidence` reads `column_count`,
`primary_key_columns`, `foreign_key_count`). It never writes a per-column
description. There is no column equivalent of that endpoint, and
`column_documentation_api.py` is read-only: two GET routes, no write, no
generate.

Saying "column-description generation is not yet discoverable" would therefore
be the wrong diagnosis. It is not undiscoverable; it does not exist.

## The three write paths, and which one a user can reach

`column_documentation.publish_column_description` is the only function that
creates a `ColumnDocumentationVersion`. It has exactly three callers:

| Caller | Trigger | Reachable in ui-next? |
|---|---|---|
| `document_ingestion.py:369` | A `DocumentClaim` with `subject_type == "COLUMN"` is approved | **No** |
| `model_import.py:624` | An uploaded model workbook's batch is approved | Yes — Sources → Model workbook |
| `description_withdrawal.py:330` | A withdrawn description is reinstated | Yes — Catalog → column panel |

The first row is the finding. Document ingestion is the closest thing the
platform has to *generating* column descriptions — extract claims from an
uploaded document, review them, publish the approved ones — and
`document_ingestion_api.py` exposes seven routes for it. **`ui-next` calls none
of them.** There is no document upload, section, mapping or claim surface
anywhere in the app; the whole path is reachable only by `curl`. This is the
same shape as AR-5/LN-3: a module with passing unit tests and no callers is
indistinguishable from a healthy one.

Reinstatement republishes text that was already reviewed once, so it cannot
introduce a description that never existed. That leaves **the model workbook as
the only way a user of this product can author a column description at all.**

## Analyst versus source-level placement

The workbook is source-level, on the Sources screen, and that is right: it
exports and imports *every table and column under one datasource*, so it is
scoped to the thing it operates on, and the upload path is role-gated
(`PlatformAdmin`, `MetadataAdmin`, `DataAdmin`, `DataSteward`) while analysts
may download and inspect. Moving bulk import/export to an analyst surface would
put a fleet-wide write behind a screen scoped to one asset.

The defect was not the placement, it was the one-way signposting. Sources
already linked out to Catalog ("Tables & columns"); Catalog's column panel named
the workbook in prose — *"use the source model workbook for bulk column business
descriptions"* — without being able to reach it. Naming a destination the reader
cannot navigate to is how a documented feature stays undiscovered. The column
panel now carries a `Describe columns in → Source model workbook` cross-link,
built on `CatalogRowRead.datasource_id`, whose own field comment already said
"a cross-link needs the id, not just the name".

Deliberately **not** shipped: a `?focus=workbook` param that scrolled the
workbook section into view on arrival. Measured across all four fixture sources,
that section lands 65–115px into a 208px detail pane — visible without
scrolling every time. The mechanism could not be shown to change anything, so it
was removed rather than kept as an unexercised control, on the same rule that
refused `write_lanes`.

## What would close this properly

Ranked, and none of it is delivered here:

1. A document-ingestion surface (upload → sections → mappings → claim review),
   which is what turns the seven orphaned routes into the generation path this
   review was originally looking for.
2. A column-level generate endpoint, if per-column drafting is actually wanted.
   The table-level scorer is not it, and reusing it would produce one
   description for many columns.
3. Until either exists, the column panel's guidance must keep saying that
   automatic drafts are table-level. It is currently the only place in the
   product that tells the truth about this, and it should not be softened.
