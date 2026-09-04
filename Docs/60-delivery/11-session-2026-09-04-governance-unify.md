# Session Addendum -- 2026-09-04 -- Bulk-action governance unification (P0-02)

> **Purpose.** New tracker row and evidence for the 2026-09-04 P0-02 fix
> (close the maker-checker bypass on the catalog router's bulk-ownership
> and bulk-certification endpoints), staged here rather than merged into
> `03-tracker.md` directly for the same reason `10-session-2026-09-04-auto-enqueue.md`
> was: `03-tracker.md` has extensive uncommitted concurrent edits and a
> landing here would conflict. Fold this row into `03-tracker.md` on the
> next tracker rebase; the file citations, event names and test names
> below are what belongs in the row's evidence column.

## Rows to add / update

### GV-2 -- Bulk-action maker-checker unification via governance threshold (P0)

Section: **F. Policy and governance** (extends the PG- series; also
referenced from the P0-02 finding in
`04-end-to-end-audit-2026-08-30.md`).

**Problem.** The 2026-08-30 end-to-end audit found three parallel
creation paths for ownership assignment and four for certification, each
enforcing different governance:

- `src/aida/stewardship_api.py::_create_bulk_operation` (line 175) --
  governed. Routes `CERTIFY_ASSET` and ownership rules through
  `BulkStewardshipOperation` + `GovernanceReview` with maker != checker
  enforced.
- `src/atlas/modules/catalog/router.py::bulk_assign_ownership` (line
  ~715) and `::bulk_certify_tables` (line ~789) -- **BYPASS the
  maker-checker contract.** Wrote straight to ACTIVE under only RBAC.
- `src/aida/playbooks.py::_apply_playbook_item` OWN + CERTIFY branches
  -- scheduled auto-apply, no human at all, no per-subject audit
  trail.

Impact: a `DataSteward` who is authorized to *request* a bulk action
could bypass the entire maker != checker discipline just by picking the
catalog bulk endpoint instead of the governed one. The audit called this
the largest remaining governance gap.

**Fix (threshold-based unification).** Two settings knobs decide, per
call, whether the request may direct-write to ACTIVE or MUST route
through `BulkStewardshipOperation` + `GovernanceReview`:

- `AIDA_BULK_GOVERNANCE_THRESHOLD` (default `10`) -- item count above
  which the operation MUST go through review.
- `AIDA_BULK_GOVERNANCE_ROLES_REQUIRING_REVIEW` (default
  `["DataSteward"]`) -- roles that always route through review
  regardless of count. Higher-privileged admins (PlatformAdmin,
  MetadataAdmin, DataAdmin) are deliberately absent from this list so
  they can still direct-write within the count threshold, matching the
  "single deliberate action by an authorized user" comment on the
  single-item endpoints. **The RBAC allowed-writers list
  (`CATALOG_BULK_ACTION_WRITE_ROLES`) is unchanged** -- this filter only
  decides which of the already-authorized callers may skip review.

The bridging function `_route_bulk_through_governance` calls into the
SAME `stewardship_api._create_bulk_operation` helper the governed
endpoint uses, so the router now shares one code path for creating a
`BulkStewardshipOperation` + its paired `GovernanceReview`; any future
change to how a review is minted lands there for free.

**Response shape.** Backward-compatible:

- Direct-write path (200): unchanged `CatalogBulkActionRunRead` shape.
- Review-routed path (202, new): `BulkStewardshipOperationRead` with a
  `Location:` header pointing at the created governance review and an
  `X-Bulk-Route-Reason` header naming why routing happened
  (`role_requires_review` / `count_above_threshold`). The 202 response
  is declared in the OpenAPI `responses` map on both endpoints so
  clients see both shapes as typed additions.

**Direct-write audit signal.** Every direct-write on the two endpoints
now emits `catalog.bulk_action.direct_write.v1` naming operator,
operation type, subject count, resolved subject ids, the resolved
reason, and the caller's roles -- so a compliance query can surface
every bypass at grep-time instead of reconstructing it after the fact
from `OwnershipAssignment`/`AssetCertification` rows.

**Playbook audit signal.** Playbook auto-apply keeps its direct-write
behaviour by design (the human is the rule author, not the applier),
but `_apply_one_item`'s OWN and CERTIFY branches now emit a per-subject
`PLAYBOOK_AUTO_APPLY` audit event carrying `playbook_id`,
`playbook_kind`, `rule_id`, and the resolved owner/certification
parameters. An admin can now query "which playbook, if any, set this
table's owner?" without correlating a run id back to its list of
results.

