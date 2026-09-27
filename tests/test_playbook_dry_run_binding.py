"""R11-REV01: stored playbook dry-runs, and runs bound to what was previewed.

`GET /v1/playbooks/{id}/dry-run` shows what a run would do *now*; a steward who then pressed
Run could not tell whether the run did what they had previewed. These tests pin the binding:

* storing a dry-run records the rule version, which subjects matched and each one's evidence
  version -- ids and digests only -- and applies nothing;
* a run bound to an unchanged preview runs through the same `evaluate_and_run_playbook`, and
  the record names the run it bound;
* a run bound to a preview the catalog or the rule has since moved away from is refused
  (`PREVIEW_MISMATCH`) with what moved counted -- added, removed, changed -- and applies
  nothing; `require_match=false` runs it anyway and records `DIFFERS`;
* a run that acts on other subjects than the evaluation it was just checked against
  (`RUN_DIVERGED_FROM_PREVIEW`) is rolled back whole;
* a preview binds one run; another organization's preview is not found.

Real in-memory SQLite with SQLAlchemy's aiosqlite BEGIN recipe, so the rolled-back run is
genuinely rolled back (pysqlite otherwise lets a per-item savepoint commit on release).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida import playbooks as playbooks_module
from aida import review_batch_models  # noqa: F401 -- registers the dry-run table
from aida.db import Base
from aida.models import AssetTag, AuditEvent, MetadataPlaybook, MetadataTable
from aida.playbooks_api import (
    PlaybookBoundRunCreate,
    run_playbook_as_previewed,
    store_playbook_dry_run,
)
from aida.review_batch_models import PlaybookDryRunRecord
from tests.support.review_batch_estate import Estate, add_tables, build_estate, reviewer


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _explicit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _steward(estate: Estate) -> Any:
    return reviewer(estate.organization_id, "steward@example.com", roles=frozenset({"DataSteward"}))


async def _tag_playbook(
    session: AsyncSession, estate: Estate, *, auto_apply_max_items: int = 20
) -> MetadataPlaybook:
    playbook = MetadataPlaybook(
        organization_id=estate.organization_id,
        name=f"tag-{uuid4().hex[:6]}",
        action="TAG",
        datasource_id=estate.datasource.id,
        match_field="TABLE_NAME",
        match_pattern="orders_*",
        column_name_pattern=None,
        action_parameters={"tag_key": "pii-review", "tag_value": "pending"},
        schedule_interval_minutes=60,
        auto_apply_max_items=auto_apply_max_items,
        enabled=True,
        created_by="steward@example.com",
    )
    session.add(playbook)
    await session.commit()
    return playbook


async def _tag_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AssetTag)) or 0)


async def test_a_stored_dry_run_holds_digests_and_ids_and_applies_nothing(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 4, prefix="orders")
    playbook = await _tag_playbook(session, estate)

    stored = await store_playbook_dry_run(
        playbook.id, context=_steward(estate), session=session
    )
    assert stored.matched_count == 4
    assert stored.predicted_disposition == "AUTOMATIC"
    assert stored.change_counts == {"CREATE": 4}
    assert len(stored.match_digest) == len(stored.evidence_digest) == 64
    assert {item.subject_id for item in stored.items} == {t.id for t in tables}

    record = await session.get(PlaybookDryRunRecord, stored.dry_run_id)
    assert record is not None
    assert record.organization_id == estate.organization_id
    # In the matcher's order, as the preview listed them.
    assert [pair[0] for pair in record.subject_versions] == [
        str(item.subject_id) for item in stored.items
    ]
    assert record.bound_at is None
    # Ids and hashes only: the values the preview displays are not stored.
    columns = repr({c.key: getattr(record, c.key) for c in record.__table__.columns})
    assert "pending" not in columns and "pii-review" not in columns
    assert await _tag_count(session) == 0
    await session.refresh(playbook)
    assert playbook.last_run_at is None
    audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "playbook.dry_run_store")
        )
    ).all()
    assert [a.details["dry_run_id"] for a in audits] == [str(stored.dry_run_id)]


async def test_a_run_bound_to_an_unchanged_preview_runs_and_names_its_run(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    await add_tables(session, estate, 3, prefix="orders")
    playbook = await _tag_playbook(session, estate)
    context = _steward(estate)
    stored = await store_playbook_dry_run(playbook.id, context=context, session=session)

    bound = await run_playbook_as_previewed(
        playbook.id, stored.dry_run_id, PlaybookBoundRunCreate(), context=context, session=session
    )
    assert bound.ran is True
    assert bound.refusal_code is None
    assert (bound.binding.status, bound.binding.reasons) == ("MATCHES", [])
    assert bound.run is not None
    assert (bound.run.outcome, bound.run.matched_count) == ("AUTO_APPLIED", 3)
    assert await _tag_count(session) == 3

    record = await session.get(PlaybookDryRunRecord, stored.dry_run_id)
    assert record is not None
    await session.refresh(record)
    assert (record.bound_binding_status, record.bound_run_outcome) == ("MATCHES", "AUTO_APPLIED")
    assert record.bound_bulk_action_run_id == bound.run.bulk_action_run_id
    assert record.bound_by == "steward@example.com"

    # A preview binds one run.
    with pytest.raises(HTTPException) as again:
        await run_playbook_as_previewed(
            playbook.id,
            stored.dry_run_id,
            PlaybookBoundRunCreate(),
            context=context,
            session=session,
        )
    assert (again.value.status_code, again.value.detail) == (409, "PLAYBOOK_DRY_RUN_ALREADY_BOUND")


async def test_a_queued_run_is_bound_through_its_operation(session: AsyncSession) -> None:
    """Above the auto-apply bound the run queues a reviewed operation; the subjects it acted
    on are read back from that operation's own subject list."""
    estate = await build_estate(session)
    await add_tables(session, estate, 3, prefix="orders")
    playbook = await _tag_playbook(session, estate, auto_apply_max_items=0)
    context = _steward(estate)
    stored = await store_playbook_dry_run(playbook.id, context=context, session=session)
    assert stored.predicted_disposition == "HUMAN_REVIEW"
    bound = await run_playbook_as_previewed(
        playbook.id, stored.dry_run_id, PlaybookBoundRunCreate(), context=context, session=session
    )
    assert bound.ran is True and bound.run is not None
    assert bound.run.outcome == "QUEUED_FOR_REVIEW"
    record = await session.get(PlaybookDryRunRecord, stored.dry_run_id)
    assert record is not None
    await session.refresh(record)
    assert record.bound_bulk_stewardship_operation_id == bound.run.bulk_stewardship_operation_id


