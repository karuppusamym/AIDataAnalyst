"""ADR-0029 / R11-FP08: the steward agent's routine descriptions are *ranked*.

The capability shipped ordering its candidates by `MetadataRoutine.name`, and the
argument for that was sound at the time -- nothing in this codebase carried a
usage signal keyed by routine, so any order invented here would have looked
measured without being it. The consequence was still a defect: on an estate
bigger than the run's limit the agent described whatever sorted first, while the
table capability beside it worked a ranked backlog.

`documentation_worklist.rank_routine_documentation_worklist` is the signal that
argument was waiting for, so these pin what the agent now chooses:

* the **ranked** routine is worked first, not the alphabetically-first one, and
  the rank it was chosen at is on the item, the review and the draft -- a choice
  nobody can inspect is the thing ranking was meant to end;
* what name order was right about is kept: an estate with no query history has
  nothing to rank by, and is worked in exactly the order it was before;
* the **three exclusions survive the ranker**, and the retired one is the whole
  point -- the worklist deliberately puts a retired description back in front of
  a *human*, and the agent must not draft over it;
* the run stays **bounded by its own limit**, and scoped (INV-5) to its
  organization and, when it has one, its datasource.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentationVersion,
)
from aida.models import (
    AuditEvent,
    DataSource,
    GovernanceReview,
    MetadataSchema,
    MetadataTable,
    Organization,
    QueryExecution,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_description_service import publish_routine_documentation_version
from aida.steward_agent import (
    CAPABILITY_ROUTINE_DESCRIPTION,
    SKIP_OPEN_DRAFT,
    run_steward_agent,
)
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    TaskAgentOutcome,
    TaskAgentRunRequest,
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

pytestmark = pytest.mark.asyncio

AGENT = "agent:steward"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _routine(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    routine_type: str = "PROCEDURE",
    comment: str | None = None,
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="()",
        routine_type=routine_type,
        language="plpgsql",
        body_sql_redacted="BEGIN NULL; END;",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        status="ACTIVE",
        fingerprint="fp",
        source_description=comment,
    )
    session.add(routine)
    await session.flush()
    return routine


async def _writes(
    session: AsyncSession, routine: MetadataRoutine, source: MetadataTable, target: MetadataTable
) -> None:
    """One ACTIVE, non-intermediate write edge -- the only lineage the ranker reads,
    and enough evidence for the draft to clear the shared submission bar."""
    session.add(
        DeepProcedureLineageEdge(
            id=uuid4(),
            organization_id=routine.organization_id,
            datasource_id=routine.datasource_id,
            routine_id=routine.id,
            statement_ordinal=1,
            source_table=f"public.{source.name}",
            source_column="amount",
            target_table=f"public.{target.name}",
            target_column="amount",
            source_resolved=True,
            source_table_id=source.id,
            target_table_id=target.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            is_write=True,
            is_intermediate=False,
            sql_hash="h",
            review_status="ACTIVE",
        )
    )
    await session.flush()


async def _queries(
    session: AsyncSession, datasource: DataSource, table: MetadataTable, *, times: int
) -> None:
    """Real governed executions against one table -- the traffic a routine's usage
    is borrowed from."""
    for _ in range(times):
        session.add(
            QueryExecution(
                id=uuid4(),
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                principal_id="analyst@bank.example",
                status="COMPLETED",
                dialect=datasource.dialect,
                sql_hash="deadbeef" * 8,
                referenced_tables=[f"public.{table.name}"],
                created_at=NOW,
            )
        )
    await session.flush()


async def _run(
    session: AsyncSession,
    org: Organization,
    *,
    limit: int = 10,
    datasource_id: Any = None,
) -> TaskAgentOutcome:
    return await run_steward_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(
            capabilities=(CAPABILITY_ROUTINE_DESCRIPTION,),
            limit=limit,
            datasource_id=datasource_id,
        ),
        settings=agent_settings(),
        triggered_by=human(org),
    )


async def _hot_and_cold(
    session: AsyncSession,
) -> tuple[Organization, MetadataRoutine, MetadataRoutine]:
    """Two describable routines that differ only in what their writes are worth.

    `sp_aaa_archive` sorts first by name and writes a table nobody queries.
    `sp_zzz_positions` sorts last by name and writes a table 40 real queries
    touched. Neither routine is itself queried -- nothing ever queries a
    procedure -- so name order and ranked order disagree about which of the two
    a steward should be asked about first.
    """
    org, datasource, schema = await seed_estate(session)
    ledger = await seed_table(session, org, datasource, schema, name="ledger")
    archive = await seed_table(session, org, datasource, schema, name="archive_2019")
    positions = await seed_table(session, org, datasource, schema, name="positions")
    quiet = await _routine(session, org, datasource, schema, name="sp_aaa_archive")
    hot = await _routine(session, org, datasource, schema, name="sp_zzz_positions")
    await _writes(session, quiet, ledger, archive)
    await _writes(session, hot, ledger, positions)
    await _queries(session, datasource, positions, times=40)
    await register_agent(session, org, principal=AGENT)
    await session.flush()
    return org, hot, quiet


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


async def test_the_ranked_routine_is_drafted_not_the_alphabetically_first(
    session: AsyncSession,
) -> None:
    """The defect, stated as the behaviour that replaces it. With one proposal to
    spend, the agent spends it on the procedure that produces a table real queries
    read -- not on the one whose name happens to sort first."""
    org, hot, quiet = await _hot_and_cold(session)

    outcome = await _run(session, org, limit=1)

    proposed = [item for item in outcome.items if item.action == ACTION_PROPOSED]
    assert [item.subject_name for item in proposed] == [hot.name]
    # And the run really was bounded by its own limit rather than by running out
    # of candidates: the quiet routine is a live candidate, just a later one.
    assert quiet.name not in {item.subject_name for item in proposed}
    assert await count_rows(session, RoutineDescriptionDraft) == 1


async def test_the_whole_backlog_is_worked_in_ranked_order(session: AsyncSession) -> None:
    """With room for both, both are proposed -- highest borrowed usage first."""
    org, hot, quiet = await _hot_and_cold(session)

    outcome = await _run(session, org, limit=10)

    proposed = [item for item in outcome.items if item.action == ACTION_PROPOSED]
    assert [item.subject_name for item in proposed] == [hot.name, quiet.name]
    assert [item.rank for item in proposed] == [1, 2]


async def test_an_estate_with_no_query_history_is_worked_in_name_order(
    session: AsyncSession,
) -> None:
    """What name order was right about, kept. Usage is a term of a product, so
    routines whose written tables nobody has touched all score zero and tie-break
    by name -- which is exactly the order this capability used before it was
    ranked. Arbitrary order is now confined to what there is nothing to order by.
    """
    org, datasource, schema = await seed_estate(session)
    ledger = await seed_table(session, org, datasource, schema, name="ledger")
    names = ["sp_charlie", "sp_alpha", "sp_bravo"]
    for name in names:
        target = await seed_table(session, org, datasource, schema, name=f"t_{name}")
        routine = await _routine(session, org, datasource, schema, name=name)
        await _writes(session, routine, ledger, target)
    await register_agent(session, org, principal=AGENT)
    await session.flush()

    outcome = await _run(session, org, limit=10)

    assert [
        item.subject_name for item in outcome.items if item.action == ACTION_PROPOSED
    ] == sorted(names)


async def test_the_rank_is_on_the_item_the_draft_and_the_proposal_audit(
    session: AsyncSession,
) -> None:
    """A reviewer opening the draft can see why this routine was chosen without
    reading the agent's source. The table and column drafts carry `worklist_rank`
    for the same reason."""
    org, hot, _quiet = await _hot_and_cold(session)

    outcome = await _run(session, org, limit=1)

    item = next(item for item in outcome.items if item.action == ACTION_PROPOSED)
    assert item.rank == 1
    draft = (
        await session.scalars(
            select(RoutineDescriptionDraft).where(RoutineDescriptionDraft.routine_id == hot.id)
        )
    ).one()
    assert draft.evidence["worklist_rank"] == 1
    review = await session.get(GovernanceReview, draft.governance_review_id)
    assert review is not None
    proposal = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "steward_agent.propose")
        )
    ).one()
    assert proposal.details["worklist_rank"] == 1


# ---------------------------------------------------------------------------
# The three exclusions, which the ranker does not make
# ---------------------------------------------------------------------------


async def test_a_retired_description_is_never_re_proposed_however_it_ranks(
    session: AsyncSession,
) -> None:
    """The exclusion the ranker cannot make, and the one that matters most.

    A WITHDRAWN version is not APPROVED, so the worklist's `is_documented` is
    False and the routine comes *back* onto the list a human steward is shown --
    which is the point of retiring a description. A human seeing it again is the
    feature; this agent drafting over it would re-propose what a reviewer
    retired, so the exclusion stays in the agent even though the top-ranked row
    is now handed to it.
    """
    org, hot, quiet = await _hot_and_cold(session)
    await publish_routine_documentation_version(
        session,
        organization_id=org.id,
        datasource_id=hot.datasource_id,
        routine_id=hot.id,
        description="Rebuilds each account's closing position from the ledger overnight.",
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=NOW,
    )
    await session.execute(
        update(RoutineDocumentationVersion)
        .where(RoutineDocumentationVersion.status == "APPROVED")
        .values(status="WITHDRAWN")
    )
    await session.flush()

    outcome = await _run(session, org, limit=10)

    # The retired routine still ranks first -- it is simply not the agent's work.
    subjects = {item.subject_name for item in outcome.items}
    assert hot.name not in subjects
    assert subjects == {quiet.name}


async def test_an_approved_description_is_not_a_gap(session: AsyncSession) -> None:
    org, hot, quiet = await _hot_and_cold(session)
    await publish_routine_documentation_version(
        session,
        organization_id=org.id,
        datasource_id=hot.datasource_id,
        routine_id=hot.id,
        description="Rebuilds each account's closing position from the ledger overnight.",
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=NOW,
    )
    await session.flush()

    outcome = await _run(session, org, limit=10)

    assert {item.subject_name for item in outcome.items} == {quiet.name}


async def test_a_package_is_not_describable_however_much_it_writes(
    session: AsyncSession,
) -> None:
    """A package is a container for subprograms, not a callable unit, so there is
    nothing about it to describe. The gather excludes it in SQL and the pure
    ranker refuses it by name; this pins that the agent's own predicate still
    holds the line, so neither of those becoming laxer can let one through."""
    org, datasource, schema = await seed_estate(session)
    ledger = await seed_table(session, org, datasource, schema, name="ledger")
    positions = await seed_table(session, org, datasource, schema, name="positions")
    package = await _routine(
        session, org, datasource, schema, name="pkg_positions", routine_type="PACKAGE"
    )
    await _writes(session, package, ledger, positions)
    await _queries(session, datasource, positions, times=40)
    await register_agent(session, org, principal=AGENT)
    await session.flush()

    outcome = await _run(session, org, limit=10)

    assert outcome.items == []
    assert await count_rows(session, RoutineDescriptionDraft) == 0


async def test_an_open_draft_is_somebody_elses_work_and_carries_its_rank(
    session: AsyncSession,
) -> None:
    """Unchanged behaviour, re-pinned because the skip now travels through the
    ranked loop: the routine is reported with its rank rather than dropped."""
    org, hot, _quiet = await _hot_and_cold(session)
    session.add(
        RoutineDescriptionDraft(
            organization_id=org.id,
            datasource_id=hot.datasource_id,
            routine_id=hot.id,
            drafted_text="Somebody is already drafting this one.",
            text_fingerprint="f" * 64,
            accuracy_score=0.9,
            clarity_score=0.9,
            style_score=0.9,
            completeness_score=0.9,
            overall_score=0.9,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="steward@bank.example",
        )
    )
    await session.flush()

    outcome = await _run(session, org, limit=10)

    skipped = [item for item in outcome.items if item.action == ACTION_SKIPPED]
    assert [(item.subject_name, item.reason, item.rank) for item in skipped] == [
        (hot.name, SKIP_OPEN_DRAFT, 1)
    ]


# ---------------------------------------------------------------------------
# Scope (INV-5)
# ---------------------------------------------------------------------------


async def test_a_run_scoped_to_one_datasource_leaves_the_others_alone(
    session: AsyncSession,
) -> None:
    """The ranker is organization-wide, as the human worklist is; the run's own
    datasource scope is applied to the routines it then loads."""
    org, hot, quiet = await _hot_and_cold(session)

    outcome = await _run(session, org, limit=10, datasource_id=quiet.datasource_id)
    assert {item.subject_name for item in outcome.items} == {hot.name, quiet.name}

    other = await seed_estate(session, organization=org)
    outcome = await _run(session, org, limit=10, datasource_id=other[1].id)
    assert outcome.items == []


async def test_another_organizations_routine_is_never_reached(session: AsyncSession) -> None:
    org, hot, quiet = await _hot_and_cold(session)
    other_org, other_ds, other_schema = await seed_estate(session)
    ledger = await seed_table(session, other_org, other_ds, other_schema, name="ledger")
    positions = await seed_table(session, other_org, other_ds, other_schema, name="positions")
    theirs = await _routine(session, other_org, other_ds, other_schema, name="sp_aaa_theirs")
    await _writes(session, theirs, ledger, positions)
    await _queries(session, other_ds, positions, times=500)
    await session.flush()

    outcome = await _run(session, org, limit=10)

    assert {item.subject_name for item in outcome.items} == {hot.name, quiet.name}
