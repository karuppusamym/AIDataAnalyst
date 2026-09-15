"""R11-FP16: rebuild what a source change made stale, through the services that own each artifact.

`change_signal_processing` holds what a redefined view can have broken, and the lineage agent reads
the new definition again. This pass carries the change the rest of the way down the dependency
chain. It never publishes anything: every rebuilt artifact is a draft in the review queue its kind
already has, decided by a person, so nothing becomes trusted context without review.

For each organization:

1. **Lineage.** A held view's ACTIVE lineage edges that its current definition no longer produces
   are marked SUPERSEDED. The edges it does produce come from the lineage agent and a reviewer.
2. **Tools.** A PUBLISHED tool version generated from a view or routine whose definition has moved
   (`tool_source_binding`) gets a version regenerated from the current definition -- keeping the
   published version's name, description, roles and semantic model -- submitted for review.
3. **Descriptions.** A view whose current approved description was drafted against another
   definition gets a draft from today's evidence, submitted for review once it clears the bar.
4. **Context products.** A PUBLISHED context product version that pins a tool version since
   superseded gets a version re-pinned to that tool's published version, submitted for review.
5. **Holds.** A source-change hold on a redefined view is resolved once nothing standing on the
   view is stale: its change signals are processed, every tool generated from it matches its
   definition, every other published tool reading it was approved after the change, its approved
   description matches its definition, and no published context product pins a superseded tool
   that reads it.

Nothing is drafted where a newer draft already waits for review. A rebuild that cannot be made (a
view no longer eligible for a tool, a description below the evidence bar) is counted by its code
and retried on the next pass; the hold stays. Each rebuild runs in its own savepoint. Value-free:
ids, codes and counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    compose_draft_text,
    definition_moved,
    evidence_payload,
    gather_evidence,
    score_evidence,
    table_refusal,
    text_fingerprint,
)
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import ACTION_VIEW_REDEFINED, SOURCE_CHANGE_ANOMALY_TYPE
from aida.config import Settings
from aida.context_product_api import (
    _definition_from_version,
    apply_context_product_definition,
    replace_context_product_role_bindings,
    validate_context_product_references,
)
from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.events import record_audit, record_outbox
from aida.ingest_screening import CLEAN
from aida.lineage_agent import as_create_view
from aida.models import (
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    ContextProduct,
    ContextProductVersion,
    DataQualityIncident,
    DataSource,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataSchema,
    MetadataTable,
    Project,
    ViewLineageEdge,
)
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    ProcedureToolBlueprintError,
    build_procedure_tool_blueprint,
    resolve_procedure_tool_source,
)
from aida.quality_coupling import resolve_table_ids
from aida.routine_lineage_edges import RoutineNotEligibleError
from aida.schemas import GovernedToolVersionCreate
from aida.security import SecurityContext
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES
from aida.tool_drafts import ToolDraftRefused, stage_tool_version_draft
from aida.tool_source_binding import (
    REASON_SOURCE_MISSING,
    REASON_SOURCE_RETIRED,
    source_binding_drift,
)
from aida.view_tool_blueprint import (
    ViewNotEligibleError,
    ViewToolBlueprintError,
    build_view_tool_blueprint,
    resolve_view_tool_source,
)

logger = structlog.get_logger(__name__)

CONTEXT_REBUILD_PRINCIPAL: Final = "scheduler:context-rebuild"

#: Why a source-change hold stays in place.
WAIT_SIGNAL_PENDING: Final = "SIGNAL_PENDING"
WAIT_VIEW_NOT_STANDING: Final = "VIEW_NOT_STANDING"
WAIT_TOOL_STALE: Final = "TOOL_STALE"
WAIT_TOOL_NOT_REVERIFIED: Final = "TOOL_NOT_REVERIFIED"
WAIT_DESCRIPTION_STALE: Final = "DESCRIPTION_STALE"
WAIT_PRODUCT_STALE: Final = "PRODUCT_STALE"

HOLD_RELEASE_REASON: Final = (
    "Everything standing on the redefined view was rebuilt against its current definition and "
    "approved, or re-approved after the change."
)
SUPERSEDED_EDGE_REASON: Final = "The view's current definition no longer produces this edge."

_HELD_STATUSES: Final = ("OPEN", "ACKNOWLEDGED")
_OPEN_TOOL_STATUSES: Final = ("DRAFT", "REVIEW_REQUIRED")
_OPEN_PRODUCT_STATUSES: Final = ("DRAFT", "REVIEW_REQUIRED")
_OPEN_DESCRIPTION_STATUSES: Final = ("DRAFT", "PENDING_APPROVAL")
_VIEW_KINDS: Final = frozenset({"VIEW", "MATERIALIZED_VIEW"})


class _RebuildRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class RebuildOutcome:
    lineage_edges_superseded: int = 0
    tools_drafted: int = 0
    descriptions_drafted: int = 0
    products_drafted: int = 0
    holds_released: int = 0
    failed: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    waiting: dict[str, int] = field(default_factory=dict)

    def block(self, code: str) -> None:
        self.blocked[code] = self.blocked.get(code, 0) + 1

    def wait(self, code: str) -> None:
        self.waiting[code] = self.waiting.get(code, 0) + 1

    @property
    def acted(self) -> bool:
        return any(
            (
                self.lineage_edges_superseded,
                self.tools_drafted,
                self.descriptions_drafted,
                self.products_drafted,
                self.holds_released,
                self.failed,
                self.blocked,
            )
        )

    def as_details(self) -> dict[str, Any]:
        return {
            "lineage_edges_superseded": self.lineage_edges_superseded,
            "tools_drafted": self.tools_drafted,
            "descriptions_drafted": self.descriptions_drafted,
            "products_drafted": self.products_drafted,
            "holds_released": self.holds_released,
            "failed": self.failed,
            "blocked": dict(sorted(self.blocked.items())),
            "waiting": dict(sorted(self.waiting.items())),
        }


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _is_view(table: MetadataTable) -> bool:
    return table.object_type.strip().replace(" ", "_").upper() in _VIEW_KINDS


def rebuild_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=CONTEXT_REBUILD_PRINCIPAL,
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


async def _open_review(
    session: AsyncSession,
    context: SecurityContext,
    organization_id: UUID,
    *,
    object_type: str,
    object_id: UUID,
    details: dict[str, Any],
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action="PUBLISH",
        requested_by=CONTEXT_REBUILD_PRINCIPAL,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        context,
        action="context_rebuild.propose",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=str(organization_id),
        details={"object_type": object_type, "object_id": str(object_id), **details},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": object_type,
            "object_id": str(object_id),
            "requested_action": "PUBLISH",
        },
    )
    return review


# --------------------------------------------------------------------------
# 1. Lineage
# --------------------------------------------------------------------------


async def _held_view_ids(session: AsyncSession, organization_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(DataQualityIncident.table_id).where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(_HELD_STATUSES),
            )
        )
    )


async def _current_definition(
    session: AsyncSession, view: MetadataTable
) -> MetadataViewDefinition | None:
    definition = await session.scalar(
        select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == view.id)
    )
    if (
        definition is None
        or definition.status != "ACTIVE"
        or definition.availability != AVAILABLE
        or not definition.definition_sql_redacted
    ):
        return None
    return definition


async def _supersede_lineage(
    session: AsyncSession, organization_id: UUID, outcome: RebuildOutcome, now: datetime
) -> None:
    for view_id in await _held_view_ids(session, organization_id):
        view = await session.get(MetadataTable, view_id)
        if view is None or view.status != "ACTIVE" or not _is_view(view):
            continue
        definition = await _current_definition(session, view)
        if (
            definition is None
            or definition.redaction_status not in VALUE_FREE_REDACTION_STATUSES
            or definition.screening_status != CLEAN
        ):
            # The lineage agent reads only value-free, clean text; so does this.
            continue
        schema = await session.get(MetadataSchema, view.schema_id)
        datasource = await session.get(DataSource, view.datasource_id)
        if schema is None or datasource is None:
            continue
        result = parse_view_lineage(
            as_create_view(f"{schema.name}.{view.name}", definition.definition_sql_redacted or ""),
            dialect=datasource.dialect,
        )
        if not result.edges:
            # A definition the parser cannot read says nothing about which edges are gone.
            continue
        produced = {
            (
                edge.source_table,
                edge.source_column,
                edge.target_table,
                edge.target_column,
                edge.transformation_type,
            )
            for edge in result.edges
        }
        edges = await session.scalars(
            select(ViewLineageEdge).where(
                ViewLineageEdge.target_table_id == view.id,
                ViewLineageEdge.review_status == "ACTIVE",
            )
        )
        for edge in edges:
            key = (
                edge.source_table,
                edge.source_column,
                edge.target_table,
                edge.target_column,
                edge.transformation_type,
            )
            if key in produced:
                continue
            edge.review_status = "SUPERSEDED"
            edge.reviewed_by = CONTEXT_REBUILD_PRINCIPAL
            edge.reviewed_at = now
            edge.review_reason = SUPERSEDED_EDGE_REASON
            outcome.lineage_edges_superseded += 1
    await session.flush()


# --------------------------------------------------------------------------
# 2. Tools
# --------------------------------------------------------------------------


async def _regenerate_tool(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    version: GovernedToolVersion,
    reason: str,
) -> None:
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    project = await session.get(Project, tool.project_id) if tool is not None else None
    if tool is None or datasource is None or project is None:
        raise _RebuildRefused("TOOL_DEPENDENCY_UNAVAILABLE")
    source_view: MetadataViewDefinition | None = None
    source_routine: MetadataRoutine | None = None
    if version.source_view_table_id is not None:
        try:
            view_source = await resolve_view_tool_source(
                session,
                organization_id=organization_id,
                datasource_id=datasource.id,
                table_id=version.source_view_table_id,
            )
            view_blueprint = build_view_tool_blueprint(view_source, dialect=datasource.dialect)
        except ViewNotEligibleError as exc:
            raise _RebuildRefused(exc.code) from exc
        except ViewToolBlueprintError as exc:
            raise _RebuildRefused("BLUEPRINT_REFUSED") from exc
        sql_template, parameters = view_blueprint.sql_template, list(view_blueprint.parameters)
        source_view = await session.scalar(
            select(MetadataViewDefinition).where(
                MetadataViewDefinition.table_id == version.source_view_table_id
            )
        )
    elif version.source_routine_id is not None:
        try:
            (
                routine,
                result_node,
                parse_result,
                routine_parameters,
            ) = await resolve_procedure_tool_source(
                session,
                organization_id=organization_id,
                datasource_id=datasource.id,
                routine_id=version.source_routine_id,
                dialect=datasource.dialect,
            )
            procedure_blueprint = build_procedure_tool_blueprint(
                result_node,
                routine_parameters,
                dialect=datasource.dialect,
                statement_count=parse_result.statement_count,
                sql_hash=parse_result.sql_hash,
            )
        except (RoutineNotEligibleError, ProcedureNotEligibleError) as exc:
            raise _RebuildRefused(exc.code) from exc
        except ProcedureToolBlueprintError as exc:
            raise _RebuildRefused("BLUEPRINT_REFUSED") from exc
        sql_template = procedure_blueprint.sql_template
        parameters = list(procedure_blueprint.parameters)
        source_routine = routine
    else:
        raise _RebuildRefused("TOOL_NOT_SOURCE_BOUND")
    body = GovernedToolVersionCreate(
        slug=tool.slug,
        name=version.name,
        description=version.description,
        datasource_id=datasource.id,
        semantic_model_version_id=version.semantic_model_version_id,
        sql_template=sql_template,
        parameters=parameters,
        allowed_roles=list(version.allowed_roles),
    )
    try:
        _, draft = await stage_tool_version_draft(
            session,
            project,
            datasource,
            body,
            audit_context=context,
            settings=settings,
            source_routine=source_routine,
            source_view=source_view,
        )
    except ToolDraftRefused as exc:
        raise _RebuildRefused(exc.code) from exc
    draft.status = "REVIEW_REQUIRED"
    await _open_review(
        session,
        context,
        organization_id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=draft.id,
        details={"rebuilds_version_id": str(version.id), "reason": reason},
    )


async def _rebuild_tools(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    outcome: RebuildOutcome,
) -> None:
    version_ids = list(
        await session.scalars(
            select(GovernedToolVersion.id).where(
                GovernedToolVersion.organization_id == organization_id,
                GovernedToolVersion.status == "PUBLISHED",
                or_(
                    GovernedToolVersion.source_routine_id.is_not(None),
                    GovernedToolVersion.source_view_table_id.is_not(None),
                ),
            )
        )
    )
    for version_id in version_ids:
        version = await session.get(GovernedToolVersion, version_id, populate_existing=True)
        if version is None:
            continue
        reason = await source_binding_drift(session, version)
        if reason is None:
            continue
        if reason in (REASON_SOURCE_MISSING, REASON_SOURCE_RETIRED):
            outcome.block(f"TOOL_{reason}")
            continue
        waiting = await session.scalar(
            select(GovernedToolVersion.id)
            .where(
                GovernedToolVersion.tool_id == version.tool_id,
                GovernedToolVersion.version > version.version,
                GovernedToolVersion.status.in_(_OPEN_TOOL_STATUSES),
            )
            .limit(1)
        )
        if waiting is not None:
            continue
        try:
            async with session.begin_nested():
                await _regenerate_tool(session, organization_id, context, settings, version, reason)
        except _RebuildRefused as refused:
            outcome.block(refused.code)
            continue
        except Exception:  # noqa: BLE001 -- one rebuild must not stop the pass
            logger.exception("context_rebuild_tool_failed", tool_version_id=str(version_id))
            outcome.failed += 1
            continue
        outcome.tools_drafted += 1


# --------------------------------------------------------------------------
# 3. Descriptions
# --------------------------------------------------------------------------


async def _current_approved_description(
    session: AsyncSession, organization_id: UUID, table_id: UUID
) -> AssetDescriptionDraft | None:
    """The approved draft whose text is the table's current published documentation, if any."""
    draft: AssetDescriptionDraft | None = await session.scalar(
        select(AssetDescriptionDraft)
        .join(
            AssetDocumentationVersion,
            AssetDocumentationVersion.id == AssetDescriptionDraft.published_version_id,
        )
        .where(
            AssetDescriptionDraft.organization_id == organization_id,
            AssetDescriptionDraft.table_id == table_id,
            AssetDescriptionDraft.status == "APPROVED",
            AssetDocumentationVersion.status == "APPROVED",
        )
        .order_by(AssetDescriptionDraft.reviewed_at.desc())
        .limit(1)
    )
    return draft


