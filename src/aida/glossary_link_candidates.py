"""GL-8: glossary-to-table link candidates from approved business labels.

Extracted from `stewardship_api.generate_glossary_link_proposals` on 2026-09-10
so that the steward agent (`aida.steward_agent`, ADR-0029) proposes links by
the same rule a steward's "generate" request applies, without importing a
router. The rule is unchanged:

* an approved business annotation's business name or synonym that equals --
  case-insensitively, after trimming -- an approved, active term's display
  name, key or synonym is a candidate;
* confidence is 1.0 when the annotation's business name matches the term's
  display name and 0.92 for every other pairing, and the strongest match per
  term wins;
* a (table, term) pair that is already linked is not a candidate, and nor is a
  (table, term, annotation) triple that was already proposed -- in any status,
  so a link a human rejected is never raised again.

`find_glossary_link_candidates` only reads. Callers build the
`GlossaryLinkProposal` rows (`build_glossary_link_proposal`) because the two
callers attribute them to different principals: the steward who asked, or the
steward agent's own workload identity.

**Bounded reads.** The handler this came from loaded every table, link and
proposal in the organization to answer a question about at most 10,000
annotations. Those three reads are now restricted to the tables the scanned
annotations name. The candidates are identical -- a candidate's table is
always an annotated table -- and the read no longer grows with the estate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.models import (
    AssetTermLink,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataBusinessAnnotation,
    MetadataTable,
)

#: The strategy recorded in every candidate's evidence. There is one.
LINK_STRATEGY: Final = "APPROVED_LABEL_EXACT_MATCH"

_PRIMARY_MATCH_CONFIDENCE: Final = 1.0
_SECONDARY_MATCH_CONFIDENCE: Final = 0.92
_TERM_SCAN_LIMIT: Final = 5000
_ANNOTATION_SCAN_LIMIT: Final = 10_000


@dataclass(frozen=True, slots=True)
class GlossaryLinkCandidate:
    """One proposed (table, term) link and the label match behind it."""

    table: MetadataTable
    term: GlossaryTerm
    term_version: GlossaryTermVersion
    source_annotation_id: UUID
    annotation_version: int
    confidence: float
    matched_label: str
    term_label_kind: str

    def evidence(self) -> dict[str, Any]:
        return {
            "strategy": LINK_STRATEGY,
            "matched_label": self.matched_label,
            "term_label_kind": self.term_label_kind,
            "annotation_version": self.annotation_version,
        }


@dataclass(frozen=True, slots=True)
class GlossaryLinkScan:
    candidates: list[GlossaryLinkCandidate]
    annotations_scanned: int
    approved_terms_scanned: int


async def find_glossary_link_candidates(
    session: AsyncSession,
    *,
    organization_id: UUID,
    minimum_confidence: float,
    limit: int,
    datasource_id: UUID | None = None,
    active_tables_only: bool = False,
) -> GlossaryLinkScan:
    """Up to `limit` link candidates, in annotation-id order.

    `datasource_id` narrows the scan to one source's annotations, and
    `active_tables_only` drops candidates whose table is no longer ACTIVE. The
    steward-facing endpoint sets neither and keeps its original
    organization-wide behaviour; the steward agent sets both.
    """
    term_rows = (
        await session.execute(
            select(GlossaryTerm, GlossaryTermVersion)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == GlossaryTerm.id)
            .where(
                GlossaryTerm.organization_id == organization_id,
                GlossaryTerm.lifecycle_status == "ACTIVE",
                GlossaryTermVersion.status == "APPROVED",
            )
            .limit(_TERM_SCAN_LIMIT)
        )
    ).all()
    label_index: dict[str, list[tuple[GlossaryTerm, GlossaryTermVersion, str]]] = {}
    for term, version in term_rows:
        labels = [(version.display_name, "DISPLAY_NAME"), (term.term_key, "TERM_KEY")]
        labels.extend((synonym, "SYNONYM") for synonym in version.synonyms)
        for label, kind in labels:
            label_index.setdefault(label.strip().casefold(), []).append((term, version, kind))
    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`,
    # not on `MetadataBusinessAnnotation` -- see `business_annotation_versions.py`.
    annotation_version_alias, annotation_version_ranked = current_version_alias()
    annotation_filters: list[Any] = [
        MetadataBusinessAnnotation.organization_id == organization_id,
        annotation_version_ranked.c.rn == 1,
    ]
    if datasource_id is not None:
        annotation_filters.append(MetadataBusinessAnnotation.datasource_id == datasource_id)
    annotation_rows = (
        await session.execute(
            select(MetadataBusinessAnnotation, annotation_version_alias)
            .join(
                annotation_version_alias,
                annotation_version_alias.annotation_id == MetadataBusinessAnnotation.id,
            )
            .where(*annotation_filters)
            .order_by(MetadataBusinessAnnotation.id)
            .limit(_ANNOTATION_SCAN_LIMIT)
        )
    ).all()
    annotated_table_ids = list({annotation.table_id for annotation, _ in annotation_rows})
    link_rows = (
        await session.execute(
            select(AssetTermLink.table_id, AssetTermLink.term_id).where(
                AssetTermLink.organization_id == organization_id,
                AssetTermLink.table_id.in_(annotated_table_ids),
            )
        )
    ).all()
    existing_links = {(row[0], row[1]) for row in link_rows}
    proposal_rows = (
        await session.execute(
            select(
                GlossaryLinkProposal.table_id,
                GlossaryLinkProposal.term_id,
                GlossaryLinkProposal.source_annotation_id,
            ).where(
                GlossaryLinkProposal.organization_id == organization_id,
                GlossaryLinkProposal.table_id.in_(annotated_table_ids),
            )
        )
    ).all()
    existing_proposals = {(row[0], row[1], row[2]) for row in proposal_rows}
    table_filters: list[Any] = [
        MetadataTable.organization_id == organization_id,
        MetadataTable.id.in_(annotated_table_ids),
    ]
    if active_tables_only:
        table_filters.append(MetadataTable.status == "ACTIVE")
    tables = {
        row.id: row
        for row in (await session.scalars(select(MetadataTable).where(*table_filters))).all()
    }

    candidates: list[GlossaryLinkCandidate] = []
    for annotation, content_version in annotation_rows:
        annotation_labels = [(content_version.business_name, "BUSINESS_NAME")]
        annotation_labels.extend(
            (value, "ANNOTATION_SYNONYM") for value in content_version.synonyms
        )
        matches: dict[UUID, tuple[GlossaryTerm, GlossaryTermVersion, float, str, str]] = {}
        for annotation_label, annotation_kind in annotation_labels:
            normalized = annotation_label.strip().casefold()
            for term, version, term_kind in label_index.get(normalized, []):
                is_primary_match = (
                    annotation_kind == "BUSINESS_NAME" and term_kind == "DISPLAY_NAME"
                )
                confidence = (
                    _PRIMARY_MATCH_CONFIDENCE if is_primary_match else _SECONDARY_MATCH_CONFIDENCE
                )
                current = matches.get(term.id)
                if current is None or confidence > current[2]:
                    matches[term.id] = (term, version, confidence, annotation_label, term_kind)
        for term, version, confidence, matched_label, term_kind in matches.values():
            key = (annotation.table_id, term.id, annotation.id)
            if (
                confidence < minimum_confidence
                or (annotation.table_id, term.id) in existing_links
                or key in existing_proposals
            ):
                continue
            table = tables.get(annotation.table_id)
            if table is None:
                continue
            candidates.append(
                GlossaryLinkCandidate(
                    table=table,
                    term=term,
                    term_version=version,
                    source_annotation_id=annotation.id,
                    annotation_version=content_version.version,
                    confidence=confidence,
                    matched_label=matched_label,
                    term_label_kind=term_kind,
                )
            )
            existing_proposals.add(key)
            if len(candidates) == limit:
                break
        if len(candidates) == limit:
            break
    return GlossaryLinkScan(
        candidates=candidates,
        annotations_scanned=len(annotation_rows),
        approved_terms_scanned=len(term_rows),
    )


def build_glossary_link_proposal(
    candidate: GlossaryLinkCandidate, *, organization_id: UUID, created_by: str
) -> GlossaryLinkProposal:
    """A `DRAFT` proposal for one candidate, attributed to `created_by`."""
    return GlossaryLinkProposal(
        organization_id=organization_id,
        table_id=candidate.table.id,
        term_id=candidate.term.id,
        source_annotation_id=candidate.source_annotation_id,
        confidence=candidate.confidence,
        evidence=candidate.evidence(),
        created_by=created_by,
    )
