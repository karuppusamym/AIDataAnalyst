"""R11-X2: `delivery_attempt` finally has a reader, and it must not leak.

The table was filed write-only. Nothing in the product read it, and the
notification runbook handed the operator a five-line `JOIN` to type into
`psql` instead. `scripts/delivery_history.py` is the reader.

The two things worth testing are not the formatting.

**It prints destinations, and a Slack or Teams webhook URL is a bearer
credential in its path.** It is safe only because the stored value is already
`delivery_intents.destination_label(...)` -- scheme, host and digest. That is
a property of the *writer*, two modules away, so the guard below asserts it
from this end: the day someone stores a raw URL, this script becomes a
credential dump and this test is what says so.

**A Teams 2xx is not a receipt.** A Power Automate Workflows webhook answers
202 from its trigger, before the post-card action runs, so a flow that then
fails records DELIVERED with a 202 and posts nothing. Reporting that as
delivered is the single most misleading thing this output could do, so
"cannot be concluded" is its own answer and is not the same as "failed".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.db import Base
from aida.delivery_intents import destination_label
from aida.models import DeliveryAttempt, DeliveryIntent, Organization
from scripts.delivery_history import _receipt_proven, collect, render

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

#: A real Slack incoming-webhook shape. The path is the credential.
RAW_WEBHOOK = "https://hooks.slack.com/services/T0123456789/B0123456789/aVerySecretToken"


@pytest_asyncio.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    # StaticPool, because `collect` opens its own session: without it each
    # new connection to ":memory:" is a *different* empty database, and the
    # pooled connection outlives the test's event loop.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # Patch the name **the script holds**, not `aida.db`'s. The script does
    # `from aida.db import session_factory` at module scope, so it keeps its
    # own reference and patching `aida.db` leaves it bound to the original.
    #
    # This is not a style note. Getting it wrong the first time did not make
    # these tests fail on the patch -- it made them silently read the
    # developer's **live Postgres database**, and they only failed because the
    # live ledger happened to have rows in it. A reader is harmless there; the
    # same mistake in a test that writes would have corrupted the dev estate.
    # `test_an_empty_ledger_names_both_flags_that_cause_it` is the canary: it
    # is the one test whose expectation is "the database is empty", so it
    # fails the moment this patch stops pointing at the fixture.
    import scripts.delivery_history as reader

    monkeypatch.setattr(reader, "session_factory", maker)
    async with maker() as active:
        yield active
    await engine.dispose()


def _attempt(**overrides: Any) -> DeliveryAttempt:
    defaults: dict[str, Any] = {
        "intent_id": uuid4(),
        "attempt_number": 1,
        "outcome": "DELIVERED",
        "transport": "webhook",
        "destination": "https://hooks.slack.com/#abc123def456",
        "status_code": 200,
        "started_at": NOW,
    }
    defaults.update(overrides)
    return DeliveryAttempt(**defaults)


async def _intent(
    session: AsyncSession,
    *,
    state: str = "DELIVERED",
    attempts: int = 1,
    transport: str = "webhook",
    status_code: int | None = 200,
    destination: str = "https://hooks.slack.com/#abc123def456",
) -> DeliveryIntent:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    intent = DeliveryIntent(
        organization_id=organization.id,
        kind="GOVERNANCE_NOTIFICATION",
        channel="SLACK",
        destination=destination,
        dedup_key=uuid4().hex,
        payload={},
        state=state,
        requested_at=NOW - timedelta(minutes=1),
        attempt_count=attempts,
    )
    session.add(intent)
    await session.flush()
    for number in range(1, attempts + 1):
        session.add(
            _attempt(
                intent_id=intent.id,
                attempt_number=number,
                transport=transport,
                status_code=status_code,
                destination=destination,
            )
        )
    await session.commit()
    return intent


# --------------------------------------------------------------------------- #
# The guard that matters
# --------------------------------------------------------------------------- #


def test_the_label_a_destination_is_stored_as_carries_no_credential() -> None:
    """Asserted from the reader's end, because the reader is what publishes it.

    `destination_label` is the writer's contract; this is the consumer stating
    what it depends on. If the two ever disagree, the failure should surface
    here, where the value is about to be printed.
    """
    label = destination_label(RAW_WEBHOOK)

    assert "aVerySecretToken" not in label
    assert "/services/" not in label
    assert label.startswith("https://hooks.slack.com")


async def test_no_raw_webhook_reaches_the_output(session: AsyncSession) -> None:
    """End to end through the real `collect` and `render`."""
    await _intent(session, destination=destination_label(RAW_WEBHOOK))

    output = render(await collect(state=None, kind=None, limit=10))

    assert "aVerySecretToken" not in output
    assert "/services/" not in output


# --------------------------------------------------------------------------- #
# Teams: 2xx is an acknowledgement of the trigger, not of the card
# --------------------------------------------------------------------------- #


def test_a_teams_2xx_does_not_claim_receipt() -> None:
    """`None`, deliberately -- not `False`. The 202 is a real answer from the
    trigger, so calling it a failure would be as wrong as calling it a
    receipt."""
    verdict = _receipt_proven(_attempt(transport="TEAMS", status_code=202))

    assert verdict is None


def test_a_non_teams_2xx_does_claim_receipt() -> None:
    """Slack answers from the posting endpoint itself, so its 2xx is the
    vendor's own acknowledgement and reporting it as unproven would train an
    operator to ignore the distinction."""
    assert _receipt_proven(_attempt(transport="webhook", status_code=200)) is True


def test_a_failure_is_a_failure_on_every_transport() -> None:
    assert _receipt_proven(_attempt(transport="TEAMS", status_code=500)) is False
    assert _receipt_proven(_attempt(transport="webhook", status_code=404)) is False
    assert _receipt_proven(_attempt(status_code=None)) is False


async def test_the_output_warns_about_every_unproven_teams_attempt(
    session: AsyncSession,
) -> None:
    await _intent(session, transport="TEAMS", status_code=202)

    output = render(await collect(state=None, kind=None, limit=10))

    assert "not a posted card" in output
    assert "Power Automate" in output


# --------------------------------------------------------------------------- #
# The two states an operator misreads
# --------------------------------------------------------------------------- #


async def test_never_tried_reads_differently_from_tried_and_refused(
    session: AsyncSession,
) -> None:
    """An intent with no attempt rows has never been tried -- usually the
    worker being switched off -- which is a different problem from having been
    tried and refused, and the fix is different too."""
    await _intent(session, state="PENDING", attempts=0)

    output = render(await collect(state=None, kind=None, limit=10))

    assert "never tried, not refused" in output


async def test_an_empty_ledger_names_both_flags_that_cause_it(
    session: AsyncSession,
) -> None:
    """"No delivery intents" on its own sends the reader to the wrong place.
    Both flags ship off, so the empty case is the *expected* case on a fresh
    install and the output should say which switch to look at."""
    output = render(await collect(state=None, kind=None, limit=10))

    assert "AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED" in output
    assert "AIDA_DELIVERY_WORKER_ENABLED" in output


async def test_filters_narrow_the_ledger(session: AsyncSession) -> None:
    await _intent(session, state="DEAD_LETTER")
    await _intent(session, state="DELIVERED")

    dead = await collect(state="DEAD_LETTER", kind=None, limit=10)

    assert [row["state"] for row in dead] == ["DEAD_LETTER"]


async def test_attempts_are_ordered_oldest_first(session: AsyncSession) -> None:
    """The history is a sequence, and reading it backwards inverts the story
    of an outage."""
    await _intent(session, state="DEAD_LETTER", attempts=3)

    rows = await collect(state=None, kind=None, limit=10)

    assert [a["number"] for a in rows[0]["attempts"]] == [1, 2, 3]