async def _regenerate_description(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    table: MetadataTable,
    previous_id: UUID,
) -> None:
    evidence = await gather_evidence(session, table)
    drafted_text = compose_draft_text(evidence)
    payload = evidence_payload(evidence)
    refusal = await table_refusal(session, table.id, drafted_text=drafted_text, payload=payload)
    if refusal is not None:
        raise _RebuildRefused(f"DESCRIPTION_REFUSED_{refusal}")
    scores = score_evidence(evidence)
    if scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
        raise _RebuildRefused("DESCRIPTION_BELOW_EVIDENCE_BAR")
    draft = AssetDescriptionDraft(
        organization_id=organization_id,
        table_id=table.id,
        drafted_text=drafted_text,
        text_fingerprint=text_fingerprint(drafted_text),
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        evidence={**payload, "origin": "REBUILD", "rebuilds_draft_id": str(previous_id)},
        status="PENDING_APPROVAL",
        created_by=CONTEXT_REBUILD_PRINCIPAL,
    )
    session.add(draft)
    await session.flush()
    review = await _open_review(
        session,
        context,
        organization_id,
        object_type="ASSET_DESCRIPTION_DRAFT",
        object_id=draft.id,
        details={"rebuilds_draft_id": str(previous_id), "overall_score": scores.overall},
    )
    draft.governance_review_id = review.id


