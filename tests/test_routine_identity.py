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
from aida.connectors.discovery import build_routines
from aida.connectors.oracle import _envelope_routines, _OracleEnvelopeRows
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


def test_an_oracle_packages_members_are_routines_with_their_own_parameters_and_overloads() -> None:
    def argument(member: str, subprogram: int, position: int, name: str | None, data_type: str):
        return {
            "OWNER": "RETAIL",
            "OBJECT_NAME": member,
            "PACKAGE_NAME": "RISK_PKG",
            "SUBPROGRAM_ID": subprogram,
            "ARGUMENT_NAME": name,
            "POSITION": position,
            "DATA_TYPE": data_type,
            "IN_OUT": "IN" if position else "OUT",
        }

    envelope = _OracleEnvelopeRows(
        routines=(
            {"OWNER": "RETAIL", "OBJECT_NAME": "RISK_PKG", "OBJECT_TYPE": "PACKAGE"},
            {"OWNER": "RETAIL", "OBJECT_NAME": "SCORE", "OBJECT_TYPE": "FUNCTION"},
        ),
        package_members=(
            {"OWNER": "RETAIL", "OBJECT_NAME": "RISK_PKG", "PROCEDURE_NAME": "SCORE",
             "SUBPROGRAM_ID": 1, "OVERLOAD": "1"},
            {"OWNER": "RETAIL", "OBJECT_NAME": "RISK_PKG", "PROCEDURE_NAME": "SCORE",
             "SUBPROGRAM_ID": 2, "OVERLOAD": "2"},
            {"OWNER": "RETAIL", "OBJECT_NAME": "RISK_PKG", "PROCEDURE_NAME": "REFRESH",
             "SUBPROGRAM_ID": 3, "OVERLOAD": None},
        ),
        arguments=(
            argument("SCORE", 1, 0, None, "NUMBER"),
            argument("SCORE", 1, 1, "P_ID", "NUMBER"),
            argument("SCORE", 2, 0, None, "NUMBER"),
            argument("SCORE", 2, 1, "P_CODE", "VARCHAR2"),
        ),
    )

    routines = _envelope_routines(envelope)["RETAIL"]

    members = [r for r in routines if r.attributes.get("package_name") == "RISK_PKG"]
    assert sorted((m.name, m.routine_type, m.attributes.get("overload")) for m in members) == [
        ("REFRESH", "PROCEDURE", None),
        ("SCORE", "FUNCTION", "1"),
        ("SCORE", "FUNCTION", "2"),
    ]
    by_overload = {m.attributes.get("overload"): m for m in members}
    assert [p.physical_type for p in by_overload["1"].parameters] == ["NUMBER"]
    assert [p.physical_type for p in by_overload["2"].parameters] == ["VARCHAR2"]
    assert all(m.body_sql is None and "RISK_PKG" in (m.unavailable_reason or "") for m in members)
    # The standalone SCORE is untouched and carries no package.
    standalone = [r for r in routines if r.name == "SCORE" and "package_name" not in r.attributes]
    assert len(standalone) == 1


async def test_a_member_and_a_standalone_routine_of_the_same_name_are_two_identities(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    standalone = {**_routine(BODY), "name": "score"}
    member = {
        **_routine(BODY),
        "name": "score",
        "body_sql": None,
        "unavailable_reason": "a member subprogram of package risk_pkg",
        "attributes": {"package_name": "risk_pkg"},
    }

    await _scan(session, datasource, _envelope(routines=[standalone, member]))

    rows = list(
        await session.scalars(select(MetadataRoutine).order_by(MetadataRoutine.package_name))
    )
    assert [(row.name, row.package_name, row.availability) for row in rows] == [
        ("score", "", "AVAILABLE"),
        ("score", "risk_pkg", "UNAVAILABLE"),
    ]


def test_native_function_kinds_become_functions_with_a_subtype() -> None:
    routines = build_routines(
        [
            {"routine_schema": "s", "routine_name": "fx", "routine_type": "SCALAR_FUNCTION"},
            {
                "routine_schema": "s",
                "routine_name": "tvf",
                "routine_type": "FUNCTION",
                "native_subtype": "INLINE_TABLE",
            },
            {"routine_schema": "s", "routine_name": "p", "routine_type": "PROCEDURE"},
        ]
    )["s"]

    by_name = {routine.name: routine for routine in routines}
    assert (by_name["fx"].routine_type, by_name["fx"].attributes) == (
        "FUNCTION",
        {"native_subtype": "SCALAR_FUNCTION"},
    )
    assert by_name["tvf"].attributes == {"native_subtype": "INLINE_TABLE"}
    assert by_name["p"].attributes == {}


async def test_the_native_subtype_is_stored_beside_the_portable_type(session: AsyncSession) -> None:
    datasource = await _datasource(session)
    await _scan(
        session,
        datasource,
        _envelope(
            routines=[
                {**_routine(BODY), "routine_type": "FUNCTION",
                 "attributes": {"native_subtype": "INLINE_TABLE"}}
            ]
        ),
    )

    routine = await session.scalar(select(MetadataRoutine))
    assert routine is not None
    assert (routine.routine_type, routine.native_subtype) == ("FUNCTION", "INLINE_TABLE")


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
