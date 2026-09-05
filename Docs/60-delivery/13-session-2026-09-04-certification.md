# Session Addendum -- 2026-09-04 -- Certification revoke, expiry warning, uniqueness (P2-08)

> **Purpose.** New tracker rows and evidence for the 2026-09-04 P2-08 fix
> covering three parallel gaps the 2026-08-30 end-to-end audit found on
> `AssetCertification`. Staged here rather than merged into
> `03-tracker.md` directly for the same reason
> `10-session-2026-09-04-auto-enqueue.md`,
> `11-session-2026-09-04-governance-unify.md`, and
> `12-session-2026-09-04-reaper.md` were: `03-tracker.md` has
> extensive uncommitted concurrent edits and a landing here would
> conflict. Fold these rows into `03-tracker.md` on the next tracker
> rebase; the file citations, event names, and test names below are what
> belongs in each row's evidence column.

## Rows to add / update

### CT-9 -- Manual `REVOKED` writer + endpoint (P2)

Section: **D. Governed catalog / stewardship** (extends CT-5's
`AssetCertification` lifecycle).

**Problem.** `AssetCertification.status = "REVOKED"` was defensively
handled by every reader --
`atlas/modules/catalog/service.py::_certification_state` (line 134),
`aida/asset_usage_decision.py::_CERTIFICATION_STATES` (line 56),
`_flag_from_certification` (line 110, `REVOKED -> BLOCKED`) -- but no
code anywhere in `src/aida` or `src/atlas` produced the value. A
compliance-triggered revocation could only be worked around by letting
the cert expire, which is neither immediate nor auditable as an
explicit action.

**Fix.**

- New endpoint `POST /v1/tables/{table_id}/certification/revoke`
  (`atlas/modules/catalog/router.py::revoke_table_certification`).
  Same write-role set as `certify_table_asset`
  (`CATALOG_BULK_ACTION_WRITE_ROLES`).
- Payload `{reason: str >=10 chars, column_id?: UUID}` --
  column-scoped when present, table-scoped otherwise; same
  `(table, asset_type, column?)` addressing shape as the certify path.
- Maker-checker on by default
  (`certification_revoke_enforce_maker_checker`): the principal who
  granted the cert cannot revoke it (409
  `same_principal_cannot_revoke_own_certification`). Off-switchable
  for single-steward deployments.
- Flips the currently-ACTIVE row to `status="REVOKED"` and stamps
  three new nullable columns atomically:
  `revoked_at` / `revoked_by` / `revocation_reason`.
  Rationale / certified_by / expires_at are left as-is so
  certification history is never mutated (same evidence-preservation
  rule DQ-3's EXPIRED path already follows,
  `quality_coupling.py::expire_sustained_incident_certifications`
  around line 320).
- Emits `catalog.asset.certification_revoked.v1` outbox event with the
  full payload and a `CERTIFICATION_REVOKED` audit row.

**Evidence.**

- Endpoint: `src/atlas/modules/catalog/router.py::revoke_table_certification`.
- Schema: `src/aida/schemas.py::CertificationRevokeRequest` and
  `AssetCertificationRead` (three added `revoked_*` fields).
- Model: `src/aida/models.py::AssetCertification` -- three new
  nullable columns (`revoked_at`, `revoked_by`, `revocation_reason`).
- Migration: `migrations/versions/f0c8a2e91b74_p2_08_certification_revoke_and_active_.py`.
- Tests: `tests/test_certification_revoke_and_expiry.py::`
  `test_revoke_by_different_principal_succeeds`,
  `test_revoke_by_same_principal_is_refused`,
  `test_revoke_nonexistent_certification_returns_404`,
  `test_revoke_column_leaves_table_certification_untouched`,
  `test_maker_checker_can_be_disabled_via_settings`,
  `test_revoked_status_has_a_single_writer_in_the_codebase`.
- UI: `ui-next/src/lib/api.ts::revokeAssetCertification`
  (+ `CertificationRevokeRequest` / three `revoked_*` fields on
  `AssetCertificationRead` in `ui-next/src/lib/types.ts`).
  UI wire-up in `CatalogTable.tsx` is a documented follow-up on this
  same tracker row -- api.ts is landed now so the UI slice is a
  copy-and-paste against a typed call.

### CT-10 -- N-day-before certification expiry warning (P2)

Section: **D. Governed catalog / stewardship** (extends CT-5).

**Problem.** A certification that expires next Monday starts reading
back as `EXPIRED` from
`atlas/modules/catalog/service.py::_certification_state` the moment
its `expires_at` passes; the `certified_by` steward learned about the
transition only when a downstream policy decision degraded on a
freshly-CERTIFIED table. No lead-time warning existed.

**Fix.**

- New sweep `warn_upcoming_certification_expiries(session, *, now,
  warn_days)` in `src/aida/certification_expiry_warning.py`.
