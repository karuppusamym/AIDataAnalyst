"""ADR-0029: the lineage agent.

The runtime properties every task agent shares are exercised in
`tests/test_steward_agent.py`. What is specific to this agent, and tested here:

* it parses the view definitions ingestion captured -- and only eligible ones;
* its edges are PROPOSED, always, whatever the organization's auto-activation
  settings say, and a person decides them in ADR-0026's per-edge queue, which
  refuses the agent as reviewer of its own edge;
* a view with any lineage -- including lineage a reviewer rejected -- is left
  alone, and a definition it cannot use is recorded once until it changes;
* its backlog bound and its outcome measure count edges in that queue.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contracts import REASON_CONTRACT_MISSING
from aida.envelope_models import MetadataViewDefinition
from aida.lineage_agent import (
    LINEAGE_AGENT,
    SKIP_NO_LINEAGE,
    SKIP_UNPARSEABLE,
    SKIP_UNSUPPORTED_DIALECT,
    as_create_view,
    run_lineage_agent,
)
from aida.lineage_agent_api import (
    LineageAgentRunRequest,
    get_lineage_agent_state,
    start_lineage_agent_run,
)
from aida.models import (
    AgentContract,
    AgentTask,
    AiAsset,
    AiAssetVersion,
    DataSource,
    MetadataSchema,
    MetadataTable,
    Organization,
    ViewLineageEdge,
)
from aida.parsed_lineage_review_api import decide_parsed_lineage_edge
from aida.schemas import ParsedLineageEdgeDecisionRequest
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    QUEUE_PARSED_LINEAGE,
    REASON_QUEUE_NOT_PERMITTED,
    STOP_REVIEW_BACKLOG,
    TaskAgentAuthority,
    TaskAgentCapability,
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRun,
    TaskAgentRunRequest,
    TaskAgentSpec,
    agent_outcomes,
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

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = "agent:lineage"
BODY = "SELECT c.id, c.name FROM public.customers c"
REVIEWER_ROLES = frozenset({"MetadataReviewer"})


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _estate(
    session: AsyncSession, *, dialect: str = "postgres"
) -> tuple[Organization, DataSource, MetadataSchema, MetadataTable]:
    org, datasource, schema = await seed_estate(session, dialect=dialect)
    customers = await seed_table(session, org, datasource, schema, name="customers")
    return org, datasource, schema, customers


async def _view(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str = "customer_view",
    body: str | None = BODY,
    **overrides: Any,
) -> tuple[MetadataTable, MetadataViewDefinition]:
    view = await seed_table(session, org, datasource, schema, name=name, object_type="VIEW")
    values: dict[str, Any] = {
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "table_id": view.id,
        "definition_sql_redacted": body,
        "definition_fingerprint": uuid4().hex,
        "redaction_status": "PARSED",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    definition = MetadataViewDefinition(**values)
    session.add(definition)
    await session.flush()
    return view, definition


async def _run(
    session: AsyncSession, org: Organization, *, settings: Any = None, **request: Any
) -> TaskAgentOutcome:
    return await run_lineage_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(**request),
        settings=settings or agent_settings(),
        triggered_by=human(org),
    )


def test_a_captured_body_is_attributed_to_the_view_and_a_statement_is_left_alone() -> None:
    assert as_create_view("public.v", " SELECT 1 ; ") == "CREATE VIEW public.v AS SELECT 1"
    assert as_create_view("public.v", "create view public.v as select 1") == (
        "create view public.v as select 1"
    )


async def test_an_eligible_view_gets_its_lineage_proposed_never_activated(
    session: AsyncSession,
) -> None:
    """With the organization's defaults -- `auto_active`, under which a
    person's parse lands ACTIVE -- the agent's edges are still PROPOSED."""
    org, datasource, schema, customers = await _estate(session)
    view, definition = await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    [item] = outcome.items
    assert (item.action, item.subject_name, item.confidence) == (
        ACTION_PROPOSED,
        "public.customer_view",
        1.0,
    )
    edges = (await session.scalars(select(ViewLineageEdge))).all()
    assert len(edges) == 2
    assert {edge.review_status for edge in edges} == {"PROPOSED"}
    assert {edge.created_by for edge in edges} == {AGENT}
    assert {edge.target_table_id for edge in edges} == {view.id}
    assert {edge.source_table_id for edge in edges} == {customers.id}
    task = await session.get(AgentTask, item.task_id)
    assert task is not None
    assert (task.proposal_ref_type, task.proposal_ref_id) == ("VIEW_DEFINITION", definition.id)
    assert task.status == "PROPOSED"
    assert (task.evidence["queue"], task.evidence["edge_count"]) == (QUEUE_PARSED_LINEAGE, 2)


async def test_auto_activation_settings_never_apply_to_the_agent(session: AsyncSession) -> None:
    """A threshold of 0.0 would activate every edge a *person* parsed."""
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)
    settings = agent_settings(
        lineage_parsed_edges_review_mode="require_review",
        lineage_high_confidence_auto_active_threshold=0.0,
    )

    await _run(session, org, settings=settings)

    statuses = set((await session.scalars(select(ViewLineageEdge.review_status))).all())
    assert statuses == {"PROPOSED"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"screening_status": "QUARANTINED"},
        {"availability": "UNAVAILABLE", "definition_sql_redacted": None},
        {"status": "DEPRECATED"},
    ],
    ids=["quarantined", "unavailable", "retired"],
)
async def test_ineligible_definitions_are_never_candidates(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema, **overrides)
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert outcome.items == []


async def test_a_view_with_any_lineage_is_left_alone_even_rejected_lineage(
    session: AsyncSession,
) -> None:
    org, datasource, schema, customers = await _estate(session)
    view, _definition = await _view(session, org, datasource, schema)
    session.add(
        ViewLineageEdge(
            organization_id=org.id,
            datasource_id=datasource.id,
            source_table="public.customers",
            source_column="id",
            target_table="public.customer_view",
            target_column="id",
            source_table_id=customers.id,
            target_table_id=view.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h" * 64,
            review_status="REJECTED",
            created_by="steward-2",
        )
    )
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert outcome.items == []
    assert await count_rows(session, ViewLineageEdge) == 1


async def test_an_unusable_definition_is_declined_once_until_it_changes(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _customers = await _estate(session)
    _view_row, definition = await _view(session, org, datasource, schema, body="this is not sql")
    await register_agent(session, org, principal=AGENT)

    first = await _run(session, org)
    second = await _run(session, org)
    definition.definition_sql_redacted = BODY
    definition.definition_fingerprint = "v2"
    definition.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    await session.flush()
    third = await _run(session, org)

    [declined] = first.items
    assert declined.action == ACTION_SKIPPED
    assert declined.reason in {SKIP_UNPARSEABLE, SKIP_NO_LINEAGE}
    task = await session.get(AgentTask, declined.task_id)
    assert task is not None and task.status == "FAILED"
    assert task.evidence["declined"] == declined.reason
    assert second.items == [], "the same definition was examined twice"
    assert [item.action for item in third.items] == [ACTION_PROPOSED]


async def test_an_unsupported_dialect_is_declined(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session, dialect="sqlite")
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert [(item.action, item.reason) for item in outcome.items] == [
        (ACTION_SKIPPED, SKIP_UNSUPPORTED_DIALECT)
    ]


async def test_a_t0_contract_observes_and_writes_no_edge(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT, tier="T0")

    outcome = await _run(session, org)

    assert [item.action for item in outcome.items] == [ACTION_WOULD_PROPOSE]
    assert await count_rows(session, ViewLineageEdge) == 0
    assert await count_rows(session, AgentTask) == 0


async def test_a_person_decides_the_agents_edge_and_the_agent_cannot(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    edge = (await session.scalars(select(ViewLineageEdge).limit(1))).one()
    edge_id = edge.id
    decision = ParsedLineageEdgeDecisionRequest(
        edge_type="VIEW", decision="APPROVED", reason="Matches the view definition."
    )

    with pytest.raises(HTTPException) as excinfo:
        await decide_parsed_lineage_edge(
            edge_id,
            decision,
            context=human(org, AGENT, REVIEWER_ROLES),
            session=session,
        )
    assert excinfo.value.status_code == 409

    await decide_parsed_lineage_edge(
        edge_id, decision, context=human(org, "reviewer-1", REVIEWER_ROLES), session=session
    )
    decided = await session.get(ViewLineageEdge, edge_id)
    assert decided is not None
    assert (decided.review_status, decided.reviewed_by) == ("ACTIVE", "reviewer-1")


async def test_the_backlog_bound_counts_edges(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema, name="a_view")
    await _view(session, org, datasource, schema, name="b_view")
    await register_agent(session, org, principal=AGENT)
    settings = agent_settings(lineage_agent_max_pending_proposals=1)

    outcome = await _run(session, org, settings=settings)

    assert outcome.count(ACTION_PROPOSED) == 1
    assert outcome.stopped_reason == STOP_REVIEW_BACKLOG


async def test_outcomes_count_edges_by_review_state(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    first, second = (await session.scalars(select(ViewLineageEdge))).all()
    first.review_status, second.review_status = "ACTIVE", "REJECTED"
    await session.flush()

    [row] = await agent_outcomes(session, org.id, spec=LINEAGE_AGENT, agent_principal_id=AGENT)

    assert (row.object_type, row.pending, row.approved, row.rejected) == (
        "VIEW_LINEAGE_EDGE",
        0,
        1,
        1,
    )
    assert row.acceptance_rate == 0.5


async def test_the_state_endpoint_names_the_dedicated_queue(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)

    state = await get_lineage_agent_state(
        org.id, context=human(org), session=session, settings=agent_settings()
    )

    assert (state.agent_key, state.registered, state.pending_proposals) == ("lineage", True, 2)
    [capability] = state.capabilities
    assert (capability.review_queue, capability.risk_tier) == (QUEUE_PARSED_LINEAGE, None)


async def test_an_unregistered_lineage_agent_is_refused(session: AsyncSession) -> None:
    org, datasource, schema, _customers = await _estate(session)
    await _view(session, org, datasource, schema)
    org_id, context = org.id, human(org)
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await start_lineage_agent_run(
            org_id,
            LineageAgentRunRequest(),
            context=context,
            session=session,
            settings=agent_settings(),
        )

    assert (excinfo.value.status_code, excinfo.value.detail) == (409, REASON_CONTRACT_MISSING)


def test_the_agent_never_writes_an_active_edge() -> None:
    source = (REPO_ROOT / "src" / "aida" / "lineage_agent.py").read_text(encoding="utf-8")
    assert 'review_status="ACTIVE"' not in source
    assert "_apply_decision" not in source
    assert "resolve_review_status_for_new_edge" not in source


async def test_a_dedicated_queue_must_be_human_only(session: AsyncSession) -> None:
    """The second write path is closed to any queue an agent could decide from."""
    org, _datasource, _schema, _customers = await _estate(session)
    spec = TaskAgentSpec(
        key="lineage",
        audit_roles=frozenset(),
        capabilities=(
            TaskAgentCapability(
                key="X", object_type="X", intent="x", producer="x", queue="SOME_OTHER_QUEUE"
            ),
        ),
    )
    run = TaskAgentRun(
        session,
        spec=spec,
        authority=TaskAgentAuthority(
            contract=AgentContract(sampling_rate=0.05),
            asset=AiAsset(),
            version=AiAssetVersion(),
            principal_id=AGENT,
        ),
        settings=agent_settings(),
        outcome=TaskAgentOutcome(
            run_id="r",
            agent_key="lineage",
            organization_id=org.id,
            agent_principal_id=AGENT,
            ai_asset_version_id=uuid4(),
            autonomy_tier="T1",
            mode="PROPOSE",
            dry_run=False,
            limit=1,
            capabilities=("X",),
            started_at=datetime.now(UTC),
        ),
        datasource_id=None,
        pending=0,
    )

    with pytest.raises(TaskAgentRefused) as excinfo:
        await run.proposed_in_queue(
            "X",
            proposal_ref_type="X",
            proposal_ref_id=uuid4(),
            subject_id=uuid4(),
            subject_name="x",
            inputs={},
            pending_added=1,
            evidence={},
        )
    assert excinfo.value.reason_code == REASON_QUEUE_NOT_PERMITTED
    with pytest.raises(ValueError):
        await run.open_review("X", object_id=uuid4(), requested_action="X", details={})
