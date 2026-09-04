"""P2-06: single generic reaper that sweeps stale rows across artifact types.

The 2026-08-30 audit found three parallel accumulation problems, each with
the same shape (a status-carrying row that lands in a terminal state and is
never cleaned up):

* ``MetadataEnrichmentProposal`` rows in ``PENDING_REVIEW`` and ``REJECTED``
  accumulate forever -- no reaper.
* ``AssetTermLink`` rows become invisible when their ``GlossaryTerm`` is
  deprecated (see ``glossary_owner_routing`` / N9's most-specific-wins
  resolution), but the row itself is never deleted -- orphan links pile up.
* ``AssetDescriptionDraft`` rows in ``REJECTED`` are retained as negative-
  knowledge fingerprint anchors (so an identical low-value draft is not
  regenerated on the next run -- see the model's own docstring), and there
  is no upper bound on how long they stay in review-queue visibility.

Each of those is a per-model reaping rule, not a per-model service: the
shape is identical -- pick rows matching a predicate, either DELETE or
STATUS_FLIP them under an audit trail, bounded per-pass -- so one generic
sweeper covers all of them, and every new artifact type gets its retention
by adding one row to ``RULES`` instead of writing another service.

Called from ``aida.workflows.scheduler.run_scheduler_iteration`` on
``settings.reaper_sweep_interval_seconds`` (default daily), matching the
bounded-pass-every-iteration shape of ``purge_expired_value_profile_artifacts``,
``run_owner_routing_pass``, and ``run_due_playbooks_pass``.

Safety rails:

* Each rule has a per-pass ``hard_cap`` (default 10,000). If the candidate
  count exceeds the cap -- i.e. something upstream broke and is producing
  reap candidates in numbers that dwarf the normal flow -- the pass emits
  an ``REAPER_CAP_EXCEEDED`` alert audit event and reaps *zero* rows for
  that rule this pass (safety over throughput). An operator triages before
  the reaper touches anything.
* Each rule reaps inside its own SAVEPOINT so one rule's failure never
  aborts every other rule's sweep in the same pass.
* Every rule that reaps at least one row emits exactly one ``REAP_*``
  audit event with the count -- one audit line per rule per pass, not one
  per row, matching ``run_owner_routing_pass``'s per-organization audit
  aggregation.

Explicit non-goals -- rows this reaper deliberately does NOT touch:

* ``APPROVED`` versions of anything (they are the current governance state).
* ``RelationshipCandidate`` (its PENDING/APPROVED/REJECTED history is
  decision-critical audit data -- see ADR notes in models.py).
* ``GovernanceReview`` (audit trail).
* ``Outbox`` (has its own separate retention concern).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import session_factory
from aida.events import record_audit
from aida.models import (
    AssetDescriptionDraft,
    AssetTermLink,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataEnrichmentProposal,
)
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

WORKER_PRINCIPAL_ID = "reaper-service-worker"

# Per-rule hard cap on how many rows a single pass may touch. If the count of
# candidates exceeds this, the rule emits REAPER_CAP_EXCEEDED and reaps zero
# rows this pass (safety over throughput). Chosen conservatively -- normal
# steady-state per-day reap volume is expected in the tens/hundreds; a count
# in the tens of thousands means something upstream is misbehaving, and
# hard-deleting that much data before a human looks at it is exactly the
# runaway a P2 fix must not create.
DEFAULT_HARD_CAP = 10_000
DEFAULT_BATCH_SIZE = 200


ReaperAction = Literal["DELETE", "STATUS_FLIP"]


@dataclass(frozen=True)
class ReaperRule:
    """One data-driven reaping rule.

    * ``candidates_stmt``: given (now, retention), returns a ``Select`` of
      *primary keys* to reap. Kept as PKs rather than full rows so the
      count-check is cheap (a COUNT on the same subquery) and the load-and-
      apply loop can page over the same predicate deterministically without
      holding a large in-memory rowset.
    * ``action``: ``"DELETE"`` hard-deletes; ``"STATUS_FLIP"`` sets
      ``row.status = new_status`` (in-place update, preserving the row -- used
      for cases where the row itself is a fingerprint anchor or audit
      artifact that must not be deleted, only aged out of active queues).
    * ``retention``: matched against ``updated_at`` for status-terminal
      rules (rejected/aged), against ``created_at`` for orphan-by-relationship
      rules -- ``candidates_stmt`` owns that choice.
    """

    name: str
    model: type
    resource_type: str
    audit_action: str
    retention: timedelta
    action: ReaperAction
    candidates_stmt: Callable[[datetime, timedelta], "Select[tuple[UUID]]"]
    new_status: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    hard_cap: int = DEFAULT_HARD_CAP

    def __post_init__(self) -> None:
        if self.action == "STATUS_FLIP" and self.new_status is None:
            raise ValueError(
                f"reaper rule {self.name!r}: action=STATUS_FLIP requires new_status"
            )
        if self.action == "DELETE" and self.new_status is not None:
            raise ValueError(
                f"reaper rule {self.name!r}: action=DELETE cannot carry new_status"
            )


# ---------------------------------------------------------------------------
# Candidate-selection statements
# ---------------------------------------------------------------------------
#
# One function per rule, taking (now, retention) and returning a Select of
# primary keys. Kept out of the dataclass definition (rather than inlined as
# lambdas) so the SQL is greppable and reviewable in one place.


def _rejected_enrichment_proposals_stmt(
    now: datetime, retention: timedelta
) -> Select[tuple[UUID]]:
    """REJECTED proposals not touched (reviewed_at written by
    ``semantic_inference.reject_enrichment_proposal`` bumps ``updated_at``)
    within the retention window. Kept simple -- no join -- because
    ``MetadataEnrichmentProposal.status="REJECTED"`` is itself a terminal
    state; the retention window is measured from the *last* transition, so
    ``updated_at`` (not ``created_at``) is the correct anchor.
    """
    cutoff = now - retention
    return (
        select(MetadataEnrichmentProposal.id)
        .where(
            MetadataEnrichmentProposal.status == "REJECTED",
            MetadataEnrichmentProposal.updated_at < cutoff,
        )
        .order_by(MetadataEnrichmentProposal.updated_at)
    )


def _stale_pending_enrichment_proposals_stmt(
    now: datetime, retention: timedelta
) -> Select[tuple[UUID]]:
    """Proposals stuck in ``PENDING_REVIEW`` past ``retention`` -- flipped
    to ``EXPIRED`` (not deleted). Keeping the row preserves the audit trail:
    a reviewer can see that the proposal existed and expired, rather than
    the review record vanishing silently.
    """
    cutoff = now - retention
    return (
        select(MetadataEnrichmentProposal.id)
        .where(
            MetadataEnrichmentProposal.status == "PENDING_REVIEW",
            MetadataEnrichmentProposal.updated_at < cutoff,
        )
        .order_by(MetadataEnrichmentProposal.updated_at)
    )


def _orphan_asset_term_links_stmt(
    now: datetime, retention: timedelta
) -> Select[tuple[UUID]]:
    """``AssetTermLink`` rows whose ``GlossaryTerm`` has been fully
    deprecated (lifecycle_status="DEPRECATED", carries a deprecated_at, and
    has at least one DEPRECATED version) and where the *link itself* was
    created before the deprecation date -- i.e. a link that was live when it
    was created and became orphaned by the term's deprecation.

    ``retention`` here is a grace period *after* the deprecation date, so an
    operator can un-deprecate a term without immediately losing every link
    to it. Retention is measured from ``term.deprecated_at``, not from
    ``link.updated_at`` -- the link itself often has no more-recent event
    to time against once its term goes stale.
    """
    cutoff = now - retention
    return (
        select(AssetTermLink.id)
        .join(GlossaryTerm, GlossaryTerm.id == AssetTermLink.term_id)
        .where(
            GlossaryTerm.lifecycle_status == "DEPRECATED",
            GlossaryTerm.deprecated_at.is_not(None),
            GlossaryTerm.deprecated_at < cutoff,
            AssetTermLink.created_at < GlossaryTerm.deprecated_at,
            select(GlossaryTermVersion.id)
            .where(
                GlossaryTermVersion.term_id == GlossaryTerm.id,
                GlossaryTermVersion.status == "DEPRECATED",
            )
            .exists(),
        )
        .order_by(AssetTermLink.created_at)
    )


def _rejected_description_drafts_stmt(
    now: datetime, retention: timedelta
) -> Select[tuple[UUID]]:
    """REJECTED ``AssetDescriptionDraft`` rows older than the retention
    window. Flipped to ``REAPED`` (not deleted): the class docstring's
    contract is that rejected drafts are retained as negative-knowledge
    fingerprint anchors so an identical low-value draft is not regenerated.
    ``REAPED`` preserves ``text_fingerprint`` (fingerprint lookup keeps
    working) while marking the row so review-queue filters exclude it.
    """
    cutoff = now - retention
    return (
        select(AssetDescriptionDraft.id)
        .where(
            AssetDescriptionDraft.status == "REJECTED",
            AssetDescriptionDraft.updated_at < cutoff,
        )
        .order_by(AssetDescriptionDraft.updated_at)
    )


def _stale_pending_description_drafts_stmt(
    now: datetime, retention: timedelta
) -> Select[tuple[UUID]]:
    """DRAFTS never submitted for review, aged past the retention window.
    Flipped to ``EXPIRED`` so a drafter can see the row expired rather than
    the draft vanishing silently and the auto-drafter re-enqueuing it.
    """
    cutoff = now - retention
    return (
        select(AssetDescriptionDraft.id)
        .where(
            AssetDescriptionDraft.status == "DRAFT",
            AssetDescriptionDraft.updated_at < cutoff,
        )
        .order_by(AssetDescriptionDraft.updated_at)
    )


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------
#
# One row per rule. The defaults below are the retention values a bank's
# steward org can operate against safely; per-rule overrides live in
# ``Settings.reaper_retention_overrides``.

RULES: list[ReaperRule] = [
    ReaperRule(
        name="rejected_enrichment_proposals",
        model=MetadataEnrichmentProposal,
        resource_type="metadata_enrichment_proposal",
        audit_action="REAP_REJECTED_ENRICHMENT_PROPOSAL",
        retention=timedelta(days=90),
        action="DELETE",
        candidates_stmt=_rejected_enrichment_proposals_stmt,
    ),
    ReaperRule(
        name="stale_pending_enrichment_proposals",
        model=MetadataEnrichmentProposal,
        resource_type="metadata_enrichment_proposal",
        audit_action="REAP_STALE_PENDING_ENRICHMENT_PROPOSAL",
        retention=timedelta(days=365),
        action="STATUS_FLIP",
        new_status="EXPIRED",
        candidates_stmt=_stale_pending_enrichment_proposals_stmt,
    ),
    ReaperRule(
        name="orphan_asset_term_links",
        model=AssetTermLink,
        resource_type="asset_term_link",
        audit_action="REAP_ORPHAN_ASSET_TERM_LINK",
        # 0-day grace: once a term is fully deprecated (lifecycle_status +
        # a DEPRECATED version), links to it are already dead as far as
        # resolve_scoped_glossary_term is concerned. A non-zero grace here
        # would let dead links linger for no read-path benefit.
        retention=timedelta(days=0),
        action="DELETE",
        candidates_stmt=_orphan_asset_term_links_stmt,
    ),
    ReaperRule(
        name="rejected_description_drafts",
        model=AssetDescriptionDraft,
        resource_type="asset_description_draft",
        audit_action="REAP_REJECTED_DESCRIPTION_DRAFT",
        retention=timedelta(days=180),
        action="STATUS_FLIP",
        # Deliberately not DELETE -- see _rejected_description_drafts_stmt
        # docstring; the model contract retains the fingerprint anchor.
        new_status="REAPED",
        candidates_stmt=_rejected_description_drafts_stmt,
    ),
    ReaperRule(
        name="stale_pending_description_drafts",
        model=AssetDescriptionDraft,
        resource_type="asset_description_draft",
        audit_action="REAP_STALE_PENDING_DESCRIPTION_DRAFT",
        retention=timedelta(days=60),
        action="STATUS_FLIP",
        new_status="EXPIRED",
        candidates_stmt=_stale_pending_description_drafts_stmt,
    ),
]


@dataclass
class ReaperRuleReport:
    name: str
    resource_type: str
    action: ReaperAction
    candidates: int
    reaped: int
    cap_exceeded: bool = False
    error: str | None = None


@dataclass
class ReaperReport:
    ran_at: datetime
    rules: list[ReaperRuleReport] = field(default_factory=list)
    disabled: bool = False

    @property
    def total_reaped(self) -> int:
        return sum(rule.reaped for rule in self.rules)


# ---------------------------------------------------------------------------
# Retention overrides
# ---------------------------------------------------------------------------


def parse_retention_overrides(spec: str | None) -> dict[str, timedelta]:
    """Parse ``AIDA_REAPER_RETENTION_OVERRIDES`` (``"rule_a:30,rule_b:7"``)
    into a per-rule ``retention`` override map.

    A malformed entry is dropped with a log line rather than raising: an
    operator typo on this knob should not take the reaper -- and therefore
    the whole scheduler -- offline. Unknown rule names are similarly
    ignored (they may be typos, or refer to a rule that has since been
    removed from ``RULES``); a warning line lets the operator notice.
    """
    if not spec:
        return {}
    known = {rule.name for rule in RULES}
    overrides: dict[str, timedelta] = {}
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            name, days_str = entry.split(":", 1)
        except ValueError:
            logger.warning("reaper_override_malformed", entry=entry)
            continue
        name = name.strip()
        try:
            days = int(days_str.strip())
        except ValueError:
            logger.warning("reaper_override_bad_days", entry=entry)
            continue
        if days < 0:
            logger.warning("reaper_override_negative_days", entry=entry)
            continue
        if name not in known:
            logger.warning("reaper_override_unknown_rule", rule=name)
            continue
        overrides[name] = timedelta(days=days)
    return overrides


def _effective_retention(
    rule: ReaperRule, overrides: dict[str, timedelta]
) -> timedelta:
    return overrides.get(rule.name, rule.retention)


# ---------------------------------------------------------------------------
# Per-rule execution
# ---------------------------------------------------------------------------


async def _emit_cap_exceeded_alert(
    session: AsyncSession, rule: ReaperRule, candidate_count: int
) -> None:
    """Emit the ``REAPER_CAP_EXCEEDED`` alert audit event under the same
    worker principal used for the reap-success audits, so a SOC can filter on
    principal_id/action and see both the alert and the eventual sweep once an
    operator has triaged and cleared the upstream cause.
    """
    context = SecurityContext(
        principal_id=WORKER_PRINCIPAL_ID,
        principal_type="WORKER",
        organization_id=None,
        roles=frozenset({"ReaperWorker"}),
    )
    record_audit(
        session,
        context,
        action="REAPER_CAP_EXCEEDED",
        resource_type=rule.resource_type,
        resource_id=None,
        outcome="FAILED",
        correlation_id=str(uuid4()),
        details={
            "rule": rule.name,
            "candidate_count": candidate_count,
            "hard_cap": rule.hard_cap,
        },
    )


async def _emit_reap_summary(
    session: AsyncSession, rule: ReaperRule, reaped: int
) -> None:
    """One audit line per rule per pass (never per row) -- matches the
    per-organization audit aggregation used by ``run_owner_routing_pass``.
    """
    context = SecurityContext(
        principal_id=WORKER_PRINCIPAL_ID,
        principal_type="WORKER",
        organization_id=None,
        roles=frozenset({"ReaperWorker"}),
    )
    record_audit(
        session,
        context,
        action=rule.audit_action,
        resource_type=rule.resource_type,
        resource_id=None,
        outcome="SUCCESS",
        correlation_id=str(uuid4()),
        details={"rule": rule.name, "reaped_count": reaped, "action": rule.action},
    )


async def _apply_rule(
    session: AsyncSession, rule: ReaperRule, ids: Sequence[UUID], now: datetime
) -> int:
    """Load each row and apply the rule's action. Kept as a per-row loop
    (rather than a bulk UPDATE/DELETE) so ``record_audit``'s per-row details
    (resource_id, correlation_id) can be attached if a future audit expansion
    wants that -- and so ORM-level cascades (``ondelete="CASCADE"`` on the
    proposal's ``governance_review_id`` is NOT cascade-into: it deletes the
    proposal when the review is dropped, not the other way) fire correctly
    for DELETE.
    """
    reaped = 0
    for row_id in ids:
        row = await session.get(rule.model, row_id)
        if row is None:
            # Raced with another writer -- fine, skip.
            continue
        if rule.action == "DELETE":
            await session.delete(row)
        elif rule.action == "STATUS_FLIP":
            # Guarded by candidates_stmt already, but re-check here so a
            # concurrent transition (e.g. a reviewer flipping REJECTED ->
            # APPROVED between count and apply -- unusual but legal for a
            # supervisor override) does not clobber the newer state.
            assert rule.new_status is not None  # __post_init__ guarantees
            current_status = getattr(row, "status", None)
            expected = _expected_source_status_for(rule)
            if current_status != expected:
                continue
            row.status = rule.new_status
        else:  # pragma: no cover - dataclass validation prevents this
            raise ValueError(f"reaper rule {rule.name!r}: unknown action {rule.action}")
        reaped += 1
    return reaped


def _expected_source_status_for(rule: ReaperRule) -> str | None:
    """The source status a STATUS_FLIP rule expects to be flipping *from*,
    used as a race guard in ``_apply_rule``. Kept as a static mapping
    (rather than parsing ``candidates_stmt``) because the source status is
    baked into each rule's selection predicate and does not vary at
    runtime.
    """
    return {
        "stale_pending_enrichment_proposals": "PENDING_REVIEW",
        "rejected_description_drafts": "REJECTED",
        "stale_pending_description_drafts": "DRAFT",
    }.get(rule.name)


async def _run_rule(
    session: AsyncSession,
    rule: ReaperRule,
    now: datetime,
    overrides: dict[str, timedelta],
) -> ReaperRuleReport:
    """Execute one rule inside its own SAVEPOINT so a single rule's failure
    (a bad predicate, a schema mismatch on a soft-flip target column) is
    isolated from every other rule's success in the same pass -- mirroring
    the fault isolation ``run_owner_routing_pass`` gives each organization.
    """
    retention = _effective_retention(rule, overrides)
    report = ReaperRuleReport(
        name=rule.name,
        resource_type=rule.resource_type,
        action=rule.action,
        candidates=0,
        reaped=0,
    )

    try:
        async with session.begin_nested():
            base_stmt = rule.candidates_stmt(now, retention)
            count_stmt = select(func.count()).select_from(base_stmt.subquery())
            candidate_count = int(await session.scalar(count_stmt) or 0)
            report.candidates = candidate_count

            if candidate_count > rule.hard_cap:
                # Do NOT reap even a partial batch -- safety over throughput.
                # An operator must triage the upstream cause before this
                # rule touches the DB again.
                report.cap_exceeded = True
                await _emit_cap_exceeded_alert(session, rule, candidate_count)
                logger.warning(
                    "reaper_cap_exceeded",
                    rule=rule.name,
                    candidate_count=candidate_count,
                    hard_cap=rule.hard_cap,
                )
                return report

            if candidate_count == 0:
                return report

            # Bounded page over the same predicate.
            ids_result = await session.scalars(base_stmt.limit(rule.batch_size))
            ids = list(ids_result)
            reaped = await _apply_rule(session, rule, ids, now)
            report.reaped = reaped
            if reaped:
                await _emit_reap_summary(session, rule, reaped)
    except Exception as exc:
        # begin_nested rolled the SAVEPOINT back on the exception; log
        # and carry on to the next rule.
        logger.exception("reaper_rule_failed", rule=rule.name)
        report.error = f"{type(exc).__name__}: {exc}"
    return report


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_reaper_pass(
    session: AsyncSession | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    rules: Sequence[ReaperRule] | None = None,
) -> ReaperReport:
    """One reaper pass across every rule.

    ``session`` is optional so production callers (scheduler) can leave it
    unset and this function opens/commits its own session -- matching the
    ``purge_expired_value_profile_artifacts`` shape -- while unit tests can
    inject a live in-memory-SQLite session directly (matching
    ``test_profiling_exception_policy.py``).
    """
    effective_now = now or datetime.now(UTC)
    effective_settings = settings or get_settings()
    report = ReaperReport(ran_at=effective_now)

    if not effective_settings.reaper_enabled:
        report.disabled = True
        logger.info("reaper_disabled_by_config")
        return report

    overrides = parse_retention_overrides(
        effective_settings.reaper_retention_overrides
    )
    rules_to_run = list(rules) if rules is not None else RULES

    owned_session = session is None
    if owned_session:
        async with session_factory() as owned:
            for rule in rules_to_run:
                report.rules.append(
                    await _run_rule(owned, rule, effective_now, overrides)
                )
            try:
                await owned.commit()
            except ResourceClosedError:  # pragma: no cover
                pass
    else:
        for rule in rules_to_run:
            report.rules.append(
                await _run_rule(session, rule, effective_now, overrides)
            )

    logger.info(
        "reaper_pass_complete",
        total_reaped=report.total_reaped,
        rule_reports=[
            {
                "name": r.name,
                "reaped": r.reaped,
                "candidates": r.candidates,
                "cap_exceeded": r.cap_exceeded,
                "error": r.error,
            }
            for r in report.rules
        ],
    )
    return report


# ---------------------------------------------------------------------------
# Scheduler entry
# ---------------------------------------------------------------------------

# Same in-process due-tracking trade-off as
# ``_owner_routing_last_run_at``/``_custom_rule_pack_last_run_at``: no
# per-rule "next-due-at" column exists, and a sweep is idempotent (reaping
# an already-reaped candidate is a no-op), so a scheduler restart costs at
# most one redundant pass.
_reaper_last_run_at: datetime | None = None


def reaper_due(
    last_run_at: datetime | None, now: datetime, interval: timedelta
) -> bool:
    """Whether a reaper pass is due. ``None`` (never run) is always due."""
    if last_run_at is None:
        return True
    return (now - last_run_at) >= interval


async def run_reaper_scheduler_pass(
    settings: Settings, *, now: datetime | None = None
) -> ReaperReport | None:
    """Scheduler-facing entry: run a reaper pass if the configured interval
    has elapsed since the last pass, otherwise no-op. Returns ``None`` when
    the pass was skipped (either not due or disabled), the report when it
    ran, to match the return shape ``run_scheduler_iteration`` expects for
    logging.
    """
    global _reaper_last_run_at
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.reaper_sweep_interval_seconds)
    if not reaper_due(_reaper_last_run_at, effective_now, interval):
        return None
    report = await run_reaper_pass(now=effective_now, settings=settings)
    _reaper_last_run_at = effective_now
    return report


def _reset_reaper_due_state_for_tests() -> None:
    """Test-only helper -- clears the in-process due tracker so each test's
    ``run_reaper_scheduler_pass`` call runs regardless of a prior test's
    run time. Nothing in production calls this.
    """
    global _reaper_last_run_at
    _reaper_last_run_at = None