async def _rebuild_descriptions(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
) -> None:
    table_ids = list(
        await session.scalars(
            select(AssetDescriptionDraft.table_id)
            .join(MetadataTable, MetadataTable.id == AssetDescriptionDraft.table_id)
            .where(
                AssetDescriptionDraft.organization_id == organization_id,
                AssetDescriptionDraft.status == "APPROVED",
                MetadataTable.status == "ACTIVE",
            )
            .distinct()
        )
    )
    for table_id in table_ids:
        table = await session.get(MetadataTable, table_id, populate_existing=True)
        if table is None or not _is_view(table):
            continue
        approved = await _current_approved_description(session, organization_id, table_id)
        if approved is None or await definition_moved(session, approved) is None:
            continue
        waiting = await session.scalar(
            select(AssetDescriptionDraft.id)
            .where(
                AssetDescriptionDraft.table_id == table_id,
                AssetDescriptionDraft.status.in_(_OPEN_DESCRIPTION_STATUSES),
            )
            .limit(1)
        )
        if waiting is not None:
            continue
        previous_id = approved.id
        try:
            async with session.begin_nested():
                await _regenerate_description(session, organization_id, context, table, previous_id)
        except _RebuildRefused as refused:
            outcome.block(refused.code)
            continue
        except Exception:  # noqa: BLE001 -- one rebuild must not stop the pass
            logger.exception("context_rebuild_description_failed", table_id=str(table_id))
            outcome.failed += 1
            continue
        outcome.descriptions_drafted += 1


