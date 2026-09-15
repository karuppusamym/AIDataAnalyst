"""Governed ontology v1: typed definitions and mappings, not an OWL reasoner."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.discovery_selection import table_kind
from aida.envelope_models import MetadataRoutine
from aida.events import record_audit
from aida.governance_decision_contracts import TargetEffect
from aida.models import GovernanceReview, MetadataColumn, MetadataTable
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.schemas import ApiModel
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["ontology"])
WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataSteward")
READ_ROLES = (*WRITE_ROLES, "Reviewer")


class Concept(ApiModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    deprecated: bool = False


class OntologyRelation(ApiModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    source: str = Field(max_length=100)
    target: str = Field(max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    cardinality: Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"]
    deprecated: bool = False


class OntologyMapping(ApiModel):
    concept: str = Field(max_length=100)
    #: R11-FP09: VIEW (a view or materialized view) and ROUTINE (a stored procedure or function)
    #: join TABLE and COLUMN. The kind must match the catalog object: a view written as TABLE
    #: is refused on every write, because retrieval and context treat the two differently.
    subject_type: Literal["TABLE", "VIEW", "COLUMN", "ROUTINE"]
    subject_id: UUID


class OntologyDefinition(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=255)
    provenance: str = Field(min_length=1, max_length=4000)
    lifecycle: Literal["ACTIVE", "DEPRECATED"] = "ACTIVE"
    concepts: list[Concept] = Field(min_length=1, max_length=200)
    relations: list[OntologyRelation] = Field(default_factory=list, max_length=500)
    mappings: list[OntologyMapping] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_graph(self) -> OntologyDefinition:
        keys = {concept.key for concept in self.concepts}
        if len(keys) != len(self.concepts):
            raise ValueError("concept keys must be unique")
        aliases: set[str] = set(keys)
        for concept in self.concepts:
            for alias in concept.aliases:
                token = alias.strip().casefold()
                if not token or len(token) > 200 or token in aliases:
                    raise ValueError("aliases must be nonempty and globally unambiguous")
                aliases.add(token)
        if len({relation.key for relation in self.relations}) != len(self.relations):
            raise ValueError("relation keys must be unique")
        for relation in self.relations:
            if relation.source not in keys or relation.target not in keys:
                raise ValueError("relation endpoints must name defined concepts")
        seen: set[tuple[str, str, UUID]] = set()
        for mapping in self.mappings:
            entry = (mapping.concept, mapping.subject_type, mapping.subject_id)
            if mapping.concept not in keys or entry in seen:
                raise ValueError("mappings must name defined concepts and must be unique")
            seen.add(entry)
        return self


class OntologyCreate(ApiModel):
    ontology_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    base_version: int = Field(default=0, ge=0)
    definition: OntologyDefinition


#: R11-FP09: what a mapping's catalog target is now. Only VALID may be created, submitted or
#: approved; a version already written keeps reading whatever its targets have since become.
MappingValidity = Literal["VALID", "TARGET_MISSING", "TARGET_DEPRECATED", "KIND_MISMATCH"]
_VIEW_KINDS = frozenset({"VIEW", "MATERIALIZED_VIEW"})


class OntologyMappingValidityRead(ApiModel):
    concept: str
    subject_type: str
    subject_id: UUID
    status: MappingValidity
    #: Where the target lives; absent when there is no target in this organization.
    datasource_id: UUID | None = None
    #: For a deprecated table that rename detection pointed forward: its successor.
    superseded_by_id: UUID | None = None


class OntologyRead(ApiModel):
    id: UUID
    ontology_key: str = ""
    published_version: int = 0
    ontology_id: UUID
    version: int
    base_version: int
    status: str
    definition: OntologyDefinition
    created_by: str
    approved_by: str | None = None
    governance_review_id: UUID | None = None
    #: One entry per mapping, in definition order (R11-FP09).
    mapping_validity: list[OntologyMappingValidityRead] = Field(default_factory=list)


def _mapping_status(
    mapping: OntologyMapping,
    organization_id: UUID | None,
    tables: dict[UUID, MetadataTable],
    columns: dict[UUID, tuple[MetadataColumn, MetadataTable]],
    routines: dict[UUID, MetadataRoutine],
) -> tuple[MappingValidity, UUID | None, UUID | None]:
    if mapping.subject_type == "ROUTINE":
        routine = routines.get(mapping.subject_id)
        if routine is None or routine.organization_id != organization_id:
            return "TARGET_MISSING", None, None
        status: MappingValidity = "VALID" if routine.status == "ACTIVE" else "TARGET_DEPRECATED"
        return status, routine.datasource_id, None
    if mapping.subject_type == "COLUMN":
        pair = columns.get(mapping.subject_id)
        if pair is None or pair[1].organization_id != organization_id:
            return "TARGET_MISSING", None, None
        column, parent = pair
        active = column.status == "ACTIVE" and parent.status == "ACTIVE"
        return ("VALID" if active else "TARGET_DEPRECATED"), parent.datasource_id, None
    table = tables.get(mapping.subject_id)
    if table is None or table.organization_id != organization_id:
        return "TARGET_MISSING", None, None
    if (table_kind(table.object_type) in _VIEW_KINDS) != (mapping.subject_type == "VIEW"):
        return "KIND_MISMATCH", table.datasource_id, None
    if table.status != "ACTIVE":
        return "TARGET_DEPRECATED", table.datasource_id, table.superseded_by_table_id
    return "VALID", table.datasource_id, None


_Targets = tuple[
    dict[UUID, MetadataTable],
    dict[UUID, tuple[MetadataColumn, MetadataTable]],
    dict[UUID, MetadataRoutine],
]


async def resolve_mapping_targets(
    session: AsyncSession, definition: OntologyDefinition, organization_id: UUID | None
) -> list[OntologyMappingValidityRead]:
    """R11-FP09: every mapping's target as the catalog holds it now, in at most three reads.

    Never raises for a missing or retired target. An approved ontology outlives the catalog
    objects it was mapped to, and before this one deprecated table made the whole version
    history answer 422. Another organization's object reads as TARGET_MISSING, exactly like
    one that never existed.
    """
    return _validity(definition, organization_id, await _load_targets(session, definition))


def _validity(
    definition: OntologyDefinition, organization_id: UUID | None, targets: _Targets
) -> list[OntologyMappingValidityRead]:
    tables, columns, routines = targets
    validity: list[OntologyMappingValidityRead] = []
    for mapping in definition.mappings:
        status, datasource_id, superseded_by_id = _mapping_status(
            mapping, organization_id, tables, columns, routines
        )
        validity.append(
            OntologyMappingValidityRead(
                concept=mapping.concept,
                subject_type=mapping.subject_type,
                subject_id=mapping.subject_id,
                status=status,
                datasource_id=datasource_id,
                superseded_by_id=superseded_by_id,
            )
        )
    return validity


async def _load_targets(session: AsyncSession, definition: OntologyDefinition) -> _Targets:
    wanted: dict[str, set[UUID]] = {}
    for mapping in definition.mappings:
        wanted.setdefault(mapping.subject_type, set()).add(mapping.subject_id)
    table_ids = wanted.get("TABLE", set()) | wanted.get("VIEW", set())
    tables = (
        {
            table.id: table
            for table in await session.scalars(
                select(MetadataTable).where(MetadataTable.id.in_(table_ids))
            )
        }
        if table_ids
        else {}
    )
    columns = (
        {
            column.id: (column, parent)
            for column, parent in (
                await session.execute(
                    select(MetadataColumn, MetadataTable)
                    .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                    .where(MetadataColumn.id.in_(wanted["COLUMN"]))
                )
            ).all()
        }
        if wanted.get("COLUMN")
        else {}
    )
    routines = (
        {
            routine.id: routine
            for routine in await session.scalars(
                select(MetadataRoutine).where(MetadataRoutine.id.in_(wanted["ROUTINE"]))
            )
        }
        if wanted.get("ROUTINE")
        else {}
    )
    return tables, columns, routines


async def validate_mappings(
    session: AsyncSession,
    definition: OntologyDefinition,
    context: SecurityContext,
    settings: Settings,
) -> None:
    """Every write -- create, submit, approve -- needs every mapping VALID and readable by the
    caller. Tables, views and columns are authorized against their table, as before; a routine
    against its datasource, the grain routine metadata is read at."""
    targets = await _load_targets(session, definition)
    validity = _validity(definition, context.organization_id, targets)
    if any(entry.status == "KIND_MISMATCH" for entry in validity):
        raise HTTPException(
            status_code=422, detail="mapping subject_type does not match the catalog object's kind"
        )
    if any(entry.status != "VALID" for entry in validity):
        raise HTTPException(
            status_code=422, detail="mapping target is unavailable in this organization"
        )
    _, columns, _ = targets
    authorized: set[tuple[str, str]] = set()
    for mapping, entry in zip(definition.mappings, validity, strict=True):
        datasource_id = entry.datasource_id
        if datasource_id is None:
            continue
        if mapping.subject_type == "ROUTINE":
            resource = ("datasource", str(datasource_id))
        elif mapping.subject_type == "COLUMN":
            resource = ("table", str(columns[mapping.subject_id][1].id))
        else:
            resource = ("table", str(mapping.subject_id))
        if resource in authorized:
            continue
        authorized.add(resource)
        await gate_read(
            session,
            context,
            settings,
            action="READ_METADATA",
            resource_type=resource[0],
            resource_id=resource[1],
            datasource_id=datasource_id,
        )


async def authorized_version(
    session: AsyncSession, version_id: UUID, context: SecurityContext
) -> OntologyVersion:
    version = await session.get(OntologyVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="ontology version not found")
    enforce_organization(context, version.organization_id)
    return version


async def decide_ontology_version(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> TargetEffect:
    """Queue adapter, preserving the existing versioned publication events."""
    version = await decide_ontology(session, review, decision, context)
    event_type = (
        "ontology.version_published.v1"
        if version.status == "APPROVED"
        else "ontology.version_rejected.v1"
    )
    return TargetEffect(
        event_type,
        "ontology_version",
        str(version.id),
        {
            "ontology_version_id": str(version.id),
            "ontology_id": str(version.ontology_id),
            "version": version.version,
            "review_id": str(review.id),
        },
    )


@router.get("/organizations/{organization_id}/ontology-versions", response_model=list[OntologyRead])
async def list_ontology_versions(
    organization_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[OntologyRead]:
    enforce_organization(context, organization_id)
    rows = list(
        await session.scalars(
            select(OntologyVersion)
            .where(OntologyVersion.organization_id == organization_id)
            .order_by(OntologyVersion.created_at.desc(), OntologyVersion.id)
            .limit(limit)
            .offset(offset)
        )
    )
    # R11-FP09: report, never refuse. Writes still require every mapping VALID; a read of
    # history must survive the catalog moving on underneath an approved version.
    validity = {
        row.id: await resolve_mapping_targets(
            session, OntologyDefinition.model_validate(row.definition), organization_id
        )
        for row in rows
    }
    heads = {
        head.id: head
        for head in await session.scalars(
            select(OntologyHead).where(
                OntologyHead.id.in_([row.ontology_id for row in rows]),
                OntologyHead.organization_id == organization_id,
            )
        )
    }
    return [
        OntologyRead.model_validate(row).model_copy(
            update={
                "ontology_key": heads[row.ontology_id].ontology_key,
                "published_version": heads[row.ontology_id].published_version,
                "mapping_validity": validity[row.id],
            }
        )
        for row in rows
    ]


@router.post("/organizations/{organization_id}/ontology-versions", response_model=OntologyRead)
async def create_ontology_version(
    organization_id: UUID,
    body: OntologyCreate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OntologyRead:
    enforce_organization(context, organization_id)
    await validate_mappings(session, body.definition, context, settings)
    head = await session.scalar(
        select(OntologyHead)
        .where(
            OntologyHead.organization_id == organization_id,
            OntologyHead.ontology_key == body.ontology_key,
        )
        .with_for_update()
    )
    if head is None:
        head = OntologyHead(
            organization_id=organization_id,
            ontology_key=body.ontology_key,
            published_version=0,
            last_version=0,
        )
        session.add(head)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail="ontology was created concurrently; refresh"
            ) from exc
    if head.published_version != body.base_version:
        raise HTTPException(
            status_code=409, detail="ontology base is stale; use its current published version"
        )
    # Never silently remove published keys: retire them explicitly so old maps
    # and consumers can still resolve their meaning.
    if body.base_version:
        base = await session.scalar(
            select(OntologyVersion).where(
                OntologyVersion.ontology_id == head.id,
                OntologyVersion.version == body.base_version,
                OntologyVersion.status == "APPROVED",
            )
        )
        if base is None:
            raise HTTPException(status_code=409, detail="ontology baseline is unavailable")
        previous = OntologyDefinition.model_validate(base.definition)
        if not {c.key for c in previous.concepts} <= {c.key for c in body.definition.concepts}:
            raise HTTPException(
                status_code=422,
                detail="deprecate published concepts instead of removing their keys",
            )
        if not {r.key for r in previous.relations} <= {r.key for r in body.definition.relations}:
            raise HTTPException(
                status_code=422,
                detail="deprecate published relations instead of removing their keys",
            )
    head.last_version += 1
    version = OntologyVersion(
        organization_id=organization_id,
        ontology_id=head.id,
        version=head.last_version,
        base_version=body.base_version,
        definition=body.definition.model_dump(mode="json"),
        status="DRAFT",
        created_by=context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        context,
        action="ontology.create",
        resource_type="ontology_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    return OntologyRead.model_validate(version).model_copy(
        update={
            "ontology_key": head.ontology_key,
            "published_version": head.published_version,
        }
    )


@router.post("/ontology-versions/{version_id}/submit", response_model=OntologyRead)
async def submit_ontology_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OntologyRead:
    version = await authorized_version(session, version_id, context)
    if version.created_by != context.principal_id:
        raise HTTPException(
            status_code=403, detail="only the ontology author may submit this version"
        )
    await validate_mappings(
        session, OntologyDefinition.model_validate(version.definition), context, settings
    )
    result = await session.execute(
        update(OntologyVersion)
        .where(OntologyVersion.id == version.id, OntologyVersion.status == "DRAFT")
        .values(status="PENDING_APPROVAL")
        .returning(OntologyVersion.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="ontology version is not a draft")
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="ONTOLOGY_VERSION",
        object_id=str(version.id),
        requested_action="PUBLISH",
        status="PENDING",
        requested_by=version.created_by,
    )
    session.add(review)
    await session.flush()
    version.governance_review_id = review.id
    record_audit(
        session,
        context,
        action="ontology.submit",
        resource_type="ontology_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    return OntologyRead.model_validate(version)


async def decide_ontology(
    session: AsyncSession, review: GovernanceReview, decision: str, context: SecurityContext
) -> OntologyVersion:
    version = await authorized_version(session, UUID(review.object_id), context)
    if version.status != "PENDING_APPROVAL" or version.governance_review_id != review.id:
        raise HTTPException(status_code=409, detail="ontology is not awaiting this review")
    if version.created_by == context.principal_id:
        raise HTTPException(
            status_code=403, detail="ontology author cannot approve their own version"
        )
    if decision == "APPROVE":
        await validate_mappings(
            session, OntologyDefinition.model_validate(version.definition), context, get_settings()
        )
        result = await session.execute(
            update(OntologyHead)
            .where(
                OntologyHead.id == version.ontology_id,
                OntologyHead.organization_id == version.organization_id,
                OntologyHead.published_version == version.base_version,
            )
            .values(published_version=version.version)
            .returning(OntologyHead.id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=409,
                detail="ontology base changed; create a new draft against the published version",
            )
        version.status = "APPROVED"
        version.approved_by = context.principal_id
    else:
        version.status = "REJECTED"
    return version
