"""R11-FP12: what a context product's routines and views stand on, resolved for its readers.

A product scoped by `table_ids` alone can say "these tables", but not "this procedure builds that
one", nor "this view's definition was withheld from us". The resolvers here feed both doors a
version is read through -- compilation (`context_product_read_service._load_source`) and MCP's
resource read -- so the two cannot describe the same version differently:

* `load_routine_references` resolves the routines a version names in `routine_ids`: identity,
  status, whether its body would be released on request, the state of its lineage, and the
  tables it reads and writes;
* `load_ontology_meaning` (R11-FP09) reads the meaning of the ontology versions a version
  is pinned to, from those versions and never from the ontology's head;
* `load_source_freshness` (R11-FP12) says when each source behind those tables was last
  read, and last read in full, so a digest can be read as fresh or stale rather than
  only as equal or different;
* `load_view_coverage` derives, from the version's own `table_ids`, the views and materialized
  views among them and the same facts about their definitions;
* `load_pinned_meaning` (R11-FP09/FP12, 2026-09-18) resolves the ontology, semantic model and
  glossary term versions the version pins: whether each still stands, and what each speaks
  about within the product's scope;
* `load_coverage_changes` (R11-FP12/FP15/FP16, 2026-09-18) says what the version covers that
  moved after it was published -- a covered view's or routine's definition, or the approved
  description of a covered table, view, column or routine. `context_rebuild` asks the same
  function which published products to re-draft, so the product a reader is told is stale and
  the product put back into review are one computation.

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

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.change_signal_meaning import RETIRED_STATUSES
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import (
    CHANGE_MEANING_REPLACED,
    CHANGE_MEANING_WITHDRAWN,
    CHANGE_STRUCTURAL,
    SIGNAL_DEFINITION_CHANGED,
    SIGNAL_DEPRECATED,
    SIGNAL_MEANING_RETIRED,
    SIGNAL_REACTIVATED,
)
from aida.context_compiler import (
    ResolvedCoverageChange,
    ResolvedMeaningCoverage,
    ResolvedOntologyMeaning,
    ResolvedRoutineReference,
    ResolvedSourceFreshness,
    ResolvedViewCoverage,
)
from aida.discovery_selection import table_kind
from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataViewDefinition,
    RoutineDescriptionDraft,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.ingest_screening import is_eligible_for_model_context, screen_text
from aida.models import (
    AnalysisRun,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    SemanticMetricVersion,
    SemanticModelVersion,
    ViewLineageEdge,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_description_service import current_routine_descriptions
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


#: R11-FP08: the standing of a routine's Atlas-authored description, as a context product
#: reports it. `PROPOSED` and `WITHDRAWN` carry no text here on purpose (see
#: `load_routine_references`); they exist so a consumer can tell an undescribed routine from
#: one whose description a reviewer retired, which a bare null cannot.
_DESCRIPTION_APPROVED: Final = "APPROVED"
_DESCRIPTION_PROPOSED: Final = "PROPOSED"
_DESCRIPTION_WITHDRAWN: Final = "WITHDRAWN"
_DESCRIPTION_NONE: Final = "NONE"


async def _routine_description_states(
    session: AsyncSession,
    organization_id: UUID,
    routine_ids: Sequence[UUID],
    *,
    approved: set[UUID],
) -> dict[UUID, str]:
    """The description standing per routine, in two batched reads for the whole page.

    Precedence matches `routine_description_service.resolve_routine_description`: approved
    wins, then a draft awaiting review, then a retirement. A routine with none of those is
    absent from the result and reads as `NONE`.
    """
    states: dict[UUID, str] = {routine_id: _DESCRIPTION_APPROVED for routine_id in approved}
    remaining = [routine_id for routine_id in routine_ids if routine_id not in approved]
    if not remaining:
        return states
    pending = set(
        await session.scalars(
            select(RoutineDescriptionDraft.routine_id).where(
                RoutineDescriptionDraft.organization_id == organization_id,
                RoutineDescriptionDraft.routine_id.in_(remaining),
                RoutineDescriptionDraft.status == "PENDING_APPROVAL",
            )
        )
    )
    retired = set(
        await session.scalars(
            select(RoutineDocumentation.routine_id)
            .join(
                RoutineDocumentationVersion,
                RoutineDocumentationVersion.documentation_id == RoutineDocumentation.id,
            )
            .where(
                RoutineDocumentation.routine_id.in_(remaining),
                RoutineDocumentationVersion.status == "WITHDRAWN",
            )
        )
    )
    for routine_id in remaining:
        if routine_id in pending:
            states[routine_id] = _DESCRIPTION_PROPOSED
        elif routine_id in retired:
            states[routine_id] = _DESCRIPTION_WITHDRAWN
    return states


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
    # R11-FP08: the approved description of each routine, plus whether one was drafted or
    # retired. Two batched reads for the whole page, not one per routine, and deliberately
    # *only* the approved text: a draft is a proposal nobody has accepted and a source comment
    # is the source speaking, so neither is what a context product asserts. Both are still
    # *reported*, through `description_state`, because a consumer acts differently on
    # "undescribed" and "described, then retired".
    approved_descriptions = await current_routine_descriptions(
        session, [routine.id for routine, _, _ in rows]
    )
    described_states = await _routine_description_states(
        session,
        organization_id,
        [routine.id for routine, _, _ in rows],
        approved=set(approved_descriptions),
    )
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
            description=(
                approved_descriptions[routine.id].description
                if routine.id in approved_descriptions
                else None
            ),
            description_state=described_states.get(routine.id, _DESCRIPTION_NONE),
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


# --------------------------------------------------------------------------
# R11-FP09/FP12: the meaning a version pins, as coverage
# --------------------------------------------------------------------------

MEANING_ONTOLOGY: Final = "ONTOLOGY"
MEANING_SEMANTIC_MODEL: Final = "SEMANTIC_MODEL"
MEANING_GLOSSARY_TERM: Final = "GLOSSARY_TERM"


async def _column_tables(
    session: AsyncSession, organization_id: UUID, column_ids: Sequence[Any]
) -> dict[str, str]:
    ids = _uuids(column_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataColumn.id, MetadataColumn.table_id).where(
                MetadataColumn.id.in_(ids), MetadataColumn.organization_id == organization_id
            )
        )
    ).all()
    return {str(column_id): str(table_id) for column_id, table_id in rows}


async def _ontology_coverage(
    session: AsyncSession,
    organization_id: UUID,
    version_ids: list[UUID],
    tables: set[str],
    routines: set[str],
) -> list[ResolvedMeaningCoverage]:
    rows = (
        await session.execute(
            select(
                OntologyVersion.id,
                OntologyVersion.version,
                OntologyVersion.status,
                OntologyVersion.definition,
                OntologyHead.ontology_key,
                OntologyHead.published_version,
            )
            .join(OntologyHead, OntologyHead.id == OntologyVersion.ontology_id)
            .where(
                OntologyVersion.id.in_(version_ids),
                OntologyVersion.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    live_mappings: dict[UUID, list[dict[str, Any]]] = {}
    for version_id, _, _, definition, _, _ in rows:
        body: dict[str, Any] = definition or {}
        # A deprecated concept is left out of the ontology section, so what it maps is too.
        retired = {
            str(concept.get("key"))
            for concept in body.get("concepts") or []
            if isinstance(concept, dict) and concept.get("deprecated")
        }
        live_mappings[version_id] = [
            mapping
            for mapping in body.get("mappings") or []
            if isinstance(mapping, dict) and str(mapping.get("concept")) not in retired
        ]
    column_tables = await _column_tables(
        session,
        organization_id,
        sorted(
            {
                str(mapping.get("subject_id"))
                for mappings in live_mappings.values()
                for mapping in mappings
                if mapping.get("subject_type") == "COLUMN"
            }
        ),
    )
    resolved: list[ResolvedMeaningCoverage] = []
    for version_id, number, status, _, ontology_key, published_version in rows:
        spoken_tables: set[str] = set()
        spoken_routines: set[str] = set()
        for mapping in live_mappings[version_id]:
            subject_type, subject_id = str(mapping.get("subject_type")), str(
                mapping.get("subject_id")
            )
            if subject_type in ("TABLE", "VIEW") and subject_id in tables:
                spoken_tables.add(subject_id)
            elif subject_type == "COLUMN" and column_tables.get(subject_id) in tables:
                spoken_tables.add(column_tables[subject_id])
            elif subject_type == "ROUTINE" and subject_id in routines:
                spoken_routines.add(subject_id)
        resolved.append(
            ResolvedMeaningCoverage(
                kind=MEANING_ONTOLOGY,
                version_id=str(version_id),
                key=ontology_key,
                version=number,
                status=status,
                current=status == "APPROVED" and published_version == number,
                table_ids=tuple(sorted(spoken_tables)),
                routine_ids=tuple(sorted(spoken_routines)),
            )
        )
    return resolved


async def _semantic_model_coverage(
    session: AsyncSession, organization_id: UUID, version_ids: list[UUID], tables: list[UUID]
) -> list[ResolvedMeaningCoverage]:
    rows = (
        await session.execute(
            select(
                SemanticModelVersion.id, SemanticModelVersion.version, SemanticModelVersion.status
            ).where(
                SemanticModelVersion.id.in_(version_ids),
                SemanticModelVersion.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    # The metrics' source tables, asked for only within the product's scope: a metric over a
    # table the product does not cover is never read, so it cannot be reported or counted.
    metric_rows = (
        (
            await session.execute(
                select(
                    SemanticMetricVersion.semantic_model_version_id,
                    SemanticMetricVersion.source_table_id,
                ).where(
                    SemanticMetricVersion.semantic_model_version_id.in_(version_ids),
                    SemanticMetricVersion.organization_id == organization_id,
                    SemanticMetricVersion.source_table_id.in_(tables),
                )
            )
        ).all()
        if tables
        else []
    )
    spoken: dict[UUID, set[str]] = {}
    for model_id, table_id in metric_rows:
        spoken.setdefault(model_id, set()).add(str(table_id))
    return [
        ResolvedMeaningCoverage(
            kind=MEANING_SEMANTIC_MODEL,
            version_id=str(model_id),
            key=None,
            version=number,
            status=status,
            current=status == "PUBLISHED",
            table_ids=tuple(sorted(spoken.get(model_id, set()))),
        )
        for model_id, number, status in rows
    ]


async def _glossary_term_coverage(
    session: AsyncSession, organization_id: UUID, version_ids: list[UUID], tables: list[UUID]
) -> list[ResolvedMeaningCoverage]:
    rows = (
        await session.execute(
            select(
                GlossaryTermVersion.id,
                GlossaryTermVersion.term_id,
                GlossaryTermVersion.version,
                GlossaryTermVersion.status,
                GlossaryTerm.term_key,
                GlossaryTerm.lifecycle_status,
            )
            .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermVersion.term_id)
            .where(
                GlossaryTermVersion.id.in_(version_ids),
                GlossaryTermVersion.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    link_rows = (
        (
            await session.execute(
                select(AssetTermLink.term_id, AssetTermLink.table_id).where(
                    AssetTermLink.term_id.in_([term_id for _, term_id, _, _, _, _ in rows]),
                    AssetTermLink.organization_id == organization_id,
                    AssetTermLink.table_id.in_(tables),
                )
            )
        ).all()
        if tables
        else []
    )
    linked: dict[UUID, set[str]] = {}
    for term_id, table_id in link_rows:
        linked.setdefault(term_id, set()).add(str(table_id))
    return [
        ResolvedMeaningCoverage(
            kind=MEANING_GLOSSARY_TERM,
            version_id=str(version_id),
            key=term_key,
            version=number,
            status=status,
            # A term retired as a whole leaves its last approved definition APPROVED; the term's
            # own lifecycle is what says nobody should be reading it any more.
            current=status == "APPROVED" and lifecycle == "ACTIVE",
            table_ids=tuple(sorted(linked.get(term_id, set()))),
        )
        for version_id, term_id, number, status, term_key, lifecycle in rows
    ]


async def load_pinned_meaning(
    session: AsyncSession,
    organization_id: UUID,
    *,
    ontology_version_ids: Sequence[Any],
    semantic_model_version_ids: Sequence[Any],
    glossary_term_version_ids: Sequence[Any],
    scope_table_ids: Sequence[Any],
    scope_routine_ids: Sequence[Any],
) -> list[ResolvedMeaningCoverage]:
    """R11-FP09/FP12: the meaning versions a version pins, as coverage entries.

    Every door a version is read through renders these with the compiler's `coverage_section`,
    so a pinned glossary term reads the same in a compiled artifact, a download, a drift check
    and MCP's resource read. A pin that does not resolve in this organization resolves to
    nothing (INV-5); the compile door already refuses a version whose ontology pin does not
    resolve, and the ids themselves are in `references` regardless.
    """
    tables = _uuids(scope_table_ids)
    table_scope = {str(value) for value in tables}
    routine_scope = {str(value) for value in _uuids(scope_routine_ids)}
    resolved: list[ResolvedMeaningCoverage] = []
    ontology_ids = _uuids(ontology_version_ids)
    if ontology_ids:
        resolved.extend(
            await _ontology_coverage(
                session, organization_id, ontology_ids, table_scope, routine_scope
            )
        )
    model_ids = _uuids(semantic_model_version_ids)
    if model_ids:
        resolved.extend(await _semantic_model_coverage(session, organization_id, model_ids, tables))
    term_ids = _uuids(glossary_term_version_ids)
    if term_ids:
        resolved.extend(await _glossary_term_coverage(session, organization_id, term_ids, tables))
    return resolved


# --------------------------------------------------------------------------
# R11-FP12/FP15/FP16: what moved under a published version
# --------------------------------------------------------------------------

#: The definition signals that mean a covered view's or routine's definition moved.
_DEFINITION_MOVES: Final = (SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED, SIGNAL_REACTIVATED)


def publication_time(version: Any) -> datetime | None:
    """When a context product version became what consumers were given, or `None` if never.

    `published_at` is stamped by the approval that published it; `approved_at` is the same
    moment on a version recorded before `published_at` existed. A draft, or a rejected version,
    has neither and so has no baseline to be stale against.
    """
    moment = getattr(version, "published_at", None) or getattr(version, "approved_at", None)
    return moment if isinstance(moment, datetime) else None


async def _definition_changes(
    session: AsyncSession,
    organization_id: UUID,
    tables: list[UUID],
    routines: list[UUID],
    since: datetime,
) -> list[ResolvedCoverageChange]:
    """Covered views' and routines' definition moves after `since`, from FP15's signals.

    Signals rather than a stored digest, because they are the one record of a definition moving
    that every source path writes, in the scan's own transaction, and a view has no definition
    history to read a digest-at-publication from. A definition that moved and moved back reads
    as moved: a reviewer re-confirms, which is the conservative answer.
    """
    covered: list[ColumnElement[bool]] = []
    if tables:
        covered.append(
            and_(
                MetadataChangeSignal.subject_kind == "VIEW",
                MetadataChangeSignal.subject_id.in_(tables),
            )
        )
    if routines:
        covered.append(
            and_(
                MetadataChangeSignal.subject_kind == "ROUTINE",
                MetadataChangeSignal.subject_id.in_(routines),
            )
        )
    if not covered:
        return []
    rows = (
        await session.execute(
            select(
                MetadataChangeSignal.subject_kind,
                MetadataChangeSignal.subject_id,
                MetadataChangeSignal.signal_type,
                MetadataChangeSignal.change_class,
            )
            .where(
                MetadataChangeSignal.organization_id == organization_id,
                MetadataChangeSignal.signal_type.in_(_DEFINITION_MOVES),
                MetadataChangeSignal.detected_at > since,
                or_(*covered),
            )
            .order_by(MetadataChangeSignal.detected_at, MetadataChangeSignal.id)
        )
    ).all()
    classes: dict[tuple[str, str, str], str | None] = {}
    for kind, subject_id, signal_type, change_class in rows:
        key = (kind, str(subject_id), signal_type)
        # Newest class wins, except that a structural move is never hidden behind a later
        # literal-only one: the digest has moved, and the class must agree with it.
        if classes.get(key) != CHANGE_STRUCTURAL:
            classes[key] = change_class
    return [
        ResolvedCoverageChange(kind, subject_id, signal_type, change_class)
        for (kind, subject_id, signal_type), change_class in classes.items()
    ]


def _retired_since(
    version: Any,
    documentation: Any,
    text: str,
    subject: Any,
    in_scope: ColumnElement[bool],
    organization_id: UUID,
    since: datetime,
) -> Select[Any]:
    """The approved description in force at `since` that no approved text now repeats.

    Net, not event-by-event: a version approved at or before `since` and retired after it is the
    one the product was published over, and it is a change only if no APPROVED version of the
    same object now says the same thing. So text replaced and then restored reads as unchanged,
    a re-approval of identical text never counts, and a draft never does -- only a version a
    reader was given can be retired. `replaced` says whether other approved text stands now.
    """
    current = aliased(version)
    same_text = exists().where(
        current.documentation_id == version.documentation_id,
        current.status == "APPROVED",
        getattr(current, text) == getattr(version, text),
    )
    other = aliased(version)
    replaced = exists().where(
        other.documentation_id == version.documentation_id,
        other.status == "APPROVED",
    )
    return (
        select(subject, replaced.label("replaced"))
        .select_from(version)
        .join(documentation, documentation.id == version.documentation_id)
        .where(
            version.organization_id == organization_id,
            in_scope,
            version.status.in_(RETIRED_STATUSES),
            or_(version.approved_at.is_(None), version.approved_at <= since),
            version.updated_at > since,
            ~same_text,
        )
    )


async def _description_changes(
    session: AsyncSession,
    organization_id: UUID,
    tables: list[UUID],
    routines: list[UUID],
    since: datetime,
) -> list[ResolvedCoverageChange]:
    """Covered objects whose approved description moved after `since`, read from the stores.

    Read from the append-only version rows rather than from FP15's meaning signals, which say
    the same thing but only once a sweep has run: a reader must learn a product is stale whether
    or not an operator has turned the maintenance passes on.
    """
    found: dict[tuple[str, str], bool] = {}

    async def collect(statement: Select[Any], kind_of: Any) -> None:
        for row in (await session.execute(statement)).all():
            # Columns: the described object, whether other approved text stands, then whatever
            # the caller added to tell its kind.
            subject_id, replaced = row[0], row[1]
            kind = kind_of(row)
            key = (kind, str(subject_id))
            found[key] = found.get(key, False) or bool(replaced)

    if tables:
        table_statement = _retired_since(
            AssetDocumentationVersion,
            AssetDocumentation,
            "readme",
            AssetDocumentation.table_id,
            AssetDocumentation.table_id.in_(tables),
            organization_id,
            since,
        ).join(MetadataTable, MetadataTable.id == AssetDocumentation.table_id)
        await collect(
            table_statement.add_columns(MetadataTable.object_type),
            lambda row: "TABLE" if table_kind(row[2]) == "TABLE" else "VIEW",
        )
        await collect(
            _retired_since(
                ColumnDocumentationVersion,
                ColumnDocumentation,
                "description",
                ColumnDocumentation.column_id,
                ColumnDocumentation.table_id.in_(tables),
                organization_id,
                since,
            ),
            lambda row: "COLUMN",
        )
    if routines:
        await collect(
            _retired_since(
                RoutineDocumentationVersion,
                RoutineDocumentation,
                "description",
                RoutineDocumentation.routine_id,
                RoutineDocumentation.routine_id.in_(routines),
                organization_id,
                since,
            ),
            lambda row: "ROUTINE",
        )
    return [
        ResolvedCoverageChange(
            kind,
            subject_id,
            SIGNAL_MEANING_RETIRED,
            CHANGE_MEANING_REPLACED if replaced else CHANGE_MEANING_WITHDRAWN,
        )
        for (kind, subject_id), replaced in found.items()
    ]


async def load_coverage_changes(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: Sequence[Any],
    routine_ids: Sequence[Any],
    *,
    since: datetime | None,
) -> list[ResolvedCoverageChange]:
    """R11-FP12/FP15/FP16: what a version covers that moved after `since` -- normally its
    `publication_time`; `None` (never published) has no baseline and yields nothing.

    Scope is the version's own `table_ids` (their views, and the columns of all of them) and
    `routine_ids`, and every query is bounded by it and by the organization (INV-5): nothing
    outside the product is read, so nothing outside it can be reported or counted. Value-free:
    kinds, ids already in scope, and codes. Uses only `session.execute(...).all()`, like every
    resolver here.
    """
    if since is None:
        return []
    tables = _uuids(table_ids)
    routines = _uuids(routine_ids)
    changes = [
        *await _definition_changes(session, organization_id, tables, routines, since),
        *await _description_changes(session, organization_id, tables, routines, since),
    ]
    return sorted(changes, key=lambda item: (item.subject_kind, item.subject_id, item.change))
