"""SM-4: propose candidate `SemanticMetric` definitions from approved
`MetadataBusinessAnnotation` evidence, reviewed through the shared
`governance_review` queue. See `aida.metric_suggestion_service` for the
deterministic evidence gathering, scoring, and composition; this module
only exposes it as a tenant-scoped, audited API -- the same split GL-9 uses
between `asset_description_service` and `asset_description_api`.
"""

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.metric_suggestion_service import (
    MINIMUM_EVIDENCE_FOR_METRIC_REVIEW,
    MetricEvidence,
    compose_metric_definition,
    ensure_reviewable,
    evidence_payload,
    is_numeric_physical_type,
    match_measure_keyword,
    score_evidence,
)
from aida.models import (
    AssetTermLink,
    DataSource,
    GlossaryTermVersion,
    GovernanceReview,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    SemanticMetricProposal,
    SemanticMetricVersion,
)
from aida.schemas import (
    GovernanceReviewRead,
    MetricSuggestionProposalGenerate,
    MetricSuggestionProposalRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["metric-suggestions"])

READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Viewer",
    "Auditor",
)
WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

# Bounds this evidence pass over: how many approved annotations one
# `generate` call scans, and how many active columns of each candidate
# table it inspects -- an inference pass over metadata must stay bounded
# regardless of how large the catalog is (module 07 doc §6 economics note).
_ANNOTATION_SCAN_LIMIT = 2000
_COLUMN_SCAN_LIMIT_PER_TABLE = 200
_TERM_QUERY_LIMIT = 10


