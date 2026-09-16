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
3. **Descriptions.** A view or table whose current approved description was drafted against
   another definition, or other columns, gets a draft from today's evidence, submitted for
   review once it clears the bar.
4. **Document claims.** A data dictionary whose claims were proposed is mapped again
   once a scan in its project finishes after it was mapped. A row that now names exactly
   one live table or column gets a claim proposed for review; one whose subject left is
   UNMATCHED again, and a pending claim on a retired subject is refused at approval.
5. **Retirements, and renames.** A published tool that reads a held table the source no
   longer has, or whose SQL names a column a held table no longer has (and that is not
   regenerated from a view or routine), gets a DEPRECATE review. Where a steward approved
   a rename for that table, the tool is proposed against what it became instead --
   regenerated from the successor view, or its SQL written against the new name -- and
   only a tool that cannot be is proposed for retirement. A person decides either way; a
   rejected proposal is not made again until the source changes again.
6. **Context products.** A PUBLISHED context product version that pins a tool version since
   superseded gets a version re-pinned to that tool's published version; one that includes a
   held table the source no longer has, or pins a tool reading it, gets a version without
   them; one pinning an ontology version its ontology has since published past, or a
   semantic model or glossary term version since superseded, gets a version pinned to the
   current one. Each is submitted for review.
7. **Holds.** A source-change hold is resolved once nothing standing on its table is stale,
   and its change signals are processed. For a table the source no longer has: no published
   tool reads it, and no published context product includes it or pins a tool reading it.
   For a view or table still in the source: every tool generated from it matches its
   definition; every other published tool reading it still binds to its columns and, where
   the change can alter what SQL that still binds answers (any held view, a column
   retyped), was approved after the change; its approved description matches its
   definition or columns; and no published context product pins a superseded tool reading
   it.

Before these, approved joins a newer scan has read are re-validated
(`aida.relationship_drift`): one whose evidence is gone is suspended back to review, and one
whose evidence is back exactly as approved is restored.

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
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot.errors import SqlglotError

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    REFUSED_EVIDENCE,
    REFUSED_TEXT,
    compose_draft_text,
    definition_moved,
    evidence_payload,
    gather_evidence,
    score_evidence,
    table_refusal,
    text_fingerprint,
)
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import ACTION_TABLE_RESHAPED, SOURCE_CHANGE_ANOMALY_TYPE
from aida.change_signals import BINDING_SAFE_SHAPE_CHANGES
from aida.config import Settings
from aida.context_product_api import (
    _definition_from_version,
    apply_context_product_definition,
    replace_context_product_role_bindings,
    validate_context_product_references,
)
from aida.document_ingestion import remap_document
from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.events import record_audit, record_outbox
from aida.ingest_screening import CLEAN
from aida.lineage_agent import as_create_view
from aida.models import (
    AnalysisRun,
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    ContextProduct,
    ContextProductVersion,
    DataQualityIncident,
    DataSource,
    Document,
    DocumentClaim,
    DocumentSection,
    GlossaryTermVersion,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Project,
    SemanticModelVersion,
    ViewLineageEdge,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    ProcedureToolBlueprintError,
    build_procedure_tool_blueprint,
    resolve_procedure_tool_source,
)
from aida.query_gateway import catalog_columns
from aida.relationship_drift import check_relationship_drift, relationship_drift_pending
from aida.routine_lineage_edges import RoutineNotEligibleError
from aida.schemas import GovernedToolVersionCreate, ToolParameterDefinition
from aida.security import SecurityContext
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES
from aida.sql_validation import (
    findings_from_columns,
    locally_defined_names,
    resolve_column_references,
)
from aida.table_rename_rewrite import rewrite_table_references
from aida.tool_drafts import ToolDraftRefused, stage_tool_version_draft
from aida.tool_impact import compute_deprecation_impact, impact_summary
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
WAIT_TABLE_NOT_STANDING: Final = "TABLE_NOT_STANDING"
WAIT_TOOL_COLUMNS_MISSING: Final = "TOOL_COLUMNS_MISSING"
WAIT_RETIRED_TABLE_IN_USE: Final = "RETIRED_TABLE_IN_USE"
#: A person refused this description already, so no pass can draft its replacement.
WAIT_DESCRIPTION_AWAITING_AUTHOR: Final = "DESCRIPTION_AWAITING_AUTHOR"
#: The refusal codes that mean a reviewer rejected exactly what a rebuild would propose again.
_DESCRIPTION_REJECTED_CODES: Final = frozenset(
    {f"DESCRIPTION_REFUSED_{REFUSED_TEXT}", f"DESCRIPTION_REFUSED_{REFUSED_EVIDENCE}"}
)

#: Why a DEPRECATE review is proposed.
DEPRECATE_TABLE_RETIRED: Final = "TABLE_RETIRED"
DEPRECATE_COLUMNS_MISSING: Final = "COLUMNS_MISSING"
#: Why a tool is rebuilt rather than retired: a steward approved a rename, and the table
#: the tool reads is the one it became.
REWRITE_TABLE_RENAMED: Final = "TABLE_RENAMED"

