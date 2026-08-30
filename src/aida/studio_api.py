"""Studio authoring environment REST API.

Provides endpoints for managing change sets, change items, testing,
conflict detection, diff viewing, and impact preview for semantic model
objects.

Change sets follow the lifecycle: DRAFT -> TESTING -> SUBMITTED -> MERGED/REJECTED.
Only tested change sets can be submitted for governance review (test gate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import StudioChangeItem, StudioChangeSet, StudioTestRun
from aida.schemas import (
    StudioChangeItemCreate,
    StudioChangeItemRead,
    StudioChangeSetCreate,
    StudioChangeSetRead,
    StudioConflict,
    StudioDiffRead,
    StudioImpactPreview,
    StudioTestResultRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.studio import (
    ChangeItem,
    ChangeSet,
    compute_diff,
    compute_impact,
    detect_conflicts,
)
from aida.studio_test_harness import TestFixture, run_test_suite

router = APIRouter(prefix="/v1", tags=["studio"])

_STUDIO_READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)

_STUDIO_WRITE_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "SemanticAdmin",
    "DataSteward",
)


async def _load_change_set(
    session: AsyncSession,
    context: SecurityContext,
    change_set_id: UUID,
) -> StudioChangeSet:
    """Load a change set and enforce organization ownership."""
    cs = await session.get(StudioChangeSet, change_set_id)
    if cs is None:
        raise HTTPException(status_code=404, detail="change set not found")
    enforce_organization(context, cs.organization_id)
    return cs


async def _load_items(
    session: AsyncSession,
    change_set_id: UUID,
) -> list[StudioChangeItem]:
    """Load all items for a change set."""
    rows = (
        await session.scalars(
            select(StudioChangeItem)
            .where(StudioChangeItem.change_set_id == change_set_id)
            .order_by(StudioChangeItem.created_at)
        )
    ).all()
    return list(rows)


def _to_domain_item(db_item: StudioChangeItem) -> ChangeItem:
    """Convert a DB model item to the domain ChangeItem dataclass."""
    return ChangeItem(
        id=db_item.id,
        object_type=db_item.object_type,
        object_id=db_item.object_id,
        operation=db_item.operation,
        before_snapshot=db_item.before_snapshot,
        after_snapshot=db_item.after_snapshot,
        diff=db_item.diff,
        test_status=db_item.test_status,
    )


def _to_domain_change_set(
    db_cs: StudioChangeSet,
    db_items: list[StudioChangeItem],
) -> ChangeSet:
    """Convert DB models to the domain ChangeSet dataclass."""
    return ChangeSet(
        id=db_cs.id,
        name=db_cs.name,
        author=db_cs.author,
        base_version=db_cs.base_version_hash,
        items=[_to_domain_item(i) for i in db_items],
        status=db_cs.status,
        conflict_status=db_cs.conflict_status,
    )


# ---------------------------------------------------------------------------
# Change Set CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/studio/change-sets",
    response_model=StudioChangeSetRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_change_set(
    body: StudioChangeSetCreate,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioChangeSetRead:
    """Create a new change set in DRAFT status."""
    cs = StudioChangeSet(
        organization_id=context.organization_id,
        name=body.name,
        author=context.principal_id,
        status="DRAFT",
        base_version_hash="0" * 64,
        conflict_status="CLEAN",
    )
    session.add(cs)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_set.create",
        resource_type="StudioChangeSet",
        resource_id=str(cs.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": body.name},
    )

    return StudioChangeSetRead.model_validate(cs)


@router.get(
    "/studio/change-sets",
    response_model=list[StudioChangeSetRead],
)
async def list_change_sets(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[StudioChangeSetRead]:
    """List change sets for the organisation."""
    stmt = (
        select(StudioChangeSet)
        .where(StudioChangeSet.organization_id == context.organization_id)
        .order_by(StudioChangeSet.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(StudioChangeSet.status == status_filter)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await session.scalars(stmt)).all()
    return [StudioChangeSetRead.model_validate(r) for r in rows]


@router.get(
    "/studio/change-sets/{change_set_id}",
    response_model=StudioChangeSetRead,
)
async def get_change_set(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioChangeSetRead:
    """Get a single change set with metadata."""
    cs = await _load_change_set(session, context, change_set_id)
    return StudioChangeSetRead.model_validate(cs)


# ---------------------------------------------------------------------------
# Change Item CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/studio/change-sets/{change_set_id}/items",
    response_model=StudioChangeItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_item(
    change_set_id: UUID,
    body: StudioChangeItemCreate,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioChangeItemRead:
    """Add a change item to a DRAFT change set."""
    cs = await _load_change_set(session, context, change_set_id)
    if cs.status != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail="items can only be added to DRAFT change sets",
        )

    diff = None
    if body.before_snapshot and body.after_snapshot:
        diff = compute_diff(body.before_snapshot, body.after_snapshot)

    item = StudioChangeItem(
        organization_id=context.organization_id,
        change_set_id=cs.id,
        object_type=body.object_type,
        object_id=body.object_id,
        operation=body.operation,
        before_snapshot=body.before_snapshot,
        after_snapshot=body.after_snapshot,
        diff=diff,
        test_status="UNTESTED",
    )
    session.add(item)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_item.add",
        resource_type="StudioChangeItem",
        resource_id=str(item.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "change_set_id": str(cs.id),
            "object_type": body.object_type,
            "object_id": body.object_id,
            "operation": body.operation,
        },
    )

    return StudioChangeItemRead.model_validate(item)


@router.get(
    "/studio/change-sets/{change_set_id}/items",
    response_model=list[StudioChangeItemRead],
)
async def list_items(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[StudioChangeItemRead]:
    """List all items in a change set."""
    cs = await _load_change_set(session, context, change_set_id)
    items = await _load_items(session, cs.id)
    return [StudioChangeItemRead.model_validate(i) for i in items]


@router.delete(
    "/studio/change-sets/{change_set_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_item(
    change_set_id: UUID,
    item_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove an item from a DRAFT change set."""
    cs = await _load_change_set(session, context, change_set_id)
    if cs.status != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail="items can only be removed from DRAFT change sets",
        )

    item = await session.get(StudioChangeItem, item_id)
    if item is None or item.change_set_id != cs.id:
        raise HTTPException(status_code=404, detail="change item not found")
    enforce_organization(context, item.organization_id)

    await session.delete(item)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_item.remove",
        resource_type="StudioChangeItem",
        resource_id=str(item_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"change_set_id": str(cs.id)},
    )


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


