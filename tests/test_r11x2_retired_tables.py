"""R11-X2: five tables nothing used are retired, and what they held has a better home.

Two were never written -- ADR-0018's `isolation_boundary` and
`business_assignment_rule`, schemas with no writer and nothing enforcing them --
and three were never read: `contract_sla_record`, `studio_test_run` and
`procedure_tool_generation_record`. Migration `c4e7b2d9a613` drops them, and
ADR-0018's addendum records that a hard wall and rule-driven assignment are
built with their enforcement and evaluator, not ahead of them.

These pin the retirement from the outside: the schema no longer maps them, a
client still sending the retired workspace field is told rather than ignored,
and reading an SLA status no longer writes a row.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.main  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.runtime_contracts import compute_sla_status
from atlas.modules.identity_tenancy.schemas import WorkspaceCreate

RETIRED_TABLES = (
    "business_assignment_rule",
    "contract_sla_record",
    "isolation_boundary",
    "procedure_tool_generation_record",
    "studio_test_run",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


def test_the_retired_tables_are_no_longer_mapped() -> None:
    assert sorted(set(RETIRED_TABLES) & set(Base.metadata.tables)) == []


def test_the_retired_columns_are_no_longer_mapped() -> None:
    assert "isolation_boundary_id" not in Base.metadata.tables["workspace"].columns
    assert "rule_id" not in Base.metadata.tables["business_assignment"].columns


def test_a_client_still_sending_an_isolation_boundary_is_told_rather_than_ignored() -> None:
    """Every value this field ever accepted was refused, because no boundary could
    exist. The schema forbids unknown fields, so an old client gets a 422 naming
    the field instead of silently losing a hard wall it believes it asked for."""
    with pytest.raises(ValidationError, match="isolation_boundary_id"):
        WorkspaceCreate(name="Retail", slug="retail", isolation_boundary_id=uuid4())


async def test_reading_an_sla_status_writes_nothing(session: AsyncSession) -> None:
    """The status endpoint inserted a row on every read, and nothing read the rows
    back. The answer is a function of the violation ledger, so it is computed."""
    end = datetime(2026, 9, 13, tzinfo=UTC)

    status = await compute_sla_status(session, uuid4(), uuid4(), end - timedelta(days=30), end)

    assert (status.uptime_percent, status.violations_count, status.breach_minutes) == (
        100.0,
        0,
        0,
    )
    assert list(session.new) == []
