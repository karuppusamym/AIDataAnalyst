"""GL-9: deterministic, evidence-scored table description drafting.

Atlan's "Context Agents" auto-draft descriptions and auto-*apply* high
confidence output with no human review. This module rejects the no-review
auto-apply: it drafts a description from real catalog evidence already
queryable in this database, scores that evidence, and hands the result to
the existing maker-checker `governance_review` queue. The score sets review
priority (list ordering, and a minimum-evidence gate on submission); it never
skips or substitutes for independent review. See
`decide_governance_review`/`apply_asset_description_draft` for the only
code path that publishes a draft, and `Docs/20-modules/08-glossary-and-
stewardship.md` GL-9 for the module contract.

No external model call is made anywhere in this module: every signal comes
from rows already in this database, and the composition and scoring below
are pure, deterministic functions of that evidence.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.models import (
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    DbtResource,
    GlossaryTermVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    OpenLineageTableEdge,
    ProcedureLineageEdge,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge

# Below this overall score a draft carries too little evidence to be worth an
# independent reviewer's time. A draft below this line stays in DRAFT and can
# never be submitted for review — the "very low-evidence assets should not
# even reach PENDING_APPROVAL" exit condition from the module doc.
MINIMUM_EVIDENCE_FOR_REVIEW = 0.4

_LINEAGE_QUERY_LIMIT = 50
_LINEAGE_PROSE_LIMIT = 3
_TERM_QUERY_LIMIT = 10


@dataclass(frozen=True, slots=True)
class AssetEvidence:
    """Value-free, DB-derived signals about one table. No source row values."""

    table_id: UUID
    table_name: str
    schema_name: str
    column_count: int
    primary_key_columns: tuple[str, ...]
    foreign_key_count: int
    upstream_table_names: tuple[str, ...]
    upstream_edge_ids: tuple[UUID, ...]
    downstream_table_names: tuple[str, ...]
    downstream_edge_ids: tuple[UUID, ...]
    dbt_description: str | None
    dbt_documented_column_count: int
    business_name: str | None
    business_description: str | None
    business_annotation_id: UUID | None
    grain_statement: str | None
    bound_term_names: tuple[str, ...]
    bound_term_ids: tuple[UUID, ...]
    #: ADR-0026's parsed lineage -- a view's definition, pasted procedure SQL, a
    #: captured routine -- as (edge type, edge id), the identifiers the per-edge
    #: review queue uses. One edge per neighbouring table; the table's name is
    #: in `upstream_table_names`/`downstream_table_names` beside OpenLineage's.
    upstream_parsed_edges: tuple[tuple[str, UUID], ...] = ()
    downstream_parsed_edges: tuple[tuple[str, UUID], ...] = ()

    @property
    def lineage_edge_count(self) -> int:
        return (
            len(self.upstream_edge_ids)
            + len(self.downstream_edge_ids)
            + len(self.upstream_parsed_edges)
            + len(self.downstream_parsed_edges)
        )


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """A composite score with an explainable per-dimension breakdown.

    Every sub-score is a deterministic function of `AssetEvidence` — more
    corroborating evidence always scores at or above less evidence for the
    same dimension. There is no learned weight and no external call.
    """

    accuracy: float
    clarity: float
    style: float
    completeness: float
    overall: float

    def as_dict(self) -> dict[str, float]:
        return {
            "accuracy": self.accuracy,
            "clarity": self.clarity,
            "style": self.style,
            "completeness": self.completeness,
            "overall": self.overall,
        }


def score_evidence(evidence: AssetEvidence) -> ConfidenceBreakdown:
    """Score drafted evidence on four dimensions, deterministically.

    - accuracy: how much of the draft rests on authoritative sources (an
      approved business annotation, a dbt-authored description, bound
      glossary terms) rather than schema facts alone.
    - clarity: how much human-authored, readable context (as opposed to a
      bare column listing) the draft can include.
    - style: how well-formed the draft can be — a named subject and enough
      columns to describe in a structured way.
    - completeness: the fraction of the evidence categories this codebase
      tracks for a table that are actually present.
    """
    categories = (
        evidence.column_count > 0,
        bool(evidence.primary_key_columns) or evidence.foreign_key_count > 0,
        evidence.lineage_edge_count > 0,
        bool(evidence.dbt_description),
        bool(evidence.business_description),
        bool(evidence.bound_term_names),
    )
    completeness = sum(1 for present in categories if present) / len(categories)

    accuracy = 0.4
    if evidence.dbt_description:
        accuracy += 0.25
    if evidence.business_description:
        accuracy += 0.25
    if evidence.bound_term_names:
        accuracy += 0.10
    accuracy = min(accuracy, 1.0)

    clarity = 0.25
    if evidence.business_description:
        clarity += 0.35
    if evidence.grain_statement:
        clarity += 0.20
    if evidence.dbt_description:
        clarity += 0.20
    clarity = min(clarity, 1.0)

    style = 0.30
    if evidence.business_name:
        style += 0.25
    if evidence.column_count >= 3:
        style += 0.20
    if evidence.bound_term_names:
        style += 0.25
    style = min(style, 1.0)

    overall = round((accuracy + clarity + style + completeness) / 4, 4)
    return ConfidenceBreakdown(
        accuracy=round(accuracy, 4),
        clarity=round(clarity, 4),
        style=round(style, 4),
        completeness=round(completeness, 4),
        overall=overall,
    )


def compose_draft_text(evidence: AssetEvidence) -> str:
    """Assemble readable prose entirely from evidence fields. No model call."""
    column_word = "column" if evidence.column_count == 1 else "columns"
    sentences = [
        f"{evidence.table_name} is a table in the {evidence.schema_name} schema with "
        f"{evidence.column_count} {column_word}."
    ]
    if evidence.primary_key_columns:
        sentences.append("It is keyed by " + ", ".join(evidence.primary_key_columns) + ".")
    if evidence.foreign_key_count:
        reference_word = "reference" if evidence.foreign_key_count == 1 else "references"
        sentences.append(
            f"It carries {evidence.foreign_key_count} foreign-key {reference_word} to other "
            "catalog tables."
        )
    if evidence.upstream_table_names:
        sentences.append(
            "Lineage shows it is populated from "
            + ", ".join(evidence.upstream_table_names[:_LINEAGE_PROSE_LIMIT])
            + "."
        )
    if evidence.downstream_table_names:
        sentences.append(
            "Downstream, it feeds "
            + ", ".join(evidence.downstream_table_names[:_LINEAGE_PROSE_LIMIT])
            + "."
        )
    if evidence.dbt_description:
        sentences.append(f"Its dbt definition describes it as: {evidence.dbt_description.strip()}")
    if evidence.business_description:
        sentences.append(f"Approved business context: {evidence.business_description.strip()}")
    if evidence.grain_statement:
        sentences.append(f"Grain: {evidence.grain_statement.strip()}")
    if evidence.bound_term_names:
        term_word = "term" if len(evidence.bound_term_names) == 1 else "terms"
        sentences.append(
            f"It is linked to the glossary {term_word} "
            + ", ".join(evidence.bound_term_names)
            + "."
        )
    return " ".join(sentences)


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_payload(evidence: AssetEvidence) -> dict[str, Any]:
    """JSON-safe evidence record: the raw signals a draft was built from."""
    return {
        "column_count": evidence.column_count,
        "primary_key_columns": list(evidence.primary_key_columns),
        "foreign_key_count": evidence.foreign_key_count,
        "upstream_edge_ids": [str(value) for value in evidence.upstream_edge_ids],
        "downstream_edge_ids": [str(value) for value in evidence.downstream_edge_ids],
        "upstream_parsed_edges": [
            {"edge_type": edge_type, "edge_id": str(edge_id)}
            for edge_type, edge_id in evidence.upstream_parsed_edges
        ],
        "downstream_parsed_edges": [
            {"edge_type": edge_type, "edge_id": str(edge_id)}
            for edge_type, edge_id in evidence.downstream_parsed_edges
        ],
        "lineage_edge_count": evidence.lineage_edge_count,
        "dbt_description_present": bool(evidence.dbt_description),
        "dbt_documented_column_count": evidence.dbt_documented_column_count,
        "business_annotation_id": (
            str(evidence.business_annotation_id) if evidence.business_annotation_id else None
        ),
        "bound_term_ids": [str(value) for value in evidence.bound_term_ids],
    }


#: ADR-0026's parsed-edge tables whose rows name two catalog tables.
_PARSED_EDGE_MODELS: tuple[tuple[str, Any], ...] = (
    ("VIEW", ViewLineageEdge),
    ("PROCEDURE", ProcedureLineageEdge),
    ("ROUTINE", DeepProcedureLineageEdge),
)

#: A neighbouring table's name, the edge type, and one edge that names it.
_ParsedNeighbour = tuple[str, str, UUID]


async def _parsed_lineage_neighbours(
    session: AsyncSession, table: MetadataTable
) -> tuple[list[_ParsedNeighbour], list[_ParsedNeighbour]]:
    """The tables `table` is parsed as populated from, and those it feeds.

    Only ACTIVE edges -- approved by a person, or activated by a person's own
    parse -- between two catalog tables of this datasource. A PROPOSED edge,
    such as every edge the lineage agent writes until someone approves it, is
    not evidence yet, and a REJECTED one is evidence of nothing. A parser writes
    one row per column pair, so one edge per neighbouring table is kept.
    """
    upstream: dict[str, tuple[str, UUID]] = {}
    downstream: dict[str, tuple[str, UUID]] = {}
    for edge_type, model in _PARSED_EDGE_MODELS:
        filters: list[Any] = [
            model.review_status == "ACTIVE",
            MetadataTable.datasource_id == table.datasource_id,
        ]
        if model is DeepProcedureLineageEdge:
            # A hop into a temp table is the procedure's own plumbing.
            filters.append(model.is_intermediate.is_(False))
        for this_side, other_side, found in (
            (model.target_table_id, model.source_table_id, upstream),
            (model.source_table_id, model.target_table_id, downstream),
        ):
            ranked = (
                select(
                    model.id.label("edge_id"),
                    MetadataTable.name.label("name"),
                    func.row_number()
                    .over(partition_by=other_side, order_by=model.id)
                    .label("neighbour_rank"),
                )
                .join(MetadataTable, MetadataTable.id == other_side)
                .where(this_side == table.id, other_side != table.id, *filters)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(ranked.c.edge_id, ranked.c.name)
                    .where(ranked.c.neighbour_rank == 1)
                    .order_by(ranked.c.name)
                    .limit(_LINEAGE_QUERY_LIMIT)
                )
            ).all()
            for edge_id, name in rows:
                found.setdefault(name, (edge_type, edge_id))
    return (
        [(name, edge_type, edge_id) for name, (edge_type, edge_id) in upstream.items()],
        [(name, edge_type, edge_id) for name, (edge_type, edge_id) in downstream.items()],
    )


def _with_parsed_names(names: list[str], parsed: list[_ParsedNeighbour]) -> tuple[str, ...]:
    """OpenLineage's names first, then each parsed neighbour not already named."""
    merged = list(names)
    for name, _edge_type, _edge_id in parsed:
        if name not in merged:
            merged.append(name)
    return tuple(merged)


