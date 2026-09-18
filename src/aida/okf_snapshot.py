"""R11-OKF01: freeze what a caller may see into the value `aida.okf_export` renders.

This is the "resolve, then pre-serialize" half of the OKF exporter, and the only half that
touches a database. It reads the catalog, the approved descriptions, the captured definition
versions, the reviewed lineage and the pinned ontology meaning **once**, under one read
decision, and returns an immutable `OkfSnapshot` whose every field is a JSON primitive. After
it returns, nothing the exporter does can be changed by a scan, a clock or a policy edit -- the
frozen-snapshot property the design requires for OKF determinism.

Three rules govern everything below.

**Authorization comes before assembly, and therefore before counting (OKF-D).** Every
datasource reached by the product's own scope is admitted or refused once, by
`_admit_datasources`, and a refused datasource's tables, routines and packages never enter the
snapshot at all. Every link is then resolved against what did enter, so a dependency the caller
may not see is absent from the text, the index, the links *and* every count -- not because a
counter filtered it, but because the exporter was never told it exists. This is the same
admit-then-assemble shape `knowledge_graph_neighborhood.NeighborhoodAuthorizationPolicy` uses,
and the same reason `context_product_api` filters a listing's total with its rows.

**Value freedom is upstream too (INV-6).** No body, definition or default expression is read
into a snapshot field, because no snapshot field can hold one. Definitions arrive as the
`ResolvedViewCoverage` / definition-version facts Atlas already computes -- availability, parse
state, truncation and a digest of the *stored value-free* text. A column's
`default_expression` is never selected. Free text that does leave -- an approved description --
is screened at export with the platform's own `screen_text`, and withheld text is reported as
withheld rather than quietly dropped.

**Only recorded approvals are exported.** An approval is read from the row that recorded it
(`AssetDocumentationVersion`, `ColumnDocumentationVersion`, `RoutineDocumentationVersion`,
`GovernanceReview`), never inferred from the existence of content. `_is_human_principal` fails
closed: a principal that looks automated is never reported as a human reviewer, because
"`verified` by a `human:` actor" is the strongest trust claim OKF has and Atlas must not
manufacture one.

INV-5: every query below restates `organization_id`, including the ones whose id set was
already produced by an organization-scoped read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings
from aida.context_compiler import (
    ResolvedOntologyMeaning,
    ResolvedRoutineReference,
    ResolvedSourceFreshness,
    ResolvedViewCoverage,
)
from aida.discovery_selection import table_kind
from aida.domain_service import check_cross_boundary_grant
from aida.envelope_models import (
    MetadataRoutine,
    MetadataRoutineDefinitionVersion,
    MetadataRoutineParameter,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.ingest_screening import CLEAN, screen_text
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    ContextProduct,
    ContextProductVersion,
    DataSource,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Project,
    ViewLineageEdge,
)
from aida.okf_export import (
    DEFINITION_TRUNCATED,
    DEFINITION_WITHHELD,
    DESCRIPTION_APPROVED,
    DESCRIPTION_NONE,
    DESCRIPTION_WITHHELD,
    KIND_TABLE,
    OkfApproval,
    OkfColumnFacts,
    OkfConceptFacts,
    OkfConceptRelation,
    OkfDefinitionFacts,
    OkfDescription,
    OkfLink,
    OkfObjectFacts,
    OkfPackageFacts,
    OkfParameterFacts,
    OkfPolicyPartition,
    OkfRoutineFacts,
    OkfSchemaFacts,
    OkfScope,
    OkfSnapshot,
    OkfSourceFacts,
    OkfSourceFreshness,
    OkfToolFacts,
    OkfToolInput,
    object_key,
    package_key,
    routine_key,
    schema_key,
    source_key,
    tool_version_key,
)
from aida.security_types import SecurityContext

#: The action a bundle read is. Not `READ_METADATA`: a bundle is assembled context a reader
#: takes away, which is the distinction `api.preview_agent_retrieval` already draws.
BUNDLE_ACTION: Final = "CONSUME_CONTEXT"
SCOPE_CONTEXT_PRODUCT: Final = "CONTEXT_PRODUCT"
#: Principal-id prefixes that mark an automated actor. `agent:` is the platform's existing
#: convention (`agent_contracts`, `mcp_server`); the rest are defensive.
_AUTOMATION_PREFIXES: Final = ("agent:", "service:", "process:", "system:", "job:")
#: Reason code when export screening refuses to release approved text.
SCREENED_OUT: Final = "EGRESS_SCREENING"


def _is_human_principal(principal: str) -> bool:
    """Whether a recorded approver may be reported as a human reviewer.

    Fails closed in the direction that matters: an actor that looks automated is never written
    as `human:`, because spec §5.3 has consumers read that prefix as "human-reviewed" and a
    bundle must not manufacture a review nobody performed.
    """
    return not principal.lower().startswith(_AUTOMATION_PREFIXES)


def _approval(actor: str | None, at: datetime | None) -> OkfApproval | None:
    """An approval only where Atlas recorded *both* who and when.

    A verification event with no instant cannot answer "how recently", which is the only
    question spec §5.2 says `verified` exists to answer, so half a record is exported as none.
    """
    if not actor or at is None:
        return None
    return OkfApproval(actor=actor, at=at.isoformat(), human=_is_human_principal(actor))


def _screened(text: str | None, origin: str) -> tuple[str | None, tuple[str, ...]]:
    """Approved text, or nothing plus the reason it was held back.

    The design says to "reuse value-free representations, screening and existing authorization
    at export and read time"; this is the screening half, and it reuses the platform's own
    detector rather than adding a second opinion.
    """
    if text is None:
        return None, ()
    verdict = screen_text(text, content_origin=origin)
    if verdict.status != CLEAN:
        return None, tuple(sorted(verdict.reason_codes)) or (SCREENED_OUT,)
    return text, ()


def _describe(
    *,
    state: str,
    text: str | None,
    version: int | None,
    approval: OkfApproval | None,
    origin: str,
) -> OkfDescription:
    if state != DESCRIPTION_APPROVED:
        return OkfDescription(state=state, version=version)
    released, reasons = _screened(text, origin)
    if released is None:
        return OkfDescription(
            state=DESCRIPTION_WITHHELD, version=version, withheld_reason_codes=reasons
        )
    return OkfDescription(
        state=DESCRIPTION_APPROVED, text=released, version=version, approval=approval
    )


# --- authorization ----------------------------------------------------------------------


async def _admit_datasources(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    *,
    organization_id: UUID,
    seed_domain_id: UUID | None,
    datasource_ids: Sequence[UUID],
) -> dict[UUID, DataSource]:
    """Decide, once each, which datasources may appear in this bundle.

    Fails closed at every step: a datasource in another organization, one across a data-domain
    boundary with no ACTIVE grant, or one the caller's own gate refuses is left out entirely --
    no row, no link, no count, and no signal in the response that it was considered. A refusal
    is not reported as "1 withheld", because a withheld *count* is the existence leak acceptance
    OKF-D exists to catch.
    """
    if not datasource_ids:
        return {}
    loaded = (
        await session.scalars(
            select(DataSource).where(
                DataSource.id.in_(list(datasource_ids)),
                DataSource.organization_id == organization_id,
            )
        )
    ).all()
    admitted: dict[UUID, DataSource] = {}
    for datasource in sorted(loaded, key=lambda row: str(row.id)):
        if seed_domain_id is not None and datasource.data_domain_id != seed_domain_id:
            granted = await check_cross_boundary_grant(
                session,
                organization_id,
                datasource.data_domain_id,
                seed_domain_id,
            )
            if not granted:
                continue
        try:
            await gate(
                session,
                context,
                settings=settings,
                action=BUNDLE_ACTION,
                resource_type="datasource",
                resource_id=str(datasource.id),
                datasource_id=datasource.id,
            )
        except AuthorizationDenied:
            continue
        admitted[datasource.id] = datasource
    return admitted


# --- description loading ----------------------------------------------------------------


async def _asset_descriptions(
    session: AsyncSession, organization_id: UUID, table_ids: Sequence[UUID]
) -> dict[UUID, AssetDocumentationVersion]:
    """The current APPROVED table description per table, in one read."""
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(AssetDocumentationVersion, AssetDocumentation.table_id)
            .join(
                AssetDocumentation,
                AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
            )
            .where(
                AssetDocumentation.table_id.in_(list(table_ids)),
                AssetDocumentation.organization_id == organization_id,
                AssetDocumentationVersion.organization_id == organization_id,
                AssetDocumentationVersion.status == DESCRIPTION_APPROVED,
            )
            .order_by(AssetDocumentationVersion.version)
        )
    ).all()
    # Ascending, so the newest approved row per table wins.
    return {table_id: version for version, table_id in rows}


async def _column_descriptions(
    session: AsyncSession, organization_id: UUID, table_ids: Sequence[UUID]
) -> dict[UUID, ColumnDocumentationVersion]:
    """The current APPROVED column description per column, for a page of tables.

    Filters on `ColumnDocumentation.table_id` -- the denormalized parent
    `column_documentation.current_descriptions_for_table` exists for -- so this is one query
    for the whole bundle rather than one per table.
    """
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(ColumnDocumentationVersion, ColumnDocumentation.column_id)
            .join(
                ColumnDocumentation,
                ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
            )
            .where(
                ColumnDocumentation.table_id.in_(list(table_ids)),
                ColumnDocumentation.organization_id == organization_id,
                ColumnDocumentationVersion.organization_id == organization_id,
                ColumnDocumentationVersion.status == DESCRIPTION_APPROVED,
            )
            .order_by(ColumnDocumentationVersion.version)
        )
    ).all()
    # Ascending, so the newest approved row per column wins -- the same last-write-wins batch
    # shape `routine_description_service.current_routine_descriptions` uses.
    return {column_id: version for version, column_id in rows}


async def _routine_description_versions(
    session: AsyncSession, organization_id: UUID, routine_ids: Sequence[UUID]
) -> dict[UUID, RoutineDocumentationVersion]:
    if not routine_ids:
        return {}
    rows = (
        await session.execute(
            select(RoutineDocumentationVersion, RoutineDocumentation.routine_id)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id.in_(list(routine_ids)),
                RoutineDocumentation.organization_id == organization_id,
                RoutineDocumentationVersion.organization_id == organization_id,
                RoutineDocumentationVersion.status == DESCRIPTION_APPROVED,
            )
            .order_by(RoutineDocumentationVersion.version)
        )
    ).all()
    return {routine_id: version for version, routine_id in rows}


async def _routine_definition_versions(
    session: AsyncSession, organization_id: UUID, routine_ids: Sequence[UUID]
) -> dict[UUID, MetadataRoutineDefinitionVersion]:
    """The newest captured definition version per routine.

    `MetadataRoutineDefinitionVersion` is append-only and an identical rescan writes nothing, so
    its `version_number`/`captured_at` are the one definition timestamps safe to put inside a
    hashed document: they move when the definition moves and not when a scan merely runs.
    """
    if not routine_ids:
        return {}
    newest = (
        select(
            MetadataRoutineDefinitionVersion.routine_id.label("routine_id"),
            func.max(MetadataRoutineDefinitionVersion.version_number).label("version_number"),
        )
        .where(
            MetadataRoutineDefinitionVersion.routine_id.in_(list(routine_ids)),
            MetadataRoutineDefinitionVersion.organization_id == organization_id,
        )
        .group_by(MetadataRoutineDefinitionVersion.routine_id)
        .subquery()
    )
    rows = (
        await session.scalars(
            select(MetadataRoutineDefinitionVersion)
            .join(
                newest,
                (MetadataRoutineDefinitionVersion.routine_id == newest.c.routine_id)
                & (
                    MetadataRoutineDefinitionVersion.version_number
                    == newest.c.version_number
                ),
            )
            .where(MetadataRoutineDefinitionVersion.organization_id == organization_id)
        )
    ).all()
    return {row.routine_id: row for row in rows}


async def _ontology_approvals(
    session: AsyncSession, organization_id: UUID, version_ids: Sequence[str]
) -> dict[str, OkfApproval]:
    """The recorded governance decision behind each pinned ontology version.

    An ontology version stores `approved_by` but no instant, so the instant comes from the
    `GovernanceReview` that decided it. A version with no such review yields no approval at all
    -- which makes its concept documents unverified rather than verified-at-an-unknown-time.
    """
    if not version_ids:
        return {}
    rows = (
        await session.execute(
            select(
                GovernanceReview.object_id,
                GovernanceReview.decided_by,
                GovernanceReview.decided_at,
            ).where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.object_type == "ONTOLOGY_VERSION",
                GovernanceReview.object_id.in_([str(value) for value in version_ids]),
                GovernanceReview.status == "APPROVED",
            )
        )
    ).all()
    approvals: dict[str, OkfApproval] = {}
    for object_id, decided_by, decided_at in rows:
        approval = _approval(decided_by, decided_at)
        if approval is not None:
            approvals[str(object_id)] = approval
    return approvals


#: R11-OKF02: tool-version statuses a reviewer's approval stands behind. Anything else -- a
#: DRAFT, a version still in review, a REJECTED one -- is exported with no asserted purpose.
_APPROVED_TOOL_STATUSES: Final = frozenset({"PUBLISHED", "SUPPORTED", "SUPERSEDED", "DEPRECATED"})


def _uuids(values: Sequence[Any]) -> list[UUID]:
    """The values that are UUIDs. A pin that is not one names no tool Atlas holds."""
    return [UUID(str(value)) for value in values if _is_uuid(value)]


async def _eligible_tools(
    session: AsyncSession, organization_id: UUID, version: ContextProductVersion
) -> list[tuple[GovernedToolVersion, GovernedTool]]:
    """The tool versions the product pins, with their tool, in one organization-scoped read."""
    ids = _uuids(list(version.eligible_tool_version_ids or []))
    if not ids:
        return []
    rows = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.id.in_(ids),
                GovernedToolVersion.organization_id == organization_id,
                GovernedTool.organization_id == organization_id,
            )
        )
    ).all()
    return [(tool_version, tool) for tool_version, tool in rows]


async def candidate_datasource_ids(
    session: AsyncSession, version: ContextProductVersion
) -> list[UUID]:
    """R11-OKF02: every datasource the product version's own scope reaches, sorted.

    Its tables, its routines and its eligible tools -- the same three sets `freeze_snapshot`
    loads rows for. `aida.okf_store` admits exactly this set to compute the reader's authority
    digest and hands the admitted result back to the freeze, so the bundle stored under a digest
    is the bundle that digest's admission produced, not a second decision taken moments later.
    """
    organization_id = version.organization_id
    table_ids = _uuids(list(version.table_ids or []))
    routine_ids = _uuids(list(version.routine_ids or []))
    candidates: set[UUID] = set()
    if table_ids:
        candidates.update(
            (
                await session.scalars(
                    select(MetadataTable.datasource_id).where(
                        MetadataTable.id.in_(table_ids),
                        MetadataTable.organization_id == organization_id,
                    )
                )
            ).all()
        )
    if routine_ids:
        candidates.update(
            (
                await session.scalars(
                    select(MetadataRoutine.datasource_id).where(
                        MetadataRoutine.id.in_(routine_ids),
                        MetadataRoutine.organization_id == organization_id,
                    )
                )
            ).all()
        )
    candidates.update(
        tool_version.datasource_id
        for tool_version, _tool in await _eligible_tools(session, organization_id, version)
    )
    return sorted(candidates, key=str)


async def admit_datasources(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    *,
    product: ContextProduct,
    version: ContextProductVersion,
) -> dict[UUID, DataSource]:
    """R11-OKF02: the reader's per-datasource admission for one product version, on its own.

    The same decision `freeze_snapshot` takes -- same seed domain, same cross-boundary grant
    check, same gate -- over the same candidate set, exposed so the store can key a stored
    bundle on it before deciding whether anything needs freezing at all.
    """
    organization_id = version.organization_id
    seed_domain_id = await session.scalar(
        select(Project.data_domain_id).where(
            Project.id == product.project_id,
            Project.organization_id == organization_id,
        )
    )
    return await _admit_datasources(
        session,
        context,
        settings,
        organization_id=organization_id,
        seed_domain_id=seed_domain_id,
        datasource_ids=await candidate_datasource_ids(session, version),
    )


# --- assembly ---------------------------------------------------------------------------


async def freeze_snapshot(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    *,
    product: ContextProduct,
    version: ContextProductVersion,
    routines: Sequence[ResolvedRoutineReference],
    views: Sequence[ResolvedViewCoverage],
    ontology: Sequence[ResolvedOntologyMeaning],
    freshness: Sequence[ResolvedSourceFreshness],
    captured_at: datetime,
    admitted: Mapping[UUID, DataSource] | None = None,
) -> OkfSnapshot:
    """Freeze one context product version into the value the exporter renders.

    `admitted` (R11-OKF02) is the reader's admission, already decided by `admit_datasources`
    for the stored bundle's authority digest. Passed in so the freeze cannot take a second,
    possibly different decision; when absent the freeze decides itself, as R11-OKF01 did.

    `routines`, `views`, `ontology` and `freshness` arrive already resolved by the context
    compiler's own scope resolver (`context_compiler_api._load_source`), which is what the
    design requires: "Source/object preview and product export must reuse the compiler's scope
    resolver." Everything this function adds -- schemas, columns, descriptions, capture
    versions, dependency edges, packages -- is loaded here and cut to what
    `_admit_datasources` allowed.

    `captured_at` is the caller's clock reading, taken once. It is the only time value in the
    result that is not a recorded content event, and it reaches the manifest only, never a
    concept document.
    """
    organization_id = version.organization_id
    table_ids = [UUID(value) for value in version.table_ids]
    routine_ids = [UUID(routine.routine_id) for routine in routines]

    table_rows = (
        (
            await session.execute(
                select(MetadataTable, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(
                    MetadataTable.id.in_(table_ids),
                    MetadataTable.organization_id == organization_id,
                )
            )
        ).all()
        if table_ids
        else []
    )
    routine_rows = (
        (
            await session.execute(
                select(MetadataRoutine, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(
                    MetadataRoutine.id.in_(routine_ids),
                    MetadataRoutine.organization_id == organization_id,
                )
            )
        ).all()
        if routine_ids
        else []
    )

    tool_rows = await _eligible_tools(session, organization_id, version)
    if admitted is None:
        seed_domain_id = await session.scalar(
            select(Project.data_domain_id).where(
                Project.id == product.project_id,
                Project.organization_id == organization_id,
            )
        )
        candidates = (
            {row[0].datasource_id for row in table_rows}
            | {row[0].datasource_id for row in routine_rows}
            | {tool_version.datasource_id for tool_version, _tool in tool_rows}
        )
        admitted = await _admit_datasources(
            session,
            context,
            settings,
            organization_id=organization_id,
            seed_domain_id=seed_domain_id,
            datasource_ids=sorted(candidates, key=str),
        )

    table_rows = [row for row in table_rows if row[0].datasource_id in admitted]
    routine_rows = [row for row in routine_rows if row[0].datasource_id in admitted]
    tool_rows = [row for row in tool_rows if row[0].datasource_id in admitted]
    admitted_table_ids = [row[0].id for row in table_rows]
    admitted_routine_ids = [row[0].id for row in routine_rows]

    columns = (
        (
            await session.scalars(
                # Deliberately no `default_expression`: the catalog's most literal-bearing
                # column, and INV-6 means it has nowhere to go in a snapshot anyway.
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(admitted_table_ids),
                    MetadataColumn.organization_id == organization_id,
                )
            )
        ).all()
        if admitted_table_ids
        else []
    )
    asset_descriptions = await _asset_descriptions(
        session, organization_id, admitted_table_ids
    )
    column_descriptions = await _column_descriptions(
        session, organization_id, admitted_table_ids
    )
    routine_descriptions = await _routine_description_versions(
        session, organization_id, admitted_routine_ids
    )
    definition_versions = await _routine_definition_versions(
        session, organization_id, admitted_routine_ids
    )
    parameters = (
        (
            await session.scalars(
                select(MetadataRoutineParameter).where(
                    MetadataRoutineParameter.routine_id.in_(admitted_routine_ids),
                    MetadataRoutineParameter.organization_id == organization_id,
                    MetadataRoutineParameter.status == "ACTIVE",
                )
            )
        ).all()
        if admitted_routine_ids
        else []
    )
    view_edges = (
        (
            await session.execute(
                select(ViewLineageEdge.target_table_id, ViewLineageEdge.source_table_id).where(
                    ViewLineageEdge.organization_id == organization_id,
                    ViewLineageEdge.target_table_id.in_(admitted_table_ids),
                    ViewLineageEdge.source_table_id.in_(admitted_table_ids),
                    ViewLineageEdge.review_status == "ACTIVE",
                )
            )
        ).all()
        if admitted_table_ids
        else []
    )

    ontology_approvals = await _ontology_approvals(
        session, organization_id, [meaning.version_id for meaning in ontology]
    )
    return _assemble(
        product=product,
        version=version,
        captured_at=captured_at,
        admitted=admitted,
        table_rows=[(row[0], row[1], row[2]) for row in table_rows],
        routine_rows=[(row[0], row[1], row[2]) for row in routine_rows],
        columns=list(columns),
        parameters=list(parameters),
        asset_descriptions=asset_descriptions,
        column_descriptions=column_descriptions,
        routine_descriptions=routine_descriptions,
        definition_versions=definition_versions,
        view_edges=[(target, source) for target, source in view_edges],
        resolved_routines=list(routines),
        view_coverage=list(views),
        ontology=list(ontology),
        ontology_approvals=ontology_approvals,
        freshness=list(freshness),
        tool_rows=tool_rows,
    )


def _tool_facts(
    tool_rows: Sequence[tuple[GovernedToolVersion, GovernedTool]],
) -> tuple[OkfToolFacts, ...]:
    """R11-OKF02: admitted tool versions as references. The SQL template is never read into a
    field, and of the parameter schema only name, type and required-ness survive -- a declared
    default, allowed value or bound is a literal, which INV-6 keeps out."""
    facts: list[OkfToolFacts] = []
    for tool_version, tool in tool_rows:
        approved = (
            tool_version.status.upper() in _APPROVED_TOOL_STATUSES
            and bool(tool_version.approved_by)
            and tool_version.approved_at is not None
        )
        inputs = []
        for item in tool_version.parameter_schema or []:
            if not isinstance(item, Mapping) or not item.get("name"):
                continue
            inputs.append(
                OkfToolInput(
                    name=str(item["name"]),
                    physical_type=str(item.get("parameter_type") or item.get("type") or "unknown"),
                    required=bool(item.get("required", True)),
                )
            )
        facts.append(
            OkfToolFacts(
                key=tool_version_key(str(tool.project_id), tool.slug, tool_version.version),
                tool_version_id=str(tool_version.id),
                slug=tool.slug,
                name=tool_version.name,
                version=tool_version.version,
                lifecycle=tool_version.status,
                source_key=source_key(str(tool_version.datasource_id)),
                fingerprint=tool_version.fingerprint,
                inputs=tuple(sorted(inputs, key=lambda entry: entry.name)),
                description=_describe(
                    state=DESCRIPTION_APPROVED if approved else DESCRIPTION_NONE,
                    text=tool_version.description if approved else None,
                    version=tool_version.version,
                    approval=(
                        _approval(tool_version.approved_by, tool_version.approved_at)
                        if approved
                        else None
                    ),
                    origin="okf_export:tool_description",
                ),
            )
        )
    return tuple(sorted(facts, key=lambda item: (item.name, item.version, item.key)))


def _policy_partition(version: ContextProductVersion) -> OkfPolicyPartition:
    """The partition the bundle belongs to, from the version's own approved policy.

    Read from `policy_summary` and `allowed_consumer_roles` rather than from the caller's
    context: the partition is a property of the product, so two callers with different roles
    reading the same published version get the same partition digest, and a manifest can be
    compared across them.
    """
    summary: dict[str, Any] = dict(version.policy_summary or {})
    classifications = summary.get("classifications") or summary.get("classification") or []
    if isinstance(classifications, str):
        classifications = [classifications]
    return OkfPolicyPartition(
        allowed_consumer_roles=tuple(sorted(version.allowed_consumer_roles or [])),
        source_values=str(summary.get("source_values", "GATEWAY_ONLY")),
        classifications=tuple(sorted(str(value) for value in classifications)),
        purpose=summary.get("purpose") or version.purpose,
    )


def _definition_from_coverage(coverage: ResolvedViewCoverage) -> OkfDefinitionFacts:
    """A view's definition coverage, carried over from what the compiler already resolved.

    No capture version and no capture instant: unlike a routine, a view definition has no
    append-only version table, and `MetadataViewDefinition.updated_at` can move on a rescan that
    changed nothing. Putting it in a document would break acceptance OKF-C, so the digest is the
    change signal here and the read time lives in the manifest.
    """
    reasons: list[str] = []
    if not coverage.definition_available:
        reasons.append(DEFINITION_WITHHELD)
    if coverage.truncated:
        reasons.append(DEFINITION_TRUNCATED)
    return OkfDefinitionFacts(
        available=coverage.definition_available,
        digest=coverage.definition_digest,
        truncated=coverage.truncated,
        lineage=coverage.lineage,
        reason_codes=tuple(reasons),
    )


def _definition_from_routine(
    resolved: ResolvedRoutineReference,
    captured: MetadataRoutineDefinitionVersion | None,
) -> OkfDefinitionFacts:
    reasons: list[str] = []
    if not resolved.definition_available:
        reasons.append(DEFINITION_WITHHELD)
    if captured is not None and captured.truncated:
        reasons.append(DEFINITION_TRUNCATED)
    return OkfDefinitionFacts(
        available=resolved.definition_available,
        digest=resolved.definition_digest,
        truncated=bool(captured is not None and captured.truncated),
        lineage=resolved.lineage,
        reason_codes=tuple(reasons),
        capture_version=captured.version_number if captured is not None else None,
        captured_at=captured.captured_at.isoformat() if captured is not None else None,
    )


def _object_limitations(
    coverage: ResolvedViewCoverage | None, kind: str, description_state: str
) -> tuple[str, ...]:
    """What the document must not let a reader assume. Never a guess: each line names a
    recorded state, because "Limitations" that lists possibilities rather than facts is noise a
    reader learns to skip."""
    limitations: list[str] = []
    if coverage is not None:
        if not coverage.definition_available:
            limitations.append(
                "The definition was not released to Atlas, so the dependencies below are "
                "whatever earlier evidence recorded and may be incomplete."
            )
        if coverage.truncated:
            limitations.append(
                "The captured definition was truncated, so parsed dependencies are partial."
            )
        if coverage.lineage == "PROPOSED":
            limitations.append(
                "Lineage for this object is proposed and undecided; it steers nothing and is "
                "not shown as a dependency."
            )
        elif coverage.lineage == "NONE" and kind != KIND_TABLE:
            limitations.append("No reviewed lineage was derived for this object.")
    if description_state != DESCRIPTION_APPROVED:
        limitations.append(
            "Atlas asserts no approved purpose for this object; nothing here is a reviewed "
            "statement of what it means."
        )
    return tuple(limitations)


def _routine_limitations(resolved: ResolvedRoutineReference) -> tuple[str, ...]:
    limitations: list[str] = []
    if not resolved.definition_available:
        limitations.append(
            "The routine body was not released to Atlas, so reads and writes below rest on "
            "earlier evidence and may be incomplete."
        )
    if not resolved.fully_parsed:
        limitations.append(
            "The body did not parse completely -- dynamic SQL or an unsupported construct -- "
            "so some reads and writes are unresolved."
        )
    if resolved.lineage == "PROPOSED":
        limitations.append(
            "Procedure lineage is proposed and undecided; it steers nothing and is not shown "
            "as a dependency."
        )
    elif resolved.lineage == "NONE":
        limitations.append("No reviewed procedure lineage was derived for this routine.")
    if resolved.description_state != DESCRIPTION_APPROVED:
        limitations.append(
            "Atlas asserts no approved purpose for this routine; nothing here is a reviewed "
            "statement of what it does."
        )
    return tuple(limitations)


def _assemble(
    *,
    product: ContextProduct,
    version: ContextProductVersion,
    captured_at: datetime,
    admitted: Mapping[UUID, DataSource],
    table_rows: Sequence[tuple[MetadataTable, MetadataSchema, MetadataCatalog]],
    routine_rows: Sequence[tuple[MetadataRoutine, MetadataSchema, MetadataCatalog]],
    columns: Sequence[MetadataColumn],
    parameters: Sequence[MetadataRoutineParameter],
    asset_descriptions: dict[UUID, AssetDocumentationVersion],
    column_descriptions: dict[UUID, ColumnDocumentationVersion],
    routine_descriptions: dict[UUID, RoutineDocumentationVersion],
    definition_versions: dict[UUID, MetadataRoutineDefinitionVersion],
    view_edges: Sequence[tuple[UUID, UUID]],
    resolved_routines: Sequence[ResolvedRoutineReference],
    view_coverage: Sequence[ResolvedViewCoverage],
    ontology: Sequence[ResolvedOntologyMeaning],
    ontology_approvals: dict[str, OkfApproval],
    freshness: Sequence[ResolvedSourceFreshness],
    tool_rows: Sequence[tuple[GovernedToolVersion, GovernedTool]] = (),
) -> OkfSnapshot:
    """Turn admitted rows into the frozen value. Pure from here down: no session, no clock.

    Split out from `freeze_snapshot` so the mapping -- which keys an object gets, which links
    survive, which approval reaches which document -- is testable without a database, and so the
    reading and the deciding cannot be accidentally interleaved.
    """
    sources = tuple(
        OkfSourceFacts(
            key=source_key(str(datasource.id)),
            name=datasource.name,
            dialect=datasource.dialect,
            connector_type=datasource.connector_type,
            environment=datasource.environment,
            lifecycle=datasource.status,
        )
        for datasource in sorted(admitted.values(), key=lambda row: (row.name, str(row.id)))
    )
    schemas: dict[str, OkfSchemaFacts] = {}
    owners: list[tuple[UUID, MetadataSchema, MetadataCatalog]] = [
        (table.datasource_id, schema, catalog) for table, schema, catalog in table_rows
    ]
    owners.extend(
        (routine.datasource_id, schema, catalog) for routine, schema, catalog in routine_rows
    )
    for datasource_id, schema, catalog in owners:
        key = schema_key(str(datasource_id), catalog.name, schema.name)
        schemas.setdefault(
            key,
            OkfSchemaFacts(
                key=key,
                name=schema.name,
                catalog_name=catalog.name,
                qualified_name=f"{catalog.name}.{schema.name}",
                source_key=source_key(str(datasource_id)),
                lifecycle=schema.status,
            ),
        )

    coverage_by_table = {UUID(row.table_id): row for row in view_coverage}
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
    parameters_by_routine: dict[UUID, list[MetadataRoutineParameter]] = {}
    for parameter in parameters:
        parameters_by_routine.setdefault(parameter.routine_id, []).append(parameter)

    keys_by_table: dict[UUID, str] = {}
    for table, schema, catalog in table_rows:
        keys_by_table[table.id] = object_key(
            str(table.datasource_id), catalog.name, schema.name, table.name
        )
    keys_by_routine: dict[UUID, str] = {}
    for routine, schema, catalog in routine_rows:
        keys_by_routine[routine.id] = routine_key(
            str(routine.datasource_id),
            catalog.name,
            schema.name,
            routine.package_name,
            routine.name,
            routine.signature,
        )
    resolved_by_id = {UUID(row.routine_id): row for row in resolved_routines}

    reads: dict[UUID, set[UUID]] = {}
    writes: dict[UUID, set[UUID]] = {}
    for routine_id, resolved in resolved_by_id.items():
        if routine_id not in keys_by_routine:
            continue
        reads[routine_id] = {
            UUID(value) for value in resolved.reads_table_ids if UUID(value) in keys_by_table
        }
        writes[routine_id] = {
            UUID(value) for value in resolved.writes_table_ids if UUID(value) in keys_by_table
        }

    objects: list[OkfObjectFacts] = []
    for table, schema, catalog in sorted(table_rows, key=lambda row: row[0].name):
        key = keys_by_table[table.id]
        kind = table_kind(table.object_type)
        asset_version = asset_descriptions.get(table.id)
        description = _describe(
            state=DESCRIPTION_APPROVED if asset_version is not None else DESCRIPTION_NONE,
            text=asset_version.readme if asset_version is not None else None,
            version=asset_version.version if asset_version is not None else None,
            approval=(
                _approval(asset_version.approved_by, asset_version.approved_at)
                if asset_version is not None
                else None
            ),
            origin="okf_export:table_description",
        )
        column_facts: list[OkfColumnFacts] = []
        for column in sorted(
            columns_by_table.get(table.id, []), key=lambda item: (item.ordinal_position, item.name)
        ):
            column_version = column_descriptions.get(column.id)
            column_facts.append(
                OkfColumnFacts(
                    name=column.name,
                    ordinal=column.ordinal_position,
                    physical_type=column.physical_type,
                    nullable=column.nullable,
                    classification=column.classification,
                    lifecycle=column.status,
                    description=_describe(
                        state=(
                            DESCRIPTION_APPROVED
                            if column_version is not None
                            else DESCRIPTION_NONE
                        ),
                        text=column_version.description if column_version is not None else None,
                        version=column_version.version if column_version is not None else None,
                        approval=(
                            _approval(column_version.approved_by, column_version.approved_at)
                            if column_version is not None
                            else None
                        ),
                        origin="okf_export:column_description",
                    ),
                )
            )
        coverage = coverage_by_table.get(table.id)
        links: list[OkfLink] = []
        for target, source in view_edges:
            if target == table.id and source in keys_by_table:
                links.append(OkfLink(target_key=keys_by_table[source], relation="reads"))
        for routine_id, written in writes.items():
            if table.id in written:
                links.append(
                    OkfLink(target_key=keys_by_routine[routine_id], relation="written by")
                )
        for routine_id, read in reads.items():
            if table.id in read and routine_id in keys_by_routine:
                links.append(OkfLink(target_key=keys_by_routine[routine_id], relation="read by"))
        objects.append(
            OkfObjectFacts(
                key=key,
                kind=kind,
                native_object_type=table.object_type,
                name=table.name,
                qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
                schema_key=schema_key(str(table.datasource_id), catalog.name, schema.name),
                source_key=source_key(str(table.datasource_id)),
                lifecycle=table.status,
                columns=tuple(column_facts),
                description=description,
                definition=_definition_from_coverage(coverage) if coverage is not None else None,
                links=tuple(sorted(set(links), key=lambda link: (link.relation, link.target_key))),
                limitations=_object_limitations(coverage, kind, description.state),
            )
        )

    routine_facts: list[OkfRoutineFacts] = []
    packages: dict[str, list[str]] = {}
    package_rows: dict[str, tuple[MetadataRoutine, MetadataSchema, MetadataCatalog]] = {}
    for routine, schema, catalog in sorted(routine_rows, key=lambda row: row[0].name):
        key = keys_by_routine[routine.id]
        reference = resolved_by_id.get(routine.id)
        if reference is None:
            continue
        package_name = routine.package_name or (
            routine.name if routine.routine_type.upper() == "PACKAGE" else ""
        )
        if routine.routine_type.upper() == "PACKAGE":
            pkey = package_key(str(routine.datasource_id), catalog.name, schema.name, package_name)
            package_rows[pkey] = (routine, schema, catalog)
            packages.setdefault(pkey, [])
            continue
        if routine.package_name:
            pkey = package_key(
                str(routine.datasource_id), catalog.name, schema.name, routine.package_name
            )
            packages.setdefault(pkey, []).append(key)
        routine_version = routine_descriptions.get(routine.id)
        description = _describe(
            state=reference.description_state,
            text=reference.description,
            version=routine_version.version if routine_version is not None else None,
            approval=(
                _approval(routine_version.approved_by, routine_version.approved_at)
                if routine_version is not None
                else None
            ),
            origin="okf_export:routine_description",
        )
        links = [
            OkfLink(target_key=keys_by_table[table_id], relation="reads")
            for table_id in sorted(reads.get(routine.id, set()), key=str)
        ]
        links.extend(
            OkfLink(target_key=keys_by_table[table_id], relation="writes")
            for table_id in sorted(writes.get(routine.id, set()), key=str)
        )
        qualified = ".".join(
            part for part in (catalog.name, schema.name, routine.package_name, routine.name) if part
        )
        routine_facts.append(
            OkfRoutineFacts(
                key=key,
                name=routine.name,
                package_name=routine.package_name,
                signature=routine.signature,
                qualified_name=qualified,
                routine_type=routine.routine_type,
                schema_key=schema_key(str(routine.datasource_id), catalog.name, schema.name),
                source_key=source_key(str(routine.datasource_id)),
                lifecycle=routine.status,
                native_subtype=routine.native_subtype,
                language=routine.language,
                return_type=routine.return_type,
                is_deterministic=routine.is_deterministic,
                security_mode=routine.security_mode,
                parameters=tuple(
                    OkfParameterFacts(
                        name=parameter.name or f"${parameter.ordinal_position}",
                        ordinal=parameter.ordinal_position,
                        mode=parameter.mode or "IN",
                        physical_type=parameter.physical_type or "unknown",
                    )
                    for parameter in sorted(
                        parameters_by_routine.get(routine.id, []),
                        key=lambda item: item.ordinal_position,
                    )
                ),
                description=description,
                definition=_definition_from_routine(
                    reference, definition_versions.get(routine.id)
                ),
                links=tuple(sorted(set(links), key=lambda link: (link.relation, link.target_key))),
                limitations=_routine_limitations(reference),
            )
        )

    package_facts: list[OkfPackageFacts] = []
    for pkey, member_keys in sorted(packages.items()):
        row = package_rows.get(pkey)
        if row is not None:
            routine, schema, catalog = row
            reference = resolved_by_id.get(routine.id)
            package_version = routine_descriptions.get(routine.id)
            description = _describe(
                state=reference.description_state if reference is not None else DESCRIPTION_NONE,
                text=reference.description if reference is not None else None,
                version=package_version.version if package_version is not None else None,
                approval=(
                    _approval(package_version.approved_by, package_version.approved_at)
                    if package_version is not None
                    else None
                ),
                origin="okf_export:package_description",
            )
            name = routine.package_name or routine.name
            package_source_id = str(routine.datasource_id)
            lifecycle = routine.status
        else:
            member = next(
                (
                    entry
                    for entry in routine_rows
                    if package_key(
                        str(entry[0].datasource_id),
                        entry[2].name,
                        entry[1].name,
                        entry[0].package_name,
                    )
                    == pkey
                ),
                None,
            )
            if member is None:
                continue
            routine, schema, catalog = member
            description = OkfDescription()
            name = routine.package_name
            package_source_id = str(routine.datasource_id)
            lifecycle = schema.status
        package_facts.append(
            OkfPackageFacts(
                key=pkey,
                name=name,
                qualified_name=f"{catalog.name}.{schema.name}.{name}",
                schema_key=schema_key(package_source_id, catalog.name, schema.name),
                source_key=source_key(package_source_id),
                lifecycle=lifecycle,
                member_keys=tuple(sorted(member_keys)),
                description=description,
            )
        )

    concepts = _concepts(ontology, ontology_approvals, keys_by_table, keys_by_routine)
    source_keys = {source.key: source for source in sources}
    return OkfSnapshot(
        captured_at=captured_at.isoformat(),
        scope=OkfScope(
            kind=SCOPE_CONTEXT_PRODUCT,
            organization_id=str(version.organization_id),
            policy_partition=_policy_partition(version),
            product_key=product.product_key,
            product_version=version.version,
            product_version_id=str(version.id),
            product_fingerprint=version.fingerprint,
            product_name=version.name,
            product_purpose=version.purpose,
            # R11-OKF02: only the pins that resolved to a tool over an admitted datasource. A
            # pin to a tool the reader's authorization refused is not listed, exactly as the
            # tool's own document is not rendered and not counted (OKF-D).
            eligible_tool_version_ids=tuple(
                sorted(str(tool_version.id) for tool_version, _tool in tool_rows)
            ),
        ),
        sources=sources,
        schemas=tuple(sorted(schemas.values(), key=lambda item: item.qualified_name)),
        objects=tuple(objects),
        routines=tuple(routine_facts),
        packages=tuple(package_facts),
        concepts=concepts,
        freshness=tuple(
            OkfSourceFreshness(
                source_key=source_key(row.datasource_id),
                last_scan_completed_at=row.last_scan_completed_at,
                last_full_scan_completed_at=row.last_full_scan_completed_at,
            )
            for row in sorted(freshness, key=lambda item: item.datasource_id)
            if source_key(row.datasource_id) in source_keys
        ),
        tools=_tool_facts(
            [row for row in tool_rows if source_key(str(row[0].datasource_id)) in source_keys]
        ),
    )


def _concepts(
    ontology: Sequence[ResolvedOntologyMeaning],
    approvals: dict[str, OkfApproval],
    keys_by_table: dict[UUID, str],
    keys_by_routine: dict[UUID, str],
) -> tuple[OkfConceptFacts, ...]:
    """Business concepts from the pinned approved ontology versions.

    Mappings are cut to objects that entered the snapshot, so a concept mapped to a table the
    caller was not admitted to shows fewer mapped objects rather than revealing one. A concept
    whose definition `load_ontology_meaning` already withheld carries the reason and no text.
    """
    facts: list[OkfConceptFacts] = []
    for meaning in sorted(ontology, key=lambda item: (item.ontology_key, item.version)):
        approval = approvals.get(meaning.version_id)
        names = {
            str(concept.get("name") or concept.get("key") or "")
            for concept in meaning.concepts
        }
        for concept in sorted(
            meaning.concepts, key=lambda item: str(item.get("name") or item.get("key") or "")
        ):
            name = str(concept.get("name") or concept.get("key") or "")
            if not name:
                continue
            mapped_tables = tuple(
                sorted(
                    keys_by_table[UUID(str(value))]
                    for value in concept.get("table_ids", []) or []
                    if _is_uuid(value) and UUID(str(value)) in keys_by_table
                )
            )
            mapped_routines = tuple(
                sorted(
                    keys_by_routine[UUID(str(value))]
                    for value in concept.get("routine_ids", []) or []
                    if _is_uuid(value) and UUID(str(value)) in keys_by_routine
                )
            )
            definition = concept.get("definition") or concept.get("description")
            withheld = tuple(
                sorted(
                    str(entry.get("reason_code") or entry.get("reason") or SCREENED_OUT)
                    for entry in meaning.withheld
                    if str(entry.get("name") or entry.get("subject") or "") == name
                )
            )
            released, reasons = _screened(
                str(definition) if definition else None, "okf_export:ontology_definition"
            )
            facts.append(
                OkfConceptFacts(
                    key=_concept_key(meaning, name),
                    name=name,
                    ontology_key=meaning.ontology_key,
                    ontology_version=meaning.version,
                    lifecycle=meaning.lifecycle,
                    label=str(concept.get("label")) if concept.get("label") else None,
                    definition=released,
                    mapped_object_keys=mapped_tables,
                    mapped_routine_keys=mapped_routines,
                    relations=_relations(meaning, name, names),
                    approval=approval,
                    withheld_reason_codes=tuple(sorted({*withheld, *reasons})),
                )
            )
    return tuple(facts)


def _concept_key(meaning: ResolvedOntologyMeaning, name: str) -> str:
    from aida.okf_export import concept_key

    return concept_key(meaning.ontology_key, meaning.version, name)


def _relations(
    meaning: ResolvedOntologyMeaning, name: str, names: set[str]
) -> tuple[OkfConceptRelation, ...]:
    relations: list[OkfConceptRelation] = []
    for relation in meaning.relations:
        subject = str(relation.get("subject") or relation.get("from") or "")
        target = str(relation.get("object") or relation.get("to") or "")
        predicate = str(relation.get("predicate") or relation.get("kind") or "relates to")
        if subject != name or not target:
            continue
        relations.append(
            OkfConceptRelation(
                predicate=predicate,
                target_name=target,
                target_key=_concept_key(meaning, target) if target in names else None,
            )
        )
    return tuple(relations)


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
