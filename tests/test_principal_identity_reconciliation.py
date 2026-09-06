"""F19 -- principal-leaver reconciliation now has a production trigger.

`ownership_principal_lifecycle` and `identity_events` contained real, tested
reconciliation that no inspected entry point could reach, and a docstring
describing an outbox consumer that did not exist. The remediation is
`principal_reconciliation.run_principal_reconciliation_pass`, called from the
fleet scheduler, replaying the two identity-lifecycle outbox event types that
`identity_events` writes.

What has to be true for that to be safe, and is tested here:

* it is OFF by default, so wiring a previously-unreachable module into a
  running process cannot start rewriting ownership on upgrade;
* a replay of an already-applied event changes nothing and writes nothing --
  which is the actual replay-safety guarantee, since the pass re-reads a
  lookback window every cycle by design;
* tenant scope comes from the event, so a delete in one organization does not
  lapse the same principal's ownership in another;
* the merge target is honoured, including the case where the successor already
  owns the subject.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida import principal_reconciliation as pass_module
from aida.db import Base
from aida.identity_events import (
    PRINCIPAL_DELETED_EVENT_TYPE,
    PRINCIPAL_MERGED_EVENT_TYPE,
    emit_principal_deleted,
)
from aida.models import AuditEvent, Organization, OutboxEvent, OwnershipAssignment
from aida.principal_reconciliation import (
    RECONCILED_EVENT_TYPES,
    reset_cadence,
    run_principal_reconciliation_pass,
)
from aida.security import SecurityContext
from atlas.platform.config import Settings

LEAVER = "leaver@bank.example"
SUCCESSOR = "successor@bank.example"


def _settings(*, enabled: bool = True, auto_reassign: bool = True) -> Settings:
    return Settings(
        principal_reconciliation_enabled=enabled,
        ownership_leaver_auto_reassign=auto_reassign,
    )


@pytest_asyncio.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """An in-memory session factory substituted for the pass's own, so the pass
    runs its real transaction-per-event shape rather than a stubbed one.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pass_module, "session_factory", maker)
    reset_cadence()
    yield maker
    reset_cadence()
    await engine.dispose()


async def _organization(session: AsyncSession, name: str) -> Organization:
    organization = Organization(name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    return organization


async def _assignment(
    session: AsyncSession,
    organization: Organization,
    *,
    principal: str,
    subject_id: str,
    owner_type: str = "STEWARD",
) -> OwnershipAssignment:
    assignment = OwnershipAssignment(
        organization_id=organization.id,
        subject_type="GLOSSARY_TERM",
        subject_id=subject_id,
        owner_type=owner_type,
        owner_principal=principal,
        status="ACTIVE",
        assignment_kind="MANUAL",
        assigned_by="admin",
    )
    session.add(assignment)
    await session.flush()
    return assignment


def _event(
    organization: Organization | None, event_type: str, payload: dict[str, Any]
) -> OutboxEvent:
    return OutboxEvent(
        organization_id=organization.id if organization else None,
        aggregate_type="principal",
        aggregate_id=str(payload.get("principal_id") or payload.get("from_principal_id")),
        event_type=event_type,
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_the_trigger_is_off_by_default() -> None:
    """Wiring a previously-unreachable module into the scheduler must not start
    mutating ownership in an existing deployment by surprise.
    """
    assert Settings().principal_reconciliation_enabled is False


async def test_disabled_pass_touches_nothing(sessions: Any) -> None:
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        assignment = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_DELETED_EVENT_TYPE,
                {"principal_id": LEAVER, "organization_id": str(organization.id)},
            )
        )
        await session.commit()
        assignment_id = assignment.id

    outcome = await run_principal_reconciliation_pass(_settings(enabled=False))
    assert outcome.skipped is True

    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, assignment_id)).status == "ACTIVE"