async def gather_evidence(session: AsyncSession, table: MetadataTable) -> AssetEvidence:
    """Collect value-free evidence for `table` from data already in this DB."""
    schema = await session.get(MetadataSchema, table.schema_id)
    schema_name = schema.name if schema is not None else "unknown"

    column_count = await session.scalar(
        select(func.count())
        .select_from(MetadataColumn)
        .where(MetadataColumn.table_id == table.id, MetadataColumn.status == "ACTIVE")
    )
    column_count = column_count or 0

    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id == table.id,
                MetadataConstraint.status == "ACTIVE",
            )
        )
    ).all()
    primary_key_columns: tuple[str, ...] = ()
    for constraint in constraints:
        if constraint.constraint_type == "PRIMARY_KEY" and constraint.columns:
            primary_key_columns = tuple(constraint.columns)
            break
    foreign_key_count = sum(
        1 for constraint in constraints if constraint.constraint_type == "FOREIGN_KEY"
    )

    # Only same-source lineage a reviewer has not rejected is evidence. A draft
    # is text anyone who can read this table will read, and naming a table in
    # another datasource would disclose it past the per-read cross-source grant
    # check (ADR-0017) -- the line `column_description_service` already draws.
    # An edge a reviewer rejected (ADR-0026) is evidence of nothing.
    lineage_filters = (
        OpenLineageTableEdge.review_status == "ACTIVE",
        MetadataTable.datasource_id == table.datasource_id,
    )
    upstream_rows = (
        await session.execute(
            select(OpenLineageTableEdge.id, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == OpenLineageTableEdge.input_table_id)
            .where(OpenLineageTableEdge.output_table_id == table.id, *lineage_filters)
            .limit(_LINEAGE_QUERY_LIMIT)
        )
    ).all()
    downstream_rows = (
        await session.execute(
            select(OpenLineageTableEdge.id, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == OpenLineageTableEdge.output_table_id)
            .where(OpenLineageTableEdge.input_table_id == table.id, *lineage_filters)
            .limit(_LINEAGE_QUERY_LIMIT)
        )
    ).all()
    # ADR-0026's parsed lineage, on the same terms -- see the helper.
    upstream_parsed, downstream_parsed = await _parsed_lineage_neighbours(session, table)

    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`,
    # not on `MetadataBusinessAnnotation` itself -- see `business_annotation_versions.py`.
    annotation_alias, annotation_ranked = current_version_alias()
    annotation = await session.scalar(
        select(annotation_alias)
        .join(
            MetadataBusinessAnnotation,
            MetadataBusinessAnnotation.id == annotation_alias.annotation_id,
        )
        .where(MetadataBusinessAnnotation.table_id == table.id, annotation_ranked.c.rn == 1)
    )

    term_rows = (
        await session.execute(
            select(GlossaryTermVersion.display_name, AssetTermLink.term_id)
            .join(AssetTermLink, AssetTermLink.term_id == GlossaryTermVersion.term_id)
            .where(
                AssetTermLink.table_id == table.id,
                GlossaryTermVersion.status == "APPROVED",
            )
            .limit(_TERM_QUERY_LIMIT)
        )
    ).all()

    dbt_description, dbt_documented_column_count = await _latest_dbt_evidence(session, table.id)

    return AssetEvidence(
        table_id=table.id,
        table_name=table.name,
        schema_name=schema_name,
        column_count=column_count,
        primary_key_columns=primary_key_columns,
        foreign_key_count=foreign_key_count,
        upstream_table_names=_with_parsed_names(
            [name for _, name in upstream_rows], upstream_parsed
        ),
        upstream_edge_ids=tuple(edge_id for edge_id, _ in upstream_rows),
        downstream_table_names=_with_parsed_names(
            [name for _, name in downstream_rows], downstream_parsed
        ),
        downstream_edge_ids=tuple(edge_id for edge_id, _ in downstream_rows),
        dbt_description=dbt_description,
        dbt_documented_column_count=dbt_documented_column_count,
        business_name=annotation.business_name if annotation else None,
        business_description=annotation.business_description if annotation else None,
        business_annotation_id=annotation.annotation_id if annotation else None,
        grain_statement=annotation.grain_statement if annotation else None,
        bound_term_names=tuple(name for name, _ in term_rows),
        bound_term_ids=tuple(term_id for _, term_id in term_rows),
        upstream_parsed_edges=tuple(
            (edge_type, edge_id) for _, edge_type, edge_id in upstream_parsed
        ),
        downstream_parsed_edges=tuple(
            (edge_type, edge_id) for _, edge_type, edge_id in downstream_parsed
        ),
    )


async def _latest_dbt_evidence(session: AsyncSession, table_id: UUID) -> tuple[str | None, int]:
    resource = await session.scalar(
        select(DbtResource)
        .where(DbtResource.matched_table_id == table_id)
        .order_by(DbtResource.created_at.desc())
        .limit(1)
    )
    if resource is None:
        return None, 0
    return resource.description, len(resource.column_descriptions)


def ensure_reviewable(overall_score: float) -> None:
    """Gate submission to review on a minimum evidence bar.

    This is the only place a draft's route to `governance_review` can be
    blocked, and it is enforced purely on the deterministic score computed
    above — never bypassed by role, urgency, or any other signal.
    """
    if overall_score < MINIMUM_EVIDENCE_FOR_REVIEW:
        raise HTTPException(
            status_code=422,
            detail="draft carries too little evidence for independent review",
        )


async def publish_asset_documentation_version(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_id: UUID,
    readme: str,
    created_by: str,
    approved_by: str,
    approved_at: datetime,
) -> AssetDocumentationVersion:
    """Publish `readme` as the table's new current `AssetDocumentationVersion`.

    Extracted from `apply_asset_description_draft` (below, its original and
    still its main caller) once a second approval route needed the same
    publish: an approved table-subject `DocumentClaim`, whose content is a
    steward-reviewed description from an uploaded data dictionary rather than
    a GL-9 evidence draft. Both are authored table descriptions reaching the
    same store through the same maker-checker guard, so they must not grow
    two different append-and-supersede implementations that can drift.

    Append-only: the prior `APPROVED` row moves to `SUPERSEDED` in this same
    transaction and is never edited for content.
    """
    documentation = await session.scalar(
        select(AssetDocumentation).where(AssetDocumentation.table_id == table_id)
    )
    if documentation is None:
        documentation = AssetDocumentation(
            organization_id=organization_id,
            table_id=table_id,
        )
        session.add(documentation)
        await session.flush()
    latest_version = await session.scalar(
        select(func.max(AssetDocumentationVersion.version)).where(
            AssetDocumentationVersion.documentation_id == documentation.id
        )
    )
    await session.execute(
        update(AssetDocumentationVersion)
        .where(
            AssetDocumentationVersion.documentation_id == documentation.id,
            AssetDocumentationVersion.status == "APPROVED",
        )
        .values(status="SUPERSEDED", updated_at=approved_at)
    )
    version = AssetDocumentationVersion(
        organization_id=organization_id,
        documentation_id=documentation.id,
        version=(latest_version or 0) + 1,
        status="APPROVED",
        readme=readme,
        created_by=created_by,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    session.add(version)
    await session.flush()
    return version


async def apply_asset_description_draft(
    session: AsyncSession,
    draft: AssetDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, AssetDocumentationVersion]:
    """Publish an approved draft onto the table's asset documentation.

    Called exclusively from `semantic_api.decide_governance_review` after its
    shared maker-checker guard (status must be PENDING, requester independent
    of the approver) has already passed. There is no other call site that
    can move a draft to APPROVED.
    """
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    version = await publish_asset_documentation_version(
        session,
        organization_id=draft.organization_id,
        table_id=draft.table_id,
        readme=draft.drafted_text,
        created_by=draft.created_by,
        approved_by=reviewer,
        approved_at=now,
    )
    draft.status = "APPROVED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    draft.published_version_id = version.id
    return "asset_description.approved.v1", version


async def reject_asset_description_draft(
    draft: AssetDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a draft. The draft is retained (not deleted) as negative
    knowledge — see `08-glossary-and-stewardship.md` section 6 — so an
    identical low-value draft is not regenerated on the next run.
    """
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    draft.status = "REJECTED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    return "asset_description.rejected.v1"
