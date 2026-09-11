"""Deterministic, evidence-scored column description drafting.

The column-level sibling of `aida.asset_description_service` (GL-9), bound by
the same contract -- restated here because it is the point of the module:

* **No model call.** Every sentence is composed from rows already in this
  database: the column's catalog facts, its primary-key and foreign-key
  memberships, dbt's column documentation, the source system's comment, and
  approved relationship candidates. Nothing is read into the column's *name*.
  `amt_ccy` with no documentation produces a thin draft that scores below the
  review bar -- not a confident sentence saying it holds an ISO 4217 currency
  code. A guess presented as fact is the error a tired reviewer is least likely
  to catch, and it is the one this module refuses to make.
* **The score orders review; it never replaces it.** Publishing happens only
  through an independent APPROVE on the draft's `GovernanceReview`
  (`semantic_api._decide_column_description_draft`), and a draft below
  `MINIMUM_EVIDENCE_FOR_REVIEW` -- the same threshold table drafts use -- cannot
  be submitted at all.
* **A draft cannot overwrite what it did not see.** `base_description_version`
  records the column's description version when the draft was composed, and
  approval refuses if it has moved. That is the workbook import's `*_version`
  rule, applied to the second path that writes column descriptions.

Scoring mirrors `asset_description_service.score_evidence`: four dimensions,
each a monotone function of the evidence, and `overall` is their mean. In
practice a column reaches the bar with any authored text (a dbt description or
a source comment), or with structure corroborated from two independent places
(a declared key or foreign key *and* an approved relationship to a different
target). A type and a name alone never do.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.asset_description_service import ConfidenceBreakdown
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.models import (
    ColumnDescriptionDraft,
    ColumnDocumentationVersion,
    DbtResource,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    RelationshipCandidate,
)

#: `GovernanceReview.object_type` for a submitted column draft.
COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE = "COLUMN_DESCRIPTION_DRAFT"

#: Statuses in which a draft is still a live proposal for its column. At most
#: one draft per column may be in either (`uq_column_description_draft_open`).
OPEN_DRAFT_STATUSES = ("DRAFT", "PENDING_APPROVAL")

#: Where a draft's text came from, recorded as `evidence["origin"]`. An edit
#: appends `_WITH_HUMAN_EDITS` and keeps the first half: a model's guess a steward
#: reworded is still a model's guess, and `reviewer_agent` abstains on it.
ORIGIN_METADATA = "METADATA"
ORIGIN_MODEL_INFERRED = "MODEL_INFERRED"

#: Ceiling on drafts one generation request may create. Refused, not sliced:
#: see `column_description_api.generate_column_description_drafts`.
GENERATE_COLUMN_LIMIT = 2_000

#: How many related tables a single sentence names before it says "and N more".
_PROSE_LIST_LIMIT = 3


@dataclass(frozen=True, slots=True)
class ColumnEvidence:
    """Value-free, database-derived signals about one column. No row values."""

    column_id: UUID
    table_id: UUID
    column_name: str
    table_name: str
    schema_name: str
    physical_type: str
    nullable: bool
    classification: str
    source_description: str | None
    dbt_description: str | None
    #: Width of the table's primary key when this column is part of it, else 0.
    primary_key_width: int
    #: Declared foreign-key targets, as `table.column`.
    references: tuple[str, ...]
    #: Approved same-source relationship candidates to targets the declared
    #: foreign keys do not already name.
    related_to: tuple[str, ...]
    relationship_candidate_ids: tuple[UUID, ...]
    #: Same-source tables whose declared foreign keys reference this column.
    referenced_by: tuple[str, ...]
    current_description_version: int | None

    @property
    def has_declared_structure(self) -> bool:
        return self.primary_key_width > 0 or bool(self.references) or bool(self.referenced_by)


async def _table_names(
    session: AsyncSession, table_ids: set[UUID], *, datasource_id: UUID
) -> dict[UUID, str]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name).where(
                MetadataTable.id.in_(table_ids),
                MetadataTable.datasource_id == datasource_id,
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def gather_table_column_evidence(
    session: AsyncSession,
    table: MetadataTable,
    columns: list[MetadataColumn],
    descriptions: dict[UUID, ColumnDocumentationVersion],
) -> dict[UUID, ColumnEvidence]:
    """Evidence for `columns` of one table, in a fixed number of queries.

    Six reads whatever the column count: the schema, the table's constraints,
    the names of the tables those point at, the foreign keys pointing *at* this
    table, the latest matched dbt resource, and approved relationship
    candidates. A per-column loop would turn one generation request into
    thousands of statements.

    Every related table is filtered to this table's own datasource. A draft is
    text that anyone who can read this column will read, and naming a table in
    another source would disclose it past the per-read cross-source grant check
    (ADR-0017) -- the same line the workbook export draws for relationships.
    """
    schema = await session.get(MetadataSchema, table.schema_id)
    schema_name = schema.name if schema is not None else "unknown"

    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id == table.id,
                MetadataConstraint.status == "ACTIVE",
            )
        )
    ).all()
    primary_key: tuple[str, ...] = ()
    for constraint in constraints:
        if constraint.constraint_type == "PRIMARY_KEY" and constraint.columns:
            primary_key = tuple(constraint.columns)
            break
    primary_key_names = {name.lower() for name in primary_key}

    foreign_keys = [
        constraint
        for constraint in constraints
        if constraint.constraint_type == "FOREIGN_KEY" and constraint.referenced_table_id
    ]
    target_names = await _table_names(
        session,
        {
            constraint.referenced_table_id
            for constraint in foreign_keys
            if constraint.referenced_table_id
        },
        datasource_id=table.datasource_id,
    )
    references: dict[str, list[str]] = defaultdict(list)
    for constraint in foreign_keys:
        target = (
            target_names.get(constraint.referenced_table_id)
            if constraint.referenced_table_id
            else None
        )
        if target is None:
            continue
        referenced_columns = constraint.referenced_columns or []
        for index, source_column in enumerate(constraint.columns or []):
            target_column = referenced_columns[index] if index < len(referenced_columns) else None
            references[source_column.lower()].append(
                f"{target}.{target_column}" if target_column else target
            )

    inbound = (
        await session.execute(
            select(MetadataConstraint, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == MetadataConstraint.table_id)
            .where(
                MetadataConstraint.referenced_table_id == table.id,
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.status == "ACTIVE",
                MetadataTable.status == "ACTIVE",
                MetadataTable.datasource_id == table.datasource_id,
            )
        )
    ).all()
    referenced_by: dict[str, list[str]] = defaultdict(list)
    for constraint, source_table_name in inbound:
        for target_column in constraint.referenced_columns or []:
            referenced_by[target_column.lower()].append(source_table_name)

    dbt = await session.scalar(
        select(DbtResource)
        .where(DbtResource.matched_table_id == table.id)
        .order_by(DbtResource.created_at.desc())
        .limit(1)
    )
    dbt_columns = {
        str(name).lower(): str(text).strip()
        for name, text in ((dbt.column_descriptions or {}) if dbt is not None else {}).items()
        if text and str(text).strip()
    }

    target_table = aliased(MetadataTable)
    target_column_row = aliased(MetadataColumn)
    related_rows = (
        await session.execute(
            select(
                RelationshipCandidate.id,
                RelationshipCandidate.source_column_id,
                target_table.name,
                target_column_row.name,
            )
            .join(target_table, target_table.id == RelationshipCandidate.target_table_id)
            .join(target_column_row, target_column_row.id == RelationshipCandidate.target_column_id)
            .where(
                RelationshipCandidate.source_column_id.in_([column.id for column in columns]),
                RelationshipCandidate.status == "APPROVED",
                RelationshipCandidate.target_datasource_id == table.datasource_id,
            )
        )
    ).all()
    related: dict[UUID, list[tuple[UUID, str]]] = defaultdict(list)
    for candidate_id, source_column_id, target_name, target_column_name in related_rows:
        related[source_column_id].append((candidate_id, f"{target_name}.{target_column_name}"))

    evidence: dict[UUID, ColumnEvidence] = {}
    for column in columns:
        key = column.name.lower()
        declared = tuple(dict.fromkeys(references.get(key, [])))
        # An approved candidate that restates a declared foreign key is the same
        # fact twice; counting it would let one piece of structure look like two.
        extra = [(cid, name) for cid, name in related.get(column.id, []) if name not in declared]
        documented = descriptions.get(column.id)
        evidence[column.id] = ColumnEvidence(
            column_id=column.id,
            table_id=table.id,
            column_name=column.name,
            table_name=table.name,
            schema_name=schema_name,
            physical_type=column.physical_type,
            nullable=column.nullable,
            classification=column.classification,
            source_description=(column.source_description or "").strip() or None,
            dbt_description=dbt_columns.get(key) or None,
            primary_key_width=len(primary_key) if key in primary_key_names else 0,
            references=declared,
            related_to=tuple(dict.fromkeys(name for _, name in extra)),
            relationship_candidate_ids=tuple(cid for cid, _ in extra),
            referenced_by=tuple(dict.fromkeys(referenced_by.get(key, []))),
            current_description_version=documented.version if documented else None,
        )
    return evidence


def _as_sentence(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if collapsed.endswith((".", "!", "?")) else f"{collapsed}."


def _listed(names: tuple[str, ...]) -> str:
    shown = list(names[:_PROSE_LIST_LIMIT])
    rest = len(names) - len(shown)
    if rest > 0:
        return ", ".join(shown) + f" and {rest} more"
    if len(shown) <= 2:
        return " and ".join(shown)
    return ", ".join(shown[:-1]) + f" and {shown[-1]}"


def _normalized(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def compose_column_draft_text(evidence: ColumnEvidence) -> str:
    """Readable prose, assembled entirely from evidence fields.

    Laid out the way `asset_description_service.compose_draft_text` lays out a
    table: a sentence of catalog fact first, then structure, then whatever
    authored text exists, each attributed to where it came from. Attribution is
    what lets a reviewer check a sentence against its source rather than take
    it on trust.
    """
    nullability = "nullable" if evidence.nullable else "not null"
    sentences = [
        f"{evidence.column_name} is a column of {evidence.schema_name}.{evidence.table_name} "
        f"({evidence.physical_type}, {nullability})."
    ]
    if evidence.primary_key_width == 1:
        sentences.append("It is the table's primary key.")
    elif evidence.primary_key_width > 1:
        sentences.append(
            f"It is part of the table's {evidence.primary_key_width}-column primary key."
        )
    if evidence.references:
        sentences.append(f"It references {_listed(evidence.references)}.")
    if evidence.related_to:
        sentences.append(f"An approved relationship links it to {_listed(evidence.related_to)}.")
    if evidence.referenced_by:
        noun = "table" if len(evidence.referenced_by) == 1 else "tables"
        sentences.append(
            f"Foreign keys on the {noun} {_listed(evidence.referenced_by)} reference it."
        )
    if evidence.dbt_description:
        sentences.append(
            "Its dbt definition describes it as: " + _as_sentence(evidence.dbt_description)
        )
    if evidence.source_description and (
        evidence.dbt_description is None
        or _normalized(evidence.source_description) != _normalized(evidence.dbt_description)
    ):
        sentences.append(
            "The source system's comment on it reads: " + _as_sentence(evidence.source_description)
        )
    return " ".join(sentences)


def score_column_evidence(evidence: ColumnEvidence) -> ConfidenceBreakdown:
    """Score a column's evidence on the four dimensions tables use.

    - accuracy: how much of the draft rests on authored sources (dbt, the
      source system's comment) and declared structure, rather than a type alone.
    - clarity: how much human-written, readable text the draft can carry.
    - style: whether the draft can say more than its first, catalog-fact
      sentence -- structure, authored text, or both.
    - completeness: the fraction of the evidence categories tracked for a
      column that are actually present.

    Each dimension only rises as evidence is added, and `overall` is their mean
    -- the same shape as `asset_description_service.score_evidence`, so the one
    `MINIMUM_EVIDENCE_FOR_REVIEW` threshold means the same thing for both.
    """
    has_dbt = bool(evidence.dbt_description)
    has_comment = bool(evidence.source_description)
    has_structure = evidence.has_declared_structure or bool(evidence.related_to)

    categories = (
        has_dbt,
        has_comment,
        evidence.has_declared_structure,
        bool(evidence.related_to),
        evidence.classification != "UNCLASSIFIED",
    )
    completeness = sum(1 for present in categories if present) / len(categories)

    accuracy = 0.2
    if has_dbt:
        accuracy += 0.4
    if has_comment:
        accuracy += 0.25
    if has_structure:
        accuracy += 0.15
    accuracy = min(accuracy, 1.0)

    clarity = 0.1
    if has_dbt:
        clarity += 0.5
    if has_comment:
        clarity += 0.4
    clarity = min(clarity, 1.0)

    style = 0.3
    if has_structure:
        style += 0.3
    if has_dbt or has_comment:
        style += 0.4
    style = min(style, 1.0)

    overall = round((accuracy + clarity + style + completeness) / 4, 4)
    return ConfidenceBreakdown(
        accuracy=round(accuracy, 4),
        clarity=round(clarity, 4),
        style=round(style, 4),
        completeness=round(completeness, 4),
        overall=overall,
    )


def column_evidence_payload(evidence: ColumnEvidence) -> dict[str, Any]:
    """JSON-safe record of the signals a draft was built from.

    `column` leads, so the review queue -- which renders these keys as
    `"key: value"` claims -- can name the subject without another read.
    """
    return {
        "column": f"{evidence.schema_name}.{evidence.table_name}.{evidence.column_name}",
        "origin": ORIGIN_METADATA,
        "physical_type": evidence.physical_type,
        "nullable": evidence.nullable,
        "classification": evidence.classification,
        "primary_key_width": evidence.primary_key_width,
        "references": list(evidence.references),
        "related_to": list(evidence.related_to),
        "relationship_candidate_ids": [str(value) for value in evidence.relationship_candidate_ids],
        "referenced_by": list(evidence.referenced_by),
        "dbt_description_present": bool(evidence.dbt_description),
        "source_description_present": bool(evidence.source_description),
        "base_description_version": evidence.current_description_version,
    }


def _version_label(version: int | None) -> str:
    return "no description" if version is None else f"v{version}"


async def apply_column_description_draft(
    session: AsyncSession,
    draft: ColumnDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, ColumnDocumentationVersion]:
    """Publish an approved draft as the column's new current description.

    Called only from `semantic_api._decide_column_description_draft`, after
    the shared maker-checker guard has passed. Two refusals of its own, both
    409 so the review stays PENDING and the reviewer can reject it instead:

    * the column must still be ACTIVE -- a draft about a dropped column has
      nothing left to describe;
    * the column's description must still be the version the draft was
      composed against. Otherwise someone published, retired or republished in
      the meantime, and approving would silently replace text this draft never
      saw. Retirement counts: it is a decision, not an absence.
    """
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    column = await session.get(MetadataColumn, draft.column_id)
    if column is None or column.status != "ACTIVE":
        raise HTTPException(
            status_code=409, detail="the column this draft describes is no longer active"
        )
    current = (await current_descriptions_by_column_id(session, [draft.column_id])).get(
        draft.column_id
    )
    current_version = current.version if current else None
    if current_version != draft.base_description_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "this column's description changed after the draft was composed "
                f"({_version_label(draft.base_description_version)} -> "
                f"{_version_label(current_version)}); reject this draft and generate a new one"
            ),
        )
    version = await publish_column_description(
        session,
        organization_id=draft.organization_id,
        table_id=draft.table_id,
        column_id=draft.column_id,
        description=draft.drafted_text,
        created_by=draft.created_by,
        approved_by=reviewer,
        approved_at=now,
    )
    draft.status = "APPROVED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    draft.published_version_id = version.id
    return "column_description.approved.v1", version


async def reject_column_description_draft(
    draft: ColumnDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a draft. Retained, not deleted, as negative knowledge: the next
    generation for this column skips text identical to a rejected draft."""
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    draft.status = "REJECTED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    return "column_description.rejected.v1"


async def supersede_open_column_drafts(
    session: AsyncSession,
    column_ids: list[UUID],
    *,
    reason: str,
    now: datetime,
) -> int:
    """Close DRAFT-status drafts for columns that just received a description.

    Only DRAFT: a draft already in review keeps its review, and approving it is
    then refused on the version check -- closing it here would decide a review
    nobody decided. Returns how many drafts were closed.
    """
    if not column_ids:
        return 0
    drafts = (
        await session.scalars(
            select(ColumnDescriptionDraft).where(
                ColumnDescriptionDraft.column_id.in_(column_ids),
                ColumnDescriptionDraft.status == "DRAFT",
            )
        )
    ).all()
    for draft in drafts:
        draft.status = "SUPERSEDED"
        draft.evidence = {
            **(draft.evidence or {}),
            "superseded_reason": reason,
            "superseded_at": now.isoformat(),
        }
    return len(drafts)