async def test_handler_gate_still_applies_when_the_pass_is_on(sessions: Any) -> None:
    """`ownership_leaver_auto_reassign` is an independent second gate: a
    deployment that requires every ownership flip to be a governed decision
    keeps that guarantee even with the pass enabled.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        assignment = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_DELETED_EVENT_TYPE,
                {"principal_id": LEAVER, "organization_id": str(organization.id)},
            )
        )
        await session.commit()
        assignment_id = assignment.id

    await run_principal_reconciliation_pass(_settings(auto_reassign=False))

    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, assignment_id)).status == "ACTIVE"


# ---------------------------------------------------------------------------
# It actually reconciles
# ---------------------------------------------------------------------------


async def test_pass_lapses_a_departed_principals_ownership(sessions: Any) -> None:
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        assignment = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_DELETED_EVENT_TYPE,
                {"principal_id": LEAVER, "organization_id": str(organization.id)},
            )
        )
        await session.commit()
        assignment_id = assignment.id

    outcome = await run_principal_reconciliation_pass(_settings())

    assert outcome.events_examined == 1
    assert outcome.assignments_lapsed == 1
    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, assignment_id)).status == "LAPSED_LEAVER"


async def test_pass_redirects_ownership_to_the_merge_target(sessions: Any) -> None:
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        assignment = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_MERGED_EVENT_TYPE,
                {
                    "from_principal_id": LEAVER,
                    "into_principal_id": SUCCESSOR,
                    "organization_id": str(organization.id),
                },
            )
        )
        await session.commit()
        assignment_id = assignment.id

    outcome = await run_principal_reconciliation_pass(_settings())

    assert outcome.assignments_reassigned == 1
    async with sessions() as session:
        row = await session.get(OwnershipAssignment, assignment_id)
        assert row.owner_principal == SUCCESSOR
        assert row.status == "ACTIVE"


async def test_merge_lapses_the_loser_when_the_successor_already_owns_the_subject(
    sessions: Any,
) -> None:
    """The unique-constraint case. Duplicating the row would be a constraint
    violation; leaving it would be the dangling ownership this feature exists
    to remove, so the losing row lapses.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        losing = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        await _assignment(session, organization, principal=SUCCESSOR, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_MERGED_EVENT_TYPE,
                {
                    "from_principal_id": LEAVER,
                    "into_principal_id": SUCCESSOR,
                    "organization_id": str(organization.id),
                },
            )
        )
        await session.commit()
        losing_id = losing.id

    outcome = await run_principal_reconciliation_pass(_settings())

    assert outcome.assignments_lapsed == 1
    assert outcome.assignments_reassigned == 0
    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, losing_id)).status == "LAPSED_LEAVER"


# ---------------------------------------------------------------------------
# Tenant scope
# ---------------------------------------------------------------------------


async def test_event_scope_confines_reconciliation_to_one_tenant(sessions: Any) -> None:
    """The same principal id can exist in two organizations. An event scoped to
    one must not lapse the other's ownership -- a cross-tenant write from a
    background pass is the worst version of this bug.
    """
    async with sessions() as session:
        mine = await _organization(session, "BankA")
        theirs = await _organization(session, "BankB")
        ours = await _assignment(session, mine, principal=LEAVER, subject_id="term-1")
        untouched = await _assignment(session, theirs, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                mine,
                PRINCIPAL_DELETED_EVENT_TYPE,
                {"principal_id": LEAVER, "organization_id": str(mine.id)},
            )
        )
        await session.commit()
        ours_id, untouched_id = ours.id, untouched.id

    await run_principal_reconciliation_pass(_settings())

    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, ours_id)).status == "LAPSED_LEAVER"
        assert (await session.get(OwnershipAssignment, untouched_id)).status == "ACTIVE"


async def test_event_without_an_organization_reconciles_every_tenant(sessions: Any) -> None:
    """Identity is a cross-tenant concern (ADR-0018): an event that genuinely
    names no organization means "everywhere this principal appears". That has
    to be a deliberate, tested behaviour rather than an accident of a missing
    filter.
    """
    async with sessions() as session:
        mine = await _organization(session, "BankA")
        theirs = await _organization(session, "BankB")
        first = await _assignment(session, mine, principal=LEAVER, subject_id="term-1")
        second = await _assignment(session, theirs, principal=LEAVER, subject_id="term-2")
        session.add(_event(None, PRINCIPAL_DELETED_EVENT_TYPE, {"principal_id": LEAVER}))
        await session.commit()
        ids = (first.id, second.id)

    await run_principal_reconciliation_pass(_settings())

    async with sessions() as session:
        for assignment_id in ids:
            assert (await session.get(OwnershipAssignment, assignment_id)).status == "LAPSED_LEAVER"


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------


async def _audit_and_outbox_counts(sessions: Any) -> tuple[int, int]:
    async with sessions() as session:
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "OWNERSHIP_AUTO_REASSIGNED_LEAVER")
        )
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "ownership.assignment.lapsed_leaver.v1")
        )
    return int(audits or 0), int(events or 0)


