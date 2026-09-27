"""SW-1: the `usage x impact x deficit` scorer for steward prioritisation.

**Status (2026-09-04): a pure scorer, not a live endpoint.**
`stewardship_api.list_documentation_worklist` (AT-5) already owns the
"what should a steward document next" surface, ranking by real query volume.
Exposing a second ranked backlog would be exactly the "two catalogues" seam
this platform's competitive research names as a thing never to build, so this
module deliberately has no router. AT-5 adopted it: `score_item` and
`enrich_tables` add the two factors AT-5 lacked -- downstream impact, and a
five-field deficit rather than description-only -- and return them alongside
the score so a screen can answer "why is this first". The module's own ranked
backlog, which nothing called once AT-5 took these over, was removed
2026-09-21 (R11-VAL05).

The blank-catalog problem is not solved by making documentation easier; it is
solved by making the *order* obvious. A steward facing 400,000 undocumented
objects does not need a better editor, they need to know which forty matter.

The score is deliberately a product of three independent factors, not a sum:

    usage x impact x deficit

A product means a zero on any factor is a zero overall, which is the correct
behaviour for all three. An asset nobody queries is not urgent however
undocumented (usage 0). An asset with no downstream is a leaf whose meaning
matters less (impact 0 floors to a small constant rather than zero, so leaves
still rank). And a fully documented, owned, certified asset needs no work at
all whatever its traffic (deficit 0). A sum would let a single huge factor
carry an item that fails the other two, which is exactly how ranked backlogs
become noise.

Everything here is deterministic and value-free: counts and identifiers, no
sampled rows, no model. It is a *prioritisation*, not a judgement, so it
belongs on the ML rulebook's "prioritise" lane (`00-product/08` §10 row 13)
where a wrong answer costs a steward one look at the wrong table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_certification import asset_certification_is_active
from aida.models import (
    AssetCertification,
    AssetTermLink,
    DataQualityIncident,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    OwnershipAssignment,
)

#: What "documented" means, as a checklist. Each missing item contributes one
#: point of deficit, so an asset with nothing is five times as urgent as one
#: missing only its quality policy.
DEFICIT_FIELDS = ("description", "owner", "certification", "glossary_term", "quality_policy")

#: A leaf table (no downstream) still gets a small impact so it can rank at
#: all; without it, every leaf would score zero and the backlog would only
#: ever show hubs.
_LEAF_IMPACT = 0.25


def _normalise(value: int, ceiling: int) -> float:
    """Squash a count into 0..1 against a soft ceiling.

    Linear with a cap rather than a log: a table referenced 500 times and one
    referenced 5,000 times are both simply "very used", and the difference
    should not swamp the deficit factor.
    """
    if ceiling <= 0:
        return 0.0
    return min(float(value) / float(ceiling), 1.0)


def score_item(
    *,
    usage_references: int,
    downstream_count: int,
    missing: tuple[str, ...],
    usage_ceiling: int,
    downstream_ceiling: int,
) -> tuple[float, float, float, float]:
    """`(score, usage, impact, deficit)` -- pure, so it is unit-testable and
    so the same inputs always produce the same ordering."""
    usage = _normalise(usage_references, usage_ceiling)
    impact = max(_normalise(downstream_count, downstream_ceiling), _LEAF_IMPACT)
    deficit = len(missing) / len(DEFICIT_FIELDS)
    return usage * impact * deficit, usage, impact, deficit


@dataclass(frozen=True, slots=True)
class TableEnrichment:
    """The two factors a usage-only ranking cannot see.

    `downstream_count` is impact; `missing` is the five-field deficit. Both
    are returned per table so a caller can rank by them *and* show why.
    """

    downstream_count: int
    missing: tuple[str, ...]
    open_incidents: int


async def enrich_tables(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: list[UUID],
    *,
    descriptions: dict[UUID, bool] | None = None,
    now: datetime | None = None,
) -> dict[UUID, TableEnrichment]:
    """Impact and documentation deficit for a set of tables.

    AT-5 (`stewardship_api.list_documentation_worklist`) ranks by these, so
    there is one definition of "documented" rather than a second, drifting
    one beside it. A fixed number of aggregate queries regardless of how many
    tables are passed.

    `descriptions` lets a caller that has already resolved description state
    through its own precedence chain (UX-12's, which AT-5 uses) supply it:
    `{table_id: has_a_real_description}`. Without it, the table's own
    `source_description` and its columns' are used, which is this module's
    own weaker check.
    """
    moment = now or datetime.now(UTC)
    if not table_ids:
        return {}

    # --- impact: how many other tables declare a foreign key *into* this one.
    # A table many others point at is a hub, and getting a hub's meaning
    # wrong is wrong in every direction at once. The unified-lineage impact
    # walk is the precise answer and is far too expensive to run per table
    # for a ranked list, so declared keys are the bounded stand-in.
    downstream_counts: dict[UUID, int] = {
        referenced_id: int(count)
        for referenced_id, count in (
            await session.execute(
                select(MetadataConstraint.referenced_table_id, func.count())
                .where(
                    MetadataConstraint.organization_id == organization_id,
                    MetadataConstraint.referenced_table_id.in_(table_ids),
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.status == "ACTIVE",
                )
                .group_by(MetadataConstraint.referenced_table_id)
            )
        ).all()
        if referenced_id is not None
    }

    # --- deficit signals ---------------------------------------------------
    owned = {
        table_id
        for (table_id,) in (
            await session.execute(
                select(OwnershipAssignment.subject_id.distinct()).where(
                    OwnershipAssignment.organization_id == organization_id,
                    OwnershipAssignment.subject_type == "TABLE",
                    OwnershipAssignment.status == "ACTIVE",
                )
            )
        ).all()
    }
    linked = {
        table_id
        for (table_id,) in (
            await session.execute(
                select(AssetTermLink.table_id.distinct()).where(
                    AssetTermLink.table_id.in_(table_ids)
                )
            )
        ).all()
    }
    certifications = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id.in_(table_ids),
                AssetCertification.asset_type == "TABLE",
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    certified = {
        certification.table_id
        for certification in certifications
        if asset_certification_is_active(certification, at=moment)
    }
    incident_counts: dict[UUID, int] = {
        table_id: int(count)
        for table_id, count in (
            await session.execute(
                select(DataQualityIncident.table_id, func.count())
                .where(
                    DataQualityIncident.table_id.in_(table_ids),
                    DataQualityIncident.status.in_(["OPEN", "ACKNOWLEDGED"]),
                )
                .group_by(DataQualityIncident.table_id)
            )
        ).all()
    }
    # A table with no described columns is undocumented even if the table
    # itself carries a description.
    described_columns = {
        table_id
        for (table_id,) in (
            await session.execute(
                select(MetadataColumn.table_id.distinct()).where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.source_description.is_not(None),
                    MetadataColumn.status == "ACTIVE",
                )
            )
        ).all()
    }

    enrichment: dict[UUID, TableEnrichment] = {}
    described_tables: set[UUID] = set()
    if descriptions is None:
        described_tables = {
            table_id
            for (table_id,) in (
                await session.execute(
                    select(MetadataTable.id).where(
                        MetadataTable.id.in_(table_ids),
                        MetadataTable.source_description.is_not(None),
                    )
                )
            ).all()
        }
    for table_id in table_ids:
        has_description = (
            descriptions.get(table_id, False)
            if descriptions is not None
            else (table_id in described_tables or table_id in described_columns)
        )
        missing: list[str] = []
        if not has_description:
            missing.append("description")
        if str(table_id) not in owned:
            missing.append("owner")
        if table_id not in certified:
            missing.append("certification")
        if table_id not in linked:
            missing.append("glossary_term")
        if table_id not in incident_counts and table_id not in certified:
            # No quality signal at all: neither an incident nor a
            # certification that implies someone looked.
            missing.append("quality_policy")
        enrichment[table_id] = TableEnrichment(
            downstream_count=downstream_counts.get(table_id, 0),
            missing=tuple(missing),
            open_incidents=incident_counts.get(table_id, 0),
        )
    return enrichment
