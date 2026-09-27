"""The compile route's read decision, decided in one place (R11-GQL01).

`GET /v1/context-product-versions/{id}/compile` -- and its download and drift siblings, the OKF
store's scope resolver and GraphQL's `contextProductCoverage` -- all begin the same way: may this
caller read this version at all, and what does the version pin? That decision used to live in
`context_compiler_api._load_source`, a router module, which neither the OKF store nor a GraphQL
resolver may import as a dependency. GraphQL therefore rebuilt it from the shared checks
(`graphql_reads._compiled_read_decision`), and only the parity tests held the two copies together.

It lives here now, moved out of the router unchanged, and every caller imports it:

* `decide_compiled_read` -- the version, its tenant, an ACTIVE product, the agent's capability
  envelope, then -- for anyone outside the compiler's lifecycle readers -- PUBLISHED, a consumer
  role, purpose and quality. Every "no" is the anti-enumeration 404 the route has always raised.
* `resolve_pinned_references` -- the tables, routines, views, source freshness and ontology
  versions the version pins, where a reference that no longer resolves is a 409.
* `load_coverage_extras` -- the pinned meaning and what moved since publication.
* `_load_source` -- the route's whole load: the decision, the references, and the two sections
  only a compilation carries (negative knowledge and exemplars). The OKF store and the compile,
  download and drift routes call it.

`aida.context_compiler_api` re-imports every name, so its routes and their tests are untouched.
Refusals are the `HTTPException`s the routes have always raised; the GraphQL layer maps them to
its field codes, and `UnresolvedReferencesError` lets it tell a stale pin from a refusal.

`tests/test_r11_gql01_compile_read_service.py` fails if a second copy of the decision reappears
in either surface.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_compiler import (
    ResolvedCoverageChange,
    ResolvedExemplar,
    ResolvedMeaningCoverage,
    ResolvedNegativeAssertion,
    ResolvedOntologyMeaning,
    ResolvedRoutineReference,
    ResolvedSourceFreshness,
    ResolvedTableReference,
    ResolvedViewCoverage,
)
from aida.context_path import derive_context_path
from aida.context_product_coverage import (
    load_coverage_changes,
    load_ontology_meaning,
    load_pinned_meaning,
    load_routine_references,
    load_source_freshness,
    load_view_coverage,
    publication_time,
)
from aida.context_product_policy import (
    ContextProductQualityDecision,
    evaluate_context_product_purpose,
    evaluate_context_product_quality_from_db,
)
from aida.context_product_reads import _enforce_capability_envelope
from aida.exemplar_store import find_confirmed_agent_runs, promote_confirmed_agent_run
from aida.models import (
    ContextProduct,
    ContextProductVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)
from aida.negative_knowledge import query_negatives_for_scope
from aida.security import SecurityContext, enforce_organization

__all__ = [
    "COMPILER_LIFECYCLE_READERS",
    "COMPILER_ROLES",
    "CompiledReadDecision",
    "PinnedReferences",
    "UnresolvedReferencesError",
    "decide_compiled_read",
    "load_coverage_extras",
    "resolve_pinned_references",
]

#: The roles `GET /v1/context-product-versions/{id}/compile` (and its download and drift
#: siblings) declare. GraphQL's `contextProductCoverage` requires the same tuple.
COMPILER_ROLES: tuple[str, ...] = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataSteward",
    "AgentDeveloper",
    "Analyst",
)

#: Who the compile route lets read a version whatever its status, purpose or quality -- its
#: own set, not the version read's `CONTEXT_PRODUCT_LIFECYCLE_READERS`.
COMPILER_LIFECYCLE_READERS: frozenset[str] = frozenset(
    {"PlatformAdmin", "MetadataAdmin", "DataSteward"}
)

_VERSION_NOT_FOUND = "context product version not found"


class UnresolvedReferencesError(HTTPException):
    """A version pins something that no longer resolves: 409, as the compile route answers.

    An `HTTPException` so the routes raise it unchanged; a subclass so GraphQL can tell "the pin
    is stale" from "you may not read this" without reading prose.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(
            status_code=409, detail=f"context product contains unresolved {kind} references"
        )


@dataclass(frozen=True, slots=True)
class CompiledReadDecision:
    """What a caller who may read a version gets: the version, its product, and the quality
    verdict the decision was made under (a consumption edge records its snapshot)."""

    product: ContextProduct
    version: ContextProductVersion
    quality: ContextProductQualityDecision


