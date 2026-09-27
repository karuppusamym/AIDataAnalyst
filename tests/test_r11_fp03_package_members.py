"""R11-FP03: member-level lineage for Oracle packages.

A stored Oracle PACKAGE is its spec and its body joined (the connector reads both from
ALL_SOURCE), and it was parsed as one body: every member's reads and writes were the package's,
the spec's declarations were PARSE_ERROR markers, and the `PACKAGE BODY ... AS PROCEDURE p IS
BEGIN` header glued each member's first statement into a chunk nothing could parse. These tests
pin what replaced that:

* each member's edges are attributed to that member (`package_member`, `MEMBER`), the package's
  own code to the package (`PACKAGE_LEVEL`), and the member resolves to the captured member
  routine -- overloads told apart by their parameter names;
* a package that cannot be split keeps the whole-body parse it always had, and every edge says
  `PACKAGE_FALLBACK` with the reason on the result -- never a silent mix of the two grains.

Every behaviour here fails on the tree before R11-FP03.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    MemberAttribution,
    PackageSplitFailure,
    parse_procedure_lineage,
)
from aida.procedure_lineage_api import (
    get_routine_parse_coverage,
    parse_deep_procedure_lineage_endpoint,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_lineage_edges import resolve_package_member_ids
from tests.test_routine_parse_coverage import _context, _seed

# What the Oracle connector stores for a package: ALL_SOURCE's spec lines, then its body lines.
PACKAGE = """PACKAGE risk_pkg AS
  PROCEDURE refresh;
  FUNCTION score(p_id NUMBER) RETURN NUMBER;
  FUNCTION score(p_id NUMBER, p_as_of DATE) RETURN NUMBER;
END risk_pkg;
PACKAGE BODY risk_pkg AS
  PROCEDURE refresh IS
  BEGIN
    INSERT INTO risk.orders_audit (id, amount)
      SELECT o.id, CASE WHEN o.amount > 0 THEN o.amount ELSE 0 END FROM risk.orders o;
    FOR r IN (SELECT f.id FROM risk.flags f) LOOP
      IF r.id > 0 THEN
        UPDATE risk.flags g SET g.seen = g.seen + 1;
      END IF;
    END LOOP;
  END refresh;
  FUNCTION score(p_id NUMBER) RETURN NUMBER IS
    v NUMBER;
    FUNCTION helper RETURN NUMBER IS
    BEGIN
      RETURN 1;
    END helper;
  BEGIN
    SELECT c.score INTO v FROM risk.customers c WHERE c.id = p_id;
    RETURN v;
  END score;
  FUNCTION score(p_id NUMBER, p_as_of DATE) RETURN NUMBER IS
  BEGIN
    INSERT INTO risk.score_log (id) SELECT h.id FROM risk.history h;
    RETURN 0;
  END score;
BEGIN
  INSERT INTO risk.pkg_log (n) SELECT l.n FROM risk.loads l;