# --------------------------------------------------------------------------
# 4. Context products
# --------------------------------------------------------------------------


async def _superseded_pins(
    session: AsyncSession, pinned: list[str]
) -> tuple[dict[str, str], str | None]:
    """Pinned tool versions no longer published, mapped to their tool's published version."""
    if not pinned:
        return {}, None
    replacements: dict[str, str] = {}
    versions = await session.scalars(
        select(GovernedToolVersion).where(GovernedToolVersion.id.in_([UUID(p) for p in pinned]))
    )
    for version in versions:
        if version.status == "PUBLISHED":
            continue
        current = await session.scalar(
            select(GovernedToolVersion.id)
            .where(
                GovernedToolVersion.tool_id == version.tool_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
            .limit(1)
        )
        if current is None:
            return {}, "PRODUCT_TOOL_UNPUBLISHED"
        replacements[str(version.id)] = str(current)
    return replacements, None


async def _draft_product_version(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    previous: ContextProductVersion,
    replacements: dict[str, str],
) -> None:
    product = await session.get(ContextProduct, previous.product_id)
    if product is None or product.lifecycle_status != "ACTIVE":
        raise _RebuildRefused("PRODUCT_NOT_ACTIVE")
    project = await session.get(Project, product.project_id)
    if project is None:
        raise _RebuildRefused("PRODUCT_PROJECT_UNAVAILABLE")
    definition = _definition_from_version(previous)
    repinned = [
        UUID(replacements.get(str(version_id), str(version_id)))
        for version_id in definition.eligible_tool_version_ids
    ]
    definition = definition.model_copy(update={"eligible_tool_version_ids": repinned})
    try:
        await validate_context_product_references(session, project, definition)
    except HTTPException as exc:
        raise _RebuildRefused("PRODUCT_REFERENCES_REFUSED") from exc
    latest = await session.scalar(
        select(func.max(ContextProductVersion.version)).where(
            ContextProductVersion.product_id == product.id
        )
    )
    version = apply_context_product_definition(
        ContextProductVersion(
            organization_id=product.organization_id,
            product_id=product.id,
            version=(latest or 0) + 1,
            created_by=CONTEXT_REBUILD_PRINCIPAL,
            based_on_version_id=previous.id,
        ),
        definition,
    )
    session.add(version)
    await session.flush()
    await replace_context_product_role_bindings(session, version)
    version.status = "REVIEW_REQUIRED"
    await _open_review(
        session,
        context,
        organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=version.id,
        details={
            "rebuilds_version_id": str(previous.id),
            "repinned_tool_versions": len(replacements),
        },
    )


async def _rebuild_products(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
) -> None:
    version_ids = list(
        await session.scalars(
            select(ContextProductVersion.id).where(
                ContextProductVersion.organization_id == organization_id,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
    )
    for version_id in version_ids:
        previous = await session.get(ContextProductVersion, version_id, populate_existing=True)
        if previous is None:
            continue
        replacements, blocked = await _superseded_pins(
            session, list(previous.eligible_tool_version_ids)
        )
        if blocked is not None:
            outcome.block(blocked)
            continue
        if not replacements:
            continue
        waiting = await session.scalar(
            select(ContextProductVersion.id)
            .where(
                ContextProductVersion.product_id == previous.product_id,
                ContextProductVersion.status.in_(_OPEN_PRODUCT_STATUSES),
            )
            .limit(1)
        )
        if waiting is not None:
            continue
        try:
            async with session.begin_nested():
                await _draft_product_version(
                    session, organization_id, context, previous, replacements
                )
        except _RebuildRefused as refused:
            outcome.block(refused.code)
            continue
        except Exception:  # noqa: BLE001 -- one rebuild must not stop the pass
            logger.exception("context_rebuild_product_failed", version_id=str(version_id))
            outcome.failed += 1
            continue
        outcome.products_drafted += 1


# --------------------------------------------------------------------------
# 5. Holds
# --------------------------------------------------------------------------


async def _reads_view(
    session: AsyncSession,
    datasource: DataSource,
    version: GovernedToolVersion,
    view: MetadataTable,
) -> bool:
    if version.source_view_table_id == view.id:
        return True
    resolved = await resolve_table_ids(
        session, datasource=datasource, table_names=version.referenced_tables
    )
    return view.id in resolved.values()


async def stale_dependent(session: AsyncSession, incident: DataQualityIncident) -> str | None:
    """Why a source-change hold on a redefined view must stay, or `None` when nothing is stale."""
    view = await session.get(MetadataTable, incident.table_id)
    if view is None or view.status != "ACTIVE" or await _current_definition(session, view) is None:
        return WAIT_VIEW_NOT_STANDING
    pending = await session.scalar(
        select(MetadataChangeSignal.id)
        .where(
            MetadataChangeSignal.subject_id == view.id,
            MetadataChangeSignal.status == "PENDING",
        )
        .limit(1)
    )
    if pending is not None:
        return WAIT_SIGNAL_PENDING
    datasource = await session.get(DataSource, view.datasource_id)
    if datasource is None:
        return WAIT_VIEW_NOT_STANDING
    changed_at = _aware(incident.last_observed_at)

    published = list(
        await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.organization_id == incident.organization_id,
                GovernedToolVersion.datasource_id == view.datasource_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
        )
    )
    for version in published:
        if version.source_view_table_id == view.id:
            if await source_binding_drift(session, version) is not None:
                return WAIT_TOOL_STALE
            continue
        if not await _reads_view(session, datasource, version, view):
            continue
        if version.approved_at is None or _aware(version.approved_at) <= changed_at:
            return WAIT_TOOL_NOT_REVERIFIED

    approved = await _current_approved_description(session, incident.organization_id, view.id)
    if approved is not None and await definition_moved(session, approved) is not None:
        return WAIT_DESCRIPTION_STALE

    products = await session.scalars(
        select(ContextProductVersion).where(
            ContextProductVersion.organization_id == incident.organization_id,
            ContextProductVersion.status == "PUBLISHED",
        )
    )
    for product_version in products:
        pinned = [UUID(value) for value in product_version.eligible_tool_version_ids]
        if not pinned:
            continue
        superseded = await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.id.in_(pinned),
                GovernedToolVersion.status != "PUBLISHED",
            )
        )
        for version in superseded:
            if await _reads_view(session, datasource, version, view):
                return WAIT_PRODUCT_STALE
    return None