async def test_replaying_the_same_event_changes_and_writes_nothing(sessions: Any) -> None:
    """The guarantee that lets this be a *scheduled* pass at all: it re-reads a
    lookback window every cycle, so re-applying an event must be free -- no
    second mutation, no second audit row, no second outbox event.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        assignment = await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        session.add(
            _event(
                organization,
                PRINCIPAL_DELETED_EVENT_TYPE,
                {"principal_id": LEAVER, "organization_id": str(organization.id)},
            )
        )
        await session.commit()
        assignment_id = assignment.id

    first = await run_principal_reconciliation_pass(_settings())
    after_first = await _audit_and_outbox_counts(sessions)

    reset_cadence()  # simulate the next due cycle, not a code path change
    second = await run_principal_reconciliation_pass(_settings())
    after_second = await _audit_and_outbox_counts(sessions)

    assert first.assignments_lapsed == 1
    assert second.events_examined == 1, "the pass must genuinely re-read the event"
    assert second.assignments_lapsed == 0, "the replay must not mutate anything"
    assert after_first == after_second, f"replay wrote extra rows: {after_first} -> {after_second}"
    async with sessions() as session:
        assert (await session.get(OwnershipAssignment, assignment_id)).status == "LAPSED_LEAVER"


async def test_a_duplicate_event_row_is_also_a_no_op(sessions: Any) -> None:
    """The other duplicate shape: two outbox rows for the same logical event
    (an emitter retry). The second must be as harmless as a replay of the
    first.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        payload = {"principal_id": LEAVER, "organization_id": str(organization.id)}
        session.add(_event(organization, PRINCIPAL_DELETED_EVENT_TYPE, dict(payload)))
        session.add(_event(organization, PRINCIPAL_DELETED_EVENT_TYPE, dict(payload)))
        await session.commit()

    outcome = await run_principal_reconciliation_pass(_settings())

    assert outcome.events_examined == 2
    assert outcome.assignments_lapsed == 1
    audits, events = await _audit_and_outbox_counts(sessions)
    assert (audits, events) == (1, 1)


async def test_same_transaction_emit_then_scheduled_replay_is_a_no_op(sessions: Any) -> None:
    """The real production sequence: an identity workflow calls
    `emit_principal_deleted` (which reconciles in the same transaction AND
    writes the outbox row), and the scheduled pass later re-reads that row.
    The pass must not double-apply what the emitter already did.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        await session.commit()
        await emit_principal_deleted(
            session,
            settings=_settings(),
            context=SecurityContext(
                principal_id="admin",
                principal_type="USER",
                organization_id=organization.id,
                roles=frozenset({"PlatformAdmin"}),
            ),
            principal_id=LEAVER,
            organization_id=organization.id,
        )
        await session.commit()

    before = await _audit_and_outbox_counts(sessions)
    outcome = await run_principal_reconciliation_pass(_settings())
    after = await _audit_and_outbox_counts(sessions)

    assert outcome.events_examined == 1
    assert outcome.assignments_lapsed == 0
    assert before == after


async def test_events_outside_the_lookback_window_are_not_re_read(sessions: Any) -> None:
    """The window is an efficiency bound, not a correctness one -- but it has
    to actually bound the scan, or the pass re-reads the whole outbox forever.
    """
    async with sessions() as session:
        organization = await _organization(session, "Bank")
        await _assignment(session, organization, principal=LEAVER, subject_id="term-1")
        stale = _event(
            organization,
            PRINCIPAL_DELETED_EVENT_TYPE,
            {"principal_id": LEAVER, "organization_id": str(organization.id)},
        )
        stale.occurred_at = datetime.now(UTC) - timedelta(days=30)
        session.add(stale)
        await session.commit()

    outcome = await run_principal_reconciliation_pass(_settings())
    assert outcome.events_examined == 0


async def test_consumer_and_emitter_agree_on_the_event_type_strings() -> None:
    """A consumer that disagreed with the emitter by one character would
    silently reconcile nothing -- which is a fresh instance of the exact class
    of failure F19 found.
    """
    assert RECONCILED_EVENT_TYPES == (
        PRINCIPAL_DELETED_EVENT_TYPE,
        PRINCIPAL_MERGED_EVENT_TYPE,
    )