HOLD_RELEASE_REASON: Final = (
    "Everything standing on the redefined view was rebuilt against its current definition and "
    "approved, or re-approved after the change."
)
RESHAPE_RELEASE_REASON: Final = (
    "Every published tool reading the table still binds to its current columns, any change "
    "that could alter an answer was re-approved after it, and its description matches its "
    "columns."
)
RETIREMENT_RELEASE_REASON: Final = (
    "The table left the source, and nothing published stands on it any more: the tools "
    "reading it were retired and its context products re-scoped through review."
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
    joins_suspended: int = 0
    joins_restored: int = 0
    tools_drafted: int = 0
    tools_rewritten: int = 0
    descriptions_drafted: int = 0
    sections_remapped: int = 0
    claims_proposed: int = 0
    deprecations_proposed: int = 0
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
                self.joins_suspended,
                self.joins_restored,
                self.tools_drafted,
                self.tools_rewritten,
                self.descriptions_drafted,
                self.sections_remapped,
                self.claims_proposed,
                self.deprecations_proposed,
                self.products_drafted,
                self.holds_released,
                self.failed,
                self.blocked,
            )
        )

    def as_details(self) -> dict[str, Any]:
        return {
            "lineage_edges_superseded": self.lineage_edges_superseded,
            "joins_suspended": self.joins_suspended,
            "joins_restored": self.joins_restored,
            "tools_drafted": self.tools_drafted,
            "tools_rewritten": self.tools_rewritten,
            "descriptions_drafted": self.descriptions_drafted,
            "sections_remapped": self.sections_remapped,
            "claims_proposed": self.claims_proposed,
            "deprecations_proposed": self.deprecations_proposed,
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
    requested_action: str = "PUBLISH",
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action=requested_action,
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
        details={
            "object_type": object_type,
            "object_id": str(object_id),
            "requested_action": requested_action,
            **details,
        },
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
            "requested_action": requested_action,
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


async def _stage_tool_rebuild(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    version: GovernedToolVersion,
    *,
    sql_template: str,
    parameters: list[ToolParameterDefinition],
    reason: str,
    details: dict[str, Any] | None = None,
    source_routine: MetadataRoutine | None = None,
    source_view: MetadataViewDefinition | None = None,
) -> None:
    """Stage one rebuilt version of a published tool and put it in the review queue.

    Shared by the two ways a tool is rebuilt -- regenerated from its source's current
    definition, and written against the table a rename replaced -- so both keep the published
    version's name, description, roles and semantic model, and both are decided by a person.
    `stage_tool_version_draft` runs the SQL guard, the placeholder check and per-object
    authorization, so a rebuild that would not have been accepted from a person is refused here.
    """
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    project = await session.get(Project, tool.project_id) if tool is not None else None
    if tool is None or datasource is None or project is None:
        raise _RebuildRefused("TOOL_DEPENDENCY_UNAVAILABLE")
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
        details={
            "rebuilds_version_id": str(version.id),
            "reason": reason,
            **(details or {}),
        },
    )


