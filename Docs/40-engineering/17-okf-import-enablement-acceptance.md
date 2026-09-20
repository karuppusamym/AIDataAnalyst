# OKF import: enablement acceptance

**Status 2026-09-19.** Import ships **off** (`okf_import_enabled`, default `false`, recorded in
`scripts/configuration_decisions.py` as "off by design"). Everything below except the last two
sections has been proven in-process and against a private PostgreSQL database
([R11-OKF03](../60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11)); what it
has **not** had is a run on a deployed stack by the person who owns the decision to switch it on.
This is that run, written so it is a checklist and not a research task. Nothing here is a claim
that it has been done.

## What enabling does, and does not

Enabling lets a person upload an edited OKF bundle and have its changes become **pending
proposals**. It never publishes anything on its own:

- An import is a *preview* first, then an *apply of the accepted preview*; a preview that went
  stale (the catalog moved after it was read) refuses the apply.
- Applying files reviews -- `OKF_IMPORT_BATCH` for table, view and column descriptions and concept
  edits, `OKF_IMPORT_ROUTINE_DESCRIPTION` for routine purposes -- both **T2 at every size**, so no
  agent may decide either. The importer cannot approve their own import (maker-checker), and neither
  can whoever last edited the text.
- Text a reviewer has already refused, a description approved since the export, a routine whose body
  changed since the export, and a routine that already has a draft open are all refused or reported
  as conflicts; none overwrites.
- Claims inside the file (`verified:`, `status:`) are reported and ignored. Unknown fields are
  listed and **not kept** (see [the decision below](#decisions-recorded-here)).

Turning it off again does not strand anything: the review-preview route is deliberately not behind
the flag, so imports already pending can still be inspected, approved or rejected.

## Preconditions

1. Deployment parity reads a match on the commit you are enabling
   ([16 §3](16-deployment-alignment-and-enablement-runbook.md#3-verify-parity-afterwards)).
2. Two **different human principals**: one holding `MetadataAdmin`, `DataSteward` or `PlatformAdmin`
   (the importer), and a second holding `DataSteward`, `SemanticAdmin`, `Reviewer` or
   `PlatformAdmin` (the checker).
3. A context product with a published version whose knowledge bundle has been read at least once,
   so there is a stored publication to import against.
4. A recorded rollback: set the flag back to `false` and restart the API.

## Enable

Set `AIDA_OKF_IMPORT_ENABLED=true` in the deployment's environment and restart the API. It is a
settings-only change: no migration, no image rebuild. Then re-read parity -- the settings row
should still match, since the setting exists in every image.

## The journey, with a pass condition per step

Run each step as the named principal. A step passes only if the stated result is observed.

| # | Who | Step | Pass condition |
|---|---|---|---|
| 1 | importer | Download the product's bundle (`GET /v1/context-product-versions/{version_id}/okf-bundle/download`, or Download bundle in Context products). Unzip. | The archive opens; `index.md` and the manifest are present |
| 2 | importer | Edit four things: a table's `# Purpose` (or a view's), one column description in `# Schema`, an alias under a concept, and a routine's `# Purpose`. Add `wiki_owner: someone` and a `verified:` block to one document. Re-zip. | -- |
| 3 | importer | `POST .../okf-bundle/imports/preview` with the archive | Every edit is `PROPOSE` with the current text beside it; `wiki_owner` is `UNKNOWN_FIELD_TOLERATED`; `verified` is `CLAIM_NOT_AUTHORITY`; the routine edit carries the definition version the export showed. **Nothing has been written** -- the Review queue is unchanged and only an audit record exists |
| 4 | importer | `POST .../okf-bundle/imports` with the same archive and the preview's digest | Two reviews appear pending: one `OKF_IMPORT_BATCH`, one `OKF_IMPORT_ROUTINE_DESCRIPTION`. No description has changed anywhere |
| 5 | importer | Try to approve the batch yourself | Refused (409, maker-checker). Also refused for anyone who edited the text |
| 6 | checker | Open each review in the Review queue. Read the preview: per document, before and proposed text, the approved version now, whether each change still applies | The decision controls stay disabled until the preview has loaded for **this** review; a change already superseded reads as a conflict, not as applicable |
| 7 | checker | Approve the batch and the routine review | Descriptions and the concept edit are published; the routine description is published by the routine workflow; `governance.review_requested.v1` and the approval events are in the outbox |
| 8 | importer | Download the bundle again | The new text appears in the exported documents; `wiki_owner` and the `verified:` block do **not** (unknown fields do not round-trip) |
| 9 | importer | Upload the *same* archive again | The edits already applied are not proposed a second time |

### Negative checks (each must be refused, with the named reason and no partial effect)

| Upload | Expected refusal |
|---|---|
| A zip with a `../` path, an absolute path, a symlink, or an encrypted member | `PATH_TRAVERSAL`, `PATH_ABSOLUTE`, `MEMBER_SYMLINK`, `MEMBER_ENCRYPTED` |
| A zip that expands to many times its size | `ARCHIVE_COMPRESSION_RATIO` or `ARCHIVE_EXPANDS_TOO_LARGE` |
| A document with YAML anchors, tags or a second document | `YAML_ALIAS_NOT_ALLOWED`, `YAML_TAG_NOT_ALLOWED`, `YAML_MULTIPLE_DOCUMENTS` |
| An edit whose text contains a link, raw HTML or a code fence | `TEXT_LINK_NOT_ALLOWED`, `TEXT_RAW_MARKUP_NOT_ALLOWED`, `TEXT_CODE_FENCE_NOT_ALLOWED` |
| An edit that changes a document's object key in its frontmatter | `IDENTITY_MISMATCH` |
| A bundle exported more than five publications ago | `BASE_PUBLICATION_NOT_RETAINED` -- export again |
| An upload while the flag is off | Refused before anything is read |
| A tool version's text, or a package | `FAMILY_NOT_SUPPORTED` -- by design |
| A description approved between your export and your apply | A conflict, and the approved text is untouched |
| An agent principal deciding either review | Refused |

## Record

Attach to the tracker row: the parity output before and after enabling, the two review ids, the
checker's approvals, the re-exported document showing the new text, and the negative-check results.
If any pass condition fails, set the flag back to `false` and record what failed; do not edit the
pass conditions.

## Decisions recorded here

- **Unknown frontmatter fields do not round-trip.** They are accepted, listed
  (`UNKNOWN_FIELD_TOLERATED`) and dropped. Keeping them would need a second, parallel store of
  edited wiki content, which the import's design forbids -- the only write path is a proposal to an
  existing reviewed store. Settled on 2026-09-19 by the engineering lead, under the instruction to
  decide open items; it changes no behaviour, and it is reversible only by building that store, which
  would be a new row.
- **Enabling is the owner's step, not this document's.** Nothing here sets the flag.
