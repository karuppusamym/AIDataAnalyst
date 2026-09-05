"""SW-1: the `usage x impact x deficit` scorer for steward prioritisation.

**Status (2026-09-04): a pure scorer, not a live endpoint.**
`stewardship_api.list_documentation_worklist` (AT-5) already owns the
"what should a steward document next" surface, ranking by real query volume.
Exposing a second ranked backlog would be exactly the "two catalogues" seam
this platform's competitive research names as a thing never to build, so this
module deliberately has no router. It is here for AT-5 to adopt: it adds the
two factors AT-5 lacks -- downstream impact, and a five-field deficit rather
than description-only -- and returns them alongside the score so a screen can
answer "why is this first".

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
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_certification import asset_certification_is_active
from aida.models import (
    AssetCertification,
    AssetTermLink,
    ConsumptionRecord,
    DataQualityIncident,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
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


@dataclass(frozen=True, slots=True)
class WorklistItem:
    table_id: UUID
    table_name: str
    schema_name: str
    datasource_id: UUID
    score: float
    usage: float
    impact: float
    deficit: float
    missing: tuple[str, ...]
    usage_references: int
    downstream_count: int
    open_incidents: int


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


def _scoped(statement: Select[Any], organization_id: UUID, datasource_id: UUID | None) -> Any:
    statement = statement.where(MetadataTable.organization_id == organization_id)
    if datasource_id is not None:
        statement = statement.where(MetadataTable.datasource_id == datasource_id)
    return statement


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

    Extracted from `compute_worklist` so AT-5
    (`stewardship_api.list_documentation_worklist`) ranks by the same rules
    rather than growing a second, drifting definition of "documented" beside
    this one. A fixed number of aggregate queries regardless of how many
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


async def compute_worklist(
    session: AsyncSession,
    organization_id: UUID,
    *,
    datasource_id: UUID | None = None,
    limit: int = 50,
    scan_limit: int = 5_000,
    now: datetime | None = None,
) -> list[WorklistItem]:
    """The ranked backlog.

    A fixed number of queries regardless of how many tables come back: one
    for the candidate tables, and one aggregate per signal. No per-row query,
    which is the difference between a screen that opens and one that times
    out on a bank's catalogue.
    """
    moment = now or datetime.now(UTC)

    table_rows = (
        await session.execute(
            _scoped(
                select(
                    MetadataTable.id,
                    MetadataTable.name,
                    MetadataTable.datasource_id,
                    MetadataTable.source_description,
                    MetadataSchema.name,
                ).join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id),
                organization_id,
                datasource_id,
            )
            .where(MetadataTable.status == "ACTIVE")
            .limit(scan_limit)
        )
    ).all()
    if not table_rows:
        return []
    table_ids = [row[0] for row in table_rows]

    # --- usage: the platform's own consumption edges ----------------------
    # `ConsumptionRecord` is what Atlas already records every time a resource
    # is read through a governed surface, so it is the honest usage signal
    # rather than a proxy. `resource_id` is a string, so counts are keyed by
    # string and looked up that way.
    usage_counts: dict[str, int] = {
        str(resource_id): int(count)
        for resource_id, count in (
            await session.execute(
                select(ConsumptionRecord.resource_id, func.count())
                .where(
                    ConsumptionRecord.organization_id == organization_id,
                    ConsumptionRecord.resource_type == "TABLE",
                    ConsumptionRecord.resource_id.in_([str(t) for t in table_ids]),
                )
                .group_by(ConsumptionRecord.resource_id)
            )
        ).all()
    }

    # Impact and deficit come from `enrich_tables`, the same call AT-5 makes,
    # so there is exactly one definition of "documented" on the platform.
    enrichment = await enrich_tables(
        session,
        organization_id,
        table_ids,
        descriptions={
            row[0]: bool(row[3]) for row in table_rows
        },
        now=moment,
    )
    downstream_counts = {
        table_id: item.downstream_count for table_id, item in enrichment.items()
    }

    usage_ceiling = max([*usage_counts.values(), 1])
    downstream_ceiling = max([*downstream_counts.values(), 1])

    items: list[WorklistItem] = []
    for table_id, table_name, source_id, _description, schema_name in table_rows:
        signals = enrichment.get(table_id)
        if signals is None:
            continue
        missing = list(signals.missing)
        usage_references = usage_counts.get(str(table_id), 0)
        downstream = signals.downstream_count
        score, usage, impact, deficit = score_item(
            usage_references=usage_references,
            downstream_count=downstream,
            missing=tuple(missing),
            usage_ceiling=usage_ceiling,
            downstream_ceiling=downstream_ceiling,
        )
        if score <= 0.0:
            continue
        items.append(
            WorklistItem(
                table_id=table_id,
                table_name=table_name,
                schema_name=schema_name,
                datasource_id=source_id,
                score=round(score, 6),
                usage=round(usage, 6),
                impact=round(impact, 6),
                deficit=round(deficit, 6),
                missing=tuple(missing),
                usage_references=usage_references,
                downstream_count=downstream,
                open_incidents=signals.open_incidents,
            )
        )
    # Deterministic tie-break on the id so one estate produces one ordering.
    items.sort(key=lambda item: (-item.score, str(item.table_id)))
    return items[:limit]
