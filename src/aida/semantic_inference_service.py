"""Shared metadata-only semantic proposal generation; caller owns the transaction."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.fleet import ensure_datasource_enabled
from aida.models import (
    AnalysisRun,
    DataSource,
    GovernanceReview,
    MetadataColumn,
    MetadataConstraint,
    MetadataEnrichmentProposal,
    MetadataSchema,
    MetadataTable,
    SemanticInferenceRun,
)
from aida.schemas import SemanticInferenceRequest
from aida.security import SecurityContext, enforce_organization
from aida.semantic_inference import SEMANTIC_INFERENCE_VERSION, enrich_with_optional_model


async def generate_semantic_inference(
    datasource_id: UUID,
    body: SemanticInferenceRequest,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
    table_ids: list[UUID] | None = None,
) -> SemanticInferenceRun:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    analysis_run = await session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.datasource_id == datasource.id,
            AnalysisRun.status == "COMPLETED",
        )
        .order_by(AnalysisRun.updated_at.desc())
        .limit(1)
    )
    if analysis_run is None:
        raise HTTPException(
            status_code=409,
            detail="a completed metadata scan is required before business inference",
        )
    table_filters = [MetadataTable.datasource_id == datasource.id, MetadataTable.status == "ACTIVE"]
    if table_ids is not None:
        table_filters.append(MetadataTable.id.in_(table_ids))
    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                *table_filters,
            )
            .order_by(MetadataSchema.name, MetadataTable.name)
            .limit(body.max_tables)
        )
    ).all()
    if not table_rows:
        raise HTTPException(status_code=409, detail="no active tables are available for inference")
    table_ids = [table.id for table, _schema in table_rows]
    columns = list(
        await session.scalars(
            select(MetadataColumn)
            .where(MetadataColumn.table_id.in_(table_ids), MetadataColumn.status == "ACTIVE")
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
        )
    )
    constraints = list(
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id.in_(table_ids),
                MetadataConstraint.status == "ACTIVE",
            )
        )
    )
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    constraints_by_table: dict[UUID, list[MetadataConstraint]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
    for constraint in constraints:
        constraints_by_table.setdefault(constraint.table_id, []).append(constraint)
    entries = [
        (
            table,
            schema.name,
            columns_by_table.get(table.id, []),
            constraints_by_table.get(table.id, []),
        )
        for table, schema in table_rows
    ]
    suggestions, model_route = await enrich_with_optional_model(
        session=session,
        settings=settings,
        organization_id=datasource.organization_id,
        entries=entries,
        use_model=body.use_model,
    )
    model_count = sum(engine == "LLM_ASSISTED" for _output, engine, _evidence in suggestions)
    run = SemanticInferenceRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=analysis_run.id,
        status="COMPLETED",
        engine_mode=("HYBRID" if model_count else "RULES_ONLY"),
        engine_version=SEMANTIC_INFERENCE_VERSION,
        model_route=model_route,
        table_count=len(entries),
        proposal_count=len(suggestions),
        model_enriched_count=model_count,
        rule_only_count=len(suggestions) - model_count,
        created_by=context.principal_id,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    for output, engine_type, evidence in suggestions:
        proposal_id = uuid4()
        review_id = uuid4()
        payload = output.model_dump(mode="json")
        evidence = {
            **evidence,
            "analysis_run_id": str(analysis_run.id),
            "evidence_ids": output.evidence_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"payload": payload, "evidence": evidence},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        session.add(
            GovernanceReview(
                id=review_id,
                organization_id=datasource.organization_id,
                object_type="METADATA_ENRICHMENT_PROPOSAL",
                object_id=str(proposal_id),
                requested_action="APPLY_BUSINESS_SEMANTICS",
                requested_by=context.principal_id,
            )
        )
        session.add(
            MetadataEnrichmentProposal(
                id=proposal_id,
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                inference_run_id=run.id,
                table_id=output.table_id,
                governance_review_id=review_id,
                engine_type=engine_type,
                engine_version=SEMANTIC_INFERENCE_VERSION,
                confidence=output.confidence,
                payload=payload,
                evidence=evidence,
                fingerprint=fingerprint,
                proposed_by=context.principal_id,
            )
        )
    audit_context = replace(context, organization_id=datasource.organization_id)
    record_audit(
        session,
        audit_context,
        action="business_semantics.inference.complete",
        resource_type="semantic_inference_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "engine_mode": run.engine_mode,
            "table_count": run.table_count,
            "proposal_count": run.proposal_count,
            "value_scope": "METADATA_ONLY",
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="semantic_inference_run",
        aggregate_id=str(run.id),
        event_type="business_semantics.proposals_created.v1",
        payload={
            "semantic_inference_run_id": str(run.id),
            "datasource_id": str(datasource.id),
            "proposal_count": len(suggestions),
        },
    )
    return run
