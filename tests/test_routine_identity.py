"""R11-FP03: routine identity -- definition versions, packages and kind drift.

* `MetadataRoutine` overwrote its body on every rescan, so the definition a lineage edge or a
  generated tool was built from vanished when the source changed. Each captured definition is
  now an immutable version, classed LITERAL_ONLY or STRUCTURAL.
* An Oracle PACKAGE is stored by the pull path but could not be pushed, was dropped by any
  restricted discovery selection, had its grants scoped as a TABLE, and could reach tool
  generation as though it were one callable thing.
* BigQuery's `SCALAR_FUNCTION` matched no selectable kind, so a restricted selection dropped it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredGrant,
    DiscoveredRoutine,
    DiscoveredSchema,
)
from aida.db import Base
from aida.discovery_selection import DiscoverySelection, apply_selection, kind_capabilities
from aida.envelope_models import MetadataRoutine, MetadataRoutineDefinitionVersion
from aida.procedure_tool_blueprint import ProcedureNotEligibleError, resolve_procedure_tool_source
from atlas.modules.ingestion.schemas import MetadataGrantEnvelope, MetadataRoutineEnvelope
from tests.test_change_signals import _datasource, _envelope, _routine, _scan

BODY = "BEGIN UPDATE customer.account SET status = 'CLOSED' WHERE closed_on < now(); END;"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _versions(session: AsyncSession) -> list[MetadataRoutineDefinitionVersion]:
    return list(
        await session.scalars(
            select(MetadataRoutineDefinitionVersion).order_by(
                MetadataRoutineDefinitionVersion.version_number
            )
        )
    )


async def test_every_captured_definition_is_kept_and_classed(session: AsyncSession) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope(routines=[_routine(BODY)]))
    await _scan(session, datasource, _envelope(routines=[_routine(BODY)]))  # identical
    await _scan(
        session, datasource, _envelope(routines=[_routine(BODY.replace("'CLOSED'", "'DORMANT'"))])
    )
    await _scan(
        session,
        datasource,
        _envelope(routines=[_routine("BEGIN DELETE FROM customer.account; END;")]),
    )

    versions = await _versions(session)
    assert [(v.version_number, v.change_class) for v in versions] == [
        (1, None),
        (2, "LITERAL_ONLY"),
        (3, "STRUCTURAL"),
    ]
    # Immutable history: the first version still holds the first definition's stored form.
    assert versions[0].body_fingerprint != versions[2].body_fingerprint
    assert "UPDATE" in (versions[0].body_sql_redacted or "")
    assert "'CLOSED'" not in (versions[0].body_sql_redacted or ""), "stored value-free"
    routine = await session.scalar(select(MetadataRoutine))
    assert routine is not None and {v.routine_id for v in versions} == {routine.id}


def test_a_package_can_be_pushed_and_its_grants_are_accepted() -> None:
    package = MetadataRoutineEnvelope(
        name="RISK_PKG", routine_type="PACKAGE", body_sql="PACKAGE RISK_PKG IS END;"
    )
    grant = MetadataGrantEnvelope(
        grantee="RISK_READER", privilege="EXECUTE", object_type="PACKAGE", object_name="RISK_PKG"
    )
    assert (package.routine_type, grant.object_type) == ("PACKAGE", "PACKAGE")


def test_a_package_is_a_selectable_kind_and_its_grants_follow_it() -> None:
    schema = DiscoveredSchema(
        name="risk",
        tables=(),
        routines=(
            DiscoveredRoutine(name="RISK_PKG", routine_type="PACKAGE"),
            DiscoveredRoutine(name="SCORE", routine_type="SCALAR_FUNCTION"),
        ),
        grants=(DiscoveredGrant("RISK_READER", "ROLE", "EXECUTE", "PACKAGE", "RISK_PKG", "risk"),),
    )
    catalogs = (DiscoveredCatalog("bank", (schema,)),)

    packages_only = apply_selection(catalogs, DiscoverySelection(object_kinds=["PACKAGE"]))
    functions_only = apply_selection(catalogs, DiscoverySelection(object_kinds=["FUNCTION"]))

    (kept,) = packages_only.catalogs[0].schemas
    assert [routine.name for routine in kept.routines] == ["RISK_PKG"]
    assert [grant.object_name for grant in kept.grants] == ["RISK_PKG"]
    (kept_functions,) = functions_only.catalogs[0].schemas
    # BigQuery's SCALAR_FUNCTION is a function, not an unknown kind every selection drops.
    assert [routine.name for routine in kept_functions.routines] == ["SCORE"]
    assert kept_functions.grants == ()


def test_package_capability_is_not_applicable_where_the_engine_has_none() -> None:
    by_connector = {
        connector: {row.kind: row for row in kind_capabilities(connector, {"routines": True})}
        for connector in ("oracle", "sqlserver")
    }
    assert by_connector["oracle"]["PACKAGE"].inventory == "SUPPORTED"
    assert by_connector["sqlserver"]["PACKAGE"].inventory == "NOT_APPLICABLE"


async def test_a_package_never_reaches_tool_generation(session: AsyncSession) -> None:
    datasource = await _datasource(session)
    await _scan(
        session,
        datasource,
        _envelope(routines=[{**_routine("PACKAGE RISK_PKG IS END;"), "routine_type": "PACKAGE"}]),
    )
    package = await session.scalar(select(MetadataRoutine))
    assert package is not None

    with pytest.raises(ProcedureNotEligibleError) as refused:
        await resolve_procedure_tool_source(
            session,
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            routine_id=package.id,
            dialect="oracle",
        )

    assert refused.value.code == "PACKAGE_NOT_CALLABLE"
