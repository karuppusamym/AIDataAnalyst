"""R11-FP12: the batched "changed since published" count, against the per-version reading.

The screens could only ask about one version at a time, and only when somebody pressed a button,
because `load_coverage_changes` costs a round of queries per version.
`load_changes_since_published_counts` answers the same question for a whole list in a fixed number
of queries, which is what lets a list row carry a passive badge.

The property that matters is that the two agree. Everything below is one estate, one set of
movements, and two versions with different baselines and different scopes: whatever the per-version
reading finds for a version, the batched one counts for that version -- including the cases the
per-version reading is careful about (a move before publication, a move outside the scope, text
replaced and restored, a re-approval of identical text, another organization's row).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.column_documentation import publish_column_description
from aida.context_product_api import (
    create_context_product,
    list_context_product_changes_since_published,
)
from aida.context_product_coverage import (
    PinnedScope,
    PublishedScope,
    load_changes_since_published_counts,
    load_coverage_changes,
    load_pinned_meaning,
    load_pinned_meaning_moved_counts,
    publication_time,
)
from aida.context_product_read_service import COMPILER_ROLES
from aida.models import (
    ColumnDocumentationVersion,
    GlossaryTerm,
    GlossaryTermVersion,
    SemanticModelVersion,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.routine_description_service import publish_routine_documentation_version
from aida.schemas import ContextProductCreate
from aida.security import SecurityContext
from tests.support.task_agents import task_agent_session
from tests.test_context_rebuild import Estate, _estate
from tests.test_r11_fp12_coverage_invalidation import (
    _column,
    _customers,
    _describe_table,
    _publish_product,
    _routine,
    _signal,
)

MATRIX = (
    Path(__file__).resolve().parent.parent / "Docs" / "50-security" / "surface-control-matrix.md"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with task_agent_session() as active:
        yield active


async def _moved_estate(session: AsyncSession) -> tuple[Estate, dict[str, Any]]:
    """An estate where things moved, on both sides of two publication moments."""
    estate = await _estate(session)
    customers = await _customers(estate)
    routine = await _routine(estate, "rebuild_totals")
    elsewhere = await _routine(estate, "unrelated")
    amount = await _column(estate, estate.orders, "amount")

    # Before either baseline: none of this is a change *since* one.
    await _describe_table(estate, estate.orders, "Orders placed online.")
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL")
    await _describe_table(estate, customers, "Customers of the bank.")
    await publish_column_description(
        session,
        organization_id=estate.org.id,
        table_id=estate.orders.id,
        column_id=amount.id,
        description="Gross amount.",
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=datetime.now(UTC),
    )
    await publish_routine_documentation_version(
        session,
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        routine_id=routine.id,
        description="Rebuilds totals.",
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=datetime.now(UTC),
    )
    await session.commit()
    early = datetime.now(UTC)

    # Between the two baselines: the view's definition moves and the orders description is
    # replaced. The earlier version is stale for both; the later one is stale for neither.
    between = early + timedelta(seconds=1)
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL", between)
    await session.commit()
    await _describe_table(estate, estate.orders, "Orders from every channel.")
    await session.commit()
    late = between + timedelta(seconds=1)

    # After both: the routine moves, its description is re-approved word for word (never a
    # change), the column description is withdrawn, the customers description is replaced and
    # then restored (net nothing), a routine outside every scope moves, and another
    # organization's signal names a covered subject.
    later = late + timedelta(seconds=1)
    _signal(estate, "ROUTINE", routine.id, "DEFINITION_CHANGED", "LITERAL_ONLY", later)
    _signal(estate, "ROUTINE", elsewhere.id, "DEFINITION_CHANGED", "STRUCTURAL", later)
    stranger = _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL", later)
    stranger.organization_id = uuid4()
    await session.commit()
    column_description = await session.scalar(
        select(ColumnDocumentationVersion).where(ColumnDocumentationVersion.status == "APPROVED")
    )
    assert column_description is not None
    column_description.status = "WITHDRAWN"
    column_description.updated_at = datetime.now(UTC)
    await publish_routine_documentation_version(
        session,
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        routine_id=routine.id,
        description="Rebuilds totals.",
        created_by="steward-1",
        approved_by="reviewer-2",
        approved_at=datetime.now(UTC),
    )
    await session.commit()
    await _describe_table(estate, customers, "Former customers too.")
    await _describe_table(estate, customers, "Customers of the bank.")
    await session.commit()

    return estate, {
        "customers": customers,
        "routine": routine,
        "elsewhere": elsewhere,
        "amount": amount,
        "early": early,
        "late": late,
    }


def _scopes(estate: Estate, seeded: dict[str, Any]) -> list[PublishedScope]:
    """Two published versions: an early one over the whole scope, a later one over less."""
    return [
        PublishedScope(
            version_id=UUID("00000000-0000-0000-0000-0000000000e1"),
            table_ids=(estate.orders.id, estate.view.id, seeded["customers"].id),
            routine_ids=(seeded["routine"].id,),
            since=seeded["early"],
        ),
        PublishedScope(
            version_id=UUID("00000000-0000-0000-0000-0000000000e2"),
            table_ids=(estate.orders.id,),
            routine_ids=(seeded["routine"].id,),
            since=seeded["late"],
        ),
    ]


async def test_the_batched_count_is_the_per_version_readings_own_answer(
    session: AsyncSession,
) -> None:
    estate, seeded = await _moved_estate(session)
    scopes = _scopes(estate, seeded)

    counts = await load_changes_since_published_counts(session, estate.org.id, scopes)

    for scope in scopes:
        expected = await load_coverage_changes(
            session,
            estate.org.id,
            list(scope.table_ids),
            list(scope.routine_ids),
            since=scope.since,
        )
        assert counts[scope.version_id] == len(expected), scope.version_id
    # And the two versions really do differ, or the agreement above would prove little.
    assert counts[scopes[0].version_id] > counts[scopes[1].version_id] > 0


async def test_a_version_that_was_never_published_is_not_counted(session: AsyncSession) -> None:
    estate, seeded = await _moved_estate(session)
    draft = PublishedScope(
        version_id=UUID("00000000-0000-0000-0000-0000000000e3"),
        table_ids=(estate.orders.id, estate.view.id),
        routine_ids=(),
        since=None,
    )

    counts = await load_changes_since_published_counts(
        session, estate.org.id, [*_scopes(estate, seeded), draft]
    )

    assert draft.version_id not in counts
    assert await load_coverage_changes(
        session, estate.org.id, list(draft.table_ids), [], since=None
    ) == []


async def test_another_organization_counts_nothing_of_this_one(session: AsyncSession) -> None:
    estate, seeded = await _moved_estate(session)

    counts = await load_changes_since_published_counts(session, uuid4(), _scopes(estate, seeded))

    assert set(counts.values()) == {0}


async def test_the_query_count_does_not_grow_with_the_number_of_versions(
    session: AsyncSession,
) -> None:
    """The point of the batched reading: a list of products costs what one product costs."""
    estate, seeded = await _moved_estate(session)
    scopes = _scopes(estate, seeded)
    many = [
        PublishedScope(
            version_id=uuid4(),
            table_ids=scopes[0].table_ids,
            routine_ids=scopes[0].routine_ids,
            since=scopes[0].since,
        )
        for _ in range(20)
    ]

    async def queries(asked: list[PublishedScope]) -> int:
        counted = 0

        def count(*_args: Any, **_kwargs: Any) -> None:
            nonlocal counted
            counted += 1

        event.listen(session.sync_session, "do_orm_execute", count)
        try:
            await load_changes_since_published_counts(session, estate.org.id, asked)
        finally:
            event.remove(session.sync_session, "do_orm_execute", count)
        return counted

    one = await queries(scopes[:1])
    twenty_two = await queries([*scopes, *many])

    assert one == twenty_two
    assert twenty_two <= 4, "one query for the signals and one per documentation store"

# --- the route the screens read -------------------------------------------------------------


async def test_the_route_answers_for_every_product_a_caller_may_list(session: AsyncSession) -> None:
    estate, seeded = await _moved_estate(session)
    first = await _publish_product(
        estate, "orders-context", table_ids=[str(estate.orders.id), str(estate.view.id)]
    )
    second = await _publish_product(
        estate, "customers-context", table_ids=[str(seeded["customers"].id)]
    )

    answer = await list_context_product_changes_since_published(
        estate.project.id, limit=200, context=estate.steward, session=session
    )

    assert answer.project_id == estate.project.id
    assert answer.truncated is False
    by_version = {item.version_id: item for item in answer.items}
    assert set(by_version) == {first.id, second.id}
    for version in (first, second):
        expected = await load_coverage_changes(
            session,
            estate.org.id,
            version.table_ids,
            version.routine_ids,
            since=publication_time(version),
        )
        assert by_version[version.id].changed_subjects == len(expected), version.product_id
        assert by_version[version.id].status == "PUBLISHED"
    # The orders product covers the view whose definition moved; the customers one covers a
    # table whose description was replaced and then restored, which is net no change.
    assert by_version[first.id].changed_subjects > 0
    assert by_version[second.id].changed_subjects == 0


async def test_a_version_never_published_reads_as_no_baseline_not_as_unchanged(
    session: AsyncSession,
) -> None:
    """`null` and `0` are different answers, and a screen must be able to tell them apart."""
    estate, _ = await _moved_estate(session)
    await create_context_product(
        estate.project.id,
        ContextProductCreate(
            product_key="draft-context",
            name="Draft",
            description="A product nobody has published yet.",
            purpose="Answer nothing yet.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            allowed_consumer_roles=["Analyst"],
            table_ids=[str(estate.orders.id)],
        ),
        context=estate.steward,
        session=session,
    )
    await session.commit()

    answer = await list_context_product_changes_since_published(
        estate.project.id, limit=200, context=estate.steward, session=session
    )

    (draft,) = [item for item in answer.items if item.status == "DRAFT"]
    assert draft.changed_subjects is None


async def test_it_says_when_it_did_not_reach_every_product(session: AsyncSession) -> None:
    estate, _ = await _moved_estate(session)
    for index in range(3):
        await _publish_product(
            estate, f"context-{index}", table_ids=[str(estate.orders.id)]
        )

    answer = await list_context_product_changes_since_published(
        estate.project.id, limit=2, context=estate.steward, session=session
    )

    assert answer.truncated is True
    assert len(answer.items) == 2


async def test_another_organizations_project_is_refused_as_the_product_list_refuses_it(
    session: AsyncSession,
) -> None:
    estate, _ = await _moved_estate(session)
    await _publish_product(estate, "orders-context", table_ids=[str(estate.orders.id)])
    stranger = SecurityContext(
        principal_id="steward-2",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )

    with pytest.raises(HTTPException) as refusal:
        await list_context_product_changes_since_published(
            estate.project.id, limit=200, context=stranger, session=session
        )

    assert refusal.value.status_code == 403


async def test_one_products_own_versions_are_answered_for_the_rollout_list(
    session: AsyncSession,
) -> None:
    """The rollout screen chooses among a product's versions, so it asks about that product."""
    estate, seeded = await _moved_estate(session)
    orders = await _publish_product(
        estate, "orders-context", table_ids=[str(estate.orders.id), str(estate.view.id)]
    )
    await _publish_product(estate, "customers-context", table_ids=[str(seeded["customers"].id)])

    answer = await list_context_product_changes_since_published(
        estate.project.id,
        product_id=orders.product_id,
        limit=200,
        context=estate.steward,
        session=session,
    )

    assert [item.version_id for item in answer.items] == [orders.id]
    expected = await load_coverage_changes(
        session,
        estate.org.id,
        orders.table_ids,
        orders.routine_ids,
        since=publication_time(orders),
    )
    assert answer.items[0].changed_subjects == len(expected)


