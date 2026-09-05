# Session Addendum -- 2026-09-04 -- Ownership re-affirmation + expiry + leaver flip (P2-07)

> **Purpose.** New tracker rows and evidence for the 2026-09-04 P2-07 fix
> closing three OwnershipAssignment audit gaps: no `expires_at` (ownership
> claimed once and never re-affirmed), no expiry-warning job, and no
> identity-merge/delete -> REASSIGNED path (dangling ownership on user
> delete). Staged here rather than merged into `03-tracker.md` directly
> for the same reason `10-14-session-2026-09-04-*.md` were: `03-tracker.md`
> has extensive uncommitted concurrent edits. Fold these rows into
> `03-tracker.md` on the next tracker rebase.

## Design decision (ratified with the audit lead)

- **Default re-affirm cadence 180 days** (`AIDA_OWNERSHIP_REAFFIRM_DAYS=180`).
  Per-org override via a future `AutoReaffirmPolicy` row (separate ADR).
  Rationale: 90d is too aggressive for slow-moving datasets, 365d is too
  long for a bank; 180d matches typical steward review-of-scope cadences.

## Rows to add

### OW-3 -- OwnershipAssignment `expires_at` + `/reaffirm` endpoints (P2-07)

Section: **G. Stewardship / ownership**.

**Problem.** `OwnershipAssignment` had no `expires_at`, `reaffirmed_at`,
`reaffirmed_by`, or `expiry_warning_emitted_at`. An ownership assertion
made in 2024 was still ACTIVE and still read as "the owner" for every
policy decision in 2026 -- no periodic re-attestation, no forced review.

**Fix.**

- Migration `a1b2c3d4e5f6_p2_07_ownership_reaffirm_expiry.py` adds four
  nullable columns (`expires_at`, `expiry_warning_emitted_at`,
  `reaffirmed_at`, `reaffirmed_by`) plus two composite indexes
  (`ix_ownership_assignment_status_expires_at` for the warning sweep,
  `ix_ownership_assignment_owner_principal_status` for the identity-
  lifecycle handler).
- Model changes on `OwnershipAssignment` in `src/aida/models.py` -- four
  new fields plus a status-value docstring naming the new `LAPSED` and
  `LAPSED_LEAVER` values and the sole writer of each.
- `stewardship_service.apply_bulk_operation` (ASSIGN_OWNERSHIP branch) now
  writes `expires_at = now + ownership_reaffirm_days` on every new or
  reactivated row, and clears any prior warning stamp on reactivation.
- Two new endpoints in `stewardship_api.py`:
  - `POST /v1/ownership-assignments/{id}/reaffirm` -- owner-or-admin,
    extends `expires_at`, records `OWNERSHIP_ASSIGNMENT_REAFFIRMED` audit
    + `ownership.assignment.reaffirmed.v1` outbox.
  - `POST /v1/ownership-assignments/bulk-reaffirm` -- up to 100 ids,
    per-item SAVEPOINT (`session.begin_nested()`), same partial-success
    shape as `bulk_decide_relationship_candidates`.
- Config in `src/atlas/platform/config.py`: `ownership_reaffirm_days`
  (Field ge=30, le=730, default 180), plus the sweep tunables in OW-4
  and the safety switch in OW-5.

**Evidence.**

- `src/aida/models.py:2077-2110` -- new columns + status-value docstring.
- `src/aida/schemas.py` -- `OwnershipAssignmentRead` extended;
  `OwnershipAssignmentBulkReaffirmRequest`/`Result`/`ItemResult` added.
- `src/aida/stewardship_service.py:107-165` -- ASSIGN_OWNERSHIP branch
  sets `expires_at` on new + reactivated rows.
- `src/aida/stewardship_api.py:773-1000` (`_OWNERSHIP_ADMIN_ROLES`,
  `_reaffirm_one`, `_caller_may_reaffirm`, `reaffirm_ownership_assignment`,
  `bulk_reaffirm_ownership_assignments`).
- Migration: `migrations/versions/a1b2c3d4e5f6_p2_07_ownership_reaffirm_expiry.py`.

### OW-4 -- Expiry-warning sweep + expire-lapsed sweep (P2-07)

Section: **G. Stewardship / ownership**.

**Problem.** No job warned owners that their ownership was about to expire,
and nothing flipped a still-un-re-affirmed ACTIVE row to LAPSED after its
grace window elapsed.

**Fix.**

