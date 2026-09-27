"""R11-B8: produce the freshness observations the evaluator reads.

`freshness.evaluate_freshness` has always judged a table by its newest
`FreshnessObservation`, and nothing outside the tests ever wrote one, so every
approved contract evaluated STALE with "no watermark observation recorded" --
correctly, and permanently. The only honest source of an observation is the
data itself: the latest value of the contract's watermark column.

That value is a source value, and ADR-0014 keeps source values out of the
platform. Its 2026-09-12 addendum permits exactly this one statistic, on the
terms enforced here:

* **Only for an ACTIVE contract.** A contract is maker-checker approved before
  anything is read, and an edit resets it to PENDING_APPROVAL, which stops the
  reading.
* **Only a temporal column.** The catalog must type the column as a date or a
  time. A contract naming `balance` would otherwise make this a way to read
  `MAX(balance)` -- a value, with nothing to do with freshness -- so it is
  refused before any statement is built.
* **Only through the gateway.** The statement is an ordinary governed query --
  identity, authorization, masking, audit, row limit -- run as the scheduler's
  identity, because ADR-0004 allows no second path. A column masked for that
  identity yields no observation rather than a masked one.
* **One timestamp, kept in one place.** The value is stored on the observation
  row and nowhere else this module writes: the sweep's audit record and its log
  lines carry counts and reason codes, never the value. Observations older
  than the contract's `retention_days` are deleted as new ones arrive.

A refusal is counted and reported, never worked around. In particular, a policy
denying reads of STALE tables to every principal denies the observer too, and
the table stays STALE: that is a policy someone wrote, and the sweep's outcome
counts are where it shows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

import sqlglot
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp

from aida.events import record_audit
from aida.models import (
    DataSource,
    FreshnessObservation,
    FreshnessWatermarkConfig,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)
from aida.query_gateway import QueryExecutionGateway
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

#: The name the statement gives the maximum, so the row is read by name.
WATERMARK_ALIAS = "watermark_value"

#: Catalog type fragments that make a column a timestamp. Physical type names
#: vary by source (`timestamptz`, `datetime2`, `date`, `TIMESTAMP_NTZ`), and
#: every one of them contains one of these.
_TEMPORAL_TYPE_FRAGMENTS = ("date", "time")

OBSERVED = "OBSERVED"


@dataclass(frozen=True, slots=True)
class ObservationSweep:
    """What one datasource's observation pass did, value-free."""

    contracts_read: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def observed(self) -> int:
        return self.outcomes.get(OBSERVED, 0)

    def as_details(self) -> dict[str, Any]:
        return {
            "contracts_read": self.contracts_read,
            "observed": self.observed,
            "outcomes": dict(sorted(self.outcomes.items())),
        }


def is_temporal_type(physical_type: str | None) -> bool:
    lowered = (physical_type or "").lower()
    return any(fragment in lowered for fragment in _TEMPORAL_TYPE_FRAGMENTS)


def watermark_statement(*, schema: str, table: str, column: str, dialect: str) -> str:
    """`SELECT MAX(<column>) AS watermark_value FROM <schema>.<table>`, quoted
    for the source's dialect.

    Built as an expression tree rather than by formatting a string: the names
    come from the catalog, and a name is not a place for SQL.
    """
    return (
        sqlglot.select(exp.Max(this=exp.column(column, quoted=True)).as_(WATERMARK_ALIAS))
        .from_(exp.table_(table, db=schema, quoted=True))
        .sql(dialect=dialect)
    )


def as_watermark(value: Any) -> datetime | None:
    """A source's maximum as an aware UTC instant, or None if it is not one.

    A `date` is its midnight in UTC: a day-grained watermark cannot say more,
    and the evaluator compares instants.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, str):
        try:
            return as_watermark(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


async def observe_freshness_for_datasource(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    context: SecurityContext,
    gateway: QueryExecutionGateway,
    max_tables: int,
    now: datetime | None = None,
) -> ObservationSweep:
    """Read one watermark per ACTIVE contract on a datasource, through the gateway.

    Returns the outcome counts; the caller commits. Note that the gateway
    commits on its own as it records each execution, so observations from
    earlier tables in the pass are durable even if a later one fails.
    """
    effective_now = now or datetime.now(UTC)
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None or datasource.organization_id != organization_id:
        return ObservationSweep()

    contracts = (
        await session.execute(
            select(FreshnessWatermarkConfig, MetadataTable, MetadataSchema)
            .join(MetadataTable, MetadataTable.id == FreshnessWatermarkConfig.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                FreshnessWatermarkConfig.organization_id == organization_id,
                FreshnessWatermarkConfig.datasource_id == datasource_id,
                FreshnessWatermarkConfig.status == "ACTIVE",
            )
            .order_by(FreshnessWatermarkConfig.created_at.asc())
            .limit(max_tables)
        )
    ).all()

    outcomes: Counter[str] = Counter()
    for contract, table, schema in contracts:
        column = await session.scalar(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.name == contract.watermark_column,
                MetadataColumn.status == "ACTIVE",
            )
        )
        if column is None:
            outcomes["COLUMN_NOT_IN_CATALOG"] += 1
            continue
        if not is_temporal_type(column.physical_type):
            # The ADR-0014 addendum's second condition, and the one that keeps
            # this from being a general-purpose MAX() reader.
            outcomes["COLUMN_NOT_TEMPORAL"] += 1
            continue

        sql = watermark_statement(
            schema=schema.name, table=table.name, column=column.name, dialect=datasource.dialect
        )
        try:
            result = await gateway.execute(
                session,
                datasource=datasource,
                context=context,
                correlation_id=f"freshness-observation:{contract.id}",
                sql=sql,
                requested_limit=1,
                semantic_version=None,
            )
        except Exception as exc:  # noqa: BLE001 -- one table must not stop the sweep
            # A reason code or the class name, never the message: a connector's
            # own error text can quote the data it failed on (INV-6).
            reason = getattr(exc, "reason_code", None) or type(exc).__name__
            outcomes[f"REFUSED:{reason}"] += 1
            logger.warning(
                "freshness_observation_refused", table_id=str(table.id), reason=reason
            )
            continue

        hidden = {name.lower() for name in (*result.masked_columns, *result.tokenized_columns)}
        if WATERMARK_ALIAS in hidden:
            outcomes["MASKED"] += 1
            continue
        row = result.rows[0] if result.rows else {}
        raw = next((value for key, value in row.items() if key.lower() == WATERMARK_ALIAS), None)
        watermark = as_watermark(raw)
        if watermark is None:
            outcomes["NO_VALUE" if raw is None else "UNREADABLE_VALUE"] += 1
            continue

        await session.execute(
            delete(FreshnessObservation).where(
                FreshnessObservation.table_id == table.id,
                FreshnessObservation.observed_at
                < effective_now - timedelta(days=contract.retention_days),
            )
        )
        session.add(
            FreshnessObservation(
                organization_id=organization_id,
                datasource_id=datasource_id,
                table_id=table.id,
                watermark_value=watermark,
                observed_at=effective_now,
            )
        )
        outcomes[OBSERVED] += 1

    sweep = ObservationSweep(contracts_read=len(contracts), outcomes=dict(outcomes))
    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.freshness.observe",
        resource_type="datasource",
        resource_id=str(datasource_id),
        outcome="SUCCESS",
        correlation_id=str(datasource_id),
        details=sweep.as_details(),
    )
    return sweep
