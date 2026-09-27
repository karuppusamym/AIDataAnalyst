"""R11-B19: a classification propagates through a captured stored procedure.

The collector read view edges, the legacy pasted-SQL procedure table (unwritten
since R11-X5), OpenLineage and -- since R11-FP01 -- trigger edges. It never read
`deep_procedure_lineage_edge`, where every captured routine's lineage lands. So a
PII column a stored procedure copied into another table did not mark the column
it was copied to: the classification stopped at the procedure, the target read as
unclassified, and every masking and authorization control keyed on classification
treated it as ordinary data.

Every row these tests propagate over is written the way production writes it: the
routine body goes through the real parser (`parse_procedure_lineage`, and
`descend_routine_calls` for a nested call) and the real writer
(`persist_routine_edges`), against a catalog seeded with the tables the body names.
Nothing hand-builds an edge except the INV-5 test, whose whole point is a row no
parse of this source would write.

Each test fails against the pre-change collector unless its docstring says it is a
guard. The rules are the trigger path's, unchanged: only ACTIVE edges move anything,
only a write, only ever a raise, and a reviewed edge that cannot be resolved to
columns is a declared gap with a reason code, never a table-grain tag. What routine
bodies add is routine-local state, and a hop through it is followed, never landed on.
"""

from __future__ import annotations

import json
from dataclasses import astuple
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from aida.classification_propagation import (
    GAP_INTERMEDIATE_NOT_CARRIED,
    GAP_TABLE_STAR,
    collect_propagation_inputs,
    propagate_for_datasource,
)
from aida.envelope_models import MetadataRoutine
from aida.ingestion import _store_source_sql
from aida.models import (
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    ViewLineageEdge,
)
from aida.procedure_lineage import parse_procedure_lineage
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import descend_routine_calls
from aida.routine_lineage_edges import persist_routine_edges, require_eligible_routine_body
from tests.support.task_agents import seed_estate, seed_table, task_agent_session
from tests.test_inv6_value_freedom import (
    SENTINEL_CUSTOMER,
    SENTINEL_LITERAL,
    SENTINEL_ROW_VALUE,
)

_SENTINELS = (SENTINEL_LITERAL, SENTINEL_ROW_VALUE, SENTINEL_CUSTOMER)

#: The simplest governance-relevant procedure: a PII column copied to another table.
COPY_BODY = """CREATE PROCEDURE public.copy_customers AS
BEGIN
    INSERT INTO public.customer_copy (ssn, email)
    SELECT c.ssn, c.email FROM public.customers c;
END;
"""

#: Through a `#temp` whose stored name -- sqlglot drops the `#` -- is also the name
#: of a real catalog table, `public.staging`, so the parse's table resolution binds
#: the temp to that table on both sides of the hop.
STAGING_BODY = """CREATE PROCEDURE public.stage_customers AS
BEGIN
    SELECT c.ssn, c.region INTO #staging FROM public.customers c;
    INSERT INTO public.customer_copy (ssn, region)
    SELECT s.ssn, s.region FROM #staging s;
END;
"""

#: Two temp hops: `customers -> #a -> #b -> customer_copy`.
CHAIN_BODY = """CREATE PROCEDURE public.chain_customers AS
BEGIN
    SELECT c.ssn INTO #a FROM public.customers c;
    SELECT a.ssn INTO #b FROM #a a;
    INSERT INTO public.customer_copy (ssn) SELECT b.ssn FROM #b b;
END;
"""

#: A temp filled by `SELECT *`: the body never names the column it later reads.
STAR_FILL_BODY = """CREATE PROCEDURE public.snapshot_customers AS
BEGIN
    SELECT * INTO #snap FROM public.customers;
    INSERT INTO public.customer_copy (ssn) SELECT s.ssn FROM #snap s;
END;
"""

#: A temp whose `ssn` is filled by dynamic SQL the parser cannot read, while its
#: `customer_id` is filled by a statement it can.
PARTIAL_FILL_BODY = """CREATE PROCEDURE public.rebuild_copy AS
BEGIN
    CREATE TABLE #work (customer_id int, ssn varchar(20));
    INSERT INTO #work (customer_id) SELECT c.customer_id FROM public.customers c;
    EXEC sp_executesql @fill;
    INSERT INTO public.customer_copy (customer_id, ssn)
    SELECT w.customer_id, w.ssn FROM #work w;
END;
"""