- New module `src/aida/ownership_expiry_warning.py`:
  - `warn_upcoming_ownership_expiries(session, now, warn_days=14)` --
    ACTIVE rows with `now < expires_at < now + warn_days` and
    `expiry_warning_emitted_at IS NULL OR < now - warn_days*2`, one
    warning per row per cycle. Legacy rows with `expires_at IS NULL` are
    deliberately out of scope.
  - `expire_lapsed_ownership_assignments(session, now, grace_days=30)` --
    ACTIVE rows with `expires_at + grace_days < now` flipped to `LAPSED`;
    per row, if it was the subject's last ACTIVE owner and the subject is
    a TABLE, an `UnownedAssetEscalation(status="PENDING")` is staged so
    `route_unowned_asset_backlog` picks it up on its next pass. Emits
    `OWNERSHIP_ASSIGNMENT_LAPSED` audit + `ownership.assignment.lapsed.v1`
    outbox.
  - `run_ownership_expiry_pass(settings, now)` -- rate-limited scheduler
    entry, same shape as `run_certification_expiry_warning_pass`.
- Config: `ownership_expiry_warn_days` (default 14, ge=1 le=90),
  `ownership_expiry_warn_interval_seconds` (default 86_400),
  `ownership_expiry_grace_days` (default 30).
- Scheduler wire-up in `src/aida/workflows/scheduler.py` at the same
  loop point as `run_certification_expiry_warning_pass`.

**Evidence.**

- `src/aida/ownership_expiry_warning.py` -- whole file.
- `src/aida/workflows/scheduler.py:30-31, 633-638` -- import + call.
- Tests: `tests/test_ownership_expiry_and_leaver.py` covers the 5d/20d/
  expired/legacy quadruple, the cooldown, and the expire-with-last-owner
  routing.

### OW-5 -- Identity-lifecycle handler + emission (P2-07)

Section: **G. Stewardship / ownership** (peer to GL-7).

**Problem.** Deleting a principal in the identity service left every
`OwnershipAssignment` row with that principal's id ACTIVE -- silent
stale-ownership after a leaver. Merging a principal into a successor
left the same.

**Fix.**

- New module `src/aida/ownership_principal_lifecycle.py`:
  - `handle_principal_deleted` flips every ACTIVE assignment owned by the
    deleted principal to `LAPSED_LEAVER`, records audit
    `OWNERSHIP_AUTO_REASSIGNED_LEAVER` + outbox
    `ownership.assignment.lapsed_leaver.v1`, and stages an
    `UnownedAssetEscalation` per subject that just lost its last owner.
  - `handle_principal_merged` redirects every ACTIVE assignment to the
    successor (`owner_principal <- into`, `assignment_kind = "MERGED"`);
    if the successor already covers the exact tuple, the losing row is
    LAPSED_LEAVER instead of duplicating (respects the unique constraint).
- New emission module `src/aida/identity_events.py`:
  - `emit_principal_deleted` / `emit_principal_merged` -- record the
    `identity.principal.deleted.v1` / `identity.principal.merged.v1`
    event AND call the lifecycle handler in the same transaction.
- Config: `ownership_leaver_auto_reassign: bool = True` (safety switch --
  set false for tightly-controlled orgs that require GL-7 REASSIGN_LEAVER
  operator flow for every ownership flip).

**Evidence.**

- `src/aida/ownership_principal_lifecycle.py` -- whole file.
- `src/aida/identity_events.py` -- whole file.
- Tests: leaver-deleted, leaver-merged, merged-with-successor-clash,
  and `ownership_leaver_auto_reassign=false` no-op cases.

### OW-6 -- UI banner + api.ts wrapper (P2-07)

Section: **G. Stewardship / ownership**.

**Problem.** No UI surface for an owner to see their pending expirations,
or to reaffirm one without database access.

**Fix.**

- `ui-next/src/lib/api.ts` (appended): `reaffirmOwnershipAssignment`,
  `bulkReaffirmOwnershipAssignments`, `fetchOwnershipAssignments`, plus
  the `OwnershipAssignmentRead`/`BulkReaffirmResult` types.
- New screen `ui-next/src/screens/OwnershipExpiryBannerScreen.tsx` --
  fetches the org's ACTIVE assignments, client-side filters to the
  current principal's rows expiring inside `WARN_DAYS` (14, matching the
  server default), and shows a per-row Reaffirm button plus a
  Reaffirm-all button that hits the bulk endpoint. Deliberately dedicated
  rather than folded into `StewardshipScreen.tsx` (which does not today
  list per-row OwnershipAssignments).

**Documented gaps left for a follow-up.**

- StewardshipScreen.tsx does not yet embed the banner; a router or Home
  shell embed is a small follow-up (`<OwnershipExpiryBannerScreen />`).
- Per-org override of `ownership_reaffirm_days` via `AutoReaffirmPolicy`
  is out of scope here -- separate ADR.