async def _release_holds(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
    now: datetime,
) -> None:
    incident_ids = list(
        await session.scalars(
            select(DataQualityIncident.id).where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(_HELD_STATUSES),
            )
        )
    )
    for incident_id in incident_ids:
        incident = await session.get(DataQualityIncident, incident_id, populate_existing=True)
        if incident is None:
            continue
        changes = (incident.evidence or {}).get("changes") or []
        actions = {change.get("action") for change in changes if isinstance(change, dict)}
        if actions != {ACTION_VIEW_REDEFINED}:
            # A retired or reshaped table is a person's call; only a redefinition is rebuilt.
            continue
        reason = await stale_dependent(session, incident)
        if reason is not None:
            outcome.wait(reason)
            continue
        incident.status = "RESOLVED"
        incident.resolved_by = CONTEXT_REBUILD_PRINCIPAL
        incident.resolved_at = now
        incident.resolution_reason = HOLD_RELEASE_REASON
        record_audit(
            session,
            context,
            action="data_quality.incident.resolve",
            resource_type="data_quality_incident",
            resource_id=str(incident.id),
            outcome="SUCCESS",
            correlation_id=str(organization_id),
            details={
                "anomaly_type": SOURCE_CHANGE_ANOMALY_TYPE,
                "table_id": str(incident.table_id),
                "resolved_by": CONTEXT_REBUILD_PRINCIPAL,
            },
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="data_quality_incident",
            aggregate_id=str(incident.id),
            event_type="data_quality.incident_resolved",
            payload={
                "incident_id": str(incident.id),
                "datasource_id": str(incident.datasource_id),
                "table_id": str(incident.table_id),
                "resolved_by": CONTEXT_REBUILD_PRINCIPAL,
            },
        )
        outcome.holds_released += 1


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------