@dataclass(frozen=True, slots=True)
class PinnedReferences:
    """A version's pins, resolved: every table, routine and ontology version it names, and the
    view coverage and source freshness beneath its tables."""

    tables: list[ResolvedTableReference]
    routines: list[ResolvedRoutineReference]
    views: list[ResolvedViewCoverage]
    sources: list[ResolvedSourceFreshness]
    ontology: list[ResolvedOntologyMeaning]


async def decide_compiled_read(
    session: AsyncSession, context: SecurityContext, version_id: UUID
) -> CompiledReadDecision:
    """Whether `context` may read this version the way the compile route reads it.

    In the route's order: the version, its tenant, an ACTIVE product, the agent's capability
    envelope (`_enforce_capability_envelope`, shared with every context-product door), then --
    for anyone outside `COMPILER_LIFECYCLE_READERS` -- PUBLISHED and a consumer role, purpose
    and quality. Every "no" is the same 404, so a refusal never says which check failed.

    R11-C6 finding 12: compile and its download read a product's governed content by version
    id -- the same kind of door as the version reads, which gate on the envelope -- and asked
    only roles. So an agent whose envelope omitted a product could still compile it. Every
    reader loads through here, so one check covers all of them. It sits above the role,
    purpose and quality gates so an out-of-envelope agent learns nothing from which of those it
    would have failed.
    """
    version = await session.get(ContextProductVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    enforce_organization(context, version.organization_id)
    product = await session.get(ContextProduct, version.product_id)
    if product is None or product.lifecycle_status != "ACTIVE":
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    await _enforce_capability_envelope(session, context, product, version)
    lifecycle_reader = not context.roles.isdisjoint(COMPILER_LIFECYCLE_READERS)
    if not lifecycle_reader and (
        version.status != "PUBLISHED" or context.roles.isdisjoint(version.allowed_consumer_roles)
    ):
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    purpose = evaluate_context_product_purpose(context.business_purpose, version.policy_summary)
    if not lifecycle_reader and not purpose.allowed:
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    quality = await evaluate_context_product_quality_from_db(
        session,
        organization_id=version.organization_id,
        table_id_values=version.table_ids,
        requirements=version.quality_requirements,
    )
    if not lifecycle_reader and not quality.allowed:
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    return CompiledReadDecision(product=product, version=version, quality=quality)


async def resolve_pinned_references(
    session: AsyncSession, version: ContextProductVersion
) -> PinnedReferences:
    """Resolve what a version pins, or refuse it as the compile route does.

    A table, routine or ontology version that no longer resolves is a 409
    (`UnresolvedReferencesError`), checked in that order. Called only after
    `decide_compiled_read` has admitted the caller.
    """
    table_ids = [UUID(value) for value in version.table_ids]
    rows = (
        (
            await session.execute(
                select(MetadataTable, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(
                    MetadataTable.id.in_(table_ids),
                    MetadataTable.organization_id == version.organization_id,
                )
            )
        ).all()
        if table_ids
        else []
    )
    tables = [
        ResolvedTableReference(
            table_id=str(table.id),
            qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
        )
        for table, schema, catalog in rows
    ]
    if len(tables) != len(table_ids):
        raise UnresolvedReferencesError("table")
    routine_ids = list(version.routine_ids or [])
    routines = await load_routine_references(
        session, version.organization_id, routine_ids, version.table_ids
    )
    if len(routines) != len(routine_ids):
        raise UnresolvedReferencesError("routine")
    views = await load_view_coverage(session, version.organization_id, version.table_ids)
    # R11-FP12: the time behind the digests, read under the same decision.
    sources = await load_source_freshness(session, version.organization_id, version.table_ids)
    ontology_version_ids = list(version.ontology_version_ids or [])
    ontology = await load_ontology_meaning(
        session, version.organization_id, ontology_version_ids, version.table_ids, routine_ids
    )
    if len(ontology) != len(ontology_version_ids):
        raise UnresolvedReferencesError("ontology")
    return PinnedReferences(
        tables=tables, routines=routines, views=views, sources=sources, ontology=ontology
    )


async def load_coverage_extras(
    session: AsyncSession, version: ContextProductVersion
) -> tuple[list[ResolvedMeaningCoverage], list[ResolvedCoverageChange]]:
    """R11-FP09/FP12: the pinned meaning as coverage, and what the version covers that moved
    after it was published.

    Called by the compile, download and drift routes and by GraphQL's coverage read, only
    *after* the read decision has been made, and rendered by the same `coverage_section` MCP's
    resource read uses, so the doors cannot disagree. A helper beside `_load_source` rather
    than two more members of its tuple, because the OKF store and its tests unpack that tuple
    by position.
    """
    routine_ids = list(version.routine_ids or [])
    meaning = await load_pinned_meaning(
        session,
        version.organization_id,
        ontology_version_ids=list(version.ontology_version_ids or []),
        semantic_model_version_ids=list(version.semantic_model_version_ids),
        glossary_term_version_ids=list(version.glossary_term_version_ids),
        scope_table_ids=version.table_ids,
        scope_routine_ids=routine_ids,
    )
    changes = await load_coverage_changes(
        session,
        version.organization_id,
        version.table_ids,
        routine_ids,
        since=publication_time(version),
    )
    return meaning, changes


async def _load_negative_knowledge(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: list[str],
) -> list[ResolvedNegativeAssertion]:
    """Load negative knowledge bounded to this context product version's own
    table scope (never the whole organization's negative-knowledge surface),
    pre-serialized so `compile_context_product` stays a pure function.
    """
    records = await query_negatives_for_scope(session, organization_id, table_ids)
    return [
        ResolvedNegativeAssertion(
            subject_id=record.subject_id,
            assertion_type=record.assertion_type,
            predicate=record.predicate,
            rejected_by=record.rejected_by,
            rejected_at=record.rejected_at.isoformat(),
            suppression_active=record.suppression_active,
            lift_reason=record.lift_reason,
        )
        for record in records
    ]


async def _load_exemplars(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: list[str],
    *,
    scan_limit: int = 200,
) -> list[ResolvedExemplar]:
    """Load promoted exemplars (N17) bounded to this context product version's
    own table scope, mirroring `_load_negative_knowledge`'s "resolve, then
    pre-serialize" split.

    `find_confirmed_agent_runs` has no DB column to filter by table -- unlike
    `NegativeAssertionRecord.subject_id`, an `AgentRun`'s resolved objects
    live inside its own `retrieval_evidence` JSON -- so this scans the
    organization's `scan_limit` most-recently-confirmed candidates and
    filters in Python by whether any `TABLE` object the run actually
    resolved falls inside `table_ids`, the same bound-then-filter shape
    `aida.studio_eval`'s mining passes already use for a comparable
    "no column to query on" situation. Never the whole organization's
    confirmed-run surface -- exactly the same scoping discipline
    `query_negatives_for_scope` applies to negative knowledge.
    """
    if not table_ids:
        return []
    scope = frozenset(table_ids)
    candidates = await find_confirmed_agent_runs(
        session, organization_id, scan_limit=scan_limit
    )
    resolved: list[ResolvedExemplar] = []
    for agent_run, memory in candidates:
        context_path = derive_context_path(agent_run)
        table_object_ids = {
            object_id
            for object_type, object_id in context_path.resolved_objects
            if object_type == "TABLE"
        }
        if not table_object_ids & scope:
            continue
        exemplar_case = await promote_confirmed_agent_run(session, agent_run, memory)
        resolved.append(
            ResolvedExemplar(
                case_id=exemplar_case.case_id,
                source=exemplar_case.source,
                resolved_object_types=tuple(
                    sorted(exemplar_case.expected_resolved_object_types)
                ),
                selected_tool_slug=exemplar_case.expected_selected_tool_slug,
                semantic_version_kind=exemplar_case.expected_semantic_version_kind,
                policy_status=exemplar_case.expected_policy_status,
                policy_reason_code=exemplar_case.expected_policy_reason_code,
                artifact_hash=exemplar_case.artifact_hash,
            )
        )
    return resolved


async def _load_source(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
) -> tuple[
    ContextProduct,
    ContextProductVersion,
    list[ResolvedTableReference],
    list[ResolvedNegativeAssertion],
    list[ResolvedExemplar],
    list[ResolvedRoutineReference],
    list[ResolvedViewCoverage],
    list[ResolvedOntologyMeaning],
    list[ResolvedSourceFreshness],
    dict[str, object],
]:
    """The compile route's whole load: the decision, what the version pins, and the two
    sections only a compilation carries.

    Every reference is resolved before negative knowledge and exemplars are read, so a stale pin
    is refused without paying for them; a successful load reads exactly what it always did.
    """
    decision = await decide_compiled_read(session, context, version_id)
    version = decision.version
    pinned = await resolve_pinned_references(session, version)
    negative_knowledge = await _load_negative_knowledge(
        session, version.organization_id, version.table_ids
    )
    exemplars = await _load_exemplars(session, version.organization_id, version.table_ids)
    return (
        decision.product,
        version,
        pinned.tables,
        negative_knowledge,
        exemplars,
        pinned.routines,
        pinned.views,
        pinned.ontology,
        pinned.sources,
        decision.quality.snapshot(),
    )
