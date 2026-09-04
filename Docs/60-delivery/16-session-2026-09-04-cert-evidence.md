# Session Addendum -- 2026-09-04 -- Certification structured evidence (P3-09)

> **Purpose.** New tracker rows and evidence for the 2026-09-04 P3-09 fix
> that replaces the free-text `AssetCertification.rationale` field's
> monopoly with a machine-consumable structured evidence blob captured at
> certify time. Staged here rather than merged into `03-tracker.md`
> directly for the same reason `10-15-session-2026-09-04-*.md` were:
> `03-tracker.md` has extensive uncommitted concurrent edits. Fold these
> rows into `03-tracker.md` on the next tracker rebase.

## Design decision (ratified with the audit lead)

- **Do NOT remove `rationale`.** The free-text field stays populated in
  parallel on every new write for backward-compatible human readability.
  The new `evidence` JSON column is the machine-consumable side.
- **`evidence` stays nullable indefinitely.** Legacy pre-P3-09 rows keep
  `evidence IS NULL` and are never NOT-NULL-tightened by a follow-up
  migration, because certification history is retained audit evidence and
  is never mutated by a schema change. Readers project a null summary
  ("legacy row, no structured evidence") rather than crashing.
- **Best-effort backfill CLI, config-gated OFF at startup.** The
  reconstructed snapshot uses *today's* description version / ownership /
  quality / glossary state (the true state at certify time is gone), so
  it is tagged `backfilled=True` inside the JSON. Operators run
  `scripts/backfill_certification_evidence.py` on their own schedule; the
  startup toggle `AIDA_CERTIFICATION_EVIDENCE_BACKFILL_ON_STARTUP` is
  reserved for small-estate dev deployments and defaults to `false`.

## Rows to add

### CT-12 -- `AssetCertification.evidence` column (P3-09 schema)

Section: **C. Catalog / certification**.

**Problem.** `AssetCertification` had only `rationale: String(2000)` --
a free-text field. Nothing linked the certification to the specific DQ
checks that passed, the description version that was current, or the
ownership row that was active at certify time. That makes it impossible
to answer "what did the certifier actually vouch for?" and impossible to
build a future `revoke-on-evidence-change` job (auto-revoke when the
description that was current at certify time is superseded, or when the
owner assignment that was ACTIVE at certify time lapses).

**Fix.**
1. Add `AssetCertification.evidence: JSON` (nullable). Alembic migration
   `d5b2e4f7a9c1_p3_09_certification_structured_evidence.py` revises
   `a1b2c3d4e5f6` (P2-07 head that already merged in P2-08).
2. Pydantic `CertificationEvidence` in `src/aida/schemas.py` validates
   the shape:
   `{description_version_id, ownership_assignment_ids, quality_snapshot,
   glossary_term_ids, supporting_dq_check_ids, certifier_notes}` plus a
   `schema_version` and a `backfilled` sentinel.
3. `AssetCertificationRead.evidence` gains `CertificationEvidence | None`
   (nullable so legacy rows still project).

**Files touched.** `src/aida/models.py` (add column), `src/aida/schemas.py`
(pydantic models + `AssetCertificationRead.evidence`), migration file.

### CT-13 -- `compute_certification_evidence` helper + wiring (P3-09 helper)

Section: **C. Catalog / certification**.

**Problem.** Four code paths create an `AssetCertification` row: the
single-certify endpoint (`certify_table_asset`), the direct-write bulk
endpoint (`bulk_certify_tables` via `apply_certify_item`), the reviewed-
bulk path (`stewardship_service` `CERTIFY_ASSET` branch), and the
playbook auto-apply CERTIFY (`playbooks._apply_one_item`). Without a
shared helper, evidence composition would drift.

**Fix.** New `src/aida/certification_evidence.py::compute_certification_evidence(session, table_id, *, organization_id, now, certifier_notes)`
runs one async composition (approved doc version / ACTIVE owners /
quality snapshot / approved-term ids) and returns a JSON-serialisable
dict. Each of the four call sites computes the blob once per subject
and passes it into the model insert. `apply_certify_item` gains a new
`evidence: dict | None = None` kwarg (default preserves single-line
unit-test callers). `_asset_certification_read` projects
`certification.evidence` into the API response.

**Files touched.** `src/aida/certification_evidence.py` (new),
`src/atlas/modules/catalog/router.py` (single + bulk + read model),
`src/atlas/modules/catalog/service.py` (`apply_certify_item` kwarg),
`src/aida/stewardship_service.py` (reviewed-bulk branch),
`src/aida/playbooks.py` (CERTIFY branch).

### CT-14 -- Catalog surface + UI tooltip (P3-09 read side)

Section: **UX-12 / catalog composed read model**.

**Problem.** `CatalogRowRead` exposed the certification cell as an
opaque `CERTIFIED | EXPIRED | NONE | REVOKED` string with no visibility
into what the certifier vouched for.

