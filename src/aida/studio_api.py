"""Studio authoring environment REST API.

Provides endpoints for managing change sets, change items, testing,
conflict detection, diff viewing, and impact preview for semantic model
objects.

Change sets follow the lifecycle: DRAFT -> TESTING -> SUBMITTED -> MERGED/REJECTED.
Only tested change sets can be submitted for governance review (test gate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    StudioChangeItem,
    StudioChangeSet,
    StudioEvalQuestion,
    StudioEvalResult,
    StudioEvalRun,
    StudioTestRun,
)
from aida.schemas import (
    StudioChangeItemCreate,
    StudioChangeItemRead,
    StudioChangeSetCreate,
    StudioChangeSetRead,
    StudioConflict,
    StudioDiffRead,
    StudioEvalMiningResult,
    StudioEvalQuestionRead,
    StudioEvalResultRead,
    StudioEvalRunRead,
    StudioImpactPreview,
    StudioParameterContractValidateRequest,
    StudioParameterContractValidateResult,
    StudioTestResultRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.studio import (
    ChangeItem,
    ChangeSet,
    compute_diff,
    compute_impact,
    detect_conflicts,
    validate_parameter_contract,
)
from aida.studio_eval import check_eval_regressions, mine_eval_questions
from aida.studio_test_harness import run_test_suite

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


def _literal[T: str](value: str, allowed: tuple[T, ...], *, field: str) -> T:
    """Narrow a database `String` column to the Literal the domain type declares.

    A `cast()` here would type-check and prove nothing: these columns carry no CHECK
    constraint, so a value written by an older release, a manual fix or a future enum
    member would flow into a domain object that claims it cannot exist. This fails closed
    instead (INV-4) — a value the domain does not model is a 500 naming the field, not a
    silently malformed object handed to a caller.
    """
    if value not in allowed:
        raise ValueError(f"{field} holds an unmodelled value: {value!r}")
    return value  # type: ignore[return-value]

def _to_domain_item(db_item: StudioChangeItem) -> ChangeItem:
    """Convert a DB model item to the domain ChangeItem dataclass."""
    return ChangeItem(
        id=db_item.id,
        object_type=_literal(
            db_item.object_type,
            ("METRIC", "TOOL", "TERM", "CONTEXT_PRODUCT"),
            field="StudioChangeItem.object_type",
        ),
        object_id=db_item.object_id,
        operation=_literal(
            db_item.operation, ("CREATE", "UPDATE", "DELETE"), field="StudioChangeItem.operation"
        ),
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
        status=_literal(
            db_cs.status,
            ("DRAFT", "TESTING", "SUBMITTED", "MERGED", "REJECTED"),
            field="StudioChangeSet.status",
        ),
        conflict_status=_literal(
            db_cs.conflict_status,
            ("CLEAN", "CONFLICTED", "RESOLVED"),
            field="StudioChangeSet.conflict_status",
        ),
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

    Also runs the ST-A8 regression gate: for every changed metric/tool that
    has a usage-derived `StudioEvalQuestion` (mined from real consumption or
    BI lineage edges via `POST /v1/studio/eval/mine`), re-checks that it
    still passes the same validator its own item-level test uses. A mined
    question that now fails fails the overall suite, exactly like any other
    failing test -- this is what makes it a *regression* gate rather than a
    duplicate of the item-level check: it fires even for objects whose own
    edit would otherwise look shaped correctly, and it is recorded separately
    (`StudioEvalRun`/`StudioEvalResult`) so the failure is attributable to a
    specific previously-resolving question, not just "something failed."
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
    for domain_item, db_item in zip(domain_cs.items, db_items, strict=False):
        db_item.test_status = domain_item.test_status

    eval_started_at = datetime.now(UTC)
    touched_keys = {(item.object_type, item.object_id) for item in domain_cs.items}
    questions: list[StudioEvalQuestion] = []
    if touched_keys:
        questions = list(
            (
                await session.scalars(
                    select(StudioEvalQuestion).where(
                        StudioEvalQuestion.organization_id == context.organization_id,
                        StudioEvalQuestion.object_type.in_({k[0] for k in touched_keys}),
                        StudioEvalQuestion.object_id.in_({k[1] for k in touched_keys}),
                    )
                )
            ).all()
        )
        questions = [q for q in questions if (q.object_type, q.object_id) in touched_keys]

    eval_checks = check_eval_regressions(domain_cs.items, questions)
    eval_failed = [c for c in eval_checks if not c.result.passed]
    eval_passed = len(eval_failed) == 0
    eval_completed_at = datetime.now(UTC)

    eval_run = StudioEvalRun(
        organization_id=context.organization_id,
        change_set_id=cs.id,
        started_at=eval_started_at,
        completed_at=eval_completed_at,
        passed=eval_passed,
        evidence={
            "checked": len(eval_checks),
            "failed": len(eval_failed),
            "failed_question_ids": [str(c.question.id) for c in eval_failed],
        },
    )
    session.add(eval_run)
    await session.flush()
    for check in eval_checks:
        session.add(
            StudioEvalResult(
                organization_id=context.organization_id,
                eval_run_id=eval_run.id,
                eval_question_id=check.question.id,
                passed=check.result.passed,
                evidence={
                    "object_type": check.question.object_type,
                    "object_id": check.question.object_id,
                    "label": check.question.label,
                    "failures": check.result.failures,
                },
                run_at=eval_completed_at,
            )
        )

    overall_passed = suite_result.passed and eval_passed
    combined_evidence = dict(suite_result.evidence)
    combined_evidence["eval_regression_checked"] = len(eval_checks)
    combined_evidence["eval_regression_failed"] = len(eval_failed)

    test_run = StudioTestRun(
        organization_id=context.organization_id,
        change_set_id=cs.id,
        started_at=suite_result.started_at,
        completed_at=suite_result.completed_at or datetime.now(UTC),
        passed=overall_passed,
        evidence=combined_evidence,
    )
    session.add(test_run)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.change_set.test",
        resource_type="StudioChangeSet",
        resource_id=str(cs.id),
        outcome="SUCCESS" if overall_passed else "FAILURE",
        correlation_id=get_correlation_id(),
        details={
            "passed": overall_passed,
            "total_items": suite_result.evidence.get("total_items"),
            "eval_regression_checked": len(eval_checks),
            "eval_regression_failed": len(eval_failed),
        },
    )
    if eval_checks:
        record_audit(
            session,
            context,
            action="studio.eval_run.run",
            resource_type="StudioEvalRun",
            resource_id=str(eval_run.id),
            outcome="SUCCESS" if eval_passed else "FAILURE",
            correlation_id=get_correlation_id(),
            details={
                "checked": len(eval_checks),
                "failed": len(eval_failed),
                "failed_question_ids": [str(c.question.id) for c in eval_failed],
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

    All items must have passed testing (test gate), and -- ST-A8 -- the most
    recent eval-regression run for this change set (if any mined questions
    were touched) must also have passed. A change set that regresses a
    usage-derived question is blocked here exactly like a change set with a
    malformed item, even when its items individually look well-formed.
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

    latest_eval_run = (
        await session.scalars(
            select(StudioEvalRun)
            .where(StudioEvalRun.change_set_id == cs.id)
            .order_by(StudioEvalRun.started_at.desc())
            .limit(1)
        )
    ).first()

    reasons: list[str] = []
    if untested:
        reasons.append(f"{len(untested)} item(s) have not passed testing")
    if latest_eval_run is not None and not latest_eval_run.passed:
        failed_question_ids = latest_eval_run.evidence.get("failed_question_ids", [])
        reasons.append(
            f"{len(failed_question_ids)} mined eval question(s) regressed: "
            f"{failed_question_ids}"
        )
    if reasons:
        raise HTTPException(status_code=409, detail="; ".join(reasons))

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
        aggregate_type="studio_change_set",
        aggregate_id=str(cs.id),
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


# ---------------------------------------------------------------------------
# ST-A4: parameter-contract designer
# ---------------------------------------------------------------------------


@router.post(
    "/studio/parameter-contracts/validate",
    response_model=StudioParameterContractValidateResult,
)
async def validate_parameter_contract_endpoint(
    body: StudioParameterContractValidateRequest,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
) -> StudioParameterContractValidateResult:
    """Validate a typed, enum-bound tool parameter contract as an author designs it.

    Stateless: does not require an existing change set or change item. Reuses the
    real `ToolParameterDefinition` schema and SQL renderer (the same contract module
    14's tool gateway enforces at publish time), cross-checks declared parameters
    against the SQL template's actual placeholders, and proves the contract renders
    against one representative in-bounds value per parameter. The same check runs
    automatically as part of the TOOL change-item test gate (`run_test_suite`); this
    endpoint lets an author validate incrementally while still drafting.
    """
    result = validate_parameter_contract(
        sql_template=body.sql_template,
        raw_definitions=body.parameters,
        dialect=body.dialect,
    )
    return StudioParameterContractValidateResult(
        valid=result.valid,
        errors=result.errors,
        definitions=result.definitions,
        sample_rendered_sql=result.sample_rendered_sql,
    )


# ---------------------------------------------------------------------------
# ST-A8: usage-derived eval question suite
# ---------------------------------------------------------------------------


@router.post(
    "/studio/eval/mine",
    response_model=StudioEvalMiningResult,
)
async def mine_eval_suite(
    context: SecurityContext = Depends(require_roles(*_STUDIO_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioEvalMiningResult:
    """Mine recent consumption and BI lineage edges into eval questions.

    Explicit, API-triggered pass rather than a scheduled job for now (no
    scheduler wiring in this increment) -- idempotent, so calling it
    repeatedly (by hand, or from a future schedule) never duplicates a
    question for an object already mined.
    """
    organization_id = context.require_organization()
    result = await mine_eval_questions(session, organization_id=organization_id)
    await session.flush()

    record_audit(
        session,
        context,
        action="studio.eval.mine",
        resource_type="StudioEvalQuestion",
        resource_id=None,
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "consumption_edges_scanned": result.consumption_edges_scanned,
            "bi_edges_scanned": result.bi_edges_scanned,
            "questions_created": result.questions_created,
            "questions_already_mined": result.questions_already_mined,
            "truncated": result.truncated,
        },
    )
    await session.commit()

    return StudioEvalMiningResult(
        consumption_edges_scanned=result.consumption_edges_scanned,
        bi_edges_scanned=result.bi_edges_scanned,
        questions_created=result.questions_created,
        questions_already_mined=result.questions_already_mined,
        truncated=result.truncated,
    )


@router.get(
    "/studio/eval/questions",
    response_model=list[StudioEvalQuestionRead],
)
async def list_eval_questions(
    object_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[StudioEvalQuestionRead]:
    """List the mined eval question corpus for the organisation."""
    stmt = (
        select(StudioEvalQuestion)
        .where(StudioEvalQuestion.organization_id == context.organization_id)
        .order_by(StudioEvalQuestion.mined_at.desc())
    )
    if object_type:
        stmt = stmt.where(StudioEvalQuestion.object_type == object_type)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await session.scalars(stmt)).all()
    return [StudioEvalQuestionRead.model_validate(r) for r in rows]


@router.get(
    "/studio/change-sets/{change_set_id}/eval",
    response_model=StudioEvalRunRead,
)
async def get_latest_eval_run(
    change_set_id: UUID,
    context: SecurityContext = Depends(require_roles(*_STUDIO_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StudioEvalRunRead:
    """Return the most recent eval-regression run for a change set."""
    cs = await _load_change_set(session, context, change_set_id)

    eval_run = (
        await session.scalars(
            select(StudioEvalRun)
            .where(StudioEvalRun.change_set_id == cs.id)
            .order_by(StudioEvalRun.started_at.desc())
            .limit(1)
        )
    ).first()
    if eval_run is None:
        raise HTTPException(status_code=404, detail="no eval run recorded for this change set")

    result_rows = (
        await session.scalars(
            select(StudioEvalResult).where(StudioEvalResult.eval_run_id == eval_run.id)
        )
    ).all()

    results = [
        StudioEvalResultRead(
            eval_question_id=r.eval_question_id,
            object_type=str(r.evidence.get("object_type", "")),
            object_id=str(r.evidence.get("object_id", "")),
            label=str(r.evidence.get("label", "")),
            passed=r.passed,
            evidence=r.evidence,
        )
        for r in result_rows
    ]
    return StudioEvalRunRead(
        id=eval_run.id,
        change_set_id=eval_run.change_set_id,
        started_at=eval_run.started_at,
        completed_at=eval_run.completed_at,
        passed=eval_run.passed,
        evidence=eval_run.evidence,
        results=results,
    )
