"""AT-1: REST surface for saved, scheduled bulk-metadata "playbook" objects.

Deliberately its own file with its own locally-scoped request/response
schemas, rather than adding to the shared `aida.api`/`aida.schemas` modules --
both are hot, frequently-edited files under concurrent development on this
branch, and this row's exit condition does not require touching either. See
`aida.playbooks` for the evaluation/execution engine this router calls into.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_bulk_actions import ALLOWED_CLASSIFICATIONS
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import MetadataPlaybook
from aida.playbook_dry_run import dry_run_playbook
from aida.playbooks import PlaybookRunOutcome, evaluate_and_run_playbook
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["playbooks"])

PLAYBOOK_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
PLAYBOOK_READ_ROLES = (*PLAYBOOK_WRITE_ROLES, "Analyst", "Viewer")

_ACTIONS = ("TAG", "CLASSIFY", "OWN", "CERTIFY")


def _validate_action_parameters(action: str, parameters: dict[str, Any]) -> None:
    """Shape-check `action_parameters` against the action it will be passed
    to -- the same required keys `aida.playbooks._apply_one_item` and
    `stewardship_service.apply_bulk_operation` read from it, checked here so
    a bad playbook fails at creation time rather than at its first scheduled
    run.
    """
    if action == "TAG":
        tag_key = parameters.get("tag_key")
        if not isinstance(tag_key, str) or not tag_key:
            raise ValueError("TAG requires a non-empty string 'tag_key'")
    elif action == "CLASSIFY":
        classification = parameters.get("classification")
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise ValueError("CLASSIFY requires a supported 'classification' value")
    elif action == "OWN":
        if parameters.get("owner_type") not in ("INDIVIDUAL", "GROUP"):
            raise ValueError("OWN requires owner_type of INDIVIDUAL or GROUP")
        owner_principal = parameters.get("owner_principal")
        if not isinstance(owner_principal, str) or len(owner_principal) < 2:
            raise ValueError("OWN requires a non-empty 'owner_principal'")
    else:
        assert action == "CERTIFY"
        rationale = parameters.get("rationale")
        if not isinstance(rationale, str) or len(rationale) < 10:
            raise ValueError("CERTIFY requires a 'rationale' of at least 10 characters")
        expires_after_days = parameters.get("expires_after_days")
        if (
            isinstance(expires_after_days, bool)
            or not isinstance(expires_after_days, int)
            or expires_after_days <= 0
        ):
            raise ValueError("CERTIFY requires a positive integer 'expires_after_days'")


class PlaybookCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    action: Literal[_ACTIONS]  # type: ignore[valid-type]
    datasource_id: UUID
    match_field: Literal["TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME"] = "TABLE_NAME"
    match_pattern: str = Field(min_length=1, max_length=255)
    column_name_pattern: str | None = Field(default=None, max_length=255)
    action_parameters: dict[str, Any]
    schedule_interval_minutes: int = Field(ge=5, le=10_080)
    auto_apply_max_items: int = Field(ge=0, default=0)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_action_parameters(self) -> PlaybookCreate:
        if self.action == "CLASSIFY" and not self.column_name_pattern:
            raise ValueError("CLASSIFY requires a 'column_name_pattern'")
        _validate_action_parameters(self.action, self.action_parameters)
        return self


class PlaybookUpdate(ApiModel):
    match_pattern: str | None = Field(default=None, min_length=1, max_length=255)
    column_name_pattern: str | None = Field(default=None, max_length=255)
    action_parameters: dict[str, Any] | None = None
    schedule_interval_minutes: int | None = Field(default=None, ge=5, le=10_080)
    auto_apply_max_items: int | None = Field(default=None, ge=0)
    enabled: bool | None = None

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> PlaybookUpdate:
        # Omission means unchanged; explicit null must not reach NOT NULL columns
        # or the action-parameter validator, which expects a dictionary.
        for field in self.model_fields_set - {"column_name_pattern"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to leave it unchanged")
        return self


class PlaybookRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    action: str
    datasource_id: UUID
    match_field: str
    match_pattern: str
    column_name_pattern: str | None
    action_parameters: dict[str, Any]
    schedule_interval_minutes: int
    auto_apply_max_items: int
    enabled: bool
    created_by: str
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlaybookRunResultRead(ApiModel):
    playbook_id: UUID
    matched_count: int
    outcome: str
    bulk_action_run_id: UUID | None
    bulk_stewardship_operation_id: UUID | None
    governance_review_id: UUID | None


class PlaybookActionAutomationRead(ApiModel):
    """R11-REV01: how this playbook's action is actually applied -- per action, not a
    blanket claim about playbooks. See `aida.playbook_dry_run`."""

    action: str
    subject_type: str
    has_automatic_branch: bool
    automatic_branch_enabled: bool
    automatic_when: str
    automatic_path: str
    automatic_principal: str
    involves_model: bool
    reviewed_operation_type: str
    compensating_operation_when_reviewed: str
    compensating_operation_when_automatic: str | None
    automatic_correction_reason: str | None


class PlaybookDryRunItemRead(ApiModel):
    subject_type: str
    subject_id: UUID
    qualified_name: str
    current_value: str | None
    proposed_value: str | None
    change: str
    evidence_version: str


class PlaybookDryRunRead(ApiModel):
    """R11-REV01: what a run would do now, without doing it."""

    playbook_id: UUID
    action: str
    enabled: bool
    rule_version: str
    evaluated_at: datetime
    matched_count: int
    tables_truncated: bool
    columns_truncated: bool
    auto_apply_max_items: int
    predicted_disposition: str
    automation: PlaybookActionAutomationRead
    items: list[PlaybookDryRunItemRead]


def _run_result_read(result: PlaybookRunOutcome) -> PlaybookRunResultRead:
    return PlaybookRunResultRead(
        playbook_id=result.playbook_id,
        matched_count=result.matched_count,
        outcome=result.outcome,
        bulk_action_run_id=result.bulk_action_run_id,
        bulk_stewardship_operation_id=result.bulk_stewardship_operation_id,
        governance_review_id=result.governance_review_id,
    )


async def _get_playbook_in_scope(
    session: AsyncSession, playbook_id: UUID, context: SecurityContext
) -> MetadataPlaybook:
    playbook = await session.get(MetadataPlaybook, playbook_id)
    if playbook is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    enforce_organization(context, playbook.organization_id)
    return playbook


@router.post(
    "/organizations/{organization_id}/playbooks",
    response_model=PlaybookRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_playbook(
    organization_id: UUID,
    body: PlaybookCreate,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> MetadataPlaybook:
    enforce_organization(context, organization_id)
    existing = await session.scalar(
        select(MetadataPlaybook).where(
            MetadataPlaybook.organization_id == organization_id,
            MetadataPlaybook.name == body.name,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="a playbook with this name already exists")
    playbook = MetadataPlaybook(
        organization_id=organization_id,
        created_by=context.principal_id,
        **body.model_dump(),
    )
    session.add(playbook)
    await session.flush()
    record_audit(
        session,
        context,
        action="playbook.create",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": playbook.name, "action": playbook.action},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="metadata_playbook",
        aggregate_id=str(playbook.id),
        event_type="metadata.playbook.created.v1",
        payload={"playbook_id": str(playbook.id), "name": playbook.name, "action": playbook.action},
    )
    await session.commit()
    return playbook


@router.get("/organizations/{organization_id}/playbooks", response_model=Page)
async def list_playbooks(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    rows = (
        await session.scalars(
            select(MetadataPlaybook)
            .where(MetadataPlaybook.organization_id == organization_id)
            .order_by(MetadataPlaybook.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    count = await session.scalar(
        select(func.count()).select_from(
            select(MetadataPlaybook.id)
            .where(MetadataPlaybook.organization_id == organization_id)
            .subquery()
        )
    )
    return Page(
        items=[PlaybookRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=count,
    )


@router.get("/playbooks/{playbook_id}", response_model=PlaybookRead)
async def get_playbook(
    playbook_id: UUID,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> MetadataPlaybook:
    return await _get_playbook_in_scope(session, playbook_id, context)


@router.patch("/playbooks/{playbook_id}", response_model=PlaybookRead)
async def update_playbook(
    playbook_id: UUID,
    body: PlaybookUpdate,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> MetadataPlaybook:
    playbook = await _get_playbook_in_scope(session, playbook_id, context)
    updates = body.model_dump(exclude_unset=True)
    if "action_parameters" in updates or "column_name_pattern" in updates:
        candidate_parameters = updates.get("action_parameters", playbook.action_parameters)
        candidate_column_pattern = updates.get(
            "column_name_pattern", playbook.column_name_pattern
        )
        if playbook.action == "CLASSIFY" and not candidate_column_pattern:
            raise HTTPException(
                status_code=422, detail="CLASSIFY requires a 'column_name_pattern'"
            )
        try:
            _validate_action_parameters(playbook.action, candidate_parameters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field, value in updates.items():
        setattr(playbook, field, value)
    await session.flush()
    record_audit(
        session,
        context,
        action="playbook.update",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"updated_fields": sorted(updates.keys())},
    )
    await session.commit()
    return playbook


@router.delete("/playbooks/{playbook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_playbook(
    playbook_id: UUID,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> None:
    playbook = await _get_playbook_in_scope(session, playbook_id, context)
    record_audit(
        session,
        context,
        action="playbook.delete",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": playbook.name},
    )
    await session.delete(playbook)
    await session.commit()


@router.post(
    "/playbooks/{playbook_id}/run",
    response_model=PlaybookRunResultRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_playbook_now(
    playbook_id: UUID,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> PlaybookRunResultRead:
    """Manual out-of-cycle trigger -- evaluates and applies/queues right now,
    independent of `schedule_interval_minutes`, using the exact same
    `aida.playbooks.evaluate_and_run_playbook` the fleet scheduler calls.
    """
    playbook = await _get_playbook_in_scope(session, playbook_id, context)
    if not playbook.enabled:
        raise HTTPException(status_code=409, detail="playbook is disabled")
    result = await evaluate_and_run_playbook(session, playbook)
    await session.commit()
    return _run_result_read(result)


@router.get("/playbooks/{playbook_id}/dry-run", response_model=PlaybookDryRunRead)
async def dry_run_playbook_now(
    playbook_id: UUID,
    context: SecurityContext = Depends(require_roles(*PLAYBOOK_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> PlaybookDryRunRead:
    """R11-REV01: evaluate this playbook's rule against the live catalog and report what a
    run would do -- the subjects, each one's before/after and evidence version, the rule's
    version, and whether the action would be applied automatically or queued for review --
    without applying, queuing or recording anything (not even `last_run_at`).

    Same population as `run`: previewing what you may trigger. A disabled playbook can be
    previewed; that is how a steward checks one before enabling it.
    """
    playbook = await _get_playbook_in_scope(session, playbook_id, context)
    preview = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    automation = preview.automation
    return PlaybookDryRunRead(
        playbook_id=preview.playbook_id,
        action=preview.action,
        enabled=preview.enabled,
        rule_version=preview.rule_version,
        evaluated_at=preview.evaluated_at,
        matched_count=preview.matched_count,
        tables_truncated=preview.tables_truncated,
        columns_truncated=preview.columns_truncated,
        auto_apply_max_items=preview.auto_apply_max_items,
        predicted_disposition=preview.predicted_disposition,
        automation=PlaybookActionAutomationRead(
            action=automation.action,
            subject_type=automation.subject_type,
            has_automatic_branch=automation.has_automatic_branch,
            automatic_branch_enabled=preview.auto_apply_max_items > 0,
            automatic_when="0 < matched_count <= auto_apply_max_items",
            automatic_path=automation.automatic_path,
            automatic_principal=automation.automatic_principal,
            involves_model=automation.involves_model,
            reviewed_operation_type=automation.reviewed_operation_type,
            compensating_operation_when_reviewed=automation.compensating_operation_when_reviewed,
            compensating_operation_when_automatic=automation.compensating_operation_when_automatic,
            automatic_correction_reason=automation.automatic_correction_reason,
        ),
        items=[
            PlaybookDryRunItemRead(
                subject_type=item.subject_type,
                subject_id=item.subject_id,
                qualified_name=item.qualified_name,
                current_value=item.current_value,
                proposed_value=item.proposed_value,
                change=item.change,
                evidence_version=item.evidence_version,
            )
            for item in preview.items
        ],
    )