async def test_a_product_the_caller_cannot_list_is_not_answered_for(session: AsyncSession) -> None:
    estate, _ = await _moved_estate(session)
    await _publish_product(estate, "orders-context", table_ids=[str(estate.orders.id)])

    with pytest.raises(HTTPException) as refusal:
        await list_context_product_changes_since_published(
            estate.project.id,
            product_id=uuid4(),
            limit=200,
            context=estate.steward,
            session=session,
        )

    assert refusal.value.status_code == 404


def test_the_route_admits_exactly_the_roles_the_coverage_doors_admit() -> None:
    """A coverage reading, so the compile route's roles -- not the product list's, which admits
    Viewer and Auditor. The matrix is generated from the app, so this reads the contract."""
    row = next(
        line
        for line in MATRIX.read_text(encoding="utf-8").splitlines()
        if "list_context_product_changes_since_published" in line
    )
    declared = [role.strip() for role in row.split("|")[4].split(",")]

    assert declared == sorted(COMPILER_ROLES)

# --- pinned meaning that no longer stands -------------------------------------------------


async def _pinned_meaning(estate: Estate) -> dict[str, Any]:
    """One of each kind of pin: an ontology version its ontology has moved past, and a semantic
    model version and a glossary definition that still stand."""
    session = estate.session
    model = SemanticModelVersion(
        organization_id=estate.org.id,
        project_id=estate.project.id,
        version=1,
        name="Revenue model",
        change_summary="First.",
        status="PUBLISHED",
        created_by="modeller",
    )
    term = GlossaryTerm(organization_id=estate.org.id, term_key="net_revenue")
    session.add_all([model, term])
    await session.flush()
    definition = GlossaryTermVersion(
        organization_id=estate.org.id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net revenue",
        definition="Revenue after discounts.",
        created_by="steward-2",
    )
    head = OntologyHead(
        organization_id=estate.org.id, ontology_key="commerce", last_version=2, published_version=2
    )
    session.add_all([definition, head])
    await session.flush()
    first = OntologyVersion(
        organization_id=estate.org.id,
        ontology_id=head.id,
        version=1,
        base_version=0,
        status="APPROVED",
        definition={"name": "Commerce", "concepts": [], "mappings": []},
        created_by="author",
    )
    session.add(first)
    await session.commit()
    return {"model": model, "term": term, "definition": definition, "ontology": first}