**Deliberate non-scope.** The single-item `certify_table_asset` and
single ownership assignment endpoints are unchanged (single deliberate
act by an authorized user, out of scope for the P0-02 bypass). The
optional `Playbook.require_review=True` DRY_RUN escape hatch is
deferred -- a schema/migration change, not straightforward alongside
concurrent ST-05 model splits; noted here for follow-up.

**Files changed:**

| File | Lines | Change |
| --- | --- | --- |
| `src/atlas/platform/config.py` | ~122 | Add `bulk_governance_threshold: int = Field(default=10, ge=0, le=10_000)` and `bulk_governance_roles_requiring_review: list[str] = Field(default_factory=lambda: ["DataSteward"])` to `Settings`. |
| `src/atlas/modules/catalog/router.py` | ~715, ~789, plus helper block ~700 | `bulk_assign_ownership` and `bulk_certify_tables` consult the new settings; on review-required they call `_route_bulk_through_governance` (bridging into `stewardship_api._create_bulk_operation`) and return HTTP 202 with `BulkStewardshipOperationRead`; on direct-write they emit `catalog.bulk_action.direct_write.v1` audit alongside the existing `CatalogBulkActionRun`. `responses={202: BulkStewardshipOperationRead}` added to both endpoints so the OpenAPI spec keeps typed additions. |
| `src/aida/playbooks.py` | 187-249 | `_apply_one_item` gains a `context: SecurityContext` kw-only arg and emits per-subject `PLAYBOOK_AUTO_APPLY` audit for OWN and CERTIFY branches. `_auto_apply` caller updated to thread `context=` through. |
| `tests/test_bulk_governance_threshold.py` | new | Seven tests: within-threshold direct-write with audit; count > threshold routes through review; DataSteward at any count routes through review; bulk_certify same threshold gate; PlatformAdmin bulk_certify direct-write within threshold; config override respects a higher threshold; playbook OWN emits per-subject audit. |
| `tests/test_catalog_bulk_actions_endpoints.py` | 216-241, 334-345, 364-374, 430-440 | Add `_platform_admin_context()` + `_high_threshold_settings()` helpers; update the three existing tests that use `bulk_assign_ownership` / `bulk_certify_tables` (500-item truncation, 40-item filter, 3-item certify SAVEPOINT) to use `PlatformAdmin` role -- `DataSteward` now correctly routes through review by default, and the CT-1 tests that prove direct-write batch mechanics (cap-at-500, SAVEPOINT isolation) still need to exercise the direct path. |

**Evidence:**

- Verification: `python3 -m py_compile` clean on every changed .py file
  (`src/atlas/platform/config.py`, `src/atlas/modules/catalog/router.py`,
  `src/aida/playbooks.py`).
- Grep confirms `_create_bulk_operation` production call sites are the
  three existing sites in `stewardship_api.py` (lines 356, 580, 716)
  plus the new bridging call at
  `atlas/modules/catalog/router.py:794` -- no duplicated review-object
  creation.
- Grep confirms every `OwnershipAssignment(` insertion now flows either
  through the governed `apply_bulk_operation` /
  `_reassign_leaver_row` in `stewardship_service.py` or through
  `apply_own_item` in `catalog/service.py` (called from the
  audited-and-threshold-gated direct-write path); no other route
  bypasses either audit or review.
- pytest not run (Python 3.14 unavailable on this sandbox; system
  Python 3.10 cannot parse PEP 695 syntax used elsewhere in the
  codebase); the test module parses cleanly (`ast.parse`) and shares
  its ORM-seeded, real-engine setup with the existing
  `tests/test_catalog_bulk_actions_endpoints.py` (CT-1) and
  `tests/test_bulk_governance_decisions.py` (PG-3) suites -- the two
  suites this row's fix borrows its test discipline from.
- Reachability gate: no new ALLOWLIST entry needed -- the new endpoint
  response variants are additions to two endpoints already mounted on
  the `aida.main` FastAPI app.
- OpenAPI: existing 200 shape unchanged on both endpoints; the 202
  addition is a declared additional response, matching the discipline
  of `stewardship_api`'s own 202-returning endpoints.

**Config surface:**

- `AIDA_BULK_GOVERNANCE_THRESHOLD=10` (default)
- `AIDA_BULK_GOVERNANCE_ROLES_REQUIRING_REVIEW=["DataSteward"]`
  (default; JSON list)

Both are `env_prefix="AIDA_"` fields on `Settings`, the same discipline
every other operational knob follows.