async def test_a_run_bound_to_a_moved_preview_is_refused_and_applies_nothing(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 4, prefix="orders")
    playbook = await _tag_playbook(session, estate)
    context = _steward(estate)
    stored = await store_playbook_dry_run(playbook.id, context=context, session=session)

    # Between the preview and the run: one new matching table (added), one renamed out of
    # the pattern (removed), one tagged by someone else (its evidence version moves).
    [_new] = await add_tables(session, estate, 1, prefix="orders_new")
    renamed = await session.get(MetadataTable, tables[0].id)
    assert renamed is not None
    renamed.name = "archive_0000"
    session.add(
        AssetTag(
            organization_id=estate.organization_id,
            table_id=tables[1].id,
            tag_key="pii-review",
            tag_value="pending",
            applied_by="someone",
        )
    )
    await session.commit()
    tags_before = await _tag_count(session)

    refused = await run_playbook_as_previewed(
        playbook.id, stored.dry_run_id, PlaybookBoundRunCreate(), context=context, session=session
    )
    assert refused.ran is False
    assert refused.refusal_code == "PREVIEW_MISMATCH"
    assert refused.run is None
    binding = refused.binding
    assert binding.status == "DIFFERS"
    assert set(binding.reasons) == {"MATCH_SET_CHANGED", "EVIDENCE_CHANGED"}
    assert (binding.added_count, binding.removed_count, binding.changed_count) == (1, 1, 1)
    assert set(binding.moved_subject_ids) == {_new.id, tables[0].id, tables[1].id}
    assert await _tag_count(session) == tags_before  # nothing applied
    await session.refresh(playbook)
    assert playbook.last_run_at is None  # the run never started
    record = await session.get(PlaybookDryRunRecord, stored.dry_run_id)
    assert record is not None
    await session.refresh(record)
    assert record.bound_at is None  # a refused binding does not use the preview up

    # Knowingly running it anyway: it runs, and the record says it differed.
    forced = await run_playbook_as_previewed(
        playbook.id,
        stored.dry_run_id,
        PlaybookBoundRunCreate(require_match=False),
        context=context,
        session=session,
    )
    assert forced.ran is True and forced.run is not None
    assert forced.binding.status == "DIFFERS"
    await session.refresh(record)
    assert record.bound_binding_status == "DIFFERS"