def _proposal_read(
    proposal: SemanticMetricProposal, table_name: str, column_name: str
) -> MetricSuggestionProposalRead:
    return MetricSuggestionProposalRead(
        id=proposal.id,
        organization_id=proposal.organization_id,
        project_id=proposal.project_id,
        table_id=proposal.table_id,
        table_name=table_name,
        measure_column_id=proposal.measure_column_id,
        measure_column_name=column_name,
        source_annotation_id=proposal.source_annotation_id,
        proposed_slug=proposal.proposed_slug,
        proposed_name=proposal.proposed_name,
        proposed_description=proposal.proposed_description,
        proposed_aggregation=proposal.proposed_aggregation,
        proposed_grain=proposal.proposed_grain,
        accuracy_score=proposal.accuracy_score,
        clarity_score=proposal.clarity_score,
        style_score=proposal.style_score,
        completeness_score=proposal.completeness_score,
        overall_score=proposal.overall_score,
        evidence=proposal.evidence,
        status=proposal.status,
        governance_review_id=proposal.governance_review_id,
        published_metric_version_id=proposal.published_metric_version_id,
        created_by=proposal.created_by,
        reviewed_by=proposal.reviewed_by,
        reviewed_at=proposal.reviewed_at,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


@router.post(
    "/organizations/{organization_id}/metric-suggestions/generate",
    response_model=Page,
)
async def generate_metric_suggestion_proposals(
    organization_id: UUID,
    body: MetricSuggestionProposalGenerate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`,
    # not on `MetadataBusinessAnnotation` itself -- see `business_annotation_versions.py`.
    annotation_version_alias, annotation_version_ranked = current_version_alias()
    annotation_rows = (
        await session.execute(
            select(MetadataBusinessAnnotation, annotation_version_alias)
            .join(
                annotation_version_alias,
                annotation_version_alias.annotation_id == MetadataBusinessAnnotation.id,
            )
            .where(
                MetadataBusinessAnnotation.organization_id == organization_id,
                annotation_version_ranked.c.rn == 1,
            )
            .order_by(MetadataBusinessAnnotation.id)
            .limit(_ANNOTATION_SCAN_LIMIT)
        )
    ).all()
    existing_proposal_keys = {
        (row[0], row[1], row[2])
        for row in (
            await session.execute(
                select(
                    SemanticMetricProposal.table_id,
                    SemanticMetricProposal.measure_column_id,
                    SemanticMetricProposal.source_annotation_id,
                ).where(SemanticMetricProposal.organization_id == organization_id)
            )
        ).all()
    }
    published_measure_column_ids = set(
        await session.scalars(
            select(SemanticMetricVersion.measure_column_id).where(
                SemanticMetricVersion.organization_id == organization_id,
                SemanticMetricVersion.status == "PUBLISHED",
                SemanticMetricVersion.measure_column_id.is_not(None),
            )
        )
    )

    created: list[tuple[SemanticMetricProposal, str, str]] = []
    annotations_scanned = 0
    columns_scanned = 0
    for annotation, annotation_version in annotation_rows:
        if len(created) >= body.limit:
            break
        annotations_scanned += 1
        table = await session.get(MetadataTable, annotation.table_id)
        if table is None or table.status != "ACTIVE":
            continue
        datasource = await session.get(DataSource, table.datasource_id)
        if datasource is None:
            continue

        columns = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.table_id == table.id,
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.ordinal_position)
                .limit(_COLUMN_SCAN_LIMIT_PER_TABLE)
            )
        ).all()
        constraints = (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.table_id == table.id,
                    MetadataConstraint.status == "ACTIVE",
                )
            )
        ).all()
        key_column_names = {
            name
            for constraint in constraints
            if constraint.constraint_type in ("PRIMARY_KEY", "FOREIGN_KEY")
            for name in constraint.columns
        }
        term_rows = (
            await session.execute(
                select(GlossaryTermVersion.display_name)
                .join(AssetTermLink, AssetTermLink.term_id == GlossaryTermVersion.term_id)
                .where(
                    AssetTermLink.table_id == table.id,
                    GlossaryTermVersion.status == "APPROVED",
                )
                .limit(_TERM_QUERY_LIMIT)
            )
        ).all()
        bound_term_names = tuple(name for (name,) in term_rows)

        for column in columns:
            columns_scanned += 1
            if len(created) >= body.limit:
                break
            if column.name in key_column_names:
                continue
            if column.id in published_measure_column_ids:
                continue
            if not is_numeric_physical_type(column.physical_type):
                continue
            match = match_measure_keyword(column.name)
            if match is None:
                continue
            keyword, aggregation, match_kind = match
            # A bare CONTAINS match is too weak to act on -- only EXACT and
            # SUFFIX matches ever create a candidate; CONTAINS still exists
            # in `match_measure_keyword` for tests/introspection but never
            # reaches proposal generation.
            if match_kind == "CONTAINS":
                continue
            key = (table.id, column.id, annotation.id)
            if key in existing_proposal_keys:
                continue

            evidence = MetricEvidence(
                table_id=table.id,
                table_name=table.name,
                project_id=datasource.project_id,
                business_annotation_id=annotation.id,
                business_name=annotation_version.business_name,
                business_description=annotation_version.business_description,
                table_role=annotation_version.table_role,
                grain_statement=annotation_version.grain_statement,
                column_id=column.id,
                column_name=column.name,
                physical_type=column.physical_type,
                nullable=column.nullable,
                matched_keyword=keyword,
                suggested_aggregation=aggregation,
                match_kind=match_kind,
                bound_term_names=bound_term_names,
            )
            scores = score_evidence(evidence)
            slug, name, description = compose_metric_definition(evidence)
            proposal = SemanticMetricProposal(
                organization_id=organization_id,
                project_id=datasource.project_id,
                table_id=table.id,
                measure_column_id=column.id,
                source_annotation_id=annotation.id,
                proposed_slug=slug,
                proposed_name=name,
                proposed_description=description,
                proposed_aggregation=aggregation,
                proposed_grain=annotation_version.grain_statement,
                accuracy_score=scores.accuracy,
                clarity_score=scores.clarity,
                style_score=scores.style,
                completeness_score=scores.completeness,
                overall_score=scores.overall,
                evidence=evidence_payload(evidence),
                created_by=context.principal_id,
            )
            session.add(proposal)
            created.append((proposal, table.name, column.name))
            existing_proposal_keys.add(key)

    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="metric_suggestion.proposal.generate",
        resource_type="semantic_metric_proposal",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "annotations_scanned": annotations_scanned,
            "columns_scanned": columns_scanned,
            "proposals_created": len(created),
        },
    )
    await session.commit()
    return Page(
        items=[
            _proposal_read(proposal, table_name, column_name)
            for proposal, table_name, column_name in created
        ],
        limit=body.limit,
        offset=0,
        total=len(created),
    )


@router.get(
    "/organizations/{organization_id}/metric-suggestions",
    response_model=Page,
)
async def list_metric_suggestion_proposals(
    organization_id: UUID,
    proposal_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [SemanticMetricProposal.organization_id == organization_id]
    if proposal_status:
        filters.append(SemanticMetricProposal.status == proposal_status.upper())
    base = (
        select(SemanticMetricProposal, MetadataTable.name, MetadataColumn.name)
        .join(MetadataTable, MetadataTable.id == SemanticMetricProposal.table_id)
        .join(MetadataColumn, MetadataColumn.id == SemanticMetricProposal.measure_column_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    # Confidence sets review priority (highest-evidence first), mirroring
    # GL-9's asset-description-draft list ordering -- it never bypasses review.
    rows = (
        await session.execute(
            base.order_by(
                SemanticMetricProposal.overall_score.desc(),
                SemanticMetricProposal.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[
            _proposal_read(proposal, table_name, column_name)
            for proposal, table_name, column_name in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/metric-suggestions/{proposal_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_metric_suggestion_proposal(
    proposal_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    proposal = await session.get(SemanticMetricProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="metric suggestion proposal not found")
    enforce_organization(context, proposal.organization_id)
    if proposal.status == "PENDING_APPROVAL":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "SEMANTIC_METRIC_PROPOSAL",
                GovernanceReview.object_id == str(proposal.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if proposal.status != "DRAFT":
        raise HTTPException(
            status_code=409, detail="only a draft proposal can be submitted for review"
        )
    # The minimum-evidence gate: this is what keeps a near-baseless proposal
    # from ever reaching a state a reviewer could mistake for vetted work.
    # Enforced here, on the deterministic score alone, before any
    # GovernanceReview row is constructed.
    ensure_reviewable(proposal.overall_score)
    proposal.status = "PENDING_APPROVAL"
    review = GovernanceReview(
        organization_id=proposal.organization_id,
        object_type="SEMANTIC_METRIC_PROPOSAL",
        object_id=str(proposal.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    proposal.governance_review_id = review.id
    record_audit(
        session,
        replace(context, organization_id=proposal.organization_id),
        action="metric_suggestion.proposal.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"proposal_id": str(proposal.id), "overall_score": proposal.overall_score},
    )
    record_outbox(
        session,
        organization_id=proposal.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(proposal.id),
            "overall_score": proposal.overall_score,
        },
    )
    await session.commit()
    return review


__all__ = ["router", "MINIMUM_EVIDENCE_FOR_METRIC_REVIEW"]