- Finds every `AssetCertification` where `status="ACTIVE"` and
  `now < expires_at < now + warn_days` and whose
  `expiry_warning_emitted_at` is either NULL or older than
  `warn_days * 2` (idempotency cooldown -- doubled so a slow scheduler
  cadence never re-warns the same row twice inside one window).
- For each: stamps `expiry_warning_emitted_at`, writes a
  `CERTIFICATION_EXPIRY_WARNING_SENT` audit row addressed to
  `certified_by`, and emits a
  `catalog.asset.certification_expiry_warning.v1` outbox event whose
  payload carries `notify_principal = certified_by`, `expires_at`,
  and `days_until_expiry`.
- Scheduler-facing entry `run_certification_expiry_warning_pass`
  registered in `src/aida/workflows/scheduler.py::run_scheduler_iteration`
  alongside the reaper pass -- same rate-limited-inside-the-callee
  shape (`certification_expiry_warn_interval_seconds`, default
  `86_400`).

**Evidence.**

- Module: `src/aida/certification_expiry_warning.py`.
- Model column: `AssetCertification.expiry_warning_emitted_at` (nullable,
  in `src/aida/models.py` + the same migration as CT-9).
- Scheduler wiring: `src/aida/workflows/scheduler.py` --
  import + one call in `run_scheduler_iteration` right after the
  reaper pass.
- Tests: `tests/test_certification_revoke_and_expiry.py::`
  `test_expiry_warning_only_fires_inside_window`,
  `test_expiry_warning_is_idempotent_inside_cooldown`.

### CT-11 -- DB uniqueness backstop on the ACTIVE tuple (P2)

Section: **D. Governed catalog / stewardship** (extends CT-5).

**Problem.** The `certify_table_asset` and
`catalog_bulk_actions.apply_certify_item` paths both use a
read-modify-write to supersede the prior ACTIVE row before inserting a
new one. Between the read and the insert nothing holds a lock on the
tuple, so two concurrent certify calls can both read "no prior
ACTIVE", both insert, and leave two ACTIVE rows behind for the same
`(table_id, asset_type, column_id?, organization_id)` tuple.
`current_asset_certification` then picks whichever the ORDER BY sees
first -- non-deterministically -- and the loser's evidence is
orphaned.

**Fix.**

- Partial unique index `ix_asset_certification_active_tuple` on
  `(table_id, asset_type, COALESCE(column_id, '00000000-0000-0000-0000-000000000000'), organization_id) WHERE status = 'ACTIVE'`.
  `column_id` participates via COALESCE to the zero-UUID sentinel
  because PostgreSQL treats NULL as *distinct* inside a unique
  index -- two concurrent table-level certifies (both
  `column_id IS NULL`) would otherwise slip past.
- Declared **both** on `AssetCertification.__table_args__` (for
  ORM/DDL parity) and in the alembic migration (server-side, via
  `op.execute` for the `WHERE` clause).
- `certify_table_asset` now catches `IntegrityError` on its `session.flush()`
  and returns HTTP 409 `certification_already_active_on_this_tuple`.

**Evidence.**

- Model: `src/aida/models.py::AssetCertification.__table_args__` --
  the `Index(..., unique=True, postgresql_where=..., sqlite_where=...)`.
- Migration: `migrations/versions/f0c8a2e91b74_p2_08_certification_revoke_and_active_.py`
  -- `op.execute` block that ships the identical `CREATE UNIQUE INDEX ... WHERE status = 'ACTIVE'`.
- Endpoint translation: `src/atlas/modules/catalog/router.py::certify_table_asset` --
  new `try/except IntegrityError` around `session.flush()` returning
  HTTP 409.
- Tests: `tests/test_certification_revoke_and_expiry.py::`
  `test_active_tuple_uniqueness_refuses_a_second_active_row`,
  `test_active_tuple_uniqueness_allows_superseded_row_alongside_new_active`.

## Config additions (`src/atlas/platform/config.py`)

Grouped alongside `quality_certification_expiry_enabled` /
`quality_certification_sustained_threshold`:

- `certification_expiry_warn_days: int = Field(default=7, ge=1, le=90)`
  -- horizon the warning job looks ahead by; also
  (doubled) the idempotency cooldown.
- `certification_expiry_warn_interval_seconds: int = Field(default=86_400, ge=900, le=604_800)`
  -- scheduler cadence for the pass.
- `certification_revoke_enforce_maker_checker: bool = True`
  -- default-on; off-switchable for single-steward
  deployments where the maker-checker rule would deadlock every
  revoke.

Environment override prefix `AIDA_` per `Settings.model_config`.

## Follow-ups not in this pass

- CatalogTable.tsx button + reason dialog for the revoke endpoint (api
  and types are landed; the UI slice is a documented follow-up under
  CT-9).
- Notification-channel wiring for
  `catalog.asset.certification_expiry_warning.v1`: the outbox event is
  emitted and consumers can subscribe today, but there is no default
  routing rule that binds the event to a delivery channel the way DQ-1
  does for quality incidents. Warrants a peer of
  `ensure_default_unowned_backlog_notification_rule` in a later slice.
