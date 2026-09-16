"""R11-FP12: what a context product's routines and views stand on, resolved for its readers.

A product scoped by `table_ids` alone can say "these tables", but not "this procedure builds that
one", nor "this view's definition was withheld from us". The resolvers here feed both doors a
version is read through -- compilation (`context_compiler_api._load_source`) and MCP's resource
read -- so the two cannot describe the same version differently:

* `load_routine_references` resolves the routines a version names in `routine_ids`: identity,
  status, whether its body would be released on request, the state of its lineage, and the
  tables it reads and writes;
* `load_ontology_meaning` (R11-FP09) reads the meaning of the ontology versions a version
  is pinned to, from those versions and never from the ontology's head;
* `load_source_freshness` (R11-FP12) says when each source behind those tables was last
  read, and last read in full, so a digest can be read as fresh or stale rather than
  only as equal or different;
* `load_view_coverage` derives, from the version's own `table_ids`, the views and materialized
  views among them and the same facts about their definitions.

**Nothing a product does not cover leaks through.** Read and write table ids are cut to the
product's own `table_ids`, and nothing is counted: a routine that also reads a table outside the
product looks exactly like one that does not. **Value-free.** Flags, lineage state, ids already in
scope, and a digest of the *stored* value-free text -- never a body, a definition, or a digest of
the literal-bearing original (`definition_fingerprint`/`body_fingerprint` are that, and stay
internal). Callers run these only after the product's own read decision, and they use only
`session.execute(...).all()`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_compiler import (
    ResolvedOntologyMeaning,
    ResolvedRoutineReference,
    ResolvedSourceFreshness,
    ResolvedViewCoverage,
)
from aida.discovery_selection import table_kind
from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.ingest_screening import is_eligible_for_model_context, screen_text
from aida.models import (
    AnalysisRun,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ViewLineageEdge,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

#: Some ACTIVE edge was parsed from the definition (UNPARSED markers do not count).
LINEAGE_ACTIVE: Final = "ACTIVE"
#: Only edges nobody has decided yet: they steer nothing, and are reported so.
LINEAGE_PROPOSED: Final = "PROPOSED"
LINEAGE_NONE: Final = "NONE"
_UNPARSED: Final = "UNPARSED"
#: R11-FP12: a discovery run that finished, and the mode that also retires what it did
#: not see (`aida.workflows.activities`' single deprecate-missing pass).
_RUN_COMPLETED: Final = "COMPLETED"
_RUN_FULL: Final = "FULL"


def _uuids(values: Sequence[Any]) -> list[UUID]:
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(str(value)))
        except ValueError:
            continue
    return parsed


def _releasable(availability: str, redaction_status: str, screening_status: str) -> bool:
    """The gate MCP `get_transformation_detail` applies before releasing code text."""
    return (
        availability == AVAILABLE
        and redaction_status in VALUE_FREE_REDACTION_STATUSES
        and is_eligible_for_model_context(screening_status)
    )


def _digest(stored_text: str | None, redaction_status: str) -> str | None:
    if stored_text is None or redaction_status not in VALUE_FREE_REDACTION_STATUSES:
        return None
    return hashlib.sha256(stored_text.encode("utf-8")).hexdigest()


def _lineage(object_id: UUID, active: set[UUID], proposed: set[UUID]) -> str:
    if object_id in active:
        return LINEAGE_ACTIVE
    if object_id in proposed:
        return LINEAGE_PROPOSED
    return LINEAGE_NONE


async def load_routine_references(
    session: AsyncSession,
    organization_id: UUID,
    routine_ids: Sequence[Any],
    scope_table_ids: Sequence[Any],
) -> list[ResolvedRoutineReference]:
    """The routines named, in this organization, whatever their status -- the caller compares
    the count with what it asked for and refuses a version that names one no longer there."""
    ids = _uuids(routine_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataRoutine.id.in_(ids),
                MetadataRoutine.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    scope = {str(value) for value in scope_table_ids}
    edge_rows = (
        await session.execute(
            select(
                DeepProcedureLineageEdge.routine_id,
                DeepProcedureLineageEdge.source_table_id,
                DeepProcedureLineageEdge.target_table_id,
                DeepProcedureLineageEdge.is_write,
                DeepProcedureLineageEdge.is_intermediate,
                DeepProcedureLineageEdge.transformation_type,
                DeepProcedureLineageEdge.review_status,
            ).where(
                DeepProcedureLineageEdge.routine_id.in_([routine.id for routine, _, _ in rows]),
                DeepProcedureLineageEdge.organization_id == organization_id,
                DeepProcedureLineageEdge.review_status.in_((LINEAGE_ACTIVE, LINEAGE_PROPOSED)),
            )
        )
    ).all()
    active: set[UUID] = set()
    proposed: set[UUID] = set()
    unparsed: set[UUID] = set()
    reads: dict[UUID, set[str]] = {}
    writes: dict[UUID, set[str]] = {}
    for (
        routine_id,
        source_table_id,
        target_table_id,
        is_write,
        is_intermediate,
        transformation_type,
        review_status,
    ) in edge_rows:
        if review_status == LINEAGE_PROPOSED:
            proposed.add(routine_id)
            continue
        if transformation_type == _UNPARSED:
            unparsed.add(routine_id)
            continue
        active.add(routine_id)
        if is_intermediate:
            continue
        if source_table_id is not None and str(source_table_id) in scope:
            reads.setdefault(routine_id, set()).add(str(source_table_id))
        if is_write and target_table_id is not None and str(target_table_id) in scope:
            writes.setdefault(routine_id, set()).add(str(target_table_id))
    return [
        ResolvedRoutineReference(
            routine_id=str(routine.id),
            qualified_name=f"{catalog_name}.{schema_name}.{routine.name}",
            routine_type=routine.routine_type,
            signature=routine.signature,
            status=routine.status,
            definition_available=_releasable(
                routine.availability, routine.redaction_status, routine.screening_status
            ),
            lineage=_lineage(routine.id, active, proposed),
            fully_parsed=routine.id in active and routine.id not in unparsed,
            reads_table_ids=tuple(sorted(reads.get(routine.id, set()))),
            writes_table_ids=tuple(sorted(writes.get(routine.id, set()))),
            definition_digest=_digest(routine.body_sql_redacted, routine.redaction_status),
        )
        for routine, schema_name, catalog_name in rows
    ]


async def load_view_coverage(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: Sequence[Any],
) -> list[ResolvedViewCoverage]:
    """The views and materialized views among a version's own tables; plain tables yield
    nothing, so a product holding none compiles as it always did."""
    ids = _uuids(table_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(
                MetadataTable.id,
                MetadataTable.object_type,
                MetadataTable.status,
                MetadataViewDefinition,
            )
            .outerjoin(MetadataViewDefinition, MetadataViewDefinition.table_id == MetadataTable.id)
            .where(
                MetadataTable.id.in_(ids),
                MetadataTable.organization_id == organization_id,
            )
        )
    ).all()
    views = [
        (table_id, table_kind(object_type), status, definition)
        for table_id, object_type, status, definition in rows
        if table_kind(object_type) != "TABLE"
    ]
    if not views:
        return []
    edge_rows = (
        await session.execute(
            select(ViewLineageEdge.target_table_id, ViewLineageEdge.review_status).where(
                ViewLineageEdge.target_table_id.in_([table_id for table_id, _, _, _ in views]),
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.review_status.in_((LINEAGE_ACTIVE, LINEAGE_PROPOSED)),
            )
        )
    ).all()
    active = {target for target, review in edge_rows if review == LINEAGE_ACTIVE}
    proposed = {target for target, review in edge_rows if review == LINEAGE_PROPOSED}
    return [
        ResolvedViewCoverage(
            table_id=str(table_id),
            object_type=kind,
            status=status,
            definition_available=(
                definition is not None
                and definition.status == "ACTIVE"
                and _releasable(
                    definition.availability,
                    definition.redaction_status,
                    definition.screening_status,
                )
            ),
            truncated=bool(definition is not None and definition.truncated),
            lineage=_lineage(table_id, active, proposed),
            definition_digest=(
                _digest(definition.definition_sql_redacted, definition.redaction_status)
                if definition is not None
                else None
            ),
        )
        for table_id, kind, status, definition in views
    ]


def _moment(value: Any) -> str | None:
    """A run's completion time as ISO-8601. PostgreSQL hands `func.max` back as a `datetime`
    and SQLite as text; a caller of this module reads the same string from either."""
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


async def _last_completed_scan(
    session: AsyncSession,
    organization_id: UUID,
    datasource_ids: list[UUID],
    *,
    mode: str | None = None,
) -> dict[UUID, str]:
    """When each of these sources last finished a discovery run, by that run row's own clock."""
    filters = [
        AnalysisRun.organization_id == organization_id,
        AnalysisRun.datasource_id.in_(datasource_ids),
        AnalysisRun.status == _RUN_COMPLETED,
    ]
    if mode is not None:
        filters.append(AnalysisRun.mode == mode)
    rows = (
        await session.execute(
            select(AnalysisRun.datasource_id, func.max(AnalysisRun.updated_at))
            .where(*filters)
            .group_by(AnalysisRun.datasource_id)
        )
    ).all()
    latest: dict[UUID, str] = {}
    for datasource_id, moment in rows:
        text = _moment(moment)
        if text is not None:
            latest[datasource_id] = text
    return latest


