"""R11-FP14: a hand-written tool over a routine's extracted query is offered the routine's binding.

A tool the agent generates from a routine records `source_routine_id` and the body's fingerprint,
so R11-FP16 holds it when the routine moves. A person who writes that same query by hand -- often
*because* generation refused it: the result query carried a literal the stored body had redacted --
gets no link, since nothing in their SQL names the routine. Their tool then keeps answering after
the routine's logic changes. These tests pin how the tool agent closes that:

* detection is **structural**: the tool's SQL and the routine's extracted result query are compared
  after `SqlGuard`-style parse and render, with every value position erased -- a re-supplied
  literal and a renamed placeholder still match; no literal is ever compared;
* detection **proposes, never binds**: a new version of the person's own tool, SQL unchanged,
  bound to the routine, in the T2 `GOVERNED_TOOL_VERSION` review; the published version is left
  exactly as it was until a person decides;
* it is **conservative**: a bare projection, a different filter, a different alias, a routine that
  writes, two routines with the same query, and another organization's tool are never matched;
* it is **idempotent**: a rejected binding is not asked again for the same routine definition, and
  a routine examined *before* the tool existed is reached again once the tool is published;
* once a person approves it, the routine **holds** the tool when it moves -- the gap closed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.models import (
    DataSource,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    Organization,
    Project,
)
from aida.schemas import GovernedToolVersionCreate, ToolParameterDefinition
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    TaskAgentRunRequest,
)
from aida.tool_agent import SKIP_SOURCE_HAS_TOOL, run_tool_agent
from aida.tool_drafts import stage_tool_version_draft
from aida.tool_source_binding import (
    REASON_DEFINITION_CHANGED,
    fetch_source_binding_holds,
    source_binding_drift,
)
from tests.support.task_agents import (
    agent_settings,
    count_rows,
    human,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)

AGENT = "agent:tool"

#: The routine's body as it is stored: its one literal already redacted to `:redacted`, and its
#: parameter referenced bare, the way a SQL-language function does.
RESULT_QUERY_BODY = (
    "CREATE FUNCTION public.open_order_totals(start_date date) "
    "RETURNS TABLE (region text, total numeric) LANGUAGE sql AS $$ "
    "SELECT o.region, SUM(o.amount) AS total FROM public.orders AS o "
    "WHERE o.status = :redacted AND o.placed_at >= start_date GROUP BY o.region $$"
)
#: What a person writes by hand: the same query, the value re-supplied, the parameter a placeholder.
HAND_WRITTEN_SQL = (
    "select o.region, sum(o.amount) as total from public.orders o "
    "where o.status = 'OPEN' and o.placed_at >= :start_date group by o.region"
)
START_DATE = ToolParameterDefinition(name="start_date", parameter_type="DATE", required=False)


async def _estate(session: Any, *, body: str = RESULT_QUERY_BODY, name: str = "open_order_totals"):
    org, datasource, schema = await seed_estate(session)
    await seed_table(session, org, datasource, schema, name="orders")
    routine = await _routine(session, org, datasource, schema, name=name, body=body)
    await register_agent(session, org, principal=AGENT)
    await session.commit()
    return org, datasource, schema, routine


async def _routine(
    session: Any, org: Organization, datasource: DataSource, schema: Any, *, name: str, body: str
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="(date)",
        routine_type="FUNCTION",
        language="sql",
        body_sql_redacted=body,
        body_fingerprint="body-1",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    session.add(
        MetadataRoutineParameter(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            name="start_date",
            ordinal_position=1,
            mode="IN",
            physical_type="date",
            fingerprint="fp",
        )
    )
    await session.flush()
    return routine


async def _hand_written(
    session: Any,
    org: Organization,
    datasource: DataSource,
    *,
    slug: str = "open_order_totals_by_region",
    sql: str = HAND_WRITTEN_SQL,
    parameters: tuple[ToolParameterDefinition, ...] = (START_DATE,),
    status: str = "PUBLISHED",
) -> GovernedToolVersion:
    """A person's tool, through the one draft write path, then published as a decision would.

    `tool_drafts` rather than the router, so this file does not depend on `tool_api`."""
    project = await session.get(Project, datasource.project_id)
    _tool, version = await stage_tool_version_draft(
        session,
        project,
        datasource,
        GovernedToolVersionCreate(
            slug=slug,
            name="Open order totals",
            description="Written by hand from the routine's query, with the status filled in.",
            datasource_id=datasource.id,
            sql_template=sql,
            parameters=list(parameters),
            allowed_roles=["Analyst"],
        ),
        audit_context=human(org, principal_id="tool-dev-2", roles=frozenset({"ToolDeveloper"})),
        settings=agent_settings(),
    )
    version.status = status
    await session.commit()
    return version


async def _run(session: Any, org: Organization, **overrides: Any):
    return await run_tool_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(capabilities=("PROCEDURE_TOOL",), **overrides),
        settings=agent_settings(),
        triggered_by=human(org, principal_id="tool-dev-1", roles=frozenset({"ToolDeveloper"})),
    )


async def _versions(session: Any, tool_id: Any) -> list[GovernedToolVersion]:
    return list(
        (
            await session.scalars(
                select(GovernedToolVersion)
                .where(GovernedToolVersion.tool_id == tool_id)
                .order_by(GovernedToolVersion.version)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Detection proposes a binding, and only proposes it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hand_written_copy_of_a_routines_query_is_offered_its_binding() -> None:
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        published = await _hand_written(session, org, datasource)

        outcome = await _run(session, org)

        (item,) = [item for item in outcome.items if item.subject_id == routine.id]
        assert item.action == ACTION_PROPOSED, item
        assert item.related_id == published.tool_id
        versions = await _versions(session, published.tool_id)
        assert [v.status for v in versions] == ["PUBLISHED", "REVIEW_REQUIRED"], (
            "the published version must stand untouched until a person decides"
        )
        proposed = versions[1]
        assert proposed.source_routine_id == routine.id
        assert proposed.source_definition_fingerprint == "body-1"
        assert proposed.sql_template == published.sql_template, "the person's SQL is not rewritten"
        assert proposed.parameter_schema == published.parameter_schema
        assert proposed.allowed_roles == published.allowed_roles
        assert proposed.created_by == AGENT
        review = await session.scalar(
            select(GovernanceReview).where(GovernanceReview.object_id == str(proposed.id))
        )
        assert review is not None
        assert (review.object_type, review.requested_action, review.status) == (
            "GOVERNED_TOOL_VERSION",
            "PUBLISH",
            "PENDING",
        )
        # No generated duplicate: the routine already has a tool standing on it.
        assert await count_rows(session, GovernedTool) == 1


@pytest.mark.asyncio
async def test_a_dry_run_reports_the_binding_and_writes_nothing() -> None:
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        published = await _hand_written(session, org, datasource)

        outcome = await _run(session, org, dry_run=True)

        (item,) = [item for item in outcome.items if item.subject_id == routine.id]
        assert (item.action, item.related_id) == (ACTION_WOULD_PROPOSE, published.tool_id)
        assert len(await _versions(session, published.tool_id)) == 1
        assert await count_rows(session, GovernanceReview) == 0


@pytest.mark.asyncio
async def test_a_routine_examined_before_the_tool_existed_is_reached_again() -> None:
    """The case this exists for: generation declined the routine, then a person wrote the tool.

    The routine scan only returns a routine that changed since it was examined, so without the
    tool-side pass the routine would never be looked at again and the tool would stay unbound.
    """
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        first = await _run(session, org)
        (declined,) = [item for item in first.items if item.subject_id == routine.id]
        assert declined.action == ACTION_SKIPPED  # generation cannot rebuild the redacted value

        published = await _hand_written(session, org, datasource)
        second = await _run(session, org)

        (item,) = [item for item in second.items if item.subject_id == routine.id]
        assert item.action == ACTION_PROPOSED
        assert item.related_id == published.tool_id


@pytest.mark.asyncio
async def test_a_rejected_binding_is_not_proposed_again() -> None:
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        published = await _hand_written(session, org, datasource)
        await _run(session, org)
        proposed = (await _versions(session, published.tool_id))[1]
        proposed.status = "REJECTED"
        await session.commit()

        again = await _run(session, org)
        later = await _run(session, org)

        assert again.count(ACTION_PROPOSED) == later.count(ACTION_PROPOSED) == 0
        assert {item.reason for item in again.items if item.subject_id == routine.id} <= {
            SKIP_SOURCE_HAS_TOOL
        }
        assert len(await _versions(session, published.tool_id)) == 2


@pytest.mark.asyncio
async def test_an_unpublished_hand_written_tool_blocks_a_duplicate_and_is_offered_once_published(
) -> None:
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        draft = await _hand_written(session, org, datasource, status="DRAFT")

        outcome = await _run(session, org)

        (item,) = [item for item in outcome.items if item.subject_id == routine.id]
        assert (item.action, item.reason) == (ACTION_SKIPPED, SKIP_SOURCE_HAS_TOOL)
        assert len(await _versions(session, draft.tool_id)) == 1

        draft.status = "PUBLISHED"  # the author finishes; `updated_at` moves with it
        await session.commit()
        published = await _run(session, org)

        (item,) = [item for item in published.items if item.subject_id == routine.id]
        assert item.action == ACTION_PROPOSED


@pytest.mark.asyncio
async def test_an_approved_binding_holds_the_tool_when_the_routine_moves() -> None:
    """What the binding is for: the silent-staleness gap, closed once a person approves it."""
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        published = await _hand_written(session, org, datasource)
        await _run(session, org)
        unbound, bound = await _versions(session, published.tool_id)
        assert await fetch_source_binding_holds(session, unbound) == ([], []), (
            "before: the hand-written version stands on nothing, so nothing can hold it"
        )
        # What an APPROVE on the PUBLISH review does to the two versions.
        unbound.status, bound.status = "SUPERSEDED", "PUBLISHED"
        await session.commit()
        assert await source_binding_drift(session, bound) is None

        routine.body_fingerprint = "body-2"  # the routine's logic moved
        await session.commit()

        assert await source_binding_drift(session, bound) == REASON_DEFINITION_CHANGED
        _assets, holds = await fetch_source_binding_holds(session, bound)
        assert [hold.severity for hold in holds] == ["CRITICAL"]


# ---------------------------------------------------------------------------
# What is deliberately not matched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        # A different filter: the routine's logic plus one more predicate is another query.
        "select o.region, sum(o.amount) as total from public.orders o "
        "where o.status = 'OPEN' and o.placed_at >= :start_date and o.region = 'EU' "
        "group by o.region",
        # A different alias: not normalised, because it is also how two real queries differ.
        "select x.region, sum(x.amount) as total from public.orders x "
        "where x.status = 'OPEN' and x.placed_at >= :start_date group by x.region",
        # The routine's query used inside a bigger one is a different tool.
        "select t.region from (select o.region, sum(o.amount) as total from public.orders o "
        "where o.status = 'OPEN' and o.placed_at >= :start_date group by o.region) t",
    ],
    ids=["extra-predicate", "different-alias", "embedded-as-subquery"],
)
async def test_a_tool_that_is_not_the_routines_query_is_not_offered_a_binding(sql: str) -> None:
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session)
        published = await _hand_written(session, org, datasource, sql=sql)

        outcome = await _run(session, org)

        assert len(await _versions(session, published.tool_id)) == 1
        assert not [
            item
            for item in outcome.items
            if item.subject_id == routine.id and item.related_id == published.tool_id
        ]


@pytest.mark.asyncio
async def test_a_bare_projection_is_never_matched() -> None:
    """The same `SELECT cols FROM t` in a routine and a tool is the obvious query, not a copy."""
    body = (
        "CREATE FUNCTION public.list_regions() RETURNS TABLE (region text) LANGUAGE sql AS $$ "
        "SELECT o.region FROM public.orders AS o $$"
    )
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session, body=body, name="list_regions")
        published = await _hand_written(
            session, org, datasource, sql="SELECT o.region FROM public.orders AS o", parameters=()
        )

        outcome = await _run(session, org)

        assert len(await _versions(session, published.tool_id)) == 1
        (item,) = [item for item in outcome.items if item.subject_id == routine.id]
        assert item.related_id is None


@pytest.mark.asyncio
async def test_a_routine_that_writes_is_never_matched() -> None:
    """Its last SELECT depends on the writes before it; "the extracted query" is read-only only."""
    body = (
        "CREATE PROCEDURE public.open_order_totals(start_date date) LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO public.orders (region) SELECT o.region FROM public.orders AS o; "
        "SELECT o.region, SUM(o.amount) AS total FROM public.orders AS o "
        "WHERE o.status = :redacted AND o.placed_at >= start_date GROUP BY o.region; END; $$"
    )
    async with task_agent_session() as session:
        org, datasource, _schema, routine = await _estate(session, body=body)
        published = await _hand_written(session, org, datasource)

        outcome = await _run(session, org)

        assert len(await _versions(session, published.tool_id)) == 1
        (item,) = [item for item in outcome.items if item.subject_id == routine.id]
        assert (item.action, item.reason) == (ACTION_SKIPPED, "PROCEDURE_WRITES")


@pytest.mark.asyncio
async def test_two_routines_with_the_same_query_make_the_match_a_guess() -> None:
    """Nothing is proposed for either: a binding to the wrong copy holds the tool on the wrong
    routine's changes and misses the right one's."""
    from aida.tool_agent import SKIP_ROUTINE_MATCH_AMBIGUOUS

    async with task_agent_session() as session:
        org, datasource, schema, routine = await _estate(session)
        clone = await _routine(
            session, org, datasource, schema, name="open_order_totals_v2", body=RESULT_QUERY_BODY
        )
        await session.commit()
        published = await _hand_written(session, org, datasource)

        outcome = await _run(session, org)

        reasons = {
            item.subject_id: (item.action, item.reason)
            for item in outcome.items
            if item.subject_id in (routine.id, clone.id)
        }
        assert set(reasons.values()) == {(ACTION_SKIPPED, SKIP_ROUTINE_MATCH_AMBIGUOUS)}, reasons
        assert len(await _versions(session, published.tool_id)) == 1


@pytest.mark.asyncio
async def test_another_organizations_identical_tool_is_never_matched() -> None:
    """INV-5: the tool index and the routine reads both restate the organization."""
    async with task_agent_session() as session:
        org, _datasource, _schema, routine = await _estate(session)
        other_org, other_source, other_schema = await seed_estate(session)
        await seed_table(session, other_org, other_source, other_schema, name="orders")
        await session.commit()
        foreign = await _hand_written(session, other_org, other_source)

        outcome = await _run(session, org)

        assert len(await _versions(session, foreign.tool_id)) == 1
        assert not [item for item in outcome.items if item.related_id == foreign.tool_id]
        assert routine.id in {item.subject_id for item in outcome.items}


# ---------------------------------------------------------------------------
# The structural key itself
# ---------------------------------------------------------------------------


def _tool_key(sql: str, dialect: str = "postgres") -> str | None:
    from aida.procedure_tool_blueprint import structural_query_key
    from aida.sql_guard import SqlGuard

    validation = SqlGuard(default_row_limit=1000, hard_row_limit=5000).validate(
        sql, dialect=dialect
    )
    assert validation.valid and validation.normalized_sql, validation.violations
    # What a tool version stores: the guard's re-rendered SQL, row cap appended.
    return structural_query_key(validation.normalized_sql, dialect=dialect)


def _routine_key(body: str, dialect: str, parameter_names: tuple[str, ...] = ()) -> str | None:
    from aida.procedure_tool_blueprint import (
        find_single_read_only_result_statement,
        structural_query_key,
    )

    node, _result = find_single_read_only_result_statement(body, dialect)
    return structural_query_key(node, dialect=dialect, parameter_names=parameter_names)


def test_values_placeholders_case_and_the_row_cap_do_not_change_the_key() -> None:
    routine = _routine_key(RESULT_QUERY_BODY, "postgres", ("start_date",))
    assert routine is not None
    assert _tool_key(HAND_WRITTEN_SQL) == routine
    # Another value and another placeholder name: still the routine's query.
    assert (
        _tool_key(
            "SELECT o.region, SUM(o.amount) AS total FROM public.orders AS o "
            "WHERE o.status = 'CLOSED' AND o.placed_at >= :since GROUP BY o.region"
        )
        == routine
    )
    # Without the routine's declared parameter names, its bare `start_date` stays a column.
    assert _routine_key(RESULT_QUERY_BODY, "postgres") != routine


def test_a_tsql_procedure_matches_through_top_and_identifier_case() -> None:
    body = (
        "CREATE PROCEDURE dbo.open_order_totals @start_date date AS BEGIN "
        "SELECT o.region, SUM(o.amount) AS total FROM dbo.orders AS o "
        "WHERE o.placed_at >= @start_date GROUP BY o.region END"
    )
    routine = _routine_key(body, "tsql")
    assert routine is not None
    assert (
        _tool_key(
            "select O.REGION, sum(O.AMOUNT) as TOTAL from DBO.ORDERS as O "
            "where O.PLACED_AT >= :start_date group by O.REGION",
            "tsql",
        )
        == routine
    )


def test_what_never_gets_a_key() -> None:
    from aida.procedure_tool_blueprint import structural_query_key

    bare = "SELECT c.id, c.name FROM sales.customers AS c"
    assert structural_query_key(bare, dialect="postgres") is None
    assert structural_query_key("this is not ( sql", dialect="postgres") is None
    assert structural_query_key("DELETE FROM sales.customers", dialect="postgres") is None
