"""R11-FP14: the tool agent turns captured views and read-only routines into reviewed tool drafts.

The two tool generators existed, but only a person who already knew which object to point them
at could use them. These tests drive `run_tool_agent` against the shared task-agent estate
(in-memory SQLite with PostgreSQL-shaped transactions) and pin what makes automatic proposal
safe:

* a proposal is a DRAFT in the existing T2 tool review -- nothing is ever published by the agent;
* a routine that writes, or that the parser cannot prove read-only, is declined with a stable
  code, never drafted;
* a second run proposes nothing twice;
* an edition without tool authoring refuses the whole run, and a dry run opens nothing.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from sqlalchemy import select

from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.models import DataSource, GovernanceReview, GovernedToolVersion, MetadataColumn
from aida.schemas import GovernedToolVersionCreate
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    TaskAgentRefused,
    TaskAgentRunRequest,
)
from aida.tool_agent import (
    DEFAULT_ALLOWED_ROLES,
    REASON_NOT_ENTITLED,
    SKIP_SOURCE_HAS_TOOL,
    SKIP_TOOL_EXISTS,
    run_tool_agent,
    tool_slug,
)
from aida.tool_api import create_tool_version
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


async def _estate(session):
    org, datasource, schema = await seed_estate(session)
    await seed_table(session, org, datasource, schema, name="orders")
    view = await seed_table(session, org, datasource, schema, name="v_revenue", object_type="VIEW")
    for position, (name, physical_type) in enumerate(
        (("customer_id", "integer"), ("region", "varchar")), start=1
    ):
        session.add(
            MetadataColumn(
                id=uuid4(),
                organization_id=org.id,
                table_id=view.id,
                name=name,
                ordinal_position=position,
                physical_type=physical_type,
                nullable=True,
                fingerprint="fp",
            )
        )
    session.add(
        MetadataViewDefinition(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=view.id,
            definition_sql_redacted="SELECT o.customer_id, o.region FROM public.orders AS o",
            definition_fingerprint="definition-1",
            redaction_status="PARSED",
            screening_status="CLEAN",
            fingerprint="fp",
        )
    )
    reader = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="read_revenue",
        signature="()",
        routine_type="FUNCTION",
        language="sql",
        body_sql_redacted=(
            "CREATE FUNCTION public.read_revenue() RETURNS TABLE (customer_id integer) "
            "LANGUAGE sql AS $$ SELECT r.customer_id FROM public.v_revenue AS r $$"
        ),
        body_fingerprint="body-1",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        fingerprint="fp",
    )
    writer = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="refresh_totals",
        signature="()",
        routine_type="PROCEDURE",
        language="plpgsql",
        body_sql_redacted=(
            "CREATE PROCEDURE public.refresh_totals() LANGUAGE plpgsql AS $$ BEGIN "
            "INSERT INTO public.orders (customer_id) SELECT r.customer_id FROM public.v_revenue r; "
            "END; $$"
        ),
        body_fingerprint="body-2",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        fingerprint="fp",
    )
    session.add_all([reader, writer])
    await session.flush()
    await register_agent(session, org, principal=AGENT)
    await session.commit()
    return org, view, reader, writer


async def _run(session, org, **overrides):
    settings = overrides.pop("settings", None) or agent_settings()
    return await run_tool_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(**overrides),
        settings=settings,
        triggered_by=human(org, principal_id="tool-dev-1", roles=frozenset({"ToolDeveloper"})),
    )


@pytest.mark.asyncio
async def test_views_and_read_only_routines_become_drafts_a_person_must_review() -> None:
    async with task_agent_session() as session:
        org, view, reader, writer = await _estate(session)

        outcome = await _run(session, org)

        by_subject = {item.subject_id: item for item in outcome.items}
        assert by_subject[view.id].action == ACTION_PROPOSED
        assert by_subject[reader.id].action == ACTION_PROPOSED
        assert by_subject[writer.id].action == ACTION_SKIPPED
        assert by_subject[writer.id].reason == "PROCEDURE_WRITES"

        versions = (await session.scalars(select(GovernedToolVersion))).all()
        assert len(versions) == 2
        assert {version.status for version in versions} == {"REVIEW_REQUIRED"}
        assert {version.created_by for version in versions} == {AGENT}
        assert all(version.allowed_roles == sorted(DEFAULT_ALLOWED_ROLES) for version in versions)
        reviews = (await session.scalars(select(GovernanceReview))).all()
        assert {
            (review.object_type, review.requested_action, review.status, review.requested_by)
            for review in reviews
        } == {("GOVERNED_TOOL_VERSION", "PUBLISH", "PENDING", AGENT)}
        assert len(reviews) == 2
        assert {review.object_id for review in reviews} == {str(v.id) for v in versions}


@pytest.mark.asyncio
async def test_a_second_run_proposes_nothing_twice() -> None:
    async with task_agent_session() as session:
        org, view, reader, writer = await _estate(session)
        await _run(session, org)

        again = await _run(session, org)

        assert again.count(ACTION_PROPOSED) == 0
        assert {item.subject_id: item.reason for item in again.items} == {
            view.id: SKIP_TOOL_EXISTS,
            reader.id: SKIP_TOOL_EXISTS,
        }, "the declined writer is not re-examined until its body changes"
        assert await count_rows(session, GovernedToolVersion) == 2


@pytest.mark.asyncio
async def test_an_edition_without_tool_authoring_refuses_the_run() -> None:
    async with task_agent_session() as session:
        org, *_ = await _estate(session)

        with pytest.raises(TaskAgentRefused) as refused:
            await _run(session, org, settings=agent_settings(edition="FOUNDATION"))

        assert refused.value.reason_code == REASON_NOT_ENTITLED
        await session.rollback()
        assert await count_rows(session, GovernedToolVersion) == 0


@pytest.mark.asyncio
async def test_a_dry_run_reports_and_opens_nothing() -> None:
    async with task_agent_session() as session:
        org, *_ = await _estate(session)

        outcome = await _run(session, org, dry_run=True)

        assert outcome.count(ACTION_WOULD_PROPOSE) == 2
        assert await count_rows(session, GovernedToolVersion) == 0
        assert await count_rows(session, GovernanceReview) == 0


def test_a_slug_is_valid_and_keeps_overloads_and_look_alikes_apart() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]{1,99}$")
    slugs = {
        tool_slug("routine", "sales", "customer_net", "(integer)"),
        tool_slug("routine", "sales", "customer_net", "(integer, numeric)"),
        tool_slug("view", "a_b", "c"),
        tool_slug("view", "a", "b_c"),
        tool_slug("view", "Sales Mart", "Revenue-" + "x" * 200),
    }
    assert len(slugs) == 5
    assert all(pattern.match(slug) for slug in slugs)


async def _hand_written(session, org, view, *, slug: str, sql: str) -> None:
    """A tool a person wrote over the same source, under a slug of their own choosing."""
    datasource = await session.get(DataSource, view.datasource_id)
    assert datasource is not None
    await create_tool_version(
        datasource.project_id,
        GovernedToolVersionCreate(
            slug=slug,
            name="Revenue report",
            description="Written by hand, before the agent ever looked at this source.",
            datasource_id=datasource.id,
            sql_template=sql,
            allowed_roles=["Analyst"],
        ),
        context=human(org, principal_id="tool-dev-2", roles=frozenset({"ToolDeveloper"})),
        session=session,
        settings=agent_settings(),
    )


@pytest.mark.asyncio
async def test_a_tool_someone_wrote_over_the_same_view_stops_a_second_proposal() -> None:
    """R11-FP14: the slug check catches the agent's own repeat; this catches a person's first."""
    async with task_agent_session() as session:
        org, view, reader, _ = await _estate(session)
        await _hand_written(
            session,
            org,
            view,
            slug="revenue_by_region",
            sql="SELECT customer_id, region FROM public.v_revenue",
        )

        outcome = await _run(session, org)

        by_subject = {item.subject_id: item for item in outcome.items}
        assert (by_subject[view.id].action, by_subject[view.id].reason) == (
            ACTION_SKIPPED,
            SKIP_SOURCE_HAS_TOOL,
        )
        # Only that view is spoken for: the read-only routine still gets its proposal.
        assert by_subject[reader.id].action == ACTION_PROPOSED


@pytest.mark.asyncio
async def test_a_tool_that_joins_the_view_to_something_else_is_a_different_tool() -> None:
    async with task_agent_session() as session:
        org, view, _, _ = await _estate(session)
        await _hand_written(
            session,
            org,
            view,
            slug="revenue_with_orders",
            sql=(
                "SELECT v.customer_id FROM public.v_revenue v "
                "JOIN public.orders o ON o.customer_id = v.customer_id"
            ),
        )

        outcome = await _run(session, org)

        by_subject = {item.subject_id: item for item in outcome.items}
        # That tool answers a question about two objects; the view still has no tool of its own,
        # so proposing one is not a duplicate.
        assert by_subject[view.id].action == ACTION_PROPOSED
