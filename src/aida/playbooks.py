"""AT-1: saved, scheduled bulk-metadata "playbook" objects, on top of CT-1.

A playbook persists a filter (a datasource plus a table-name pattern,
optionally narrowed further to a column-name pattern for CLASSIFY), one of
CT-1's four actions (TAG/CLASSIFY/OWN/CERTIFY), and a schedule. Evaluating one
resolves its filter against the live catalog, using the exact same matcher
CT-1's own filter-mode bulk endpoints use, and then either:

- applies the action immediately, reusing CT-1's own single-item cores
  (``aida.catalog_bulk_actions.apply_*_item``) -- the same functions the
  synchronous ``/tables/bulk-*`` endpoints call per item -- when the match
  count is at or below the playbook's own ``auto_apply_max_items``; or
- queues the action behind a ``GovernanceReview`` (a ``BulkStewardshipOperation``
  a checker later approves or rejects) when the match count is larger.

This mirrors GL-2 (rule-based ownership auto-applies at small scale) and GL-5
(certification goes through review) rather than inventing a third governance
shape: scale-dependent risk already has a precedent on this platform.

``describe`` is named in this row's own exit text as a fifth action but has no
existing CT-1 single-item core to reuse -- describing a table means writing to
the steward-authored description tables module 04 owns, a different
mechanism this row does not build. Honestly out of scope for this pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_bulk_actions import (
    CATALOG_BULK_ACTION_MAX_ITEMS,
    CATALOG_BULK_FILTER_SCAN_CAP,
    BulkItemResult,
    BulkPlan,
    CatalogBulkItemError,
    apply_certify_item,
    apply_classify_item,
    apply_own_item,
    apply_tag_item,
    match_columns_by_pattern,
    match_tables_by_filter,
)
from aida.context import get_correlation_id
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetCertification,
    AssetTag,
    BulkStewardshipOperation,
    CatalogBulkActionRun,
    GovernanceReview,
    MetadataColumn,
    MetadataPlaybook,
    MetadataSchema,
    MetadataTable,
    OwnershipAssignment,
)
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

#: The action a playbook's ``BulkStewardshipOperation`` is queued under, when
#: it differs from the playbook's own action name -- OWN/CERTIFY already had
#: an established operation type before this row existed (GL-2/GL-5/GL-7);
#: TAG/CLASSIFY are new (added alongside this module).
_OPERATION_TYPE_FOR_ACTION = {
    "TAG": "TAG",
    "CLASSIFY": "CLASSIFY",
    "OWN": "ASSIGN_OWNERSHIP",
    "CERTIFY": "CERTIFY_ASSET",
}

_EVENT_TYPE_FOR_ACTION = {
    "TAG": "catalog.asset_tag.applied.v1",
    "CLASSIFY": "catalog.column.classified.v1",
    "OWN": "ownership.assigned.v1",
    "CERTIFY": "certification.granted.v1",
}


def _worker_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="fleet-scheduler",
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


@dataclass(frozen=True, slots=True)
class PlaybookRunOutcome:
    """What one evaluation of one playbook did."""

    playbook_id: UUID
    matched_count: int
    outcome: str  # "NO_MATCHES" | "AUTO_APPLIED" | "QUEUED_FOR_REVIEW"
    bulk_action_run_id: UUID | None = None
    bulk_stewardship_operation_id: UUID | None = None
    governance_review_id: UUID | None = None


async def resolve_playbook_matches(
    session: AsyncSession, playbook: MetadataPlaybook
) -> list[UUID]:
    """The table or column ids (depending on ``playbook.action``) this
    playbook's filter currently matches, capped the same way CT-1's own
    filter-mode bulk endpoints are (``CATALOG_BULK_ACTION_MAX_ITEMS``).
    """
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == playbook.organization_id,
                MetadataTable.datasource_id == playbook.datasource_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.id)
            .limit(CATALOG_BULK_FILTER_SCAN_CAP)
        )
    ).all()
    table_candidates = [(row[0], row[1]) for row in rows]
    matched_table_ids, _truncated = match_tables_by_filter(
        table_candidates,
        match_field=playbook.match_field,
        match_pattern=playbook.match_pattern,
        cap=CATALOG_BULK_ACTION_MAX_ITEMS,
    )
    if playbook.action != "CLASSIFY":
        return matched_table_ids
    if not matched_table_ids:
        return []
    column_rows = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id.in_(matched_table_ids),
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    matched_column_ids, _truncated = match_columns_by_pattern(
        column_rows,
        name_pattern=playbook.column_name_pattern or "*",
        cap=CATALOG_BULK_ACTION_MAX_ITEMS,
    )
    return matched_column_ids


async def evaluate_and_run_playbook(
    session: AsyncSession, playbook: MetadataPlaybook, *, now: datetime | None = None
) -> PlaybookRunOutcome:
    """Resolve one playbook's filter and apply or queue its action.

    Always updates ``playbook.last_run_at`` (including on a no-match run, so
    the scheduler's due-check advances and a permanently-unmatched playbook
    does not get re-evaluated every single iteration).
    """
    now = now or datetime.now(UTC)
    playbook.last_run_at = now
    matched_ids = await resolve_playbook_matches(session, playbook)
    if not matched_ids:
        return PlaybookRunOutcome(playbook.id, 0, "NO_MATCHES")

    if len(matched_ids) <= playbook.auto_apply_max_items:
        run = await _auto_apply(session, playbook, matched_ids, now=now)
        return PlaybookRunOutcome(
            playbook.id, len(matched_ids), "AUTO_APPLIED", bulk_action_run_id=run.id
        )

    operation, review = await _queue_for_review(session, playbook, matched_ids, now=now)
    return PlaybookRunOutcome(
        playbook.id,
        len(matched_ids),
        "QUEUED_FOR_REVIEW",
        bulk_stewardship_operation_id=operation.id,
        governance_review_id=review.id,
    )


async def _apply_one_item(
    session: AsyncSession,
    playbook: MetadataPlaybook,
    subject_id: UUID,
    *,
    applied_by: str,
    tables: dict[UUID, MetadataTable],
    existing_tags: dict[UUID, AssetTag],
    existing_assignments: dict[UUID, OwnershipAssignment],
    active_certifications: dict[UUID, list[AssetCertification]],
    columns: dict[UUID, tuple[MetadataColumn, MetadataTable]],
    now: datetime,
) -> None:
    """Dispatch one subject through the matching CT-1 single-item core,
    add/flush whatever it returns, and raise `CatalogBulkItemError` on a
    precondition failure -- the caller records that as a FAILED item.
    """
    params = playbook.action_parameters
    if playbook.action == "TAG":
        tag, is_new = apply_tag_item(
            subject_id,
            tables=tables,
            existing_tags=existing_tags,
            organization_id=playbook.organization_id,
            tag_key=params["tag_key"],
            tag_value=params.get("tag_value"),
            applied_by=applied_by,
        )
        if is_new:
            session.add(tag)
        await session.flush([tag])
    elif playbook.action == "CLASSIFY":
        column = apply_classify_item(
            subject_id, columns=columns, classification=params["classification"]
        )
        await session.flush([column])
    elif playbook.action == "OWN":
        assignment, is_new = apply_own_item(
            subject_id,
            tables=tables,
            existing_assignments=existing_assignments,
            organization_id=playbook.organization_id,
            owner_type=params["owner_type"],
            owner_principal=params["owner_principal"],
            assigned_by=applied_by,
        )
        if is_new:
            session.add(assignment)
        await session.flush([assignment])
    else:
        assert playbook.action == "CERTIFY"
        expires_at = now + timedelta(days=int(params["expires_after_days"]))
        new_certification, superseded = apply_certify_item(
            subject_id,
            tables=tables,
            active_certifications=active_certifications,
            organization_id=playbook.organization_id,
            rationale=params["rationale"],
            expires_at=expires_at,
            certified_by=applied_by,
        )
        session.add(new_certification)
        await session.flush([new_certification, *superseded])


async def _auto_apply(
    session: AsyncSession,
    playbook: MetadataPlaybook,
    subject_ids: list[UUID],
    *,
    now: datetime,
) -> CatalogBulkActionRun:
    context = _worker_context(playbook.organization_id)
    tables: dict[UUID, MetadataTable] = {}
    existing_tags: dict[UUID, AssetTag] = {}
    existing_assignments: dict[UUID, OwnershipAssignment] = {}
    active_certifications: dict[UUID, list[AssetCertification]] = {}
    columns: dict[UUID, tuple[MetadataColumn, MetadataTable]] = {}

    if playbook.action == "CLASSIFY":
        column_rows = (
            await session.execute(
                select(MetadataColumn, MetadataTable)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(MetadataColumn.id.in_(subject_ids))
            )
        ).all()
        columns = {row[0].id: (row[0], row[1]) for row in column_rows}
    else:
        table_rows = (
            await session.scalars(select(MetadataTable).where(MetadataTable.id.in_(subject_ids)))
        ).all()
        tables = {row.id: row for row in table_rows}
        if playbook.action == "TAG":
            tag_rows = (
                await session.scalars(
                    select(AssetTag).where(
                        AssetTag.table_id.in_(subject_ids),
                        AssetTag.tag_key == playbook.action_parameters["tag_key"],
                    )
                )
            ).all()
            existing_tags = {row.table_id: row for row in tag_rows}
        elif playbook.action == "OWN":
            params = playbook.action_parameters
            assignment_rows = (
                await session.scalars(
                    select(OwnershipAssignment).where(
                        OwnershipAssignment.organization_id == playbook.organization_id,
                        OwnershipAssignment.subject_type == "TABLE",
                        OwnershipAssignment.subject_id.in_(str(value) for value in subject_ids),
                        OwnershipAssignment.owner_type == params["owner_type"],
                        OwnershipAssignment.owner_principal == params["owner_principal"],
                    )
                )
            ).all()
            existing_assignments = {UUID(row.subject_id): row for row in assignment_rows}
        elif playbook.action == "CERTIFY":
            certification_rows = (
                await session.scalars(
                    select(AssetCertification).where(
                        AssetCertification.table_id.in_(subject_ids),
                        AssetCertification.asset_type == "TABLE",
                        AssetCertification.status == "ACTIVE",
                    )
                )
            ).all()
            for row in certification_rows:
                active_certifications.setdefault(row.table_id, []).append(row)

    results: list[BulkItemResult] = []
    for subject_id in subject_ids:
        try:
            async with session.begin_nested():
                await _apply_one_item(
                    session,
                    playbook,
                    subject_id,
                    applied_by=context.principal_id,
                    tables=tables,
                    existing_tags=existing_tags,
                    existing_assignments=existing_assignments,
                    active_certifications=active_certifications,
                    columns=columns,
                    now=now,
                )
        except CatalogBulkItemError as exc:
            results.append(BulkItemResult(str(subject_id), "FAILED", str(exc)))
            continue
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))

    plan = BulkPlan(results=results)
    run = CatalogBulkActionRun(
        organization_id=playbook.organization_id,
        action=playbook.action,
        selection_mode="PLAYBOOK_AUTO",
        parameters={**playbook.action_parameters, "playbook_id": str(playbook.id)},
        requested_count=len(results),
        succeeded_count=plan.succeeded_count,
        failed_count=plan.failed_count,
        results=[item.as_dict() for item in results],
        requested_by=context.principal_id,
    )
    session.add(run)
    await session.flush()
    outcome = (
        "PARTIAL_SUCCESS"
        if plan.succeeded_count and plan.failed_count
        else "SUCCESS"
        if plan.succeeded_count
        else "FAILURE"
    )
    record_audit(
        session,
        context,
        action=f"playbook.auto_apply.{playbook.action.lower()}",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "run_id": str(run.id),
            "matched_count": len(subject_ids),
            "succeeded_count": plan.succeeded_count,
            "failed_count": plan.failed_count,
        },
    )
    record_outbox(
        session,
        organization_id=playbook.organization_id,
        aggregate_type="catalog_bulk_action_run",
        aggregate_id=str(run.id),
        event_type=_EVENT_TYPE_FOR_ACTION[playbook.action],
        payload={
            "run_id": str(run.id),
            "playbook_id": str(playbook.id),
            "action": playbook.action,
            "succeeded_count": plan.succeeded_count,
            "failed_count": plan.failed_count,
        },
    )
    return run


def _resolved_operation_parameters(
    playbook: MetadataPlaybook, *, now: datetime
) -> dict[str, object]:
    """The concrete parameters this run's ``BulkStewardshipOperation`` needs,
    derived from the playbook's own stored config. Only CERTIFY differs: its
    config holds a relative ``expires_after_days`` (so a schedule that runs
    repeatedly always certifies a fresh window), resolved here to the
    absolute ISO timestamp `apply_bulk_operation`'s `CERTIFY_ASSET` branch
    expects.
    """
    params = dict(playbook.action_parameters)
    if playbook.action == "CERTIFY":
        expires_after_days = int(params.pop("expires_after_days"))
        params["expires_at"] = (now + timedelta(days=expires_after_days)).isoformat()
    return params


async def _queue_for_review(
    session: AsyncSession,
    playbook: MetadataPlaybook,
    subject_ids: list[UUID],
    *,
    now: datetime,
) -> tuple[BulkStewardshipOperation, GovernanceReview]:
    context = _worker_context(playbook.organization_id)
    subject_type = "COLUMN" if playbook.action == "CLASSIFY" else "TABLE"
    review = GovernanceReview(
        organization_id=playbook.organization_id,
        object_type="BULK_STEWARDSHIP_OPERATION",
        object_id="pending",
        requested_action=_OPERATION_TYPE_FOR_ACTION[playbook.action],
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    operation = BulkStewardshipOperation(
        organization_id=playbook.organization_id,
        operation_type=_OPERATION_TYPE_FOR_ACTION[playbook.action],
        subject_type=subject_type,
        subject_ids=[str(value) for value in subject_ids],
        parameters={
            **_resolved_operation_parameters(playbook, now=now),
            "playbook_id": str(playbook.id),
        },
        governance_review_id=review.id,
        requested_by=context.principal_id,
    )
    session.add(operation)
    await session.flush()
    review.object_id = str(operation.id)
    record_audit(
        session,
        context,
        action="playbook.queued_for_review",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "operation_id": str(operation.id),
            "review_id": str(review.id),
            "matched_count": len(subject_ids),
        },
    )
    record_outbox(
        session,
        organization_id=playbook.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(operation.id),
            "requested_action": _OPERATION_TYPE_FOR_ACTION[playbook.action],
            "playbook_id": str(playbook.id),
        },
    )
    return operation, review


def playbook_due(last_run_at: datetime | None, now: datetime, interval_minutes: int) -> bool:
    """Whether a playbook sweep is due. ``None`` (never run) is always due."""
    if last_run_at is None:
        return True
    return (now - last_run_at).total_seconds() >= interval_minutes * 60


async def run_due_playbooks_pass(*, now: datetime | None = None) -> int:
    """Sweep every enabled ``MetadataPlaybook`` due per its own
    ``schedule_interval_minutes``, called from
    ``aida.workflows.scheduler.run_scheduler_iteration`` the same way
    ``custom_quality_rules.run_due_rule_packs`` (DQ-4) is. Unlike that pass's
    in-process ``last_run_at`` dict, a playbook's due-ness is tracked on the
    row itself (this table's own ``last_run_at`` column), so a scheduler
    restart re-sweeps nothing early.

    One playbook's failure is logged and skipped, matching
    ``run_owner_routing_pass``/``run_due_rule_packs``'s fault isolation, so a
    bad playbook never blocks every other playbook's sweep in the same
    iteration.
    """
    effective_now = now or datetime.now(UTC)
    async with session_factory() as session:
        playbooks = (
            await session.scalars(
                select(MetadataPlaybook).where(MetadataPlaybook.enabled.is_(True))
            )
        ).all()
        due_ids = [
            playbook.id
            for playbook in playbooks
            if playbook_due(
                playbook.last_run_at, effective_now, playbook.schedule_interval_minutes
            )
        ]

    swept = 0
    for playbook_id in due_ids:
        try:
            async with session_factory() as session:
                playbook = await session.get(MetadataPlaybook, playbook_id)
                if playbook is None or not playbook.enabled:
                    continue
                await evaluate_and_run_playbook(session, playbook, now=effective_now)
                await session.commit()
        except Exception:
            logger.exception("playbook_sweep_failed", playbook_id=str(playbook_id))
            continue
        swept += 1
    return swept
