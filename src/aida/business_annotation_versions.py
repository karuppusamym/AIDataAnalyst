"""AT-6: append-only content versioning for `MetadataBusinessAnnotation`.

`MetadataBusinessAnnotation` used to hold its own content and get mutated in
place on every re-approval (`semantic_inference.apply_enrichment_proposal`'s
old `else:` branch overwrote `business_name`, `business_description`, ... and
bumped `version` on the same row) -- see
`Docs/review-2026-08/atlan-context/00-decisions.md` §1. That made it
impossible to know what content an `AgentRun` retrieved and was grounded on
once a later approval overwrote it: history could not be reconstructed
because none was kept.

This module is the single write path for `MetadataBusinessAnnotationVersion`
content (`write_annotation_version`) and the shared "current version" read
helpers, following the exact supersede-on-publish shape already used for
`AssetDocumentationVersion` (`asset_description_service.apply_asset_description_draft`)
and `GlossaryTermVersion` (`semantic_api.decide_governance_review`): the prior
`APPROVED` row is flipped to `SUPERSEDED` in the same transaction that inserts
the new `APPROVED` row, never edited for content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from aida.models import MetadataBusinessAnnotationVersion


def annotation_version_content_digest(version: MetadataBusinessAnnotationVersion) -> str:
    """The AT-6 fragment digest for one `MetadataBusinessAnnotationVersion`'s
    content -- `"sha256:" + hexdigest` of a canonical (sorted-key, no
    incidental whitespace) JSON encoding, so the same content always digests
    identically. The single definition both the orchestrator (hashing at
    grounding-assembly time, `agent_orchestrator._compute_grounding_fragment_digests`)
    and the replay proof (recomputing to verify, `agent_run_replay.resolve_grounding`)
    hash against, so the two can never silently drift apart.
    """
    content = {
        "business_name": version.business_name,
        "business_description": version.business_description,
        "table_role": version.table_role,
        "grain_statement": version.grain_statement,
        "synonyms": version.synonyms,
        "suggested_questions": version.suggested_questions,
        "tags": version.tags,
        "confidence": version.confidence,
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class AnnotationVersionContent:
    """The fields a re-approval supplies for a new version row."""

    __slots__ = (
        "business_name",
        "business_description",
        "table_role",
        "grain_statement",
        "synonyms",
        "suggested_questions",
        "tags",
        "confidence",
    )

    def __init__(
        self,
        *,
        business_name: str,
        business_description: str,
        table_role: str,
        grain_statement: str,
        synonyms: list[str],
        suggested_questions: list[str],
        tags: list[str],
        confidence: float,
    ) -> None:
        self.business_name = business_name
        self.business_description = business_description
        self.table_role = table_role
        self.grain_statement = grain_statement
        self.synonyms = synonyms
        self.suggested_questions = suggested_questions
        self.tags = tags
        self.confidence = confidence


async def write_annotation_version(
    session: AsyncSession,
    *,
    organization_id: UUID,
    annotation_id: UUID,
    content: AnnotationVersionContent,
    approved_by: str,
    approved_at: datetime,
) -> MetadataBusinessAnnotationVersion:
    """Insert a new `APPROVED` version, superseding the prior one if any.

    Append-only: the prior `APPROVED` row's `status` moves to `SUPERSEDED` and
    every other column on it is left untouched, so any `AgentRun` that hashed
    that row's content at grounding time can still resolve back to it by id.
    """
    latest_version_number = await session.scalar(
        select(func.max(MetadataBusinessAnnotationVersion.version)).where(
            MetadataBusinessAnnotationVersion.annotation_id == annotation_id
        )
    )
    await session.execute(
        update(MetadataBusinessAnnotationVersion)
        .where(
            MetadataBusinessAnnotationVersion.annotation_id == annotation_id,
            MetadataBusinessAnnotationVersion.status == "APPROVED",
        )
        .values(status="SUPERSEDED", updated_at=approved_at)
    )
    version = MetadataBusinessAnnotationVersion(
        organization_id=organization_id,
        annotation_id=annotation_id,
        version=(latest_version_number or 0) + 1,
        status="APPROVED",
        business_name=content.business_name,
        business_description=content.business_description,
        table_role=content.table_role,
        grain_statement=content.grain_statement,
        synonyms=content.synonyms,
        suggested_questions=content.suggested_questions,
        tags=content.tags,
        confidence=content.confidence,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    session.add(version)
    await session.flush()
    return version


def current_version_ranked_subquery() -> Subquery:
    """Every `APPROVED` `MetadataBusinessAnnotationVersion`, ranked latest-first
    per `annotation_id`. Callers filter `.c.rn == 1` for the current version --
    the same window-function shape `catalog_read_model._latest_approved_documentation`
    already uses for `AssetDocumentationVersion`.
    """
    return (
        select(
            MetadataBusinessAnnotationVersion,
            func.row_number()
            .over(
                partition_by=MetadataBusinessAnnotationVersion.annotation_id,
                order_by=MetadataBusinessAnnotationVersion.version.desc(),
            )
            .label("rn"),
        )
        .where(MetadataBusinessAnnotationVersion.status == "APPROVED")
        .subquery()
    )


def current_version_alias() -> tuple[type[MetadataBusinessAnnotationVersion], Subquery]:
    """`(alias, ranked)` pair for joining the current version onto its parent:

        alias, ranked = current_version_alias()
        select(MetadataBusinessAnnotation, alias)
            .join(alias, alias.annotation_id == MetadataBusinessAnnotation.id)
            .where(ranked.c.rn == 1, ...)
    """
    ranked = current_version_ranked_subquery()
    alias = aliased(MetadataBusinessAnnotationVersion, ranked)
    return alias, ranked


async def current_versions_by_annotation_id(
    session: AsyncSession, annotation_ids: list[UUID]
) -> dict[UUID, MetadataBusinessAnnotationVersion]:
    """The current `APPROVED` version for each of `annotation_ids`, keyed by
    `annotation_id`. Missing keys mean no approved content exists yet.
    """
    if not annotation_ids:
        return {}
    alias, ranked = current_version_alias()
    rows = (
        await session.execute(
            select(alias, ranked.c.annotation_id).where(
                ranked.c.rn == 1,
                ranked.c.annotation_id.in_(annotation_ids),
            )
        )
    ).all()
    return {annotation_id: version for version, annotation_id in rows}


async def resolve_annotation_version(
    session: AsyncSession, version_id: UUID
) -> MetadataBusinessAnnotationVersion | None:
    """Resolve one `MetadataBusinessAnnotationVersion` by id, regardless of its
    current `status` -- a superseded version is still the exact content a past
    `AgentRun` was grounded on and must remain resolvable (AT-6 replay proof).
    """
    return await session.get(MetadataBusinessAnnotationVersion, version_id)