async def organizations_needing_rebuild(session: AsyncSession) -> list[UUID]:
    """Organizations with a source-change hold open, or a published source-bound tool."""
    held = await session.scalars(
        select(DataQualityIncident.organization_id)
        .where(
            DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
            DataQualityIncident.status.in_(_HELD_STATUSES),
        )
        .distinct()
    )
    bound = await session.scalars(
        select(GovernedToolVersion.organization_id)
        .where(
            GovernedToolVersion.status == "PUBLISHED",
            or_(
                GovernedToolVersion.source_routine_id.is_not(None),
                GovernedToolVersion.source_view_table_id.is_not(None),
            ),
        )
        .distinct()
    )
    return sorted(set(held) | set(bound), key=str)


async def run_context_rebuild(
    session: AsyncSession,
    organization_id: UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> RebuildOutcome:
    """One rebuild pass for one organization. The caller commits."""
    effective_now = now or datetime.now(UTC)
    context = rebuild_context(organization_id)
    outcome = RebuildOutcome()
    await _supersede_lineage(session, organization_id, outcome, effective_now)
    await _rebuild_tools(session, organization_id, context, settings, outcome)
    await _rebuild_descriptions(session, organization_id, context, outcome)
    await _rebuild_products(session, organization_id, context, outcome)
    await _release_holds(session, organization_id, context, outcome, effective_now)
    if outcome.acted:
        record_audit(
            session,
            context,
            action="context_rebuild.pass",
            resource_type="organization",
            resource_id=str(organization_id),
            outcome="SUCCESS",
            correlation_id=str(organization_id),
            details=outcome.as_details(),
        )
    return outcome
