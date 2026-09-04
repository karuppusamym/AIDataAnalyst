# Session Addendum -- 2026-09-04 -- Generic stale-row reaper (P2-06)

> **Purpose.** New tracker row and evidence for the 2026-09-04 P2-06 fix
> (single generic reaper that sweeps stale rows across artifact types),
> staged here rather than merged into `03-tracker.md` directly for the
> same reason `10-session-2026-09-04-auto-enqueue.md` and
> `11-session-2026-09-04-governance-unify.md` were: `03-tracker.md` has
> extensive uncommitted concurrent edits and a landing here would
> conflict. Fold this row into `03-tracker.md` on the next tracker
> rebase; the file citations, event names, and test names below are what
> belongs in the row's evidence column.

## Rows to add / update

### RP-1 -- Generic stale-row reaper across governance artifact types (P2)

Section: **G. Data retention and housekeeping** (follows PR-2's value-
profile artifact purge; also referenced from the P2-06 finding in
`04-end-to-end-audit-2026-08-30.md`).

**Problem.** The 2026-08-30 end-to-end audit found three parallel
accumulation problems, all with the same shape (a status-carrying row
that lands in a terminal state and is never cleaned up):

- `MetadataEnrichmentProposal` -- `PENDING_REVIEW` and `REJECTED` rows
  accumulate forever; no reaper existed.
