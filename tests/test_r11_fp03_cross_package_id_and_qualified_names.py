"""R11-FP03's remaining clause, two parts: the callee's id on a cross-package splice, and the
schema in a three-part call.

* **`via_routine_id` for `other_pkg.member(...)`.** A spliced edge carries the callee's own
  captured routine id (`via_routine_id`, migration `53558182d9fb`). A cross-routine call has
  had it since that slice, and an in-package sibling call through a locator; a cross-package
  member call had only the display text, `via_routine`, because the resolver read the member
  out of the other package's parse and never looked up the captured routine row the member is.
  It is that row -- the one ACTIVE routine captured under the package's name, in the package's
  own schema, with the member's name -- and NULL when there is not exactly one: an overload
  the catalog holds twice, or a member nothing captured, is never a guess.
* **`schema.pkg.member`.** Only the last two dotted parts of a called name were read, so
  `hr.util.log(1)` was looked up as package `util`, member `log` *in the caller's own schema*.
  Where the caller's schema also has a package `util` -- an ordinary thing for a utility
  package to be called -- its member's lineage was recorded for the call: a wrong edge, stated
  as a fact. An Oracle name of three parts is `schema.package.member`, so the schema is read.

Every test here fails on the tree before this change except those marked *guard*.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataSchema
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import descend_routine_calls
from tests.test_routine_parse_coverage import _context, _seed
from tests.test_routine_parse_coverage import session as pkg_db_session  # noqa: F401


def _real(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


def _gaps(result: ProcedureParseResult) -> list[str]:
    return [
        e.unparsed_reason or ""
        for e in result.edges
        if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


async def _routine(
    db: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    body: str | None = None,
    **overrides: object,
) -> MetadataRoutine:
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": datasource.organization_id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "routine_type": "PROCEDURE",
        "package_name": "",
        "body_sql_redacted": body,
        "body_fingerprint": uuid4().hex if body else None,
        "redaction_status": "PARSED",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE" if body else "UNAVAILABLE",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    routine = MetadataRoutine(**values)  # type: ignore[arg-type]
    db.add(routine)
    await db.flush()
    return routine


async def _schema(db: AsyncSession, template: MetadataSchema, name: str) -> MetadataSchema:
    """A second schema in the same catalog."""
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=template.organization_id,
        catalog_id=template.catalog_id,
        name=name,
        fingerprint="fp",
    )
    db.add(schema)
    await db.flush()
    return schema


def _package(name: str, member: str, target: str, source: str) -> str:
    # A package body handed to the parser, never executed.
    return (
        f"PACKAGE BODY {name} AS\n"  # noqa: S608
        f"  PROCEDURE {member}(p_id NUMBER) IS\n"
        "  BEGIN\n"
        f"    INSERT INTO {target} (id) SELECT s.id FROM {source} s;\n"
        f"  END {member};\n"
        f"END {name};\n"
    )


def _caller(name: str, call: str) -> str:
    return (
        f"PACKAGE BODY {name} AS\n"
        "  PROCEDURE run IS\n"
        "  BEGIN\n"
        f"    {call};\n"
        "  END run;\n"
        f"END {name};\n"
    )


async def _descend(
    db: AsyncSession, datasource: DataSource, caller: MetadataRoutine
) -> ProcedureParseResult:
    assert caller.body_sql_redacted is not None
    return await descend_routine_calls(
        db,
        datasource,
        caller,
        parse_procedure_lineage(caller.body_sql_redacted, dialect=datasource.dialect),
    )


def _targets(result: ProcedureParseResult) -> set[str]:
    return {e.target_table for e in _real(result) if e.via_routine is not None}


# ---------------------------------------------------------------------------
# 1. The member's own captured id on a cross-package splice.
# ---------------------------------------------------------------------------


async def test_a_cross_package_splice_carries_the_members_captured_routine_id(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    helper = await _routine(
        pkg_db_session, datasource, schema, name="helper", package_name="pkg2"
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    spliced = [e for e in _real(result) if e.via_routine is not None]
    assert spliced and {e.target_table for e in spliced} == {"ops.out2"}
    assert {e.via_routine_id for e in spliced} == {helper.id}
    assert result.is_fully_parsed


async def test_the_id_is_the_named_packages_member_when_another_package_shares_the_name(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """`pkg3` has a captured `helper` too. A lookup by member name alone could not tell them
    apart; the package's own name and schema can."""
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    other = await _routine(
        pkg_db_session, datasource, schema, name="helper", package_name="pkg3"
    )
    wanted = await _routine(
        pkg_db_session, datasource, schema, name="helper", package_name="pkg2"
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    ids = {e.via_routine_id for e in _real(result) if e.via_routine is not None}
    assert ids == {wanted.id}
    assert other.id not in ids


async def test_a_member_nothing_captured_as_a_routine_gets_no_id_and_keeps_its_edges(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """The lineage is read from the package's own parse; the row is a separate fact. Absent, it
    is NULL -- never the package's id, the caller's, or another member's."""
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    spliced = [e for e in _real(result) if e.via_routine is not None]
    assert {e.target_table for e in spliced} == {"ops.out2"}
    assert {e.via_routine for e in spliced} == {"dbo.pkg2.helper"}
    assert {e.via_routine_id for e in spliced} == {None}


async def test_two_captured_rows_for_one_member_name_get_no_id(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """guard: the body has one `helper`, the catalog two. Either could be the routine the call
    reaches, so neither is named."""
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    for signature in ("(p_id NUMBER)", "(p_id NUMBER, p_note VARCHAR2)"):
        await _routine(
            pkg_db_session, datasource, schema, name="helper", package_name="pkg2",
            signature=signature,
        )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    assert {e.via_routine_id for e in _real(result) if e.via_routine is not None} == {None}


async def test_a_retired_member_row_is_not_the_callee(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    await _routine(
        pkg_db_session, datasource, schema, name="helper", package_name="pkg2", status="RETIRED"
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    assert {e.via_routine_id for e in _real(result) if e.via_routine is not None} == {None}


async def test_the_stored_row_carries_the_members_id(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    await _routine(
        pkg_db_session, datasource, schema, name="pkg2", routine_type="PACKAGE",
        body=_package("pkg2", "helper", "ops.out2", "ops.src2"),
    )
    helper = await _routine(
        pkg_db_session, datasource, schema, name="helper", package_name="pkg2"
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="pkg1", routine_type="PACKAGE",
        body=_caller("pkg1", "pkg2.helper(1)"),
    )

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, caller.id, _context(datasource), pkg_db_session
    )

    rows = (
        await pkg_db_session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == caller.id,
                DeepProcedureLineageEdge.transformation_type != UNPARSED_TRANSFORMATION_TYPE,
            )
        )
    ).all()
    assert rows and {row.via_routine_id for row in rows} == {helper.id}
    # The caller is who made the call, and stays the edge's owner.
    assert {row.routine_id for row in rows} == {caller.id}


# ---------------------------------------------------------------------------
# 3. Descent's last hop pass runs over a whole package at once.
# ---------------------------------------------------------------------------


def test_the_package_wide_hop_pass_never_joins_one_members_record_to_anothers() -> None:
    """The tracker's remaining clause: descent's final `propagate_intermediate_hops` runs across
    every member's edges, not member by member, so an intermediate two members both named would
    join one's source to the other's write. This is the case that could: each member has a
    record `r`, fetched from its own cursor, and one member calls out so that descent runs.

    It cannot be made to. The walk numbers statements once across a package, and a record's
    intermediate is named by its ordinal (`<LOCAL:r@2>`, `<LOCAL:r@7>`), so two members' records
    are two names; a called routine's records take the callee's name. The package-wide pass
    finds nothing shared to join. Pinned here so that a change which let members share a name
    (a per-member counter, an unscoped fetched-record table) fails here, not in a review queue."""
    from aida import routine_call_descent
    from aida.routine_call_descent import Callee, descend_nested_calls

    package = """PACKAGE BODY ops_pkg AS
  CURSOR c_a IS SELECT o.id FROM ops.orders o;
  CURSOR c_b IS SELECT k.id FROM ops.customers k;
  PROCEDURE a_member IS
    r c_a%ROWTYPE;
  BEGIN
    OPEN c_a;
    FETCH c_a INTO r;
    INSERT INTO ops.log_a (id) VALUES (r.id);
    ext_call(1);
  END a_member;
  PROCEDURE b_member IS
    r c_b%ROWTYPE;
  BEGIN
    OPEN c_b;
    FETCH c_b INTO r;
    INSERT INTO ops.log_b (id) VALUES (r.id);
  END b_member;
END ops_pkg;
"""
    external = (
        "PROCEDURE ext_call(p_id NUMBER) IS BEGIN "
        "INSERT INTO ops.ext_out (id) SELECT e.id FROM ops.ext_src e; END ext_call;"
    )

    def resolve(name: str) -> Callee:
        if name.lower() == "ext_call":
            return Callee("ext", "ext_call", external)
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    root = parse_procedure_lineage(package, dialect="oracle")
    result = descend_nested_calls(root, dialect="oracle", resolve=resolve, root_key="ops_pkg")

    assert result.is_fully_parsed, _gaps(result)
    flows = {
        (e.source_table, e.target_table)
        for e in _real(result)
        if e.is_write and e.via_temp_table is not None and e.via_temp_table.startswith("<LOCAL:")
    }
    assert flows == {("ops.orders", "ops.log_a"), ("ops.customers", "ops.log_b")}
    # The same record name in both members really is two intermediates.
    fetched = {e.target_table for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"}
    assert len(fetched) == 2


# ---------------------------------------------------------------------------
# 2. schema.pkg.member names its schema.
# ---------------------------------------------------------------------------


async def _two_schema_estate(
    db: AsyncSession, call: str
) -> tuple[DataSource, MetadataRoutine, dict[str, UUID]]:
    """`dbo.util` and `hr.util`, each with a `log` that writes somewhere of its own, and a
    caller in `dbo` making `call`."""
    datasource, dbo = await _seed(db)
    datasource.dialect = "oracle"
    hr = await _schema(db, dbo, "hr")
    dbo_util = await _routine(
        db, datasource, dbo, name="util", routine_type="PACKAGE",
        body=_package("util", "log", "dbo.local_log", "dbo.src_local"),
    )
    hr_util = await _routine(
        db, datasource, hr, name="util", routine_type="PACKAGE",
        body=_package("util", "log", "hr.audit", "hr.src_audit"),
    )
    caller = await _routine(
        db, datasource, dbo, name="caller_pkg", routine_type="PACKAGE",
        body=_caller("caller_pkg", call),
    )
    return datasource, caller, {"dbo": dbo_util.id, "hr": hr_util.id}


async def test_a_schema_qualified_package_call_reads_that_schemas_package(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """The wrong-edge case: `hr.util.log` was read as `util.log` in the caller's schema, so the
    caller was recorded as writing `dbo.local_log` -- which it never touches."""
    datasource, caller, _ = await _two_schema_estate(pkg_db_session, "hr.util.log(1)")

    result = await _descend(pkg_db_session, datasource, caller)

    assert _targets(result) == {"hr.audit"}
    assert {e.via_routine for e in _real(result) if e.via_routine} == {"hr.util.log"}
    assert result.is_fully_parsed


async def test_a_call_qualified_with_the_callers_own_schema_reads_the_callers_package(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """guard: this one resolved before, by the accident of the schema part being ignored."""
    datasource, caller, _ = await _two_schema_estate(pkg_db_session, "dbo.util.log(1)")

    result = await _descend(pkg_db_session, datasource, caller)

    assert _targets(result) == {"dbo.local_log"}
    assert {e.via_routine for e in _real(result) if e.via_routine} == {"dbo.util.log"}


async def test_a_call_into_a_schema_nothing_captured_is_not_captured(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """Not the caller's own `util.log` under another name."""
    datasource, caller, _ = await _two_schema_estate(pkg_db_session, "nope.util.log(1)")

    result = await _descend(pkg_db_session, datasource, caller)

    assert _targets(result) == set()
    assert "NESTED_PROCEDURE_CALL: nope.util.log (NOT_CAPTURED)" in _gaps(result)
    assert not result.is_fully_parsed


async def test_a_two_part_package_call_still_reads_the_callers_schema(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """guard: `util.log(1)` names no schema, so it is the caller's."""
    datasource, caller, _ = await _two_schema_estate(pkg_db_session, "util.log(1)")

    result = await _descend(pkg_db_session, datasource, caller)

    assert _targets(result) == {"dbo.local_log"}


async def test_a_schema_qualified_call_carries_the_members_id_too(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, dbo = await _seed(pkg_db_session)
    datasource.dialect = "oracle"
    hr = await _schema(pkg_db_session, dbo, "hr")
    await _routine(
        pkg_db_session, datasource, hr, name="util", routine_type="PACKAGE",
        body=_package("util", "log", "hr.audit", "hr.src_audit"),
    )
    hr_log = await _routine(
        pkg_db_session, datasource, hr, name="log", package_name="util"
    )
    # A `log` under a package of the same name in the caller's schema is not the callee.
    await _routine(pkg_db_session, datasource, dbo, name="log", package_name="util")
    caller = await _routine(
        pkg_db_session, datasource, dbo, name="caller_pkg", routine_type="PACKAGE",
        body=_caller("caller_pkg", "hr.util.log(1)"),
    )

    result = await _descend(pkg_db_session, datasource, caller)

    assert {e.via_routine_id for e in _real(result) if e.via_routine} == {hr_log.id}


async def test_a_three_part_sql_server_name_still_reads_its_last_two_parts(
    pkg_db_session: AsyncSession,  # noqa: F811
) -> None:
    """guard: `db.schema.proc` is not `schema.package.member`. Only Oracle names three parts
    that way; here the database is the datasource's own, and was always ignored."""
    datasource, schema = await _seed(pkg_db_session)
    await _routine(
        pkg_db_session, datasource, schema, name="refresh_totals",
        body=(
            "CREATE PROCEDURE dbo.refresh_totals AS BEGIN "
            "INSERT INTO dbo.totals (id) SELECT c.id FROM dbo.customers c; END"
        ),
    )
    caller = await _routine(
        pkg_db_session, datasource, schema, name="nightly",
        body="CREATE PROCEDURE dbo.nightly AS BEGIN EXEC bank.dbo.refresh_totals; END",
    )

    result = await _descend(pkg_db_session, datasource, caller)

    assert _targets(result) == {"dbo.totals"}
    assert result.is_fully_parsed