@router.post(
    "/studio/change-sets/{change_set_id}/test",
    response_model=StudioTestResultRead,
)
async def run_tests(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioTestResultRead:
    """Run the test harness against all items in the change set.

    Transitions the change set to TESTING, executes validators, then records
    the result.  All items must pass for the suite to pass.
    """
    cs = await _load_change_set(session, context, change_set_id)
    if cs.status not in ("DRAFT", "TESTING"):
        raise HTTPException(
            status_code=409,
            detail="tests can only be run on DRAFT or TESTING change sets",
        )

    cs.status = "TESTING"
    db_items = await _load_items(session, cs.id)
    domain_cs = _to_domain_change_set(cs, db_items)

    suite_result = run_test_suite(domain_cs)

    # Persist item-level test status back to DB
    for domain_item, db_item in zip(domain_cs.items, db_items):
        db_item.test_status = domain_item.test_status

    test_run = StudioTestRun(
        organization_id=context.organization_id,
        change_set_id=cs.id,
        started_at=suite_result.started_at,
        completed_at=suite_result.completed_at or datetime.now(UTC),
        passed=suite_result.passed,
        evidence=suite_result.evidence,
    )
    session.add(test_run)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_set.test",
        resource_type="StudioChangeSet",
        resource_id=str(cs.id),
        outcome="SUCCESS" if suite_result.passed else "FAILURE",
        correlation_id=get_correlation_id(),
        details={
            "passed": suite_result.passed,
            "total_items": suite_result.evidence.get("total_items"),
        },
    )

    return StudioTestResultRead.model_validate(test_run)


# ---------------------------------------------------------------------------
# Submit for review
# ---------------------------------------------------------------------------