- `AssetTermLink` -- rows become invisible once their `GlossaryTerm`
  is deprecated (see `semantic_inference.resolve_scoped_glossary_term`
  / N9's most-specific-wins), but the row itself is never deleted, so
  orphan links pile up.
- `AssetDescriptionDraft` -- `REJECTED` rows are retained as
  fingerprint anchors (per the class docstring) but never age out of
  active review-queue visibility. `DRAFT` rows likewise never expire
  if a drafter walks away.

**Fix (one generic reaper, data-driven rules).** New module
`src/aida/reaper_service.py` with a `RULES` registry -- one row per
reaping rule -- and a `run_reaper_pass` that runs every rule inside
its own SAVEPOINT (fault isolation), enforces a per-rule hard cap
(safety over throughput), and emits one `REAP_*` audit event per rule
that reaped >=1 row (bounded audit trail).

Registry (retention chosen so PENDING and REJECTED rows both age out
without breaking existing model contracts):

| Rule | Model | Retention | Action | New status | Rationale |
| --- | --- | --- | --- | --- | --- |
| `rejected_enrichment_proposals` | `MetadataEnrichmentProposal` | 90d | DELETE | -- | Terminal REJECTED; safe to delete once past reviewer-appeal window |
| `stale_pending_enrichment_proposals` | `MetadataEnrichmentProposal` | 365d | STATUS_FLIP | `EXPIRED` | Preserve audit trail: reviewer can see it existed and expired |
| `orphan_asset_term_links` | `AssetTermLink` | 0d | DELETE | -- | Term is fully DEPRECATED with a DEPRECATED version and the link predates deprecation; no read-path benefit to keeping |
| `rejected_description_drafts` | `AssetDescriptionDraft` | 180d | STATUS_FLIP | `REAPED` | Model contract retains rejected drafts as `text_fingerprint` anchors; soft flip ages them out of queues without losing the anchor |
| `stale_pending_description_drafts` | `AssetDescriptionDraft` | 60d | STATUS_FLIP | `EXPIRED` | Same rationale as stale pending proposals -- expire visibly, don't delete |

Per-rule `hard_cap` default 10,000. If a rule's candidate count
exceeds the cap it emits `REAPER_CAP_EXCEEDED` and reaps zero rows
for that rule this pass -- an operator triages upstream cause before
the reaper touches anything. Prevents a misused status column from
becoming a runaway deletion.

Explicit non-goals (per audit constraint):

- `APPROVED` versions of anything -- never touched.
- `RelationshipCandidate` -- PENDING/APPROVED/REJECTED history is
  decision-critical audit data.
- `GovernanceReview` -- audit trail.
- `Outbox` -- has its own separate retention concern
  (`outbox_max_attempts`).

**Files touched.**

- `src/aida/reaper_service.py` (NEW): rule dataclass, registry,
  `run_reaper_pass`, `run_reaper_scheduler_pass`,
  `parse_retention_overrides`.
- `src/aida/workflows/scheduler.py` (imports `run_reaper_scheduler_pass`;
  calls it in `run_scheduler_iteration` after
  `purge_expired_value_profile_artifacts`).
- `src/atlas/platform/config.py` (new `reaper_enabled: bool = True`,
  `reaper_sweep_interval_seconds: int = 86_400`, and
  `reaper_retention_overrides: str | None = None`).

**Env-var contract.**

- `AIDA_REAPER_ENABLED` (default `true`) -- ops kill switch; when
  false the pass is a no-op without stopping the rest of the
  scheduler.
- `AIDA_REAPER_SWEEP_INTERVAL_SECONDS` (default `86400`, bounded
  15 min - 7 d).
- `AIDA_REAPER_RETENTION_OVERRIDES` (optional) -- comma-separated
  `rule_name:days` (`"rejected_enrichment_proposals:30,orphan_asset_term_links:7"`);
  malformed entries and unknown rule names are dropped with a
  warning, never a raise.

**Emitted events.**

- Audit action per rule: `REAP_REJECTED_ENRICHMENT_PROPOSAL`,
  `REAP_STALE_PENDING_ENRICHMENT_PROPOSAL`,
  `REAP_ORPHAN_ASSET_TERM_LINK`,
  `REAP_REJECTED_DESCRIPTION_DRAFT`,
  `REAP_STALE_PENDING_DESCRIPTION_DRAFT`.
- Alert audit: `REAPER_CAP_EXCEEDED` (outcome=`FAILED`); its details
  carry `{rule, candidate_count, hard_cap}`.

**Tests.** `tests/test_reaper_service.py`:

- `test_rejected_enrichment_proposals_only_old_ones_reaped` --
  age-boundary check; only rows past retention are reaped, PENDING
  rows are untouched by the REJECTED rule.
- `test_stale_pending_enrichment_proposals_flip_not_deleted` --
  STATUS_FLIP preserves the row, sets status=EXPIRED.
- `test_orphan_asset_term_links_deleted` -- the audit finding's
  exact shape: DEPRECATED term + DEPRECATED version + 3 links, all
  3 links go.
- `test_active_term_links_not_touched` -- guard against a
  predicate slip silently deleting links to still-ACTIVE terms.
- `test_stale_pending_description_drafts_flip_and_young_stay` --
  60-day boundary for AssetDescriptionDraft DRAFT.
- `test_rejected_description_drafts_soft_flag_preserves_row` --
  the fingerprint anchor contract is preserved (row + text_fingerprint
  survive; status flips to REAPED).
- `test_reaper_disabled_by_config_is_noop` -- kill switch works.
- `test_hard_cap_exceeded_reaps_zero_and_emits_alert` -- REAPER_CAP_EXCEEDED
  audit and zero-row reap on cap breach (uses a tight hard_cap=2
  variant rule to avoid seeding 15k rows against sqlite; same code
  path).
- `test_audit_event_emitted_per_rule_that_reaped` -- one audit event
  per rule, not per row.
- `test_no_audit_when_a_rule_reaped_nothing` -- no audit noise on
  empty passes.
- `test_parse_retention_overrides_valid_and_invalid`,
  `test_parse_retention_overrides_empty_or_none` -- override
  parser robustness.
- `test_rules_registry_covers_expected_names`,
  `test_rules_registry_shape_invariants` -- registry-level guards
  so adding or removing a rule is a deliberate edit.

**Not covered (deliberately deferred).**

- No integration test against a live Postgres instance -- the whole
  suite runs on in-memory sqlite via the pattern in
  `test_profiling_exception_policy.py`; a Postgres-only edge case
  would need a compose-based test harness this repo does not yet
  have.
- No test asserts the scheduler wire-up actually fires
  `run_reaper_scheduler_pass` in `run_scheduler_iteration`: the
  scheduler's own test surface today is exercised via the
  `test_scheduler_*` suite's per-function tests rather than a
  full-iteration test, and adding one is scope beyond this fix.
- Deleting `MetadataEnrichmentProposal` leaves its paired
  `GovernanceReview` in place. That is intentional (the review is
  audit data), but the review's `object_id` will point at a now-
  missing proposal id. Existing readers of `GovernanceReview` do
  not follow this pointer -- the review carries its own status --
  so this is not a broken invariant, but a future audit view that
  joins reviews back to proposals should filter for existence.