async def load_source_freshness(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: Sequence[Any],
) -> list[ResolvedSourceFreshness]:
    """R11-FP12: when each source behind the product's own tables was last read.

    A product reported digests, which say whether something changed, and never a time, which
    says how old the answer is. This is that time, per datasource -- the unit a discovery run
    actually covers -- with the product's own table ids grouped under the source they live in,
    so a consumer can see which part of the product a stale source is about. Every mode of run
    reads the whole catalog, but only a FULL run retires what it did not see, so both times are
    reported rather than one. Value-free: ids already in the product's scope, and two clocks.
    """
    ids = _uuids(table_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(MetadataTable.datasource_id, MetadataTable.id).where(
                MetadataTable.id.in_(ids),
                MetadataTable.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    by_source: dict[UUID, set[str]] = {}
    for datasource_id, table_id in rows:
        by_source.setdefault(datasource_id, set()).add(str(table_id))
    datasource_ids = list(by_source)
    completed = await _last_completed_scan(session, organization_id, datasource_ids)
    full = await _last_completed_scan(session, organization_id, datasource_ids, mode=_RUN_FULL)
    return [
        ResolvedSourceFreshness(
            datasource_id=str(datasource_id),
            table_ids=tuple(sorted(scoped)),
            last_scan_completed_at=completed.get(datasource_id),
            last_full_scan_completed_at=full.get(datasource_id),
        )
        for datasource_id, scoped in sorted(by_source.items(), key=lambda item: str(item[0]))
    ]


def _screened(value: Any, origin: str, withheld: list[dict[str, Any]]) -> str | None:
    """Free text an ontology author typed, released only when egress screening allows it."""
    if value is None:
        return None
    text = str(value)
    verdict = screen_text(text, content_origin=origin)
    if is_eligible_for_model_context(verdict.status):
        return text
    withheld.append(
        {
            "field": origin.split(":", 2)[-1],
            "status": verdict.status,
            "reason_codes": list(verdict.reason_codes),
        }
    )
    return None


async def load_ontology_meaning(
    session: AsyncSession,
    organization_id: UUID,
    ontology_version_ids: Sequence[Any],
    scope_table_ids: Sequence[Any],
    scope_routine_ids: Sequence[Any],
) -> list[ResolvedOntologyMeaning]:
    """R11-FP09: the meaning of the approved ontology versions named, in this organization.

    Read from each pinned version, never from its ontology's head. The caller compares the count
    with what it asked for and refuses a version that names one no longer approved here. A mapping
    stands only on an object the product covers -- a table or view in `scope_table_ids`, a column
    of one, a routine in `scope_routine_ids` -- so the meaning says nothing about anything else.
    """
    ids = _uuids(ontology_version_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(OntologyVersion, OntologyHead.ontology_key)
            .join(OntologyHead, OntologyHead.id == OntologyVersion.ontology_id)
            .where(
                OntologyVersion.id.in_(ids),
                OntologyVersion.organization_id == organization_id,
                OntologyVersion.status == "APPROVED",
            )
        )
    ).all()
    if not rows:
        return []
    tables = {str(value) for value in scope_table_ids}
    routines = {str(value) for value in scope_routine_ids}
    column_ids = {
        str(mapping.get("subject_id"))
        for version, _ in rows
        for mapping in (version.definition or {}).get("mappings") or []
        if isinstance(mapping, dict) and mapping.get("subject_type") == "COLUMN"
    }
    column_tables: dict[str, str] = {}
    if column_ids:
        column_rows = (
            await session.execute(
                select(MetadataColumn.id, MetadataColumn.table_id).where(
                    MetadataColumn.id.in_(_uuids(sorted(column_ids))),
                    MetadataColumn.organization_id == organization_id,
                )
            )
        ).all()
        column_tables = {str(column_id): str(table_id) for column_id, table_id in column_rows}

    def in_scope(subject_type: str, subject_id: str) -> bool:
        if subject_type in ("TABLE", "VIEW"):
            return subject_id in tables
        if subject_type == "COLUMN":
            return column_tables.get(subject_id) in tables
        return subject_type == "ROUTINE" and subject_id in routines

    meanings: list[ResolvedOntologyMeaning] = []
    for version, ontology_key in rows:
        definition: dict[str, Any] = version.definition or {}
        origin = f"ontology_version:{version.id}"
        withheld: list[dict[str, Any]] = []
        mappings = [
            mapping for mapping in definition.get("mappings") or [] if isinstance(mapping, dict)
        ]
        concepts: list[dict[str, Any]] = []
        for concept in definition.get("concepts") or []:
            if not isinstance(concept, dict) or concept.get("deprecated"):
                continue
            key = str(concept.get("key"))
            aliases = [
                str(alias)
                for alias in concept.get("aliases") or []
                if _screened(alias, f"{origin}:concept:{key}:alias", withheld) is not None
            ]
            concepts.append(
                {
                    "key": key,
                    "name": _screened(
                        concept.get("name"), f"{origin}:concept:{key}:name", withheld
                    ),
                    "description": _screened(
                        concept.get("description"), f"{origin}:concept:{key}:description", withheld
                    ),
                    "aliases": sorted(aliases),
                    "mappings": sorted(
                        (
                            {
                                "subject_type": str(mapping.get("subject_type")),
                                "subject_id": str(mapping.get("subject_id")),
                            }
                            for mapping in mappings
                            if mapping.get("concept") == key
                            and in_scope(
                                str(mapping.get("subject_type")), str(mapping.get("subject_id"))
                            )
                        ),
                        key=lambda item: (item["subject_type"], item["subject_id"]),
                    ),
                }
            )
        delivered = {concept["key"] for concept in concepts}
        relations: list[dict[str, Any]] = []
        for relation in definition.get("relations") or []:
            if not isinstance(relation, dict) or relation.get("deprecated"):
                continue
            if relation.get("source") not in delivered or relation.get("target") not in delivered:
                continue
            relation_key = str(relation.get("key"))
            relations.append(
                {
                    "key": relation_key,
                    "source": str(relation.get("source")),
                    "target": str(relation.get("target")),
                    "cardinality": relation.get("cardinality"),
                    "description": _screened(
                        relation.get("description"),
                        f"{origin}:relation:{relation_key}:description",
                        withheld,
                    ),
                }
            )
        meanings.append(
            ResolvedOntologyMeaning(
                version_id=str(version.id),
                ontology_key=ontology_key,
                version=version.version,
                name=_screened(definition.get("name"), f"{origin}:name", withheld),
                lifecycle=str(definition.get("lifecycle") or "ACTIVE"),
                concepts=tuple(sorted(concepts, key=lambda item: str(item["key"]))),
                relations=tuple(sorted(relations, key=lambda item: str(item["key"]))),
                withheld=tuple(withheld),
            )
        )
    return meanings