@router.post(
    "/studio/change-sets/{change_set_id}/submit",
    response_model=StudioChangeSetRead,
)
async def submit_change_set(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioChangeSetRead:
    """Submit a change set for governance review.

    All items must have passed testing (test gate).
    """
    cs = await _load_change_set(session, context, change_set_id)
    if cs.status not in ("DRAFT", "TESTING"):
        raise HTTPException(
            status_code=409,
            detail="only DRAFT or TESTING change sets can be submitted",
        )

    # Enforce test gate: all items must be PASSED
    db_items = await _load_items(session, cs.id)
    if not db_items:
        raise HTTPException(
            status_code=409,
            detail="cannot submit an empty change set",
        )
    untested = [i for i in db_items if i.test_status != "PASSED"]
    if untested:
        raise HTTPException(
            status_code=409,
            detail=f"{len(untested)} item(s) have not passed testing",
        )

    cs.status = "SUBMITTED"
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_set.submit",
        resource_type="StudioChangeSet",
        resource_id=str(cs.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"item_count": len(db_items)},
    )
    record_outbox(
        session,
        organization_id=context.organization_id,
        event_type="studio.change_set.submitted",
        payload={
            "change_set_id": str(cs.id),
            "name": cs.name,
            "author": cs.author,
            "item_count": len(db_items),
        },
    )

    return StudioChangeSetRead.model_validate(cs)


# ---------------------------------------------------------------------------
# Diff view
# ---------------------------------------------------------------------------


@router.get(
    "/studio/change-sets/{change_set_id}/diff",
    response_model=StudioDiffRead,
)
async def view_diff(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioDiffRead:
    """View structured diffs for every item in the change set."""
    cs = await _load_change_set(session, context, change_set_id)
    db_items = await _load_items(session, cs.id)

    diff_items = []
    for item in db_items:
        entry = {
            "item_id": str(item.id),
            "object_type": item.object_type,
            "object_id": item.object_id,
            "operation": item.operation,
            "diff": item.diff,
        }
        diff_items.append(entry)

    return StudioDiffRead(
        change_set_id=cs.id,
        items=diff_items,
    )


# ---------------------------------------------------------------------------
# Impact preview
# ---------------------------------------------------------------------------


@router.get(
    "/studio/change-sets/{change_set_id}/impact",
    response_model=StudioImpactPreview,
)
async def impact_preview(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioImpactPreview:
    """Preview the impact of merging a change set."""
    cs = await _load_change_set(session, context, change_set_id)
    db_items = await _load_items(session, cs.id)
    domain_cs = _to_domain_change_set(cs, db_items)

    impact = compute_impact(domain_cs)

    return StudioImpactPreview(
        change_set_id=impact.change_set_id,
        affected_object_count=impact.affected_object_count,
        affected_objects=impact.affected_objects,
    )


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


@router.post(
    "/studio/change-sets/{change_set_id}/detect-conflicts",
    response_model=list[StudioConflict],
)
async def detect_conflicts_endpoint(
    change_set_id: UUID,
    current_state: dict[str, dict[str, Any]] | None = None,
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[StudioConflict]:
    """Detect conflicts between a change set and the current published state.

    The caller may supply `current_state` mapping "OBJECT_TYPE:object_id" to the
    current published snapshot.  If omitted, an empty state is used (no conflicts).
    """
    from typing import Any

    cs = await _load_change_set(session, context, change_set_id)
    db_items = await _load_items(session, cs.id)
    domain_cs = _to_domain_change_set(cs, db_items)

    state: dict[str, dict[str, Any]] = current_state or {}
    conflicts = detect_conflicts(domain_cs, state)

    if conflicts:
        cs.conflict_status = "CONFLICTED"
    else:
        cs.conflict_status = "CLEAN"
    record_audit(
        session,
        context,
        action="studio.detect_conflicts",
        resource_type="change_set",
        resource_id=str(change_set_id),
        outcome=cs.conflict_status,
        correlation_id=get_correlation_id(),
    )
    await session.flush()

    return [
        StudioConflict(
            object_type=c.object_type,
            object_id=c.object_id,
            field_name=c.field_name,
            change_set_value=c.change_set_value,
            current_value=c.current_value,
        )
        for c in conflicts
    ]