async def _per_version_moved(estate: Estate, pin: PinnedScope) -> int:
    resolved = await load_pinned_meaning(
        estate.session,
        estate.org.id,
        ontology_version_ids=pin.ontology_version_ids,
        semantic_model_version_ids=pin.semantic_model_version_ids,
        glossary_term_version_ids=pin.glossary_term_version_ids,
        scope_table_ids=[],
        scope_routine_ids=[],
    )
    return sum(1 for entry in resolved if not entry.current)


async def test_the_batched_meaning_count_is_load_pinned_meanings_own_answer(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    seeded = await _pinned_meaning(estate)
    every_kind = PinnedScope(
        version_id=UUID("00000000-0000-0000-0000-0000000000f1"),
        ontology_version_ids=[str(seeded["ontology"].id)],
        semantic_model_version_ids=[str(seeded["model"].id)],
        # A pin that resolves to nothing in this organization is not counted either way.
        glossary_term_version_ids=[str(seeded["definition"].id), str(uuid4())],
    )
    standing_only = PinnedScope(
        version_id=UUID("00000000-0000-0000-0000-0000000000f2"),
        ontology_version_ids=[],
        semantic_model_version_ids=[str(seeded["model"].id)],
        glossary_term_version_ids=[str(seeded["definition"].id)],
    )
    nothing_pinned = PinnedScope(
        version_id=UUID("00000000-0000-0000-0000-0000000000f3"),
        ontology_version_ids=[],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
    )
    pins = [every_kind, standing_only, nothing_pinned]

    before = await load_pinned_meaning_moved_counts(session, estate.org.id, pins)
    seeded["model"].status = "SUPERSEDED"
    seeded["term"].lifecycle_status = "DEPRECATED"
    await session.commit()
    after = await load_pinned_meaning_moved_counts(session, estate.org.id, pins)

    ids = (every_kind.version_id, standing_only.version_id, nothing_pinned.version_id)
    assert before == dict(zip(ids, (1, 0, 0), strict=True))
    assert after == dict(zip(ids, (3, 2, 0), strict=True))
    for pin in pins:
        assert after[pin.version_id] == await _per_version_moved(estate, pin), pin.version_id
    # Another organization's reader resolves none of these pins.
    assert set((await load_pinned_meaning_moved_counts(session, uuid4(), pins)).values()) == {0}


async def test_the_route_carries_the_meaning_count_for_every_version(session: AsyncSession) -> None:
    estate = await _estate(session)
    seeded = await _pinned_meaning(estate)
    version = await _publish_product(
        estate,
        "commerce-context",
        table_ids=[str(estate.orders.id)],
        glossary_term_version_ids=[str(seeded["definition"].id)],
    )
    seeded["term"].lifecycle_status = "DEPRECATED"
    await session.commit()

    answer = await list_context_product_changes_since_published(
        estate.project.id, limit=200, context=estate.steward, session=session
    )

    (item,) = [row for row in answer.items if row.version_id == version.id]
    assert item.meaning_moved == 1

