"""UX-18: the consumer footer -- who/what currently consumes one specific
version of a semantic object, so a steward opening it for edit never changes
it blind to its downstream impact.

Pure aggregation over `consumption_lineage.py`'s existing `ConsumptionRecord`
data (CX-4) -- no new persisted state, and `get_consumption_for_resource` is
reused verbatim rather than re-derived, the same "reused, not re-derived"
discipline `asset_evidence.py` (UX-13) already applies to the same table.

Response models are defined here rather than in `aida.schemas`, the same way
SM-7's `GovernanceReviewDiffRead`/`SemanticFieldDeltaRead` live in
`aida.semantic_api` and UX-17's `ReviewQueueRead` lives in
`aida.review_queue_schemas` -- `aida/schemas.py` is read-only for this row
(`Docs/60-delivery/03-tracker.md` UX-18).

Version-specificity
--------------------
`ConsumptionRecord` carries no separate version column, and does not need
one: every consumption write CX-4 already performs for a versioned resource
uses that *version row's own primary key* as `resource_id` -- never a
logical/parent id. `mcp_server.py`'s and `context_product_api.py`'s reads of
a `ContextProductVersion` already record
`resource_type="context_product_version"`, `resource_id=str(version.id)`
this exact way. `SemanticModelVersion`, `SemanticMetricVersion` and
`GlossaryTermVersion` (`aida/models.py`) all share that same shape -- a UUID
primary key per version row, a separate integer `version` field, and a
foreign key back to the logical parent (`project_id`/`metric_id`/`term_id`)
-- so scoping `get_consumption_for_resource` to `resource_id=str(version.id)`
is exactly as version-specific as the already-proven context-product case:
two versions of the same object are two different rows with two different
primary keys, so a consumption edge recorded against version 3 can never be
returned as a consumer of version 4. Proven directly by
`tests/test_consumer_footer.py::test_does_not_leak_consumers_of_a_different_version`.

Because no MCP tool or REST route currently records a direct per-object
consumption edge for a metric/glossary-term/semantic-model version read in
isolation (today CX-4 only records `metadata_table` and
`context_product_version` reads -- a semantic object consumed only as part of
a bundled, published context product is attributed to that
`context_product_version`, not decomposed back to each semantic object inside
it), a freshly-authored draft legitimately shows an empty footer. That is the
honest answer for a version nothing has consumed yet, not a defect in this
composition -- see this row's tracker note for the full account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import computed_field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.consumption_lineage import get_consumption_for_resource
from aida.schemas import ApiModel

# Bounded the same way `asset_evidence.compose_asset_evidence`'s
# `consumption_limit` is bounded: a footer is a steward-facing summary, not
# an unbounded audit export (that already exists --
# `consumption_lineage_api.py`) -- so this is a window over the most recent
# consumption events, not the full history.
DEFAULT_CONSUMER_FOOTER_LIMIT = 200


class ConsumerFooterEntryRead(ApiModel):
    """One consumer of the resource: who/what, over which channel it most
    recently read this exact version, how many times (within the composed
    window), and when it last did so.
    """

    consumer_id: str
    consumer_type: str
    channel: str
    consumption_count: int
    last_consumed_at: datetime


class ConsumerFooterRead(ApiModel):
    """CX-4 consumption lineage, scoped to one specific version of one
    semantic object -- "no semantic edit is made blind" (tracker UX-18).

    `total_consumption_events` is the exact count of matching
    `ConsumptionRecord` rows (`get_consumption_for_resource`'s own `COUNT(*)`,
    independent of the window `consumers` was built from); `total_consumers`
    is the number of distinct consumers found within that window, so it can
    legitimately undercount when a resource's consumption history is larger
    than the composed window -- an honestly-bounded summary, not a claim of
    exhaustiveness.
    """

    resource_type: str
    resource_id: str
    version: int | None
    generated_at: datetime
    total_consumption_events: int
    consumers: list[ConsumerFooterEntryRead]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_consumers(self) -> int:
        return len(self.consumers)


async def compose_consumer_footer(
    session: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: str,
    version: int | None = None,
    now: datetime | None = None,
    limit: int = DEFAULT_CONSUMER_FOOTER_LIMIT,
) -> ConsumerFooterRead:
    """Who/what currently consumes this exact version, aggregated from
    `consumption_lineage.get_consumption_for_resource` -- reused verbatim,
    not re-derived. That helper already scopes by `(organization_id,
    resource_type, resource_id)` and orders newest-first; this collapses its
    rows to one entry per distinct consumer (`consumer_id` + `consumer_type`),
    keeping the most recent `channel`/`last_consumed_at` and a per-consumer
    event count (bounded by `limit`), sorted newest-consumer-first.
    """
    moment = now or datetime.now(UTC)
    records, total = await get_consumption_for_resource(
        session,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
    )
    by_consumer: dict[tuple[str, str], ConsumerFooterEntryRead] = {}
    # `records` is newest-first, so the first row seen for a given consumer
    # key is already that consumer's most recent read of this version.
    for record in records:
        key = (record.consumer_id, record.consumer_type)
        existing = by_consumer.get(key)
        if existing is None:
            by_consumer[key] = ConsumerFooterEntryRead(
                consumer_id=record.consumer_id,
                consumer_type=record.consumer_type,
                channel=record.channel,
                consumption_count=1,
                last_consumed_at=record.consumed_at,
            )
        else:
            by_consumer[key] = existing.model_copy(
                update={"consumption_count": existing.consumption_count + 1}
            )
    consumers = sorted(
        by_consumer.values(), key=lambda entry: entry.last_consumed_at, reverse=True
    )
    return ConsumerFooterRead(
        resource_type=resource_type,
        resource_id=resource_id,
        version=version,
        generated_at=moment,
        total_consumption_events=total,
        consumers=consumers,
    )