async def test_an_edited_rule_no_longer_matches_its_preview(session: AsyncSession) -> None:
    estate = await build_estate(session)
    await add_tables(session, estate, 2, prefix="orders")
    playbook = await _tag_playbook(session, estate)
    context = _steward(estate)
    stored = await store_playbook_dry_run(playbook.id, context=context, session=session)
    playbook.action_parameters = {"tag_key": "pii-review", "tag_value": "cleared"}
    await session.commit()
    refused = await run_playbook_as_previewed(
        playbook.id, stored.dry_run_id, PlaybookBoundRunCreate(), context=context, session=session
    )
    assert refused.ran is False
    assert refused.binding.reasons == ["RULE_CHANGED"]
    assert refused.binding.match_set_matches is True


async def test_a_run_that_acts_on_other_subjects_than_checked_is_rolled_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow race: the preview check passes, then a commit lands before the run's own
    matcher reads the catalog. Simulated by making the run's matcher see one subject fewer
    than the check did; the run is rolled back whole and the preview stays unbound."""
    estate = await build_estate(session)
    await add_tables(session, estate, 3, prefix="orders")
    playbook = await _tag_playbook(session, estate)
    context = _steward(estate)
    stored = await store_playbook_dry_run(playbook.id, context=context, session=session)

    real_resolve = playbooks_module.resolve_playbook_matches

    async def one_fewer(session_: AsyncSession, playbook_: MetadataPlaybook) -> list[UUID]:
        return (await real_resolve(session_, playbook_))[1:]

    monkeypatch.setattr(playbooks_module, "resolve_playbook_matches", one_fewer)
    diverged = await run_playbook_as_previewed(
        playbook.id, stored.dry_run_id, PlaybookBoundRunCreate(), context=context, session=session
    )
    assert diverged.ran is False
    assert diverged.refusal_code == "RUN_DIVERGED_FROM_PREVIEW"
    assert "RUN_DIVERGED_FROM_PREVIEW" in diverged.binding.reasons
    assert await _tag_count(session) == 0  # the two tags the run applied were rolled back
    record = await session.get(PlaybookDryRunRecord, stored.dry_run_id)
    assert record is not None
    await session.refresh(record)
    assert record.bound_at is None


async def test_bound_runs_are_scoped_and_need_an_enabled_playbook(session: AsyncSession) -> None:
    estate = await build_estate(session)
    other = await build_estate(session, name="Other")
    await add_tables(session, estate, 1, prefix="orders")
    await add_tables(session, other, 1, prefix="orders")
    mine = await _tag_playbook(session, estate)
    theirs = await _tag_playbook(session, other)
    their_preview = await store_playbook_dry_run(
        theirs.id, context=_steward(other), session=session
    )
    context = _steward(estate)
    with pytest.raises(HTTPException) as foreign:
        # Another organization's preview, asked for through this organization's playbook.
        await run_playbook_as_previewed(
            mine.id,
            their_preview.dry_run_id,
            PlaybookBoundRunCreate(),
            context=context,
            session=session,
        )
    assert (foreign.value.status_code, foreign.value.detail) == (404, "PLAYBOOK_DRY_RUN_NOT_FOUND")

    my_preview = await store_playbook_dry_run(mine.id, context=context, session=session)
    mine.enabled = False
    await session.commit()
    with pytest.raises(HTTPException) as disabled:
        await run_playbook_as_previewed(
            mine.id, my_preview.dry_run_id, PlaybookBoundRunCreate(), context=context,
            session=session,
        )
    assert disabled.value.status_code == 409