END risk_pkg;
"""


def _by_target(result, target: str):
    return [edge for edge in result.edges if edge.target_table == target]


def test_each_members_edges_are_attributed_to_that_member() -> None:
    result = parse_procedure_lineage(PACKAGE, dialect="oracle")

    assert result.member_attribution == MemberAttribution.MEMBER.value
    assert result.member_fallback_reason is None
    # The first member's first statement used to be glued to the package header and lost.
    audit = _by_target(result, "risk.orders_audit")
    assert audit and {(e.package_member, e.member_attribution) for e in audit} == {
        ("refresh", "MEMBER")
    }
    flags = _by_target(result, "risk.flags")
    assert flags and {e.package_member for e in flags} == {"refresh"}
    score_log = _by_target(result, "risk.score_log")
    assert score_log and {e.package_member for e in score_log} == {"score"}
    # The initialization block is the package's own code.
    pkg_log = _by_target(result, "risk.pkg_log")
    assert pkg_log and {(e.package_member, e.member_attribution) for e in pkg_log} == {
        (None, "PACKAGE_LEVEL")
    }
    # Every edge of a split package is at one of the two split grains; none fell back.
    assert {e.member_attribution for e in result.edges} <= {"MEMBER", "PACKAGE_LEVEL"}


def test_the_spec_and_a_nested_helper_are_no_longer_unparsed_statements() -> None:
    """A spec declares members; a member's own declaration section holds a nested helper.
    Neither is an unreadable statement, and neither ends a member early."""
    result = parse_procedure_lineage(PACKAGE, dialect="oracle")
    assert [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE] == []
    assert result.is_fully_parsed is True
    names = [member.name for member in result.package_members]
    assert names == ["refresh", "score", "score"]
    assert [member.parameter_names for member in result.package_members] == [
        (),
        ("p_id",),
        ("p_id", "p_as_of"),
    ]


def test_a_plsql_select_into_is_a_variable_not_a_table() -> None:
    """Split into members, `score` would otherwise have read as a member writing a table
    named `v` -- the wrong fact PL/pgSQL was cured of in FP-07."""
    result = parse_procedure_lineage(PACKAGE, dialect="oracle")
    assert _by_target(result, "v") == []
    local = _by_target(result, PROCEDURE_LOCAL_TARGET)
    assert local and all(not edge.is_write for edge in local)
    assert {e.package_member for e in local} == {"score"}


def test_a_package_that_cannot_be_split_says_so_on_every_edge() -> None:
    """A body the source truncated: the last member never closes. The whole text is parsed as
    it always was, and no edge claims a member."""
    truncated = PACKAGE[: PACKAGE.index("RETURN 0;")]
    result = parse_procedure_lineage(truncated, dialect="oracle")

    assert result.member_attribution == MemberAttribution.PACKAGE_FALLBACK.value
    assert result.member_fallback_reason == PackageSplitFailure.UNBALANCED_BLOCKS.value
    assert result.edges
    assert {e.member_attribution for e in result.edges} == {"PACKAGE_FALLBACK"}
    assert {e.package_member for e in result.edges} == {None}
    assert result.package_members == ()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # A spec whose body the scanning login could not see.
        (PACKAGE[: PACKAGE.index("PACKAGE BODY")], PackageSplitFailure.NO_PACKAGE_BODY),
        # A quoted member name, which the scanner steps over like a string.
        (
            PACKAGE.replace("PROCEDURE refresh IS", 'PROCEDURE "Refresh" IS'),
            PackageSplitFailure.UNREADABLE_MEMBER,
        ),
    ],
)
def test_each_reason_a_split_fails_is_named(text: str, reason: PackageSplitFailure) -> None:
    result = parse_procedure_lineage(text, dialect="oracle")
    assert result.member_attribution == MemberAttribution.PACKAGE_FALLBACK.value
    assert result.member_fallback_reason == reason.value


def test_anything_that_is_not_a_package_carries_no_member_attribution() -> None:
    result = parse_procedure_lineage(
        "CREATE PROCEDURE dbo.p AS BEGIN INSERT INTO dbo.a (x) SELECT s.x FROM dbo.s s; END",
        dialect="tsql",
    )
    assert result.member_attribution is None
    assert {(e.package_member, e.member_attribution) for e in result.edges} == {(None, None)}
    # The same text on a non-Oracle source is never read as a package.
    assert parse_procedure_lineage(PACKAGE, dialect="postgres").member_attribution is None


def test_a_standalone_routine_as_all_source_stores_it_is_read_like_a_create() -> None:
    """ALL_SOURCE keeps `PROCEDURE p IS ...` with no CREATE. The header was never stripped, so
    the declaration section and the first statement were one unparseable chunk."""
    body = (
        "PROCEDURE refresh IS\n  v NUMBER;\nBEGIN\n"
        "  INSERT INTO risk.a (x) SELECT s.x FROM risk.s s;\nEND refresh;\n"
    )
    stored = parse_procedure_lineage(body, dialect="oracle")
    created = parse_procedure_lineage("CREATE OR REPLACE " + body, dialect="oracle")
    assert stored.is_fully_parsed is True
    assert [(e.source_table, e.target_table) for e in stored.edges] == [
        (e.source_table, e.target_table) for e in created.edges
    ]


# ---------------------------------------------------------------------------
# Storage: the member is a captured routine, resolved once, overloads included.
# ---------------------------------------------------------------------------


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


def _routine(datasource, schema, name: str, **values) -> MetadataRoutine:
    defaults = {
        "id": uuid4(),
        "organization_id": datasource.organization_id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "routine_type": "PROCEDURE",
        "availability": "AVAILABLE",
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    defaults.update(values)
    return MetadataRoutine(**defaults)


def _parameter(datasource, routine: MetadataRoutine, name: str, position: int):
    return MetadataRoutineParameter(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        routine_id=routine.id,
        name=name,
        ordinal_position=position,
        physical_type="NUMBER",
        fingerprint="fp",
    )


async def _estate(session: AsyncSession):
    datasource, schema = await _seed(session)
    datasource.dialect = "oracle"
    package = _routine(
        datasource, schema, "RISK_PKG", routine_type="PACKAGE", body_sql_redacted=PACKAGE
    )
    unavailable = {"availability": "UNAVAILABLE", "body_sql_redacted": None,
                   "package_name": "RISK_PKG"}
    refresh = _routine(datasource, schema, "REFRESH", signature="()", **unavailable)
    score_one = _routine(datasource, schema, "SCORE", signature="(NUMBER)",
                         routine_type="FUNCTION", **unavailable)
    score_two = _routine(datasource, schema, "SCORE", signature="(NUMBER, DATE)",
                         routine_type="FUNCTION", **unavailable)
    session.add_all([package, refresh, score_one, score_two])
    await session.flush()
    session.add_all(
        [
            _parameter(datasource, score_one, "P_ID", 1),
            _parameter(datasource, score_two, "P_ID", 1),
            _parameter(datasource, score_two, "P_AS_OF", 2),
        ]
    )
    await session.flush()
    return datasource, package, refresh, score_one, score_two


async def test_overloaded_members_resolve_by_their_parameter_names(session) -> None:
    datasource, package, refresh, score_one, score_two = await _estate(session)
    result = parse_procedure_lineage(PACKAGE, dialect="oracle")

    resolved = await resolve_package_member_ids(
        session, datasource=datasource, package=package, result=result
    )
    by_target = {e.target_table: resolved.get(e.statement_ordinal) for e in result.edges}
    assert by_target["risk.orders_audit"] == refresh.id
    assert by_target[PROCEDURE_LOCAL_TARGET] == score_one.id
    assert by_target["risk.score_log"] == score_two.id
    # Package-level code belongs to no member.
    assert by_target["risk.pkg_log"] is None


async def test_the_parse_route_stores_member_attribution_and_the_coverage_says_how(
    session,
) -> None:
    datasource, package, refresh, _score_one, score_two = await _estate(session)

    response = await parse_deep_procedure_lineage_endpoint(
        datasource.id, package.id, _context(datasource), session
    )
    assert response.member_attribution == "MEMBER"

    rows = (await session.scalars(select(DeepProcedureLineageEdge))).all()
    # The package stays the owner: what was parsed was its body.
    assert {row.routine_id for row in rows} == {package.id}
    audit = [row for row in rows if row.target_table == "risk.orders_audit"]
    assert audit and {(r.package_member, r.member_routine_id) for r in audit} == {
        ("refresh", refresh.id)
    }
    assert {r.member_routine_id for r in rows if r.target_table == "risk.score_log"} == {
        score_two.id
    }
    coverage = await get_routine_parse_coverage(
        datasource.id, package.id, _context(datasource), session
    )
    assert (coverage.member_attribution, coverage.member_fallback_reason) == ("MEMBER", None)


async def test_a_fallback_is_recorded_on_the_coverage_row_with_its_reason(session) -> None:
    datasource, package, *_ = await _estate(session)
    package.body_sql_redacted = PACKAGE[: PACKAGE.index("RETURN 0;")]
    await session.flush()

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, package.id, _context(datasource), session
    )
    rows = (await session.scalars(select(DeepProcedureLineageEdge))).all()
    assert rows and {(r.member_attribution, r.member_routine_id) for r in rows} == {
        ("PACKAGE_FALLBACK", None)
    }
    coverage = await get_routine_parse_coverage(
        datasource.id, package.id, _context(datasource), session
    )
    assert (coverage.member_attribution, coverage.member_fallback_reason) == (
        "PACKAGE_FALLBACK",
        "UNBALANCED_BLOCKS",
    )
