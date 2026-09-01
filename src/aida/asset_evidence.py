"""UX-13: `AssetEvidenceRead` composition for one table (module 21 experience
shell, built on module 09 lineage).

`Docs/60-delivery/03-tracker.md` UX-13's exit criterion is that every claim in
the pane carries a `source` string, composing business meaning, ownership and
certification (GL-2/GL-5), data quality, consumption lineage (CX-4) and AI
decision lineage including the refusal edge where one exists (LN-3). Where a
field is already batch-composed by UX-12's `catalog_read_model` (description,
owner, certification state, glossary terms, the open-incident set), this
module reuses those functions directly rather than re-deriving the same
precedence rules for a single table -- the same "reused directly, not a fork"
discipline GL-6 applied to DQ-1's routing engine.

Field sources
-------------
``BUSINESS_MEANING``
    The same description precedence `catalog_read_model._description` uses
    (approved GL-9 documentation, then a pending draft named explicitly as a
    proposal per ADR-0001, then an approved business annotation, then the
    connector-sourced comment), plus one item per APPROVED glossary term
    bound via `AssetTermLink` (GL-8/SM-2).
``OWNERSHIP`` / ``CERTIFICATION``
    GL-2's earliest active `OwnershipAssignment`, falling back to
    documentation's `owner_principal`; GL-5/CT-5's certification state via
    `asset_certification_is_active`, the same query-time projection every
    other certification caller uses.
``DATA_QUALITY``
    An item per open (`OPEN`/`ACKNOWLEDGED`) `DataQualityIncident`, plus a
    summary item for the overall state (`INCIDENT_OPEN` / `STALE` /
    `PASSING` / `UNKNOWN`) computed by the same predicate `catalog_read_model`
    uses so the two surfaces cannot disagree.
``CONSUMPTION``
    CX-4 `ConsumptionRecord` rows for `resource_type="metadata_table"`,
    newest first, bounded by `consumption_limit` (`consumption_lineage.py`).
``AI_DECISION``
    LN-3 decisions targeting this table's node (`ai_decision_lineage.py`,
    `target_node == f"table:{id}"`), plus the refusal edge where one exists.
    A `REFUSAL` decision's own `target_node` is the agent run it refused, not
    the asset (`agent_orchestrator.py`), so a refusal cannot be looked up by
    asset id directly -- instead this composes the refusal for a *run that
    considered this table*: the run ids from this table's own
    RETRIEVAL_SELECTED/REJECTED decisions, then any REFUSAL recorded against
    one of those run ids. A table an agent never looked at surfaces no
    refusal, which is the honest answer, not a false negative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.ai_decision_lineage import get_decisions_for_asset
from aida.catalog_read_model import (
    _business_annotations,
    _certification_state,
    _description,
    _earliest_active_owners,
    _glossary_terms_by_table,
    _latest_approved_documentation,
    _latest_certifications,
    _latest_observation_at,
    _latest_pending_drafts,
    _open_incident_table_ids,
    _quality_state,
)
from aida.consumption_lineage import get_consumption_for_resource
from aida.models import AiDecisionRecord, DataQualityIncident, MetadataTable
from aida.schemas import AssetEvidenceRead, EvidenceItemRead

_OPEN_INCIDENT_STATUSES = ("OPEN", "ACKNOWLEDGED")
_INCIDENT_ITEM_LIMIT = 20


async def _open_incidents(
    session: AsyncSession, table_id: UUID, *, limit: int
) -> list[DataQualityIncident]:
    rows = await session.scalars(
        select(DataQualityIncident)
        .where(
            DataQualityIncident.table_id == table_id,
            DataQualityIncident.status.in_(_OPEN_INCIDENT_STATUSES),
        )
        .order_by(DataQualityIncident.last_observed_at.desc())
        .limit(limit)
    )
    return list(rows.all())


async def _refusals_for_runs(
    session: AsyncSession, run_ids: set[UUID], *, limit: int
) -> list[AiDecisionRecord]:
    if not run_ids:
        return []
    rows = await session.scalars(
        select(AiDecisionRecord)
        .where(
            AiDecisionRecord.run_id.in_(run_ids),
            AiDecisionRecord.decision_type == "REFUSAL",
        )
        .order_by(AiDecisionRecord.decided_at.desc())
        .limit(limit)
    )
    return list(rows.all())


async def compose_asset_evidence(
    session: AsyncSession,
    table: MetadataTable,
    *,
    now: datetime | None = None,
    consumption_limit: int = 20,
    ai_decision_limit: int = 20,
) -> AssetEvidenceRead:
    moment = now or datetime.now(UTC)
    table_ids = [table.id]
    items: list[EvidenceItemRead] = []

    # --- BUSINESS_MEANING ---------------------------------------------------
    documentation = (await _latest_approved_documentation(session, table_ids)).get(table.id)
    pending_draft = (await _latest_pending_drafts(session, table_ids)).get(table.id)
    annotation = (await _business_annotations(session, table_ids)).get(table.id)
    description, description_is_proposed = _description(
        table, documentation=documentation, pending_draft=pending_draft, annotation=annotation
    )
    if description:
        if documentation is not None:
            items.append(
                EvidenceItemRead(
                    category="BUSINESS_MEANING",
                    claim=f"Description: {description}",
                    source="asset_documentation_version (GL-9, APPROVED)",
                    occurred_at=documentation.approved_at,
                )
            )
        elif description_is_proposed:
            # ADR-0001: a pending draft is a model proposal, not an
            # established fact -- named explicitly as such in the claim.
            items.append(
                EvidenceItemRead(
                    category="BUSINESS_MEANING",
                    claim=f"Proposed description, awaiting review: {description}",
                    source="asset_description_draft (GL-9, PENDING_APPROVAL)",
                    occurred_at=pending_draft.created_at if pending_draft else None,
                )
            )
        elif annotation is not None:
            items.append(
                EvidenceItemRead(
                    category="BUSINESS_MEANING",
                    claim=f"Description: {description}",
                    source="metadata_business_annotation_version (AT-6, APPROVED)",
                    occurred_at=annotation.approved_at,
                )
            )
        else:
            items.append(
                EvidenceItemRead(
                    category="BUSINESS_MEANING",
                    claim=f"Description: {description}",
                    source="metadata_table.source_description (connector-scanned)",
                )
            )

    glossary_terms = (await _glossary_terms_by_table(session, table_ids)).get(table.id, [])
    for term in glossary_terms:
        items.append(
            EvidenceItemRead(
                category="BUSINESS_MEANING",
                claim=f"Bound to glossary term '{term}'",
                source="asset_term_link + glossary_term_version (GL-8/SM-2, APPROVED)",
            )
        )

    # --- OWNERSHIP / CERTIFICATION (GL-2 / GL-5) -----------------------------
    assigned_owner = (await _earliest_active_owners(session, table_ids)).get(table.id)
    owner = assigned_owner
    if owner is None and documentation is not None:
        owner = documentation.owner_principal
    if owner:
        items.append(
            EvidenceItemRead(
                category="OWNERSHIP",
                claim=f"Owned by {owner}",
                source="ownership_assignment (GL-2, ACTIVE)"
                if assigned_owner
                else "asset_documentation_version.owner_principal (GL-9 fallback)",
            )
        )

    certification = (await _latest_certifications(session, table_ids)).get(table.id)
    certification_state, certification_expires_at = _certification_state(
        certification, now=moment
    )
    if certification_state != "NONE":
        expiry = (
            f", expires {certification_expires_at.isoformat()}"
            if certification_expires_at
            else ""
        )
        items.append(
            EvidenceItemRead(
                category="CERTIFICATION",
                claim=f"Certification: {certification_state}{expiry}",
                source="asset_certification (GL-5/CT-5)",
                occurred_at=certification.created_at if certification else None,
            )
        )

    # --- DATA_QUALITY ---------------------------------------------------------
    open_incidents = await _open_incidents(session, table.id, limit=_INCIDENT_ITEM_LIMIT)
    open_incident_ids = await _open_incident_table_ids(session, table_ids)
    latest_observation_at = await _latest_observation_at(session, table_ids)
    quality_state = _quality_state(
        table.id,
        open_incident_ids=open_incident_ids,
        latest_observation_at=latest_observation_at,
        now=moment,
    )
    items.append(
        EvidenceItemRead(
            category="DATA_QUALITY",
            claim=f"Overall quality state: {quality_state}",
            source="data_quality_incident + data_quality_observation (module 11, "
            "same predicate as UX-12)",
            occurred_at=latest_observation_at.get(table.id),
        )
    )
    for incident in open_incidents:
        items.append(
            EvidenceItemRead(
                category="DATA_QUALITY",
                claim=f"Open {incident.severity} {incident.anomaly_type} incident: "
                f"{incident.summary}",
                source=f"data_quality_incident:{incident.id}",
                occurred_at=incident.last_observed_at,
            )
        )

    # --- CONSUMPTION (CX-4) ----------------------------------------------------
    consumption_records, consumption_total = await get_consumption_for_resource(
        session,
        organization_id=table.organization_id,
        resource_type="metadata_table",
        resource_id=str(table.id),
        limit=consumption_limit,
    )
    if consumption_total:
        items.append(
            EvidenceItemRead(
                category="CONSUMPTION",
                claim=f"{consumption_total} total consumption event(s) recorded",
                source="consumption_record (CX-4)",
            )
        )
    for record in consumption_records:
        items.append(
            EvidenceItemRead(
                category="CONSUMPTION",
                claim=f"Consumed by {record.consumer_id} ({record.consumer_type}) "
                f"via {record.channel}",
                source=f"consumption_record:{record.id}",
                occurred_at=record.consumed_at,
            )
        )

    # --- AI_DECISION (LN-3), including the refusal edge -------------------------
    decisions = await get_decisions_for_asset(
        session, f"table:{table.id}", limit=ai_decision_limit
    )
    for decision in decisions:
        items.append(
            EvidenceItemRead(
                category="AI_DECISION",
                claim=f"{decision.decision_type} by {decision.source_node}: {decision.reason}",
                source=f"ai_decision_record:{decision.id}",
                occurred_at=decision.decided_at,
            )
        )
    run_ids = {decision.run_id for decision in decisions}
    refusals = await _refusals_for_runs(session, run_ids, limit=ai_decision_limit)
    for refusal in refusals:
        items.append(
            EvidenceItemRead(
                category="AI_DECISION",
                claim=f"Run refused after considering this asset: {refusal.reason}",
                source=f"ai_decision_record:{refusal.id} (REFUSAL, run {refusal.run_id})",
                occurred_at=refusal.decided_at,
            )
        )

    return AssetEvidenceRead(
        table_id=table.id,
        table_name=table.name,
        generated_at=moment,
        items=items,
    )