async def _regenerate_tool(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    version: GovernedToolVersion,
    reason: str,
    *,
    source_view_table_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    project = await session.get(Project, tool.project_id) if tool is not None else None
    if tool is None or datasource is None or project is None:
        raise _RebuildRefused("TOOL_DEPENDENCY_UNAVAILABLE")
    source_view: MetadataViewDefinition | None = None
    source_routine: MetadataRoutine | None = None
    # R11-FP16: the view to generate from is this version's own source, except where a
    # rename replaced it -- then it is what the steward said that view became.
    view_table_id = source_view_table_id or version.source_view_table_id
    if view_table_id is not None:
        try:
            view_source = await resolve_view_tool_source(
                session,
                organization_id=organization_id,
                datasource_id=datasource.id,
                table_id=view_table_id,
            )
            view_blueprint = build_view_tool_blueprint(view_source, dialect=datasource.dialect)
        except ViewNotEligibleError as exc:
            raise _RebuildRefused(exc.code) from exc
        except ViewToolBlueprintError as exc:
            raise _RebuildRefused("BLUEPRINT_REFUSED") from exc
        sql_template, parameters = view_blueprint.sql_template, list(view_blueprint.parameters)
        source_view = await session.scalar(
            select(MetadataViewDefinition).where(
                MetadataViewDefinition.table_id == view_table_id
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
    await _stage_tool_rebuild(
        session,
        organization_id,
        context,
        settings,
        version,
        sql_template=sql_template,
        parameters=parameters,
        reason=reason,
        details=details,
        source_routine=source_routine,
        source_view=source_view,
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
        if table is None:
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
            if refused.code in _DESCRIPTION_REJECTED_CODES:
                # A reviewer already refused this proposal, and a redraft is the same words on
                # the same evidence, so the refusal rule refuses it again: no pass can move this
                # on. The hold stays -- the published description is still written against a
                # definition that moved, and that is what a hold is for -- but it is reported as
                # waiting for a person to author the replacement rather than as a refusal the
                # next pass will repeat, which read as churn and named no way out.
                outcome.wait(WAIT_DESCRIPTION_AWAITING_AUTHOR)
            else:
                outcome.block(refused.code)
            continue
        except Exception:  # noqa: BLE001 -- one rebuild must not stop the pass
            logger.exception("context_rebuild_description_failed", table_id=str(table_id))
            outcome.failed += 1
            continue
        outcome.descriptions_drafted += 1


# --------------------------------------------------------------------------
# 4. Document claims
# --------------------------------------------------------------------------


def _document_needs_remap() -> ColumnElement[bool]:
    """A document whose claims were proposed, read by a scan that finished since it was mapped."""
    newer_scan = (
        select(AnalysisRun.id)
        .join(DataSource, DataSource.id == AnalysisRun.datasource_id)
        .where(
            DataSource.project_id == Document.project_id,
            AnalysisRun.status == "COMPLETED",
            AnalysisRun.updated_at > Document.updated_at,
        )
        .exists()
    )
    proposed = (
        select(DocumentClaim.id)
        .join(DocumentSection, DocumentSection.id == DocumentClaim.document_section_id)
        .where(DocumentSection.document_id == Document.id)
        .exists()
    )
    return and_(Document.status == "MAPPED", newer_scan, proposed)


def _record_claim_proposal(
    session: AsyncSession,
    context: SecurityContext,
    organization_id: UUID,
    document: Document,
    claim: DocumentClaim,
) -> None:
    review_id = str(claim.governance_review_id)
    record_audit(
        session,
        context,
        action="context_rebuild.propose",
        resource_type="governance_review",
        resource_id=review_id,
        outcome="SUCCESS",
        correlation_id=str(organization_id),
        details={
            "object_type": "DOCUMENT_CLAIM",
            "object_id": str(claim.id),
            "requested_action": "DESCRIBES",
            "document_id": str(document.id),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="governance_review",
        aggregate_id=review_id,
        event_type="governance.review_requested.v1",
        payload={
            "review_id": review_id,
            "object_type": "DOCUMENT_CLAIM",
            "object_id": str(claim.id),
            "requested_action": "DESCRIBES",
        },
    )


async def _rebuild_document_claims(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
    now: datetime,
) -> None:
    document_ids = list(
        await session.scalars(
            select(Document.id).where(
                Document.organization_id == organization_id, _document_needs_remap()
            )
        )
    )
    for document_id in document_ids:
        document = await session.get(Document, document_id, populate_existing=True)
        if document is None:
            continue
        try:
            async with session.begin_nested():
                remapped = await remap_document(
                    session, document, requested_by=CONTEXT_REBUILD_PRINCIPAL
                )
                for claim in remapped.claims:
                    _record_claim_proposal(session, context, organization_id, document, claim)
                # The watermark: the document waits for a scan that finishes after this pass.
                document.updated_at = now
        except Exception:  # noqa: BLE001 -- one document must not stop the pass
            logger.exception("context_rebuild_document_failed", document_id=str(document_id))
            outcome.failed += 1
            continue
        outcome.sections_remapped += remapped.remapped + remapped.unmatched
        outcome.claims_proposed += len(remapped.claims)


# --------------------------------------------------------------------------
# What stands on a held table
# --------------------------------------------------------------------------


async def _held_incident_ids(session: AsyncSession, organization_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(DataQualityIncident.id).where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(_HELD_STATUSES),
            )
        )
    )


async def _retired_held_table_ids(session: AsyncSession, organization_id: UUID) -> set[UUID]:
    """Tables under a source-change hold that the source no longer has."""
    return set(
        await session.scalars(
            select(DataQualityIncident.table_id)
            .join(MetadataTable, MetadataTable.id == DataQualityIncident.table_id)
            .where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(_HELD_STATUSES),
                MetadataTable.status != "ACTIVE",
            )
        )
    )


async def _signal_pending(session: AsyncSession, table_id: UUID) -> bool:
    pending = await session.scalar(
        select(MetadataChangeSignal.id)
        .where(
            MetadataChangeSignal.subject_id == table_id,
            MetadataChangeSignal.status == "PENDING",
        )
        .limit(1)
    )
    return pending is not None


async def _published_tools(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[GovernedToolVersion]:
    return list(
        await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.organization_id == organization_id,
                GovernedToolVersion.datasource_id == datasource_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
        )
    )


async def _published_products(
    session: AsyncSession, organization_id: UUID
) -> list[ContextProductVersion]:
    return list(
        await session.scalars(
            select(ContextProductVersion).where(
                ContextProductVersion.organization_id == organization_id,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
    )


async def _tool_table_ids(
    session: AsyncSession, datasource: DataSource, version: GovernedToolVersion
) -> set[UUID]:
    """The tables a tool version's SQL names, including ones the source no longer has.

    The name shapes the gateway authorises: `schema.table`, `catalog.schema.table`, or an
    unambiguous bare name. A retired table still resolves, because a tool naming it is exactly
    what stands on it. A bare name resolves to its one table in the source, or failing that to
    its one retired table.
    """
    resolved: set[UUID] = set()
    if version.source_view_table_id is not None:
        resolved.add(version.source_view_table_id)
    names = [name.lower() for name in version.referenced_tables]
    leaf_names = {name.rsplit(".", 1)[-1] for name in names}
    if not leaf_names:
        return resolved
    rows = (
        await session.execute(
            select(
                MetadataCatalog.name,
                MetadataSchema.name,
                MetadataTable.name,
                MetadataTable.id,
                MetadataTable.status,
            )
            .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
            .join(MetadataTable, MetadataTable.schema_id == MetadataSchema.id)
            .where(
                MetadataCatalog.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                func.lower(MetadataTable.name).in_(leaf_names),
            )
        )
    ).all()
    by_qualified: dict[str, UUID] = {}
    by_leaf: dict[str, set[UUID]] = {}
    active_by_leaf: dict[str, set[UUID]] = {}
    for catalog_name, schema_name, table_name, table_id, status in rows:
        by_qualified[f"{schema_name}.{table_name}".lower()] = table_id
        by_qualified[f"{catalog_name}.{schema_name}.{table_name}".lower()] = table_id
        by_leaf.setdefault(table_name.lower(), set()).add(table_id)
        if status == "ACTIVE":
            active_by_leaf.setdefault(table_name.lower(), set()).add(table_id)
    for leaf, table_ids in by_leaf.items():
        candidates = active_by_leaf.get(leaf) or table_ids
        if len(candidates) == 1:
            by_qualified.setdefault(leaf, next(iter(candidates)))
    resolved.update(by_qualified[name] for name in names if name in by_qualified)
    return resolved


async def _columns_unbound(
    session: AsyncSession, datasource: DataSource, version: GovernedToolVersion
) -> bool:
    """Whether a tool's SQL names a column its tables no longer have: the gateway's own check.

    SQL that can no longer be read counts as unbound, because nothing can vouch for it.
    """
    try:
        references = resolve_column_references(version.sql_template, dialect=datasource.dialect)
        local_names = locally_defined_names(version.sql_template, dialect=datasource.dialect)
    except SqlglotError:
        return True
    columns = await catalog_columns(session, datasource, list(version.referenced_tables))
    return bool(findings_from_columns(references, catalog_columns=columns, local_names=local_names))


def _answers_can_move(table: MetadataTable, incident: DataQualityIncident) -> bool:
    """Whether the change can alter what SQL whose columns all still bind answers.

    Any held view can: redefined, reshaped, or gone and back, its logic may differ. A table can
    when a column was retyped, or when it was reshaped before shape changes were classed.
    """
    if _is_view(table):
        return True
    changes = (incident.evidence or {}).get("changes") or []
    return any(
        change.get("change_class") not in BINDING_SAFE_SHAPE_CHANGES
        for change in changes
        if isinstance(change, dict) and change.get("action") == ACTION_TABLE_RESHAPED
    )


# --------------------------------------------------------------------------
# 5. Retirements
# --------------------------------------------------------------------------


async def _deprecation_proposed(
    session: AsyncSession, version: GovernedToolVersion, since: datetime
) -> bool:
    """A DEPRECATE review for the version waits, or a person decided one since the change."""
    reviews = await session.scalars(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "GOVERNED_TOOL_VERSION",
            GovernanceReview.object_id == str(version.id),
            GovernanceReview.requested_action == "DEPRECATE",
        )
    )
    return any(
        review.status == "PENDING"
        or (review.decided_at is not None and _aware(review.decided_at) >= since)
        for review in reviews
    )


async def _propose_deprecation(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    version: GovernedToolVersion,
    *,
    reason: str,
    table_id: UUID,
) -> None:
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    if tool is None or datasource is None:
        raise _RebuildRefused("TOOL_DEPENDENCY_UNAVAILABLE")
    # TL-7: the checker sees the same blast radius a person's deprecation request records.
    impact = await compute_deprecation_impact(
        session, tool=tool, version=version, datasource=datasource, settings=settings
    )
    await _open_review(
        session,
        context,
        organization_id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=version.id,
        requested_action="DEPRECATE",
        details={
            "reason": reason,
            "table_id": str(table_id),
            "deprecation_impact": impact_summary(impact),
        },
    )


async def _successor(session: AsyncSession, table: MetadataTable) -> MetadataTable | None:
    """The table an approved rename says this one became, where the source still has it.

    CT-4 writes `superseded_by_table_id` when a steward approves a rename and identity merges;
    nothing writes it automatically. Followed at most three hops -- a table renamed twice
    between two passes is ordinary, a cycle is not -- and only within the same datasource,
    since a rename is one source's event.
    """
    seen = {table.id}
    current = table
    for _ in range(3):
        successor_id = current.superseded_by_table_id
        if successor_id is None or successor_id in seen:
            return None
        successor = await session.get(MetadataTable, successor_id)
        if successor is None or successor.datasource_id != table.datasource_id:
            return None
        if successor.status == "ACTIVE":
            return successor
        seen.add(successor.id)
        current = successor
    return None


async def _qualified_names(session: AsyncSession, table: MetadataTable) -> tuple[str, str] | None:
    """`(schema.table, catalog.schema.table)` -- the two shapes the gateway authorises."""
    row = (
        await session.execute(
            select(MetadataCatalog.name, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
            .where(MetadataSchema.id == table.schema_id)
        )
    ).first()
    if row is None:
        return None
    catalog_name, schema_name = row
    return f"{schema_name}.{table.name}", f"{catalog_name}.{schema_name}.{table.name}"


async def _bare_name_is_unambiguous(
    session: AsyncSession, datasource: DataSource, table: MetadataTable
) -> bool:
    """Whether this source has exactly one table of this name, retired ones included.

    A bare `orders` in a tool's SQL is only this table when no other schema has one too.
    Where two do, the bare form is left alone: rewriting the wrong one would be worse than
    proposing the retirement this rewrite exists to avoid.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(MetadataTable)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            MetadataCatalog.datasource_id == datasource.id,
            MetadataTable.organization_id == datasource.organization_id,
            func.lower(MetadataTable.name) == table.name.lower(),
        )
    )
    return count == 1


async def _rebuild_proposed(
    session: AsyncSession, version: GovernedToolVersion, since: datetime
) -> bool:
    """A newer version of this tool was drafted since the change: proposed once is enough.

    Covers a draft still waiting and one a person rejected. A published successor never
    reaches here, because publishing it supersedes the version this pass is looking at.
    """
    drafted = await session.scalar(
        select(GovernedToolVersion.created_at)
        .where(
            GovernedToolVersion.tool_id == version.tool_id,
            GovernedToolVersion.version > version.version,
        )
        .order_by(GovernedToolVersion.created_at.desc())
        .limit(1)
    )
    return drafted is not None and _aware(drafted) >= since


async def _propose_rewrite(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    datasource: DataSource,
    version: GovernedToolVersion,
    table: MetadataTable,
    successor: MetadataTable,
) -> bool:
    """Propose the same tool against the table a rename says the retired one became.

    A retired table used to leave one answer: retire every tool that reads it. That is right
    when the table is gone and wrong when it was renamed -- the query is still correct, the
    name is not. A tool generated from the renamed view is regenerated from the successor's own
    definition; any other tool has its SQL written against the new name
    (`aida.table_rename_rewrite`). Returns False when nothing can be proposed, and the caller
    then proposes the retirement it would have proposed anyway.
    """
    if version.source_routine_id is not None:
        # A routine's tool stands on the routine, not on this table: its own definition moving
        # is what rebuilds it, through the binding pass above.
        return False
    replacement = await _qualified_names(session, successor)
    if replacement is None:
        return False
    if version.source_view_table_id == table.id:
        await _regenerate_tool(
            session,
            organization_id,
            context,
            settings,
            version,
            REWRITE_TABLE_RENAMED,
            source_view_table_id=successor.id,
            details={"replaces_table_id": str(table.id), "with_table_id": str(successor.id)},
        )
        return True
    names = await _qualified_names(session, table)
    if names is None:
        return False
    schema_qualified, catalog_qualified = names
    replacements = {
        schema_qualified.lower(): replacement[0],
        catalog_qualified.lower(): replacement[0],
    }
    if await _bare_name_is_unambiguous(session, datasource, table):
        replacements[table.name.lower()] = replacement[0]
    rewritten = rewrite_table_references(
        version.sql_template, dialect=datasource.dialect, replacements=replacements
    )
    if rewritten is None:
        return False
    await _stage_tool_rebuild(
        session,
        organization_id,
        context,
        settings,
        version,
        sql_template=rewritten,
        parameters=[
            ToolParameterDefinition.model_validate(parameter)
            for parameter in version.parameter_schema
        ],
        reason=REWRITE_TABLE_RENAMED,
        details={"replaces_table_id": str(table.id), "with_table_id": str(successor.id)},
    )
    return True


async def _propose_deprecations(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    settings: Settings,
    outcome: RebuildOutcome,
) -> None:
    for incident_id in await _held_incident_ids(session, organization_id):
        incident = await session.get(DataQualityIncident, incident_id, populate_existing=True)
        if incident is None:
            continue
        table = await session.get(MetadataTable, incident.table_id, populate_existing=True)
        if table is None or await _signal_pending(session, table.id):
            continue
        table_id, datasource_id = table.id, table.datasource_id
        retired = table.status != "ACTIVE"
        since = _aware(incident.last_observed_at)
        version_ids = [
            version.id
            for version in await _published_tools(session, organization_id, datasource_id)
        ]
        for version_id in version_ids:
            datasource = await session.get(DataSource, datasource_id)
            version = await session.get(GovernedToolVersion, version_id, populate_existing=True)
            if datasource is None or version is None or version.status != "PUBLISHED":
                continue
            if table_id not in await _tool_table_ids(session, datasource, version):
                continue
            if retired:
                reason = DEPRECATE_TABLE_RETIRED
            elif (
                version.source_view_table_id is None
                and version.source_routine_id is None
                and await _columns_unbound(session, datasource, version)
            ):
                # A tool generated from a view or routine is regenerated instead.
                reason = DEPRECATE_COLUMNS_MISSING
            else:
                continue
            if await _deprecation_proposed(session, version, since):
                continue
            replacement = await session.scalar(
                select(GovernedToolVersion.id)
                .where(
                    GovernedToolVersion.tool_id == version.tool_id,
                    GovernedToolVersion.version > version.version,
                    GovernedToolVersion.status.in_(_OPEN_TOOL_STATUSES),
                )
                .limit(1)
            )
            if replacement is not None:
                # Someone is already drafting the version that replaces it.
                continue
            # A rename is not a retirement: where a steward approved one, the tool is proposed
            # against what the table became, and only a tool that cannot be is proposed for
            # retirement below.
            successor = await _successor(session, table) if retired else None
            if successor is not None and not await _rebuild_proposed(session, version, since):
                try:
                    async with session.begin_nested():
                        rewritten = await _propose_rewrite(
                            session,
                            organization_id,
                            context,
                            settings,
                            datasource,
                            version,
                            table,
                            successor,
                        )
                except _RebuildRefused as refused:
                    outcome.block(refused.code)
                    continue
                except Exception:  # noqa: BLE001 -- one rewrite must not stop the pass
                    logger.exception(
                        "context_rebuild_rewrite_failed", tool_version_id=str(version_id)
                    )
                    outcome.failed += 1
                    continue
                if rewritten:
                    outcome.tools_rewritten += 1
                    continue
            try:
                async with session.begin_nested():
                    await _propose_deprecation(
                        session,
                        organization_id,
                        context,
                        settings,
                        version,
                        reason=reason,
                        table_id=table_id,
                    )
            except _RebuildRefused as refused:
                outcome.block(refused.code)
                continue
            except Exception:  # noqa: BLE001 -- one proposal must not stop the pass
                logger.exception(
                    "context_rebuild_deprecation_failed", tool_version_id=str(version_id)
                )
                outcome.failed += 1
                continue
            outcome.deprecations_proposed += 1


# --------------------------------------------------------------------------
# 6. Context products
# --------------------------------------------------------------------------


async def _repin(
    session: AsyncSession, pinned: list[str], retired_table_ids: set[UUID]
) -> tuple[dict[str, str], set[str], str | None]:
    """Pins to point at their tool's published version, pins to drop, or why neither is possible.

    A pin to a version that reads a held table the source no longer has is dropped, published or
    not. A pin to a version no longer published points at its tool's published version; a tool
    with none leaves the product for a person.
    """
    replacements: dict[str, str] = {}
    dropped: set[str] = set()
    if not pinned:
        return replacements, dropped, None
    versions = list(
        await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.id.in_([UUID(value) for value in pinned])
            )
        )
    )
    for version in versions:
        if retired_table_ids:
            datasource = await session.get(DataSource, version.datasource_id)
            if datasource is not None and (
                await _tool_table_ids(session, datasource, version) & retired_table_ids
            ):
                dropped.add(str(version.id))
                continue
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
            return {}, set(), "PRODUCT_TOOL_UNPUBLISHED"
        replacements[str(version.id)] = str(current)
    return replacements, dropped, None


async def _meaning_replacements(session: AsyncSession, pinned: list[str]) -> dict[str, str]:
    """Pinned ontology versions their ontology has published past, mapped to the published one.

    An earlier version stays APPROVED when a later one is published, so the pin still validates;
    only the ontology's head says which meaning is current.
    """
    if not pinned:
        return {}
    replacements: dict[str, str] = {}
    versions = await session.scalars(
        select(OntologyVersion).where(OntologyVersion.id.in_([UUID(value) for value in pinned]))
    )
    for version in versions:
        head = await session.get(OntologyHead, version.ontology_id)
        if head is None or head.published_version <= version.version:
            continue
        current = await session.scalar(
            select(OntologyVersion.id).where(
                OntologyVersion.ontology_id == version.ontology_id,
                OntologyVersion.version == head.published_version,
                OntologyVersion.status == "APPROVED",
            )
        )
        if current is not None:
            replacements[str(version.id)] = str(current)
    return replacements


async def _current_meaning_replacements(
    session: AsyncSession, semantic_pins: list[str], glossary_pins: list[str]
) -> dict[str, str]:
    """Pinned semantic model and glossary term versions since superseded, mapped to the current one.

    A project publishes one semantic model at a time and a term keeps one approved definition, so
    a pin no longer PUBLISHED or APPROVED names meaning its project or term has moved past -- and
    product validation accepts only the current versions, so no re-pin of the product could pass
    while it stays. A pin with no current successor is left for the validation to refuse.
    """
    replacements: dict[str, str] = {}
    if semantic_pins:
        models = await session.scalars(
            select(SemanticModelVersion).where(
                SemanticModelVersion.id.in_([UUID(value) for value in semantic_pins]),
                SemanticModelVersion.status != "PUBLISHED",
            )
        )
        for model in models:
            current = await session.scalar(
                select(SemanticModelVersion.id)
                .where(
                    SemanticModelVersion.project_id == model.project_id,
                    SemanticModelVersion.status == "PUBLISHED",
                )
                .limit(1)
            )
            if current is not None:
                replacements[str(model.id)] = str(current)
    if glossary_pins:
        terms = await session.scalars(
            select(GlossaryTermVersion).where(
                GlossaryTermVersion.id.in_([UUID(value) for value in glossary_pins]),
                GlossaryTermVersion.status != "APPROVED",
            )
        )
        for term in terms:
            current = await session.scalar(
                select(GlossaryTermVersion.id)
                .where(
                    GlossaryTermVersion.term_id == term.term_id,
                    GlossaryTermVersion.status == "APPROVED",
                )
                .limit(1)
            )
            if current is not None:
                replacements[str(term.id)] = str(current)
    return replacements


async def _draft_product_version(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    previous: ContextProductVersion,
    replacements: dict[str, str],
    dropped_versions: set[str],
    dropped_tables: set[UUID],
    meanings: dict[str, str],
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
        if str(version_id) not in dropped_versions
    ]
    tables = [table_id for table_id in definition.table_ids if table_id not in dropped_tables]
    # Meaning pins are keyed by version id, unique across the three groups.
    definition = definition.model_copy(
        update={
            "eligible_tool_version_ids": repinned,
            "table_ids": tables,
            **{
                group: [
                    UUID(meanings.get(str(version_id), str(version_id)))
                    for version_id in getattr(definition, group)
                ]
                for group in (
                    "ontology_version_ids",
                    "semantic_model_version_ids",
                    "glossary_term_version_ids",
                )
            },
        }
    )
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
            "dropped_tool_versions": len(dropped_versions),
            "dropped_tables": len(dropped_tables),
            "repinned_meaning_versions": len(meanings),
        },
    )


async def _rebuild_products(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
) -> None:
    retired = await _retired_held_table_ids(session, organization_id)
    version_ids = [version.id for version in await _published_products(session, organization_id)]
    for version_id in version_ids:
        previous = await session.get(ContextProductVersion, version_id, populate_existing=True)
        if previous is None:
            continue
        replacements, dropped, blocked = await _repin(
            session, list(previous.eligible_tool_version_ids), retired
        )
        if blocked is not None:
            outcome.block(blocked)
            continue
        dropped_tables = {UUID(value) for value in previous.table_ids} & retired
        meanings = {
            **await _meaning_replacements(session, list(previous.ontology_version_ids or [])),
            **await _current_meaning_replacements(
                session,
                list(previous.semantic_model_version_ids),
                list(previous.glossary_term_version_ids),
            ),
        }
        if not (replacements or dropped or dropped_tables or meanings):
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
                    session,
                    organization_id,
                    context,
                    previous,
                    replacements,
                    dropped,
                    dropped_tables,
                    meanings,
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
# 7. Holds
# --------------------------------------------------------------------------

_RELEASE_REASONS: Final = {
    "RETIRED": RETIREMENT_RELEASE_REASON,
    "VIEW": HOLD_RELEASE_REASON,
    "TABLE": RESHAPE_RELEASE_REASON,
}


async def _retired_table_in_use(
    session: AsyncSession, table: MetadataTable, datasource: DataSource
) -> str | None:
    """Why a table the source no longer has still stands in published context, or `None`."""
    for version in await _published_tools(session, table.organization_id, datasource.id):
        if table.id in await _tool_table_ids(session, datasource, version):
            return WAIT_RETIRED_TABLE_IN_USE
    for product_version in await _published_products(session, table.organization_id):
        if table.id in {UUID(value) for value in product_version.table_ids}:
            return WAIT_RETIRED_TABLE_IN_USE
        pinned = [UUID(value) for value in product_version.eligible_tool_version_ids]
        if not pinned:
            continue
        versions = await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.id.in_(pinned),
                GovernedToolVersion.datasource_id == datasource.id,
            )
        )
        for version in versions:
            if table.id in await _tool_table_ids(session, datasource, version):
                return WAIT_RETIRED_TABLE_IN_USE
    return None


async def stale_dependent(session: AsyncSession, incident: DataQualityIncident) -> str | None:
    """Why a source-change hold must stay, or `None` when nothing standing on its table is stale."""
    table = await session.get(MetadataTable, incident.table_id)
    datasource = await session.get(DataSource, table.datasource_id) if table is not None else None
    if table is None or datasource is None:
        return WAIT_TABLE_NOT_STANDING
    if await _signal_pending(session, table.id):
        return WAIT_SIGNAL_PENDING
    if table.status != "ACTIVE":
        return await _retired_table_in_use(session, table, datasource)
    if _is_view(table) and await _current_definition(session, table) is None:
        return WAIT_VIEW_NOT_STANDING
    changed_at = _aware(incident.last_observed_at)
    answers_can_move = _answers_can_move(table, incident)

    for version in await _published_tools(session, incident.organization_id, datasource.id):
        if version.source_view_table_id == table.id:
            if await source_binding_drift(session, version) is not None:
                return WAIT_TOOL_STALE
            continue
        if table.id not in await _tool_table_ids(session, datasource, version):
            continue
        if await _columns_unbound(session, datasource, version):
            return WAIT_TOOL_COLUMNS_MISSING
        if answers_can_move and (
            version.approved_at is None or _aware(version.approved_at) <= changed_at
        ):
            return WAIT_TOOL_NOT_REVERIFIED

    approved = await _current_approved_description(session, incident.organization_id, table.id)
    if approved is not None and await definition_moved(session, approved) is not None:
        return WAIT_DESCRIPTION_STALE

    for product_version in await _published_products(session, incident.organization_id):
        pinned = [UUID(value) for value in product_version.eligible_tool_version_ids]
        if not pinned:
            continue
        superseded = await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.id.in_(pinned),
                GovernedToolVersion.status != "PUBLISHED",
                GovernedToolVersion.datasource_id == datasource.id,
            )
        )
        for version in superseded:
            if table.id in await _tool_table_ids(session, datasource, version):
                return WAIT_PRODUCT_STALE
    return None


async def _release_holds(
    session: AsyncSession,
    organization_id: UUID,
    context: SecurityContext,
    outcome: RebuildOutcome,
    now: datetime,
) -> None:
    for incident_id in await _held_incident_ids(session, organization_id):
        incident = await session.get(DataQualityIncident, incident_id, populate_existing=True)
        if incident is None:
            continue
        reason = await stale_dependent(session, incident)
        if reason is not None:
            outcome.wait(reason)
            continue
        table = await session.get(MetadataTable, incident.table_id)
        if table is None:
            continue
        if table.status != "ACTIVE":
            release = "RETIRED"
        else:
            release = "VIEW" if _is_view(table) else "TABLE"
        incident.status = "RESOLVED"
        incident.resolved_by = CONTEXT_REBUILD_PRINCIPAL
        incident.resolved_at = now
        incident.resolution_reason = _RELEASE_REASONS[release]
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
                "release": release,
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
    """Organizations with a source-change hold open, a published source-bound tool, or a
    proposed document a scan has read since it was mapped, or a published context product
    in an organization where an ontology has published past an earlier version."""
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
    remapping = await session.scalars(
        select(Document.organization_id).where(_document_needs_remap()).distinct()
    )
    meaning = await session.scalars(
        select(ContextProductVersion.organization_id)
        .join(OntologyHead, OntologyHead.organization_id == ContextProductVersion.organization_id)
        .join(OntologyVersion, OntologyVersion.ontology_id == OntologyHead.id)
        .where(
            ContextProductVersion.status == "PUBLISHED",
            OntologyVersion.status == "APPROVED",
            OntologyVersion.version < OntologyHead.published_version,
        )
        .distinct()
    )
    superseded_meaning = await session.scalars(
        select(ContextProductVersion.organization_id)
        .where(
            ContextProductVersion.status == "PUBLISHED",
            or_(
                select(SemanticModelVersion.id)
                .where(
                    SemanticModelVersion.organization_id == ContextProductVersion.organization_id,
                    SemanticModelVersion.status == "SUPERSEDED",
                )
                .exists(),
                select(GlossaryTermVersion.id)
                .where(
                    GlossaryTermVersion.organization_id == ContextProductVersion.organization_id,
                    GlossaryTermVersion.status == "SUPERSEDED",
                )
                .exists(),
            ),
        )
        .distinct()
    )
    joins = await relationship_drift_pending(session)
    return sorted(
        set(held)
        | set(bound)
        | set(remapping)
        | set(meaning)
        | set(superseded_meaning)
        | set(joins),
        key=str,
    )


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
    joins = await check_relationship_drift(session, organization_id, now=effective_now)
    outcome.joins_suspended += joins.suspended
    outcome.joins_restored += joins.restored
    outcome.failed += joins.failed
    await _supersede_lineage(session, organization_id, outcome, effective_now)
    await _rebuild_tools(session, organization_id, context, settings, outcome)
    await _rebuild_descriptions(session, organization_id, context, outcome)
    await _rebuild_document_claims(session, organization_id, context, outcome, effective_now)
    await _propose_deprecations(session, organization_id, context, settings, outcome)
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