#: A caller whose own body is only a call; the callee does the copy.
CALLER_BODY = """CREATE PROCEDURE public.nightly AS
BEGIN
    EXEC public.copy_customers;
END;
"""


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _routine(
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    body: str,
    **overrides: Any,
) -> MetadataRoutine:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "signature": "()",
        "routine_type": "PROCEDURE",
        "language": "SQL",
        "body_sql_redacted": body,
        "body_fingerprint": "rf" * 32,
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    return MetadataRoutine(**values)


async def _column(
    session: AsyncSession,
    table: MetadataTable,
    name: str,
    *,
    classification: str = "UNCLASSIFIED",
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=1,
        physical_type="text",
        nullable=True,
        classification=classification,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


async def _estate(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataSchema, MetadataTable, MetadataTable]:
    """A T-SQL source with `customers` (the origin) and `customer_copy` (the target)."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    customers = await seed_table(session, org, datasource, schema, name="customers")
    copy = await seed_table(session, org, datasource, schema, name="customer_copy")
    return org, datasource, schema, customers, copy


async def _captured(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    body: str,
    descend: bool = False,
) -> tuple[MetadataRoutine, list[DeepProcedureLineageEdge]]:
    """Capture a routine, parse its stored body and write its edges as a person's
    parse does (`auto_active`: every edge ACTIVE). Returns the rows written."""
    routine = _routine(org, datasource, schema, name=name, body=body)
    session.add(routine)
    await session.flush()
    result = parse_procedure_lineage(
        require_eligible_routine_body(routine), dialect=datasource.dialect
    )
    if descend:
        result = await descend_routine_calls(session, datasource, routine, result)
    await persist_routine_edges(
        session,
        datasource=datasource,
        routine=routine,
        result=result,
        review_mode="auto_active",
        threshold=1.0,
        created_by="steward-1",
    )
    await session.flush()
    rows = (
        await session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == routine.id
            )
        )
    ).all()
    return routine, list(rows)


async def _propagate(session: AsyncSession, datasource: DataSource) -> list[Any]:
    written = await propagate_for_datasource(
        session,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        created_by="scheduler",
    )
    await session.flush()
    return written


async def _inputs(session: AsyncSession, datasource: DataSource, **kwargs: Any) -> Any:
    return await collect_propagation_inputs(
        session,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        **kwargs,
    )


def _edge(
    rows: list[DeepProcedureLineageEdge], source_column: str, target_column: str, **match: Any
) -> DeepProcedureLineageEdge:
    [row] = [
        row
        for row in rows
        if (row.source_column, row.target_column) == (source_column, target_column)
        and all(getattr(row, key) == value for key, value in match.items())
    ]
    return row


# --------------------------------------------------------------------------- #
# 1. A reviewed procedure write carries the classification; nothing else does
# --------------------------------------------------------------------------- #


async def test_a_reviewed_procedure_write_carries_a_classification_to_what_it_writes(
    session: AsyncSession,
) -> None:
    """Fails before: the collector never read captured-routine lineage, so the PII
    column a procedure copies derived nothing downstream. The asserted value on the
    target is untouched -- the derived value waits for review."""
    org, datasource, schema, customers, copy = await _estate(session)
    origin = await _column(session, customers, "ssn", classification="PII")
    await _column(session, customers, "email")
    copied = await _column(session, copy, "ssn")
    await _column(session, copy, "email")
    _routine_row, rows = await _captured(
        session, org, datasource, schema, name="copy_customers", body=COPY_BODY
    )

    written = await _propagate(session, datasource)

    assert [(row.column_id, row.classification) for row in written] == [(copied.id, "PII")]
    assert written[0].origin_column_id == origin.id
    [link] = written[0].edge_chain
    assert link["kind"] == "VIEW_DDL"
    assert link["edge_ref"] == str(_edge(rows, "ssn", "ssn").id)
    refreshed = await session.get(MetadataColumn, copied.id)
    assert refreshed is not None and refreshed.classification == "UNCLASSIFIED"


@pytest.mark.parametrize("review_status", ["PROPOSED", "REJECTED", "SUPERSEDED"])
async def test_only_an_approved_procedure_edge_moves_a_classification(
    session: AsyncSession, review_status: str
) -> None:
    """One procedure writes two PII columns. The edge a person approved carries its
    classification; the other -- the agent's proposal, a rejected edge, or one a
    later parse retired -- carries nothing. Fails before: the approved half derived
    nothing either."""
    org, datasource, schema, customers, copy = await _estate(session)
    await _column(session, customers, "ssn", classification="PII")
    await _column(session, customers, "email", classification="PII")
    approved_target = await _column(session, copy, "ssn")
    held_target = await _column(session, copy, "email")
    _routine_row, rows = await _captured(
        session, org, datasource, schema, name="copy_customers", body=COPY_BODY
    )
    await session.execute(
        update(DeepProcedureLineageEdge)
        .where(DeepProcedureLineageEdge.id == _edge(rows, "email", "email").id)
        .values(review_status=review_status)
    )

    written = await _propagate(session, datasource)

    assert [row.column_id for row in written] == [approved_target.id]
    assert held_target.id not in {row.column_id for row in written}


async def test_propagation_across_a_procedure_is_a_floor_never_a_replacement(
    session: AsyncSession,
) -> None:
    """Raise-only, across a procedure edge.

    * `ssn` on the target is already asserted PHI; the procedure copies PII into it
      -- nothing is derived and the asserted value is untouched.
    * `email` is reached by a PHI procedure path and a PII view path -- it derives
      PHI, the more restrictive. Fails before: only the view was read, so PII.
    """
    org, datasource, schema, customers, copy = await _estate(session)
    await _column(session, customers, "ssn", classification="PII")
    procedure_origin = await _column(session, customers, "email", classification="PHI")
    already_phi = await _column(session, copy, "ssn", classification="PHI")
    email = await _column(session, copy, "email")
    contacts = await seed_table(session, org, datasource, schema, name="contacts")
    await _column(session, contacts, "email", classification="PII")
    session.add(
        ViewLineageEdge(
            organization_id=org.id,
            datasource_id=datasource.id,
            source_table="public.contacts",
            source_column="email",
            target_table="public.customer_copy",
            target_column="email",
            source_table_id=contacts.id,
            target_table_id=copy.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="tsql",
            sql_hash="h" * 64,
            review_status="ACTIVE",
        )
    )
    await _captured(session, org, datasource, schema, name="copy_customers", body=COPY_BODY)

    written = {row.column_id: row for row in await _propagate(session, datasource)}

    assert set(written) == {email.id}
    assert written[email.id].classification == "PHI"
    assert written[email.id].origin_column_id == procedure_origin.id
    refreshed = await session.get(MetadataColumn, already_phi.id)
    assert refreshed is not None and refreshed.classification == "PHI"


# --------------------------------------------------------------------------- #
# 2. Routine-local state: followed through, never landed on
# --------------------------------------------------------------------------- #


async def test_a_temp_table_is_followed_through_never_tagged_or_read_as_its_namesake(
    session: AsyncSession,
) -> None:
    """`customers.ssn -> #staging.ssn -> customer_copy.ssn`. The parser stores the
    temp as `staging`, which is also a real catalog table here, so both hops were
    bound to it. Collecting either as an ordinary edge is a defect:

    * the fill would tag the real `staging.ssn` PII (a table the procedure never
      touches -- its temp does not outlive the call);
    * the hop would carry the real `staging.region`'s PHI into `customer_copy`.

    Only the transitive edge the parser synthesised across the hop moves anything,
    and the carried hops are not declared gaps. Fails before: nothing was derived.
    """
    org, datasource, schema, customers, copy = await _estate(session)
    origin = await _column(session, customers, "ssn", classification="PII")
    await _column(session, customers, "region")
    namesake = await seed_table(session, org, datasource, schema, name="staging")
    namesake_ssn = await _column(session, namesake, "ssn")
    await _column(session, namesake, "region", classification="PHI")
    copied_ssn = await _column(session, copy, "ssn")
    copied_region = await _column(session, copy, "region")
    _routine_row, rows = await _captured(
        session, org, datasource, schema, name="stage_customers", body=STAGING_BODY
    )
    # The collision is real, not hypothetical: the stored hop names the namesake.
    hop = _edge(rows, "region", "region", via_temp_table=None, is_intermediate=False)
    assert hop.source_table_id == namesake.id
    fill = _edge(rows, "ssn", "ssn", is_intermediate=True)
    assert fill.target_table_id == namesake.id
    transitive = _edge(rows, "ssn", "ssn", via_temp_table="staging")

    inputs = await _inputs(session, datasource)
    written = {row.column_id: row for row in await _propagate(session, datasource)}

    assert set(written) == {copied_ssn.id}
    assert written[copied_ssn.id].classification == "PII"
    assert written[copied_ssn.id].origin_column_id == origin.id
    assert [link["edge_ref"] for link in written[copied_ssn.id].edge_chain] == [
        str(transitive.id)
    ]
    assert namesake_ssn.id not in written and copied_region.id not in written
    assert inputs.gaps == ()


async def test_a_chain_of_temps_is_followed_to_the_table_it_ends_in(
    session: AsyncSession,
) -> None:
    """Two hops. The parser synthesises `customers.ssn -> customer_copy.ssn` and,
    on the way, `#a.ssn -> customer_copy.ssn`: a transitive edge whose own source is
    an intermediate. That one is a hop too -- carried, not an unresolved source.
    Fails before: nothing was derived."""
    org, datasource, schema, customers, copy = await _estate(session)
    origin = await _column(session, customers, "ssn", classification="PII")
    copied = await _column(session, copy, "ssn")
    _routine_row, rows = await _captured(
        session, org, datasource, schema, name="chain_customers", body=CHAIN_BODY
    )
    assert _edge(rows, "ssn", "ssn", source_table="a", via_temp_table="b").source_table_id is None

    inputs = await _inputs(session, datasource)
    written = await _propagate(session, datasource)

    assert [(row.column_id, row.origin_column_id) for row in written] == [
        (copied.id, origin.id)
    ]
    assert inputs.gaps == ()


async def test_a_temp_filled_by_select_star_is_a_declared_gap_never_a_table_grain_tag(
    session: AsyncSession,
) -> None:
    """`SELECT * INTO #snap`, then `#snap.ssn` copied out: the body never names the
    column the PII arrives in, so the parser carries nothing across the hop. That is
    the star's gap -- declared with ids and a reason code, logged, and never folded
    in at table grain. Fails before: no gap was recorded; the edge was never read."""
    org, datasource, schema, customers, copy = await _estate(session)
    await _column(session, customers, "ssn", classification="PII")
    await _column(session, copy, "ssn")
    routine, rows = await _captured(
        session, org, datasource, schema, name="snapshot_customers", body=STAR_FILL_BODY
    )
    hop = _edge(rows, "ssn", "ssn")

    inputs = await _inputs(session, datasource)
    with capture_logs() as logs:
        written = await _propagate(session, datasource)

    assert written == []
    assert [astuple(gap) for gap in inputs.gaps] == [
        ("PROCEDURE_DEFINITION", str(hop.id), str(routine.id), str(copy.id), GAP_TABLE_STAR)
    ]
    [event] = [log for log in logs if log["event"] == "classification_propagation_gaps"]
    assert event["reasons"] == {GAP_TABLE_STAR: 1}
    assert event["edge_sources"] == {"PROCEDURE_DEFINITION": 1}
    assert event["edge_refs"] == [str(hop.id)]


async def test_a_hop_the_parse_could_not_carry_is_a_declared_gap(
    session: AsyncSession,
) -> None:
    """`#work.customer_id` is filled by a statement the parser reads, `#work.ssn` by
    dynamic SQL it cannot. The `customer_id` hop is carried; the `ssn` hop reads an
    intermediate nothing was carried through -- not an unresolved source (the name
    is bound, to the body's own temp) but `INTERMEDIATE_NOT_CARRIED`, pointing at
    the fill. Fails before: the edge was never read, so no gap."""
    org, datasource, schema, customers, copy = await _estate(session)
    await _column(session, customers, "customer_id", classification="PII")
    await _column(session, customers, "ssn", classification="PII")
    copied_id = await _column(session, copy, "customer_id")
    await _column(session, copy, "ssn")
    routine, rows = await _captured(
        session, org, datasource, schema, name="rebuild_copy", body=PARTIAL_FILL_BODY
    )
    uncarried = _edge(rows, "ssn", "ssn")

    inputs = await _inputs(session, datasource)
    written = await _propagate(session, datasource)

    assert [row.column_id for row in written] == [copied_id.id]
    assert [(gap.edge_ref, gap.owner_ref, gap.reason) for gap in inputs.gaps] == [
        (str(uncarried.id), str(routine.id), GAP_INTERMEDIATE_NOT_CARRIED)
    ]


# --------------------------------------------------------------------------- #
# 3. A nested call is real lineage
# --------------------------------------------------------------------------- #


async def test_a_called_routines_write_propagates_through_the_caller(
    session: AsyncSession,
) -> None:
    """`nightly` only `EXEC`s `copy_customers`; the copy happens in the callee. The
    call is read through (`routine_call_descent`), so the caller's reviewed edges
    carry `via_routine` -- and only those exist here: the callee is captured but not
    parsed in its own right. The PII still reaches the copy. Fails before."""
    org, datasource, schema, customers, copy = await _estate(session)
    origin = await _column(session, customers, "ssn", classification="PII")
    await _column(session, customers, "email")
    copied = await _column(session, copy, "ssn")
    await _column(session, copy, "email")
    session.add(_routine(org, datasource, schema, name="copy_customers", body=COPY_BODY))
    await session.flush()
    _caller, rows = await _captured(
        session, org, datasource, schema, name="nightly", body=CALLER_BODY, descend=True
    )
    through_call = _edge(rows, "ssn", "ssn")
    assert (through_call.via_routine or "").lower() == "public.copy_customers"
    assert (
        await session.scalar(
            select(DeepProcedureLineageEdge.id).where(
                DeepProcedureLineageEdge.routine_id != through_call.routine_id
            )
        )
    ) is None, "the callee's own parse must not be what carries it"

    written = await _propagate(session, datasource)

    assert [(row.column_id, row.origin_column_id) for row in written] == [
        (copied.id, origin.id)
    ]
    assert written[0].edge_chain[0]["edge_ref"] == str(through_call.id)


# --------------------------------------------------------------------------- #
# 4. Bound, tenant isolation, value freedom
# --------------------------------------------------------------------------- #


async def test_routine_edges_are_bounded_and_a_carried_hop_costs_no_budget(
    session: AsyncSession,
) -> None:
    """The staging body stores two transitive writes and two carried hops. With a
    budget of two, both transitive edges are collected and nothing is reported
    truncated -- a hop the pass does not collect does not spend what it cannot use.
    With a budget of one, truncation is said. Fails before: nothing was collected."""
    org, datasource, schema, customers, copy = await _estate(session)
    for name in ("ssn", "region"):
        await _column(session, customers, name, classification="PII")
        await _column(session, copy, name)
    await _captured(session, org, datasource, schema, name="stage_customers", body=STAGING_BODY)

    exact = await _inputs(session, datasource, max_edges=2)
    short = await _inputs(session, datasource, max_edges=1)

    assert (len(exact.edges), exact.truncated) == (2, False)
    assert (len(short.edges), short.truncated) == (1, True)


async def test_another_sources_routine_rows_never_steer_propagation_here(
    session: AsyncSession,
) -> None:
    """INV-5, twice. `load_staging` copies a *real* table, `staging`, into the copy.

    * A row filed under another datasource of the same organization, naming this
      source's tables, is not this source's lineage: it derives nothing.
    * A row filed under another datasource that names *this* routine and claims it
      filled a `staging` intermediate must not turn this routine's real read into a
      "carried hop" and silently drop it -- the intermediate check restates the
      organization and the datasource too.

    Fails before: the real copy derived nothing at all."""
    org, datasource, schema, customers, copy = await _estate(session)
    staging = await seed_table(session, org, datasource, schema, name="staging")
    origin = await _column(session, staging, "ssn", classification="PII")
    await _column(session, customers, "email", classification="PII")
    copied = await _column(session, copy, "ssn")
    foreign_target = await _column(session, copy, "email")
    load_body = (
        "CREATE PROCEDURE public.load_staging AS BEGIN "
        "INSERT INTO public.customer_copy (ssn) SELECT s.ssn FROM public.staging s; END;"
    )
    routine, own_rows = await _captured(
        session, org, datasource, schema, name="load_staging", body=load_body
    )
    _o, elsewhere, _elsewhere_schema = await seed_estate(session, organization=org)

    def foreign_row(**values: Any) -> DeepProcedureLineageEdge:
        base: dict[str, Any] = {
            "id": uuid4(),
            "organization_id": org.id,
            "datasource_id": elsewhere.id,
            "routine_id": routine.id,
            "statement_ordinal": 0,
            "source_resolved": True,
            "confidence": "FULL",
            "dialect": "tsql",
            "is_write": True,
            "sql_hash": "h" * 64,
            "review_status": "ACTIVE",
        }
        base.update(values)
        return DeepProcedureLineageEdge(**base)

    session.add_all(
        [
            foreign_row(
                source_table="public.customers", source_column="email",
                target_table="public.customer_copy", target_column="email",
                source_table_id=customers.id, target_table_id=copy.id,
                transformation_type="DIRECT",
            ),
            # Spelled exactly as this routine's own read names its source, so only
            # the datasource clause stands between it and a "carried hop".
            foreign_row(
                source_table="public.customers", source_column="ssn",
                target_table="public.staging", target_column="ssn",
                target_table_id=staging.id, is_intermediate=True,
                transformation_type="DIRECT",
            ),
        ]
    )
    await session.flush()
    [real_read] = own_rows
    assert (real_read.source_table, real_read.source_column) == ("public.staging", "ssn")

    written = await _propagate(session, datasource)

    assert [(row.column_id, row.origin_column_id) for row in written] == [
        (copied.id, origin.id)
    ]
    assert foreign_target.id not in {row.column_id for row in written}


async def test_no_source_value_reaches_the_procedure_propagation_path(
    session: AsyncSession,
) -> None:
    """INV-6. A procedure body carrying source values in its literals is stored the
    way ingestion stores one (redact, fingerprint, screen), released only through
    the routine gate, parsed and propagated; every output this change added -- the
    derived rows' evidence, the gap record, the gap log -- is searched for the
    sentinels. Non-vacuous: the raw body carries them, and a derived row and a gap
    are both produced (which is also why this fails before)."""
    org, datasource, schema, customers, copy = await _estate(session)
    archive = await seed_table(session, org, datasource, schema, name="customer_archive")
    await _column(session, customers, "ssn", classification="PII")
    await _column(session, copy, "ssn")
    await _column(session, copy, "note")
    await _column(session, archive, "ssn")
    raw_body = (
        "CREATE PROCEDURE public.copy_customers AS BEGIN "  # noqa: S608 -- a test body, never executed
        "INSERT INTO public.customer_copy (ssn, note) "
        f"SELECT c.ssn, '{SENTINEL_LITERAL}' FROM public.customers c "
        f"WHERE c.region <> '{SENTINEL_CUSTOMER}'; "
        "INSERT INTO public.customer_archive SELECT * FROM public.customers "
        f"WHERE note <> '{SENTINEL_ROW_VALUE}'; END;"
    )
    assert all(sentinel in raw_body for sentinel in _SENTINELS)
    redacted, fingerprint, redaction, screening, _reasons, _version = _store_source_sql(
        raw_body, dialect="tsql"
    )
    assert redacted is not None
    await _captured(session, org, datasource, schema, name="copy_customers", body=redacted)
    routine = await session.scalar(
        select(MetadataRoutine).where(MetadataRoutine.name == "copy_customers")
    )
    assert routine is not None
    routine.body_fingerprint, routine.redaction_status, routine.screening_status = (
        fingerprint, redaction, screening,
    )

    inputs = await _inputs(session, datasource)
    with capture_logs() as logs:
        written = await _propagate(session, datasource)

    assert written and inputs.gaps, "the path under test produced nothing to search"
    rendered = json.dumps(
        {
            "derived": [
                [row.classification, row.origin_classification, row.edge_chain, row.graph_version]
                for row in written
            ],
            "gaps": [astuple(gap) for gap in inputs.gaps],
            "edges": [astuple(edge) for edge in inputs.edges],
            "logs": logs,
        },
        default=str,
    )
    for sentinel in _SENTINELS:
        assert sentinel not in rendered
    for gap in inputs.gaps:
        UUID(gap.edge_ref), UUID(gap.owner_ref), UUID(gap.target_table_id)