**Fix.**
1. `_certification_state` in `atlas/modules/catalog/service.py` returns
   `(state, expires_at, evidence_summary)`; the composer passes the
   summary into `CatalogRowRead.certification_evidence_summary`.
2. Pydantic `CertificationEvidenceSummary` in `src/aida/schemas.py` folds
   the JSON blob into small counts:
   `{description_version_id, active_owner_count, open_incident_count_at_certify,
   glossary_term_count, backfilled}`. Null when the current cert is
   legacy or when the row is not CERTIFIED.
3. `ui-next/src/lib/ui-types.ts` mirrors the new field; the fixture and
   test factory populate it (nullable-null on legacy / non-CERTIFIED).
4. `ui-next/src/components/CatalogTable.tsx`: the certification pill
   gains a hover `title` sourced from `certEvidenceTitle(row)`, rendering
   "Based on: description v..., N owners, K open incidents at certify,
   M glossary terms" (with " (backfilled)" appended when
   `summary.backfilled` is true). Legacy / non-CERTIFIED rows show no
   tooltip (title is `undefined`).

**Files touched.** `src/atlas/modules/catalog/service.py`,
`src/aida/schemas.py`, `ui-next/src/lib/ui-types.ts`,
`ui-next/src/lib/fixtures.ts`, `ui-next/src/components/CatalogTable.tsx`,
`ui-next/src/components/CatalogTable.test.tsx`.

### CT-15 -- Backfill CLI (P3-09 legacy data)

Section: **C. Catalog / certification** (operations).

**Problem.** Pre-P3-09 rows carry `evidence IS NULL` and cannot answer
"what did the certifier vouch for?" until backfilled.

**Fix.** `backfill_certification_evidence_v1(session)` in
`certification_evidence.py` walks ACTIVE table-level rows with
`evidence IS NULL`, computes a best-effort snapshot from today's state,
and stamps `backfilled=True` (plus `backfilled_at`). Idempotent -- the
`evidence IS NULL` filter is the write guard, second runs are no-ops on
already-populated rows. Wrapped by
`scripts/backfill_certification_evidence.py` (with `--dry-run`).
Config-gated by
`Settings.certification_evidence_backfill_on_startup`
(env `AIDA_CERTIFICATION_EVIDENCE_BACKFILL_ON_STARTUP`, default `false`).

**Files touched.** `src/aida/certification_evidence.py` (backfill helper),
`scripts/backfill_certification_evidence.py` (new),
`src/atlas/platform/config.py` (settings toggle).

## Test coverage

`tests/test_certification_structured_evidence.py` -- exercises against
an in-memory SQLite (same posture as `test_certification_revoke_and_expiry.py`):

- `compute_certification_evidence` returns the correct shape on a
  fixture table (1 approved doc version, 2 owners, 0 open incidents, 3
  approved terms, 1 completed profile).
- **Path 1** (single-certify) via `certify_table_asset` -- `evidence`
  populated on the returned model and on the persisted row; `rationale`
  is populated in parallel.
- **Path 2** (direct-write bulk) via `apply_certify_item` -- evidence
  round-trips into the persisted row.
- **Path 3** (reviewed-bulk) via `apply_bulk_operation` CERTIFY_ASSET.
- **Path 4** (playbook auto-apply) via `_apply_one_item` CERTIFY.
- `_certification_state` returns a non-null `evidence_summary` when the
  cert has evidence; null when the row is legacy (`evidence IS NULL`).
- Backfill: legacy NULL row is populated with `backfilled=True`; second
  run is a no-op.
- `summarize_evidence(None)` returns `None`; a rich blob folds to the
  expected counts.

## Verification

- `python3 -m py_compile` clean on every changed .py file.
- Grep confirms every `AssetCertification(` construction in `src/`
  passes `evidence=...`:
  `src/aida/stewardship_service.py:241`, `src/atlas/modules/catalog/router.py:343`,
  `src/atlas/modules/catalog/service.py:530` (`apply_certify_item` --
  called with `evidence=` from both `router.py:1234` and
  `playbooks.py:281`). Zero uncovered call sites.
- `CatalogRowRead.certification_evidence_summary` is nullable -- no
  breaking change to existing clients.
- Reachability: `certification_evidence.py` is imported by four modules
  already reachable from `aida.main`.

## Gaps deliberately left

- `supporting_dq_check_ids` is written as `[]` today. Wiring specific
  DQ rule outcomes (custom rule packs, external signals) into the
  evidence at certify time is a follow-up; the field is present in the
  shape so downstream readers do not have to grow a new optional key
  later.
- Backfill snapshots today's state, not the historical state at certify
  time (there is no as-of query for description version / ownership /
  quality / glossary), and stamps `backfilled=True` so a downstream
  reader can distinguish reconstructed evidence from an as-of-certify
  snapshot.
- Column-level certifications skip the backfill helper (the composition
  is currently table-scoped). New column-level certifications still write
  evidence at certify time via the shared helper.
- The revoke path (P2-08) intentionally does not touch `evidence` --
  revocation is orthogonal to the captured attestation.
