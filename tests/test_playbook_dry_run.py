"""R11-REV01: playbook rule dry-run with versioned evidence, and automation stated per action.

The dry-run must (a) write nothing, (b) match exactly what the run would match, (c) predict
the run's disposition with the run's own comparison, (d) version the rule and every subject
so a re-run shows what moved, and (e) say, for the specific action and bound, whether the run
applies without a human -- never "playbooks are human-only".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida import playbooks as playbooks_module
from aida import stewardship_service
from aida.db import Base
from aida.models import AssetTag, MetadataPlaybook
from aida.playbook_dry_run import PLAYBOOK_ACTION_AUTOMATION, dry_run_playbook
from aida.playbooks import evaluate_and_run_playbook, resolve_playbook_matches
from aida.playbooks_api import dry_run_playbook_now
from tests.support.review_batch_estate import (
    Estate,
    add_columns,
    add_tables,
    build_estate,
    reviewer,
)

_WRITES = ("INSERT", "UPDATE", "DELETE")


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    created = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active


@pytest.fixture
def statements(engine: Any) -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _playbook(estate: Estate, action: str = "TAG", **overrides: Any) -> MetadataPlaybook:
    parameters = {
        "TAG": {"tag_key": "pii-review", "tag_value": "pending"},
        "CLASSIFY": {"classification": "CONFIDENTIAL"},
        "OWN": {"owner_type": "INDIVIDUAL", "owner_principal": "owner@example.com"},
        "CERTIFY": {"rationale": "certified by the finance steward", "expires_after_days": 90},
    }[action]
    values: dict[str, Any] = {
        "organization_id": estate.organization_id,
        "name": f"{action.lower()}-playbook",
        "action": action,
        "datasource_id": estate.datasource.id,
        "match_field": "TABLE_NAME",
        "match_pattern": "*",
        "column_name_pattern": "*" if action == "CLASSIFY" else None,
        "action_parameters": parameters,
        "schedule_interval_minutes": 60,
        "auto_apply_max_items": 0,
        "enabled": True,
        "created_by": "steward@example.com",
    }
    values.update(overrides)
    return MetadataPlaybook(**values)


def _steward(estate: Estate) -> Any:
    return reviewer(estate.organization_id, "steward@example.com", roles=frozenset({"DataSteward"}))


async def test_dry_run_over_1000_tables_writes_nothing_and_reports_truncation(
    session: AsyncSession, statements: list[str]
) -> None:
    estate = await build_estate(session)
    await add_tables(session, estate, 1000)
    playbook = _playbook(estate)
    session.add(playbook)
    await session.commit()
    session.expunge_all()

    statements.clear()
    preview = await dry_run_playbook_now(
        playbook.id, context=_steward(estate), session=session
    )
    assert not [s for s in statements if s.lstrip().upper().startswith(_WRITES)]
    # Playbook load, matcher scan, subject reload, current tags: fixed, not per subject.
    assert len(statements) <= 5, statements

    # The matcher caps at CATALOG_BULK_ACTION_MAX_ITEMS (500) and the run silently acts on
    # the first 500; the dry-run is where that becomes visible.
    assert preview.matched_count == 500
    assert preview.tables_truncated is True
    assert len(preview.items) == 500
    assert {item.change for item in preview.items} == {"CREATE"}
    assert preview.predicted_disposition == "HUMAN_REVIEW"
    assert preview.automation.has_automatic_branch is True
    assert preview.automation.automatic_branch_enabled is False
    assert preview.automation.involves_model is False

    reloaded = await session.get(MetadataPlaybook, playbook.id)
    assert reloaded is not None and reloaded.last_run_at is None  # not even the due-clock


async def test_dry_run_matches_exactly_what_the_run_matches(session: AsyncSession) -> None:
    estate = await build_estate(session)
    await add_tables(session, estate, 12, prefix="orders")
    await add_tables(session, estate, 5, prefix="audit")
    playbook = _playbook(estate, match_pattern="orders_*")
    session.add(playbook)
    await session.flush()
    preview = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    assert [item.subject_id for item in preview.items] == await resolve_playbook_matches(
        session, playbook
    )
    assert all(item.qualified_name.startswith("finance.orders_") for item in preview.items)


@pytest.mark.parametrize(
    ("bound", "predicted", "actual"),
    [(20, "AUTOMATIC", "AUTO_APPLIED"), (3, "HUMAN_REVIEW", "QUEUED_FOR_REVIEW")],
)
async def test_predicted_disposition_is_what_the_run_does(
    session: AsyncSession, bound: int, predicted: str, actual: str
) -> None:
    """Automation is stated for this action and this bound: TAG applies with no human when
    0 < matched <= auto_apply_max_items, and goes to maker-checker review above it."""
    estate = await build_estate(session)
    await add_tables(session, estate, 8)
    playbook = _playbook(estate, auto_apply_max_items=bound)
    session.add(playbook)
    await session.flush()
    preview = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    assert preview.predicted_disposition == predicted
    outcome = await evaluate_and_run_playbook(session, playbook)
    assert outcome.outcome == actual
    assert outcome.matched_count == preview.matched_count


async def test_evidence_and_rule_versions_move_with_what_they_describe(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 3)
    playbook = _playbook(estate)
    session.add(playbook)
    await session.flush()
    first = await dry_run_playbook(session, playbook, now=datetime.now(UTC))

    # One subject's current state changes: only its evidence version moves.
    session.add(
        AssetTag(
            organization_id=estate.organization_id,
            table_id=tables[1].id,
            tag_key="pii-review",
            tag_value="pending",
            applied_by="someone",
        )
    )
    await session.flush()
    second = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    moved = {
        a.subject_id
        for a, b in zip(first.items, second.items, strict=True)
        if a.evidence_version != b.evidence_version
    }
    assert moved == {tables[1].id}
    changed_item = next(i for i in second.items if i.subject_id == tables[1].id)
    assert (changed_item.current_value, changed_item.change) == (
        "pii-review=pending",
        "NO_CHANGE",
    )
    assert second.rule_version == first.rule_version

    playbook.action_parameters = {"tag_key": "pii-review", "tag_value": "cleared"}
    await session.flush()
    third = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    assert third.rule_version != first.rule_version
    assert next(i for i in third.items if i.subject_id == tables[1].id).change == "UPDATE"


async def test_classify_over_a_1000_column_table_is_bounded_and_truncated(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    [wide] = await add_tables(session, estate, 1, prefix="wide")
    await add_columns(session, wide, 1000)
    playbook = _playbook(estate, "CLASSIFY")
    session.add(playbook)
    await session.flush()
    preview = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    assert preview.matched_count == 500
    assert preview.columns_truncated is True
    assert preview.tables_truncated is False
    assert {item.change for item in preview.items} == {"UPDATE"}
    assert {item.current_value for item in preview.items} == {"UNCLASSIFIED"}
    assert preview.automation.subject_type == "COLUMN"


@pytest.mark.parametrize("action", ["OWN", "CERTIFY"])
async def test_own_and_certify_previews(session: AsyncSession, action: str) -> None:
    estate = await build_estate(session)
    await add_tables(session, estate, 2)
    playbook = _playbook(estate, action)
    session.add(playbook)
    await session.flush()
    preview = await dry_run_playbook(session, playbook, now=datetime.now(UTC))
    assert preview.matched_count == 2
    assert {item.change for item in preview.items} == {"CREATE"}
    expected = (
        "INDIVIDUAL:owner@example.com:ACTIVE"
        if action == "OWN"
        else "CERTIFIED:expires_after_days=90"
    )
    assert {item.proposed_value for item in preview.items} == {expected}


def test_per_action_automation_matches_the_code_paths_it_describes() -> None:
    """The registry is a statement about real code; keep it in step with it."""
    operation_types = playbooks_module._OPERATION_TYPE_FOR_ACTION
    reversals = stewardship_service._REVERSAL_OF
    assert set(PLAYBOOK_ACTION_AUTOMATION) == set(operation_types)
    for action, automation in PLAYBOOK_ACTION_AUTOMATION.items():
        assert automation.reviewed_operation_type == operation_types[action]
        assert automation.compensating_operation_when_reviewed == reversals[
            operation_types[action]
        ]
        # Every action has the automatic branch; none of them consults a model; an
        # auto-applied run records no before-image, so it has no governed reversal.
        assert automation.has_automatic_branch is True
        assert automation.involves_model is False
        assert automation.compensating_operation_when_automatic is None


async def test_the_run_itself_is_unchanged(session: AsyncSession) -> None:
    """The matcher refactor returns the same ids to the run as before."""
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 4)
    playbook = _playbook(estate, auto_apply_max_items=10)
    session.add(playbook)
    await session.flush()
    assert set(await resolve_playbook_matches(session, playbook)) == {t.id for t in tables}
    outcome = await evaluate_and_run_playbook(session, playbook)
    assert (outcome.outcome, outcome.matched_count) == ("AUTO_APPLIED", 4)
    tags = (await session.scalars(select(AssetTag))).all()
    assert len(tags) == 4
